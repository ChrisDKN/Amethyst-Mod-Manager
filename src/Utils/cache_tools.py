"""Neutral (toolkit-free) cache helpers for the download cache + orphaned temp
dirs.

The Tk Settings panel (``gui/status_bar.py``) grew these as private helpers, but
that module imports customtkinter so the Qt port can't reuse them. This module
holds the same logic with no GUI dependency, so both the Tk app and the Qt
Settings tab can call it.

The download cache stores extracted/queued mod archives under
``get_download_cache_dir()`` (honours ``[paths] download_cache_path``). Aborted
extractions can leave ``modmgr_*`` temp dirs scattered across every game's
staging path - :func:`orphaned_tmp_dirs` finds them and
:func:`clear_download_cache` removes both.
"""

from __future__ import annotations

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from Utils.config_paths import (
    get_config_dir, get_download_cache_dir, get_profiles_dir,
)

# Names at the cache root that are NOT per-game caches and must survive a
# "Clear All" (moved here from gui/cache_manager_overlay.py so both toolkits
# share one definition).
CLEAR_ALL_PRESERVE: frozenset[str] = frozenset({"md5_cache.json"})

# How far below a staging root the modmgr_* sweep descends. Aborted extractions
# dropped their temp dir at the root (or one level in, beside a mod folder), so
# a shallow sweep finds every real orphan - while an unbounded rglob walked the
# whole mod library (100k+ dirents) and made "Manage Caches" take seconds.
ORPHAN_SCAN_MAX_DEPTH = 3


def format_size(n_bytes: int) -> str:
    """Human-readable byte count ("12.3 MB"); "-" for empty/unknown."""
    if n_bytes <= 0:
        return "-"
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n_bytes >= threshold:
            return f"{n_bytes / threshold:.1f} {unit}"
    return f"{n_bytes} B"


def dir_size(path: Path) -> int:
    """Total size in bytes of every regular file under *path* (0 if missing).

    scandir-based: the dirent already carries the file/dir type, so this costs
    one stat per file instead of rglob's stat-per-is_file plus stat-per-size.
    """
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def enumerate_game_caches() -> list[Path]:
    """Per-game cache subdirs at the download-cache root, sorted
    case-insensitively, excluding CLEAR_ALL_PRESERVE names. [] if root missing.
    (Neutral port of gui/cache_manager_overlay._enumerate_game_caches.)"""
    cache_dir = get_download_cache_dir()
    if not cache_dir.is_dir():
        return []
    try:
        return sorted(
            (p for p in cache_dir.iterdir()
             if p.is_dir() and p.name not in CLEAR_ALL_PRESERVE),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return []


def game_cache_sizes(names: list[str]) -> dict[str, int]:
    """Map each per-game cache name -> total byte size (reuses dir_size).

    The per-cache walks run on a small thread pool - they're pure I/O wait, so
    overlapping them cuts the wall clock on a multi-game cache roughly by the
    worker count.
    """
    root = get_download_cache_dir()
    if not names:
        return {}
    if len(names) == 1:
        return {names[0]: dir_size(root / names[0])}
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
        sizes = pool.map(lambda n: dir_size(root / n), names)
    return dict(zip(names, sizes))


def clear_game_caches(names: list[str]) -> tuple[int, list[str]]:
    """rmtree each named per-game cache dir under the download-cache root.
    Returns (cleared_count, errors) where errors are 'name: msg' strings.
    Best-effort; never touches CLEAR_ALL_PRESERVE names."""
    root = get_download_cache_dir()
    cleared = 0
    errors: list[str] = []
    for name in names:
        if name in CLEAR_ALL_PRESERVE:
            continue
        target = root / name
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                cleared += 1
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    return cleared, errors


def orphaned_tmp_dirs() -> list[Path]:
    """Orphaned ``modmgr_*`` temp dirs across all known staging paths.

    Collects staging roots from every game's ``paths.json`` plus the env-var
    profiles dir, then returns the ``modmgr_*`` directories found under them.
    """
    found: list[Path] = []
    search_roots: list[Path] = []

    try:
        games_dir = get_config_dir() / "games"
        for paths_json in games_dir.rglob("paths.json"):
            try:
                data = json.loads(paths_json.read_text(encoding="utf-8"))
                sp = data.get("staging_path", "")
                if sp:
                    search_roots.append(Path(sp))
            except Exception:
                pass
    except Exception:
        pass

    try:
        search_roots.append(get_profiles_dir())
    except Exception:
        pass

    seen: set[Path] = set()
    for root in search_roots:
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        found.extend(_scan_for_orphans(root))
    return found


def _scan_for_orphans(root: Path) -> list[Path]:
    """``modmgr_*`` dirs at most ORPHAN_SCAN_MAX_DEPTH levels below *root*.

    Never descends through a symlink (staging trees are full of deploy symlinks
    pointing back into the game dir) nor into a matched dir - its children are
    already covered by the rmtree that removes it.
    """
    found: list[Path] = []
    level = [str(root)]
    for _ in range(ORPHAN_SCAN_MAX_DEPTH):
        nxt: list[str] = []
        for d in level:
            try:
                with os.scandir(d) as it:
                    for entry in it:
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if entry.name.startswith("modmgr_"):
                            found.append(Path(entry.path))
                        else:
                            nxt.append(entry.path)
            except OSError:
                pass
        if not nxt:
            break
        level = nxt
    return found


def orphaned_tmp_scan() -> tuple[list[Path], int]:
    """(orphaned ``modmgr_*`` dirs, their total bytes) in one sweep.

    Callers that need both must use this - ``orphaned_tmp_dirs`` +
    ``orphaned_tmp_size`` walks the staging roots twice.
    """
    dirs = orphaned_tmp_dirs()
    return dirs, sum(dir_size(d) for d in dirs)


def orphaned_tmp_size() -> int:
    """Total bytes across every orphaned ``modmgr_*`` temp dir."""
    return sum(dir_size(d) for d in orphaned_tmp_dirs())


def clear_orphaned_tmp_dirs() -> tuple[int, list[str]]:
    """Delete every orphaned ``modmgr_*`` temp dir. Returns (cleared, errors)
    ('path: msg' strings). Best-effort - individual failures are recorded."""
    cleared = 0
    errors: list[str] = []
    for orphan in orphaned_tmp_dirs():
        try:
            shutil.rmtree(orphan, ignore_errors=True)
            cleared += 1
        except OSError as exc:
            errors.append(f"{orphan}: {exc}")
    return cleared, errors


def clear_download_cache() -> int:
    """Delete the download cache contents + orphaned temp dirs.

    Removes the *contents* of the cache root (keeping the root itself so the
    path stays valid) and every ``modmgr_*`` orphan dir. Returns the number of
    top-level entries removed. Best-effort - individual failures are skipped.
    """
    removed = 0
    cache_root = get_download_cache_dir()
    try:
        for entry in cache_root.iterdir():
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    cleared_orphans, _ = clear_orphaned_tmp_dirs()
    return removed + cleared_orphans
