"""
bsa_filemap.py
BSA archive conflict detection — index cache and conflict engine.

Scans BSA files across enabled mods, caches the file lists in
bsa_index.bin (msgpack), and computes BSA-vs-BSA conflicts using the
same priority-merge algorithm as the loose-file filemap builder.

Cache format — msgpack binary, v1:
    {
        "v": 1,
        "mods": [
            [mod_name, [
                [bsa_filename, mtime_float, [file_path, ...]],
                ...
            ]],
            ...
        ]
    }

File paths stored in the cache are lowercase, forward-slash separated.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import msgpack

from Utils.bsa_reader import read_bsa_file_list
from Utils.filemap import (
    CONFLICT_NONE,
    _compute_conflict_status,
)
from Utils.modlist import read_modlist

_BSA_INDEX_VERSION = 1

# Thread pool for parallel BSA scanning
_POOL = ThreadPoolExecutor(max_workers=12)

# In-memory cache: (path_str, mtime) → parsed index
_BsaIndex = dict[str, list[tuple[str, float, list[str]]]]
_bsa_cache: tuple[str, float, _BsaIndex] | None = None
_bsa_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Index read / write
# ---------------------------------------------------------------------------

def read_bsa_index(
    index_path: Path,
) -> _BsaIndex | None:
    """Read bsa_index.bin and return {mod_name: [(bsa_filename, mtime, [paths])]}.

    Returns None if the index does not exist or has an unrecognised version.
    Results are cached in memory by (path, mtime).
    """
    global _bsa_cache
    path_str = str(index_path)
    with _bsa_cache_lock:
        try:
            mtime = index_path.stat().st_mtime
        except OSError:
            return None
        if _bsa_cache is not None and _bsa_cache[0] == path_str and _bsa_cache[1] == mtime:
            return _bsa_cache[2]
    try:
        with index_path.open("rb") as f:
            data = msgpack.unpack(f, raw=False)
        if not isinstance(data, dict) or data.get("v") != _BSA_INDEX_VERSION:
            return None
        index: _BsaIndex = {}
        for mod_name, archives in data["mods"]:
            entries: list[tuple[str, float, list[str]]] = []
            for bsa_name, mt, paths in archives:
                entries.append((bsa_name, float(mt), paths))
            index[mod_name] = entries
    except Exception:
        return None
    with _bsa_cache_lock:
        _bsa_cache = (path_str, mtime, index)
    return index


def _write_bsa_index(index_path: Path, index: _BsaIndex) -> None:
    """Write bsa_index.bin atomically and update the in-memory cache."""
    global _bsa_cache
    index_path.parent.mkdir(parents=True, exist_ok=True)
    mods = []
    for mod_name, archives in index.items():
        entries = [[bsa_name, mt, paths] for bsa_name, mt, paths in archives]
        mods.append([mod_name, entries])
    payload = {"v": _BSA_INDEX_VERSION, "mods": mods}
    tmp = index_path.with_suffix(".tmp")
    try:
        with tmp.open("wb") as f:
            msgpack.pack(payload, f, use_bin_type=True)
        tmp.replace(index_path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    with _bsa_cache_lock:
        try:
            mtime = index_path.stat().st_mtime
            _bsa_cache = (str(index_path), mtime, index)
        except OSError:
            _bsa_cache = None


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _scan_mod_bsas(
    mod_name: str,
    mod_dir: str,
    archive_extensions: frozenset[str],
) -> tuple[str, list[tuple[str, float, list[str]]]]:
    """Scan a single mod directory for BSA files and parse their TOCs.

    Returns (mod_name, [(bsa_filename, mtime, [file_paths])]).
    Thread-safe — no shared mutable state.
    """
    results: list[tuple[str, float, list[str]]] = []
    try:
        with os.scandir(mod_dir) as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext not in archive_extensions:
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                paths = read_bsa_file_list(entry.path)
                if paths:
                    results.append((entry.name, mtime, paths))
    except OSError:
        pass
    return (mod_name, results)


def rebuild_bsa_index(
    index_path: Path,
    staging_root: Path,
    archive_extensions: frozenset[str],
    log_fn: "Callable[[str], None] | None" = None,
) -> None:
    """Scan all mod folders for BSA files and write bsa_index.bin.

    Uses the existing BSA index for incremental updates: only re-parses
    BSAs whose mtime has changed since the last scan.
    """
    if not staging_root.is_dir():
        return

    # Read existing index for mtime comparison
    old_index = read_bsa_index(index_path) or {}
    old_mtimes: dict[str, dict[str, float]] = {}
    for mod_name, archives in old_index.items():
        old_mtimes[mod_name] = {bsa_name: mt for bsa_name, mt, _ in archives}

    # Collect mod directories to scan
    mod_dirs: list[tuple[str, str]] = []
    try:
        with os.scandir(str(staging_root)) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    mod_dirs.append((entry.name, entry.path))
    except OSError:
        return

    # Submit parallel scans
    futures = []
    for mod_name, mod_path in mod_dirs:
        futures.append(_POOL.submit(_scan_mod_bsas, mod_name, mod_path, archive_extensions))

    index: _BsaIndex = {}
    total_bsa = 0
    total_files = 0
    for future in futures:
        mod_name, archives = future.result()
        if archives:
            # Use cached file lists for BSAs whose mtime hasn't changed
            old_mod_mtimes = old_mtimes.get(mod_name, {})
            old_mod_archives = {a[0]: a for a in old_index.get(mod_name, [])}
            final_archives: list[tuple[str, float, list[str]]] = []
            for bsa_name, mtime, paths in archives:
                old_mt = old_mod_mtimes.get(bsa_name)
                if old_mt is not None and old_mt == mtime and bsa_name in old_mod_archives:
                    # Reuse cached entry
                    final_archives.append(old_mod_archives[bsa_name])
                else:
                    final_archives.append((bsa_name, mtime, paths))
                total_bsa += 1
                total_files += len(final_archives[-1][2])
            index[mod_name] = final_archives

    _write_bsa_index(index_path, index)
    if log_fn:
        log_fn(f"BSA index: {total_bsa} archive(s), {total_files} file(s) across {len(index)} mod(s).")


def update_bsa_index(
    index_path: Path,
    mod_name: str,
    mod_dir: Path | str,
    archive_extensions: frozenset[str],
) -> None:
    """Add or replace a single mod's BSA entries in the index.

    Call this after installing a mod.
    """
    _, archives = _scan_mod_bsas(mod_name, str(mod_dir), archive_extensions)
    index = read_bsa_index(index_path) or {}
    if archives:
        index[mod_name] = archives
    else:
        index.pop(mod_name, None)
    _write_bsa_index(index_path, index)


def remove_from_bsa_index(
    index_path: Path,
    mod_names: list[str] | str,
) -> None:
    """Remove one or more mods' BSA entries from the index.

    Call this after removing mod folders from staging.
    No-op if the index does not exist or the mod is not in it.
    """
    if isinstance(mod_names, str):
        mod_names = [mod_names]
    if not index_path.is_file():
        return
    index = read_bsa_index(index_path)
    if not index:
        return
    changed = False
    for name in mod_names:
        if name in index:
            del index[name]
            changed = True
    if changed:
        _write_bsa_index(index_path, index)


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

def build_bsa_conflicts(
    modlist_path: Path,
    index_path: Path,
    archive_extensions: frozenset[str],
    log_fn: "Callable[[str], None] | None" = None,
) -> tuple[dict[str, int], dict[str, set[str]], dict[str, set[str]]]:
    """Compute BSA-vs-BSA conflicts.

    Walks enabled mods from lowest priority to highest. For each file path
    inside each mod's BSA archives, the higher-priority mod wins.

    Returns (bsa_conflict_map, bsa_overrides, bsa_overridden_by) using the
    same CONFLICT_* constants from filemap.py.
    """
    entries = read_modlist(modlist_path)
    enabled = [e for e in entries if not e.is_separator and e.enabled]
    enabled_low_to_high = list(reversed(enabled))

    priority_order = [e.name for e in enabled_low_to_high]

    index = read_bsa_index(index_path)
    if index is None:
        # No index — return empty results
        empty_map = {name: CONFLICT_NONE for name in priority_order}
        empty_set: dict[str, set[str]] = {name: set() for name in priority_order}
        return empty_map, empty_set, dict(empty_set)

    # Single-pass merge: low priority → high priority
    bsa_winner: dict[str, str] = {}  # file_path → mod_name
    overrides:     dict[str, set[str]] = {name: set() for name in priority_order}
    overridden_by: dict[str, set[str]] = {name: set() for name in priority_order}
    win_count: dict[str, int] = {}
    mods_with_files: set[str] = set()

    for name in priority_order:
        mod_archives = index.get(name)
        if not mod_archives:
            continue
        had_file = False
        for _bsa_name, _mtime, paths in mod_archives:
            for file_path in paths:
                had_file = True
                prev = bsa_winner.get(file_path)
                if prev is not None:
                    win_count[prev] = win_count.get(prev, 0) - 1
                    overrides[name].add(prev)
                    overridden_by[prev].add(name)
                bsa_winner[file_path] = name
                win_count[name] = win_count.get(name, 0) + 1
        if had_file:
            mods_with_files.add(name)

    conflict_map = _compute_conflict_status(
        priority_order, overrides, overridden_by, win_count, mods_with_files,
    )

    return conflict_map, overrides, overridden_by
