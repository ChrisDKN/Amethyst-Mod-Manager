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
from dataclasses import dataclass
from functools import lru_cache

# Below this compressed size the `7z l -slt` metadata probe is skipped and the
# 15× fallback used instead. The estimate only gates extraction memory, and the
# worst-case fallback for a small archive is a trivially small reservation -
# while collections install thousands of tiny archives, so one process spawn
# per mod adds real wall time to the (already bottlenecked) install consumers.
_PROBE_MIN_COMPRESSED_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ArchiveProbe:
    """Reusable archive metadata gathered before an extraction starts.

    ``members_inspected`` distinguishes a real negative FOMOD result from the
    small-non-ZIP fast path, where starting ``7z l`` solely to estimate memory
    would cost more than the conservative size fallback.
    """

    compressed_size: int
    uncompressed_size: int
    has_fomod_config: bool = False
    members_inspected: bool = False


def _is_fomod_member(name: str) -> bool:
    member = name.replace("\\", "/").lstrip("/").lower()
    target = "fomod/moduleconfig.xml"
    return member == target or member.endswith("/" + target)


@lru_cache(maxsize=512)
def _probe_archive_cached(path: str, compressed_size: int, file_size: int,
                          mtime_ns: int, inspect_members: bool) -> ArchiveProbe:
    # file_size + mtime_ns deliberately participate in the cache key: a
    # re-downloaded/replaced archive at the same path must be probed again.
    del file_size, mtime_ns
    total = 0
    has_fomod = False
    members_inspected = False
    lower = path.lower()

    # ZIP's central directory gives both answers in one cheap read, even for a
    # small archive, so never run a second member scan for ZIP files.
    if lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(path, "r") as archive:
                infos = archive.infolist()
            total = sum(member.file_size for member in infos)
            has_fomod = any(_is_fomod_member(member.filename) for member in infos)
            members_inspected = True
        except Exception:
            pass

    # For non-ZIP archives a single 7z listing supplies both expanded sizes and
    # member paths. Preserve the old small-archive optimisation unless a caller
    # specifically needs member detection (the batch/collection FOMOD preflight).
    should_list = not members_inspected and (
        inspect_members
        or compressed_size <= 0
        or compressed_size >= _PROBE_MIN_COMPRESSED_BYTES
    )
    if should_list:
        binary = (shutil.which("7zzs") or shutil.which("7zz")
                  or shutil.which("7z") or shutil.which("7za"))
        if binary:
            try:
                import subprocess
                result = subprocess.run(
                    [binary, "l", "-slt", path],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, timeout=30,
                )
                if result.returncode == 0:
                    members_inspected = True
                    for line in result.stdout.splitlines():
                        if line.startswith("Size = "):
                            try:
                                total += int(line.split("=", 1)[1].strip())
                            except ValueError:
                                pass
                        elif (not has_fomod
                              and line.startswith("Path = ")
                              and _is_fomod_member(line[7:].strip())):
                            has_fomod = True
            except Exception:
                pass
        elif inspect_members and lower.endswith(".7z"):
            # Match the old FOMOD preflight fallback on systems without a 7z
            # executable. Size estimation remains the conservative 15× path.
            try:
                import py7zr
                with py7zr.SevenZipFile(path, "r") as archive:
                    names = archive.getnames()
                has_fomod = any(_is_fomod_member(name) for name in names)
                members_inspected = True
            except Exception:
                pass

    if total <= 0:
        # Generous fallback retained verbatim for extreme texture packs.
        total = compressed_size * 15
    return ArchiveProbe(
        compressed_size=compressed_size,
        uncompressed_size=total,
        has_fomod_config=has_fomod,
        members_inspected=members_inspected,
    )


def probe_archive(path: str, compressed_size: int = 0, *,
                  inspect_members: bool = False) -> ArchiveProbe:
    """Return cached size/member metadata for *path*.

    The cache identity includes size and nanosecond mtime, so keeping a result
    across the memory gate and extraction-location decision cannot make a newly
    replaced archive inherit stale metadata.
    """
    try:
        stat = os.stat(path)
        file_size = int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)
    except OSError:
        file_size = 0
        mtime_ns = 0
    if compressed_size <= 0:
        compressed_size = file_size
    return _probe_archive_cached(
        os.path.abspath(path), int(compressed_size), file_size, mtime_ns,
        bool(inspect_members))


def get_uncompressed_size(path: str, compressed_size: int = 0) -> int:
    """Return best-effort total uncompressed size of the archive in bytes.

    Tries archive metadata first (zipfile headers, ``7z l -slt``), then falls
    back to a 15× multiplier of *compressed_size* (handles extreme texture
    packs).  If *compressed_size* is 0, the on-disk file size is used instead.
    Archives smaller than ``_PROBE_MIN_COMPRESSED_BYTES`` skip the ``7z``
    process spawn and go straight to the fallback (zip headers are still read -
    they're free).
    """
    return probe_archive(path, compressed_size).uncompressed_size


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
