"""Toolkit-neutral auto-acquire for the MPI-package wizards (ESM Fixes /
BSA Decompressor): when the source page can't find the Nexus archive in the
download folders, this fetches it hands-free instead of making the user press
"Detect again".

Premium Nexus accounts download the pinned (mod_id, file_id) directly through
the API (NexusDownloader - cache-aware, writes the .fileid sidecar). Everyone
else falls back to watching the download folders: the wizard opens the Nexus
page as before, and the archive is picked up automatically the moment the
browser download completes (partial/in-flight files are never accepted).

All callbacks fire on the WORKER thread - Qt callers must marshal (safe_emit).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

_POLL_S = 2.0

# In-flight browser download temp names (Firefox / Chromium / Safari). A
# final-named file with one of these siblings is still being written.
_PARTIAL_SUFFIXES = (".part", ".crdownload", ".download")


def _noop(*_a) -> None:
    pass


def _has_partial_sibling(path: Path) -> bool:
    for suffix in _PARTIAL_SUFFIXES:
        try:
            if path.with_name(path.name + suffix).exists():
                return True
        except OSError:
            pass
    return False


def start_auto_fetch(
    *,
    api,
    game_domain: str,
    mod_id: int,
    file_id: int,
    find_archive_fn: Callable[[], "Path | None"],
    on_archive: Callable[[Path], None],
    cancel: threading.Event,
    label: str = "",
    on_download_started: Callable[[], None] = _noop,
    on_progress: Callable[[int, int], None] = _noop,
    on_waiting: Callable[[], None] = _noop,
    log_fn: Callable[[str], None] = _noop,
) -> threading.Thread:
    """Start the acquire worker; returns the (daemon) thread.

    *api* is a NexusAPI or None (not logged in). Premium accounts download
    (mod_id, file_id) directly - ``on_download_started`` fires first, then
    ``on_progress(done, total)`` while streaming; otherwise ``on_waiting``
    fires once and the download folders are polled until a COMPLETE matching
    archive appears. Either way ``on_archive(path)`` fires exactly once with
    the finished archive. Set *cancel* to stop (aborts an in-flight premium
    download too).

    ``find_archive_fn`` is the wizard's existing keyword scan - the watcher's
    fallback when there is no API to supply the exact file name/size.
    """
    name = label or f"{game_domain}/{mod_id}/{file_id}"

    def worker():
        premium = False
        if api is not None:
            try:
                premium = bool(api.validate().is_premium)
            except Exception as exc:
                log_fn(f"could not check Nexus membership: {exc}")

        if premium and not cancel.is_set():
            on_download_started()
            log_fn(f"premium account - downloading '{name}' from Nexus…")
            result = None
            try:
                from Nexus.nexus_download import NexusDownloader
                result = NexusDownloader(api).download_file(
                    game_domain, mod_id, file_id,
                    progress_cb=on_progress, cancel=cancel)
            except Exception as exc:
                log_fn(f"direct download failed: {exc}")
            if cancel.is_set():
                return
            if result is not None and result.success and result.file_path:
                log_fn(f"downloaded {Path(result.file_path).name}")
                on_archive(Path(result.file_path))
                return
            if result is not None and result.error:
                log_fn(f"direct download failed: {result.error}")
            log_fn("falling back to watching the download folders.")

        # ---- watch mode: wait for a browser download to complete ----------
        expected_name, expected_size = "", 0
        if api is not None:
            try:
                info = api.get_file_info(game_domain, mod_id, file_id)
                fn = (getattr(info, "file_name", "") or "").strip()
                if fn and "/" not in fn:
                    expected_name = fn
                expected_size = int(
                    (getattr(info, "size_in_bytes", 0) or 0)
                    or (getattr(info, "size_kb", 0) or 0) * 1024)
            except Exception as exc:
                log_fn(f"could not fetch file info: {exc}")

        on_waiting()
        last_sizes: dict = {}
        while not cancel.wait(_POLL_S):
            found = _scan_once(expected_name, expected_size, mod_id, file_id,
                               find_archive_fn, last_sizes)
            if found is not None:
                log_fn(f"found downloaded archive → {found}")
                on_archive(found)
                return

    def _scan_once(expected_name, expected_size, mid, fid,
                   find_fn, last_sizes) -> "Path | None":
        # With API file info: exact match + completeness via the download
        # cache scanner (sidecar / size-vs-expected / zip integrity).
        if expected_name or expected_size:
            try:
                from Nexus.manual_download_watch import scan_download_dirs
                from Nexus.nexus_download import _find_cached_archive
                for folder in scan_download_dirs():
                    found, complete = _find_cached_archive(
                        folder, expected_name, expected_size, mid, fid)
                    if found is not None and complete:
                        return found
            except Exception:
                pass
            return None
        # No API: keyword scan + "looks finished" guards - non-empty, no
        # browser temp sibling, size stable across two consecutive polls
        # (browsers pre-allocate/grow the final-named file while writing).
        try:
            p = find_fn()
        except Exception:
            return None
        if p is None:
            return None
        if _has_partial_sibling(p):
            return None
        try:
            size = p.stat().st_size
        except OSError:
            return None
        if size <= 0:
            return None
        key = str(p)
        if last_sizes.get(key) == size:
            return p
        last_sizes[key] = size
        return None

    thread = threading.Thread(target=worker, daemon=True,
                              name=f"mpi-auto-fetch-{mod_id}")
    thread.start()
    return thread
