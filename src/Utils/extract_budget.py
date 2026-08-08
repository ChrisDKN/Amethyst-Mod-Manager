"""Toolkit-neutral extraction memory budgeting (no Tk / no Qt).

Moved verbatim out of ``gui/install_mod.py`` so both the Tk installer and the
Qt collection-install orchestrator can share ONE implementation. Pure stdlib
(``os``/``shutil``/``subprocess``/``zipfile``/``threading``) - no UI, no
project imports.

``gui/install_mod.py`` re-imports ``ExtractionMemoryBudget`` +
``get_uncompressed_size`` from here.
"""

from __future__ import annotations

import os
import shutil
import threading
import zipfile

# Below this compressed size the `7z l -slt` metadata probe is skipped and the
# 15× fallback used instead. The estimate only gates extraction memory, and the
# worst-case fallback for a small archive is a trivially small reservation -
# while collections install thousands of tiny archives, so one process spawn
# per mod adds real wall time to the (already bottlenecked) install consumers.
_PROBE_MIN_COMPRESSED_BYTES = 64 * 1024 * 1024


def get_uncompressed_size(path: str, compressed_size: int = 0) -> int:
    """Return best-effort total uncompressed size of the archive in bytes.

    Tries archive metadata first (zipfile headers, ``7z l -slt``), then falls
    back to a 15× multiplier of *compressed_size* (handles extreme texture
    packs).  If *compressed_size* is 0, the on-disk file size is used instead.
    Archives smaller than ``_PROBE_MIN_COMPRESSED_BYTES`` skip the ``7z``
    process spawn and go straight to the fallback (zip headers are still read -
    they're free).
    """
    if compressed_size <= 0:
        try:
            compressed_size = os.path.getsize(path)
        except OSError:
            compressed_size = 0
    _ext = path.lower()
    # ZIP: fast metadata read via zipfile
    if _ext.endswith(".zip"):
        try:
            with zipfile.ZipFile(path, "r") as _zf:
                _total = sum(m.file_size for m in _zf.infolist())
            if _total > 0:
                return _total
        except Exception:
            pass
    # Small archive: the spawn costs more than the accuracy is worth.
    if 0 < compressed_size < _PROBE_MIN_COMPRESSED_BYTES:
        return compressed_size * 15
    # 7z/rar/zip fallback: use `7z l -slt` which prints Size: per entry
    _7z_bin = shutil.which("7zzs") or shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
    if _7z_bin:
        try:
            import subprocess
            _res = subprocess.run(
                [_7z_bin, "l", "-slt", path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=30,
            )
            _total = 0
            for _line in _res.stdout.splitlines():
                if _line.startswith("Size = "):
                    try:
                        _total += int(_line.split("=", 1)[1].strip())
                    except ValueError:
                        pass
            if _total > 0:
                return _total
        except Exception:
            pass
    # Fallback: assume a generous 15× expansion (handles extreme texture packs)
    return compressed_size * 15


def _get_available_memory_bytes() -> int:
    """Return available system memory in bytes via /proc/meminfo."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024  # kB → bytes
    except (OSError, ValueError):
        pass
    return 4 * 1024 ** 3  # conservative 4 GB fallback


class ExtractionMemoryBudget:
    """Gate concurrent extractions by estimated memory usage.

    Each extraction must ``acquire(size)`` before starting and ``release(size)``
    when finished.  *acquire* blocks until enough budget is available **and**
    live system memory confirms headroom.

    The budget is the lesser of *max_budget_bytes* and (available RAM at init
    minus *safety_margin_bytes*).  A 1.5× spike factor is applied to each
    request to account for transient memory spikes during decompression.

    *max_workers* caps the number of concurrent extractions regardless of
    memory (the caller still needs a thread-pool of this size).

    A floor of 1 ensures at least one extraction can always proceed, even if
    the estimated size exceeds the budget (otherwise a single large archive
    would deadlock the pipeline).
    """

    SPIKE_FACTOR = 1.5  # headroom multiplier for decompression spikes

    def __init__(self, max_workers: int = 4,
                 safety_margin_bytes: int = 1024 * 1024 * 1024,
                 max_budget_bytes: int | None = None):
        avail = _get_available_memory_bytes()
        auto_budget = max(0, avail - safety_margin_bytes)
        self._budget = min(auto_budget, max_budget_bytes) if max_budget_bytes else auto_budget
        self._reserved: int = 0
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._semaphore = threading.Semaphore(max(1, max_workers))

    @property
    def budget(self) -> int:
        return self._budget

    def acquire(self, estimated_bytes: int) -> None:
        """Reserve *estimated_bytes* (with spike factor) of extraction budget.

        Blocks until budget and a worker slot are available.  If the request
        is larger than the total budget, it is allowed through once all other
        reservations have drained (prevents deadlock on single huge archives).
        A live memory check adds a second safety net: even if the bookkeeping
        says there is room, we wait if the OS reports less than 1 GB free.
        """
        cost = int(estimated_bytes * self.SPIKE_FACTOR)
        self._semaphore.acquire()
        with self._cv:
            while True:
                fits_budget = (
                    self._reserved + cost <= self._budget
                    or self._reserved == 0  # allow oversized archive when alone
                )
                # Live memory check - even if budget bookkeeping says OK, wait
                # if the system is actually low on RAM (< 1 GB free).
                live_ok = _get_available_memory_bytes() >= 1024 * 1024 * 1024
                if fits_budget and live_ok:
                    break
                self._cv.wait(timeout=2.0)  # re-check periodically
            self._reserved += cost

    def release(self, estimated_bytes: int) -> None:
        """Return *estimated_bytes* (with spike factor) to the budget pool."""
        cost = int(estimated_bytes * self.SPIKE_FACTOR)
        with self._cv:
            self._reserved = max(0, self._reserved - cost)
            self._cv.notify_all()
        self._semaphore.release()
