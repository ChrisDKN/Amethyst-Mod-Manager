"""
deploy_root.py
Root-folder deployment (BepInEx, UE5, Mewgenics, Bannerlord, KCD2, BG3).

Extracted from deploy.py during the 2026-04 refactor. No behaviour changes.
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import stat as _stat
import time as _time
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.deploy_shared import (
    LinkMode,
    _deploy_workers,
    _do_link_ex,
    _mkdir_leaves,
    _move_crash_safe,
    _path_under_root,
    _prune_empty_dirs,
    _resolve_nocase,
    _resolve_root_path,
    _restore_backup_dir,
)


# Name of the sibling directory used to back up pre-existing root files.
_ROOT_BACKUP_NAME = "Root_Backup"
# Name of the log file written next to Root_Folder/ recording what was placed.
_ROOT_LOG_NAME    = "root_folder_deployed.txt"


def deploy_root_folder(
    root_folder_dir: Path,
    game_root: Path,
    mode: LinkMode = LinkMode.HARDLINK,
    log_fn=None,
) -> int:
    """Transfer files from root_folder_dir into game_root.

    root_folder_dir — Profiles/<game>/Root_Folder/
    game_root       — the game's install directory (the root, not Data/)
    mode            — transfer method (HARDLINK / SYMLINK / COPY)

    Behaviour:
      - If root_folder_dir is empty or missing, does nothing and returns 0.
      - For each file that already exists in game_root, the existing file is
        moved to a sibling Root_Backup/ directory (preserving relative paths)
        before the mod file is transferred in.
      - A log file (root_folder_deployed.txt) is written next to Root_Folder/
        listing every relative path that was successfully placed.  This log is
        consumed by restore_root_folder() to undo the operation.

    Returns the number of files transferred.
    """
    _log = _safe_log(log_fn)

    if not root_folder_dir.is_dir():
        return 0

    # Collect all source files first; bail early if none.  os.walk gets the
    # file/dir split from readdir d_type — no stat per entry like rglob+is_file.
    sources: list[tuple[Path, Path]] = []   # (src, rel)
    _root_str = str(root_folder_dir)
    _root_plen = len(_root_str) + 1
    for dirpath, _dirnames, filenames in os.walk(_root_str):
        for fname in filenames:
            full = dirpath + "/" + fname
            sources.append((Path(full), Path(full[_root_plen:])))

    if not sources:
        return 0

    backup_dir = root_folder_dir.parent / _ROOT_BACKUP_NAME
    log_path   = root_folder_dir.parent / _ROOT_LOG_NAME

    # Resolve destinations case-insensitively against the game tree (shared
    # dir cache — one iterdir per directory instead of one per file) and
    # track which top-level directories we are creating so restore can wipe
    # them entirely — including any game-generated files written into them
    # after deploy (e.g. BepInEx cache/config/log files).
    _dir_cache: dict = {}
    _top_preexisted: dict[str, bool] = {}
    created_dirs: set[str] = set()
    tasks: list[tuple[Path, Path, Path, str]] = []  # (src, dst, rel, rel_posix)
    for src, rel in sources:
        dst = _resolve_root_path(game_root, rel, _dir_cache)
        resolved_rel = dst.relative_to(game_root)
        if len(rel.parts) > 1:
            # Use the resolved (possibly case-corrected) top-level name.
            top = resolved_rel.parts[0]
            pre = _top_preexisted.get(top)
            if pre is None:
                pre = (game_root / top).exists()
                _top_preexisted[top] = pre
            if not pre:
                created_dirs.add(top)
        tasks.append((src, dst, resolved_rel, resolved_rel.as_posix()))

    def _write_log(rels: "list[str]") -> None:
        # Files on the first line block, then a separator, then directories
        # we created that should be fully removed on restore.
        # surrogateescape: filenames with non-UTF-8 bytes surface as surrogate
        # code points (via the filesystem's surrogateescape decode) and would
        # otherwise raise UnicodeEncodeError here.  Round-trips the bytes out.
        with log_path.open("w", encoding="utf-8", errors="surrogateescape") as f:
            f.write("\n".join(rels))
            if created_dirs:
                f.write("\n---dirs---\n")
                f.write("\n".join(sorted(created_dirs)))

    # Write the log BEFORE touching the game dir: if the deploy is interrupted
    # mid-transfer, restore still knows everything we may have placed (a
    # listed file that never landed is a harmless no-op on restore).
    _write_log([rel_posix for _s, _d, _r, rel_posix in tasks])

    # Back up any pre-existing files so restore can put them back; drop stale
    # symlinks from a previous deploy.  One lstat per destination.
    for _src, dst, rel, _rel_posix in tasks:
        try:
            st = os.lstat(dst)
        except OSError:
            continue
        if _stat.S_ISLNK(st.st_mode):
            dst.unlink()
        elif _stat.S_ISREG(st.st_mode):
            bak = backup_dir / rel
            _move_crash_safe(dst, bak)
            _log(f"  Backed up existing {rel} → Root_Backup/")

    # Pre-create destination directories, then transfer in parallel.
    _mkdir_leaves({os.path.dirname(str(dst)) for _s, dst, _r, _p in tasks})
    placed: list[str] = []

    def _do_root(item: "tuple[Path, Path, Path, str]"):
        src, dst, _rel, rel_posix = item
        _actual, err = _do_link_ex(str(src), str(dst), mode)
        return rel_posix, err

    with concurrent.futures.ThreadPoolExecutor(max_workers=_deploy_workers()) as pool:
        for rel_posix, err in pool.map(_do_root, tasks):
            if err is None:
                placed.append(rel_posix)
            else:
                _log(f"  WARN: could not transfer root file {rel_posix}: {err}")

    # Re-write the log with what actually landed.
    _write_log(placed)

    print(f"  [TIMER] deploy_root_folder: transferred {len(placed)} files")
    _log(f"  Root Folder: {len(placed)} file(s) transferred to game root.")
    return len(placed)


def deploy_root_flagged_mods(
    filemap_root_path: Path,
    game_root: Path,
    staging_root: Path,
    mode: LinkMode = LinkMode.HARDLINK,
    strip_prefixes: "set[str] | None" = None,
    per_mod_strip_prefixes: "dict[str, list[str]] | None" = None,
    excluded_raw: "dict[str, set[str]] | None" = None,
    log_fn=None,
) -> int:
    """Deploy files from root-flagged mods (filemap_root.txt) directly into game_root.

    filemap_root_path      — Profiles/<game>/filemap_root.txt  (written by build_filemap)
    game_root              — the game's install directory (not Data/)
    staging_root           — the mod staging root (same as used by deploy_filemap)
    mode                   — HARDLINK / SYMLINK / COPY
    strip_prefixes         — shared top-level folder names stripped during staging
    per_mod_strip_prefixes — per-mod overrides for strip_prefixes (same as deploy_filemap)
    excluded_raw           — per-mod RAW excluded keys; skipped as sources, so a
                             collision deploys the variant the user kept

    Files are appended to the same root_folder_deployed.txt log and Root_Backup/ directory
    used by deploy_root_folder(), so restore_root_folder() undoes everything in one pass.

    Returns the number of files transferred.
    """
    _log = _safe_log(log_fn)

    if not filemap_root_path.is_file():
        return 0

    # Read filemap_root.txt — each line is "rel_str\tmod_name"
    entries: list[tuple[str, str]] = []
    with filemap_root_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if "\t" not in line:
                continue
            rel_str, mod_name = line.split("\t", 1)
            entries.append((rel_str, mod_name))

    if not entries:
        return 0

    backup_dir = filemap_root_path.parent / _ROOT_BACKUP_NAME
    log_path   = filemap_root_path.parent / _ROOT_LOG_NAME

    # Read existing log so we can append (deploy_root_folder may run after us)
    existing_placed: list[str] = []
    existing_dirs: list[str] = []
    if log_path.is_file():
        content = log_path.read_text(encoding="utf-8", errors="surrogateescape")
        if "---dirs---" in content:
            _files_sec, _dirs_sec = content.split("---dirs---", 1)
            existing_placed = [p for p in _files_sec.splitlines() if p]
            existing_dirs   = [d for d in _dirs_sec.splitlines()  if d]
        else:
            existing_placed = [p for p in content.splitlines() if p]

    existing_placed_set = set(existing_placed)
    created_dirs: set[str] = set(existing_dirs)

    # Build a quick dir-resolution cache for game_root lookups
    _dir_cache: dict = {}
    _top_seen: dict[str, bool] = {}
    tasks: list[tuple[Path, Path, str]] = []  # (src, dst, rel_posix)

    _excluded_raw = excluded_raw or {}
    _nocase_cache: dict = {}

    def _usable(candidate: Path, rel_raw: str, exc) -> bool:
        """A real file the user has not disabled."""
        if exc and rel_raw.lower().replace("\\", "/") in exc:
            return False
        return candidate.is_file()

    for rel_str, mod_name in entries:
        _exc = _excluded_raw.get(mod_name)
        mod_root = staging_root / mod_name
        # Candidate mod-relative paths: the bare path, then each strip prefix
        # the scan peeled off (per-mod, shared, and the stacked combination —
        # e.g. a "MyPreset" Top Level strip followed by "Data").
        _mod_prefixes = (per_mod_strip_prefixes or {}).get(mod_name)
        _prefixes = list(_mod_prefixes) if _mod_prefixes else []
        if strip_prefixes:
            _prefixes.extend(strip_prefixes)
            if _mod_prefixes:
                _prefixes.extend(f"{mp}/{sp}"
                                 for mp in _mod_prefixes for sp in strip_prefixes)
        _rels = [rel_str] + [f"{p}/{rel_str}" for p in _prefixes]

        src = None
        for cand_rel in _rels:
            candidate = mod_root / cand_rel
            if _usable(candidate, cand_rel, _exc):
                src = candidate
                break
        if src is None:
            # filemap casing is canonicalised across ALL mods, so a mod whose
            # folder is `sound` gets listed as `Sound` when another mod spells
            # it that way — on a case-sensitive FS the literal path above then
            # misses and the file silently never deploys. Resolve the real
            # on-disk casing instead.
            for cand_rel in _rels:
                candidate = _resolve_nocase(mod_root, cand_rel,
                                            cache=_nocase_cache)
                if candidate is None:
                    continue
                try:
                    _rel_real = candidate.relative_to(mod_root).as_posix()
                except ValueError:
                    _rel_real = cand_rel
                if _usable(candidate, _rel_real, _exc):
                    src = candidate
                    break
        if src is None or not src.is_file():
            _log(f"  WARN: source not found for root-flagged file: {mod_name}/{rel_str}")
            continue

        dst = _resolve_root_path(game_root, Path(rel_str), _dir_cache)
        rel_posix = dst.relative_to(game_root).as_posix()

        # Skip if already placed by a previous call (avoid double-backup)
        if rel_posix in existing_placed_set:
            continue

        # Record whether the top-level dir existed *before* we transfer,
        # so restore knows whether to remove it. Only meaningful for nested paths.
        if len(Path(rel_str).parts) > 1:
            try:
                _top_name = dst.relative_to(game_root).parts[0]
            except ValueError:
                _top_name = None
            if _top_name:
                pre = _top_seen.get(_top_name)
                if pre is None:
                    pre = (game_root / _top_name).exists()
                    _top_seen[_top_name] = pre
                if not pre:
                    created_dirs.add(_top_name)

        tasks.append((src, dst, rel_posix))

    if not tasks:
        return 0

    def _write_log(rels: "list[str]") -> None:
        # Merge with any existing entries from deploy_root_folder.
        with log_path.open("w", encoding="utf-8", errors="surrogateescape") as f:
            f.write("\n".join(existing_placed + rels))
            if created_dirs:
                f.write("\n---dirs---\n")
                f.write("\n".join(sorted(created_dirs)))

    # Write the log BEFORE touching the game dir (see deploy_root_folder).
    _write_log([rel_posix for _s, _d, rel_posix in tasks])

    # Back up pre-existing files / drop stale symlinks.  One lstat each.
    for _src, dst, rel_posix in tasks:
        try:
            st = os.lstat(dst)
        except OSError:
            continue
        if _stat.S_ISLNK(st.st_mode):
            dst.unlink()
        elif _stat.S_ISREG(st.st_mode):
            bak = backup_dir / rel_posix
            _move_crash_safe(dst, bak)
            _log(f"  Backed up existing {rel_posix} → Root_Backup/")

    # Pre-create destination directories, then transfer in parallel.
    _mkdir_leaves({os.path.dirname(str(dst)) for _s, dst, _p in tasks})
    placed: list[str] = []

    def _do_flagged(item: "tuple[Path, Path, str]"):
        src, dst, rel_posix = item
        _actual, err = _do_link_ex(str(src), str(dst), mode)
        return rel_posix, err

    with concurrent.futures.ThreadPoolExecutor(max_workers=_deploy_workers()) as pool:
        for rel_posix, err in pool.map(_do_flagged, tasks):
            if err is None:
                placed.append(rel_posix)
            else:
                _log(f"  WARN: could not transfer root-flagged file {rel_posix}: {err}")

    # Re-write the log with what actually landed.
    _write_log(placed)

    _log(f"  Root-flagged mods: {len(placed)} file(s) transferred to game root.")
    return len(placed)


def restore_root_folder(
    root_folder_dir: Path,
    game_root: Path,
    log_fn=None,
    data_deploy_dirs: "set[str] | None" = None,
) -> int:
    """Undo a deploy_root_folder() operation.

    Reads the log written by deploy_root_folder(), removes every file that
    was placed into game_root, restores any backed-up originals from
    Root_Backup/, then removes the log and any empty directories left behind.

    root_folder_dir — Profiles/<game>/Root_Folder/  (used to locate the log)
    game_root       — the game's install directory
    data_deploy_dirs — top-level dir names (e.g. {"Data"}) that the standard
                     Data/ deploy also owns.  A placed file under one of these
                     dirs that is now a plain regular file with no Root_Backup/
                     original was a deployed-vanilla symlink/hardlink at deploy
                     time (so we never backed it up) and has since been restored
                     to genuine vanilla by restore_data_core() — leaving it
                     intact, not deleting it, keeps that vanilla file.  Defaults
                     to no protection; callers pass the game's
                     root_restore_protect_dirs() (e.g. {"Data"} for Bethesda).
    Returns the number of files removed from game_root.
    Silently does nothing if the log file is absent (no prior deploy).
    """
    _log = _safe_log(log_fn)
    _t_root_restore = _time.perf_counter()

    log_path   = root_folder_dir.parent / _ROOT_LOG_NAME
    backup_dir = root_folder_dir.parent / _ROOT_BACKUP_NAME

    if not log_path.is_file():
        return 0

    # Parse log: files section and optional ---dirs--- section.
    content = log_path.read_text(encoding="utf-8", errors="surrogateescape")
    if "---dirs---" in content:
        files_section, dirs_section = content.split("---dirs---", 1)
    else:
        files_section, dirs_section = content, ""
    placed      = [p for p in files_section.splitlines() if p]
    created_dirs = [d for d in dirs_section.splitlines() if d]
    removed = 0

    # A placed file that overwrote a path which *also* belongs to the standard
    # Data/ deploy (e.g. a root-flagged mod shipping its own Data/Fallout4.esm)
    # is dangerous to delete blindly.  At deploy time the pre-existing file there
    # was a deployed-vanilla symlink/hardlink into Data_Core/, so we never copied
    # an original into Root_Backup/ — only its raw bytes live in Data_Core/.  By
    # the time this restore runs, restore_data_core() has already wiped Data/ and
    # renamed Data_Core/ back, so the path now holds the genuine vanilla file.
    # Unlinking it here (with nothing in Root_Backup/ to put back) destroys the
    # vanilla copy for good.  Rule: only remove a placed file when Root_Backup/
    # actually holds its original — otherwise the file is owned by the Data_Core
    # mechanism (or was already cleared) and must be left alone.
    def _has_backup(rel_str: str) -> bool:
        bak = backup_dir / rel_str
        try:
            return bak.exists() or os.path.islink(bak)
        except OSError:
            return False

    _protect_dirs = {d.lower() for d in (data_deploy_dirs or set())}

    def _under_data_deploy(rel_str: str) -> bool:
        # First path segment matches a Data-deploy dir (case-insensitively).
        head = rel_str.replace("\\", "/").split("/", 1)[0].lower()
        return head in _protect_dirs

    # Remove files we placed (parallelised — one lstat + one unlink per worker).
    _game_root_str = str(game_root)
    safe_targets: list[str] = []
    for rel_str in placed:
        dst = game_root / rel_str
        if not _path_under_root(dst, game_root):
            _log(f"  SKIP: path traversal blocked — {rel_str}")
            continue
        # Protect only Data-deploy paths with no Root_Backup original — those are
        # the files restore_data_core owns.  Pure root-folder mod files (winhttp
        # .dll, BepInEx/, etc.) are still removed even without a backup.
        protect = _under_data_deploy(rel_str) and not _has_backup(rel_str)
        safe_targets.append((_game_root_str + "/" + rel_str, protect))

    def _unlink_one(item) -> int:
        p, protect = item
        try:
            st = os.lstat(p)
        except OSError:
            return 0
        # A real regular file at a protected Data path is the vanilla file that
        # restore_data_core put back — leave it (deleting it would lose vanilla).
        # Symlinks are always our own deploy artifacts: safe to drop.
        if protect and _stat.S_ISREG(st.st_mode):
            return 0
        if _stat.S_ISLNK(st.st_mode) or _stat.S_ISREG(st.st_mode):
            try:
                os.unlink(p)
                return 1
            except OSError:
                return 0
        return 0

    if safe_targets:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_deploy_workers()) as pool:
            for n in pool.map(_unlink_one, safe_targets):
                removed += n

    # Restore backed-up originals if any.
    _restore_backup_dir(backup_dir, game_root, _log)

    # Remove the log.
    log_path.unlink()

    # Wipe entire top-level directories we freshly created — removes any
    # game-generated files written into them after deploy.
    for dir_name in created_dirs:
        if ".." in dir_name or "/" in dir_name or "\\" in dir_name:
            _log(f"  SKIP: path traversal blocked — {dir_name}/")
            continue
        d = game_root / dir_name
        if not _path_under_root(d, game_root):
            _log(f"  SKIP: path traversal blocked — {dir_name}/")
            continue
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            _log(f"  Removed created directory {dir_name}/")

    # Remove any empty subdirectories left behind inside pre-existing dirs
    # (e.g. BepInEx/patchers/Tobey/ left empty after our files were removed).
    dirs_to_check: set[Path] = {(game_root / rel_str).parent for rel_str in placed}
    _prune_empty_dirs(dirs_to_check, stop_dirs={game_root})

    print(f"  [TIMER] restore_root_folder: {_time.perf_counter() - _t_root_restore:.3f}s")
    _log(f"  Root Folder restore: removed {removed} file(s) from game root.")
    return removed


__all__ = [
    "_ROOT_BACKUP_NAME",
    "_ROOT_LOG_NAME",
    "deploy_root_folder",
    "deploy_root_flagged_mods",
    "restore_root_folder",
]
