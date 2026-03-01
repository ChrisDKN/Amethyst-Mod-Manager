"""
deploy.py
Shared deployment logic for linking mod files into a game's install directory.

Provides move_to_core(), deploy_filemap(), deploy_core(), restore_data_core().
Game handlers call these instead of reimplementing the file-linking logic.

Transfer modes (LinkMode enum):
  HARDLINK  — os.link()     No extra disk space; same filesystem required.
  SYMLINK   — os.symlink()  Works across filesystems; dest is a pointer.
  COPY      — shutil.copy2() Full independent copy.

Every function that operates on a backup directory accepts an explicit
core_dir parameter.  When omitted it defaults to a sibling of deploy_dir
named "<deploy_dir.name>_Core" (e.g. Data/ → Data_Core/).  Games whose
install layout differs can pass any Path they like.

Typical deploy workflow:
  1. move_to_core(deploy_dir, core_dir)       — backs up vanilla files
  2. deploy_filemap(filemap, deploy_dir, ...)  — links mod files in
  3. deploy_core(deploy_dir, core_dir, ...)    — fills gaps with vanilla
  4. restore_data_core(deploy_dir, core_dir)  — undoes the deploy
"""

from __future__ import annotations

import json
import os
import shutil
from enum import Enum, auto
from pathlib import Path


def load_per_mod_strip_prefixes(profile_dir: Path) -> dict[str, list[str]]:
    """Load per-mod strip prefixes from profile_dir/mod_strip_prefixes.json.

    Returns a dict mapping mod name to list of top-level folder names to
    ignore during deployment (contents move up one level). Missing or
    invalid file returns {}.
    """
    path = profile_dir / "mod_strip_prefixes.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {
            k: v if isinstance(v, list) else []
            for k, v in data.items()
            if isinstance(k, str)
        }
    except (OSError, json.JSONDecodeError):
        return {}


class LinkMode(Enum):
    HARDLINK = auto()
    SYMLINK  = auto()
    COPY     = auto()


def _default_core(deploy_dir: Path) -> Path:
    """Return the default backup directory for deploy_dir."""
    return deploy_dir.parent / f"{deploy_dir.name}_Core"


def _transfer(src: Path, dst: Path, mode: LinkMode) -> None:
    """Transfer a single file from src to dst using the requested mode."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if mode is LinkMode.HARDLINK:
        os.link(src, dst)
    elif mode is LinkMode.SYMLINK:
        os.symlink(src, dst)
    else:
        shutil.copy2(src, dst)


def _clear_dir(directory: Path) -> int:
    """Delete all files inside directory and remove empty subdirectories.
    Returns the number of files deleted.  The directory itself is kept.
    """
    removed = 0
    for path in directory.rglob("*"):
        if path.is_file():
            path.unlink()
            removed += 1
    for dirpath in sorted(directory.rglob("*"), reverse=True):
        if dirpath.is_dir():
            try:
                dirpath.rmdir()
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# Step 1 — back up the game install directory
# ---------------------------------------------------------------------------

def move_to_core(
    deploy_dir: Path,
    core_dir: Path | None = None,
    log_fn=None,
) -> int:
    """Move all files from deploy_dir into core_dir (the vanilla backup).

    deploy_dir — directory whose contents will be moved out (e.g. Data/)
    core_dir   — destination for the backup; defaults to Data_Core/ sibling
    Returns the number of files moved.

    If core_dir already exists it is removed first so we always start clean.
    If deploy_dir is empty, core_dir is still created (empty) so restore always
    finds a core folder and does not report "nothing to restore".
    """
    _log = log_fn or (lambda _: None)
    core_dir = core_dir or _default_core(deploy_dir)

    if core_dir.exists():
        _log(f"  {core_dir.name} already exists — removing old backup first.")
        shutil.rmtree(core_dir)

    files = [p for p in deploy_dir.rglob("*") if p.is_file()] if deploy_dir.is_dir() else []
    if not files:
        core_dir.mkdir(parents=True, exist_ok=True)
        return 0

    for src in files:
        rel = src.relative_to(deploy_dir)
        dst = core_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))

    _clear_dir(deploy_dir)
    return len(files)


# ---------------------------------------------------------------------------
# Step 2 — link mod files listed in filemap.txt into the deploy directory
# ---------------------------------------------------------------------------

_OVERWRITE_NAME = "[Overwrite]"


def deploy_filemap(
    filemap_path: Path,
    deploy_dir: Path,
    staging_root: Path,
    mode: LinkMode = LinkMode.HARDLINK,
    strip_prefixes: set[str] | None = None,
    per_mod_strip_prefixes: dict[str, list[str]] | None = None,
    log_fn=None,
    progress_fn=None,
) -> tuple[int, set[str]]:
    """Read filemap.txt and transfer every listed file into deploy_dir.

    filemap_path   — Profiles/<game>/filemap.txt
    deploy_dir     — destination directory (e.g. <game_path>/Data)
    staging_root   — Profiles/<game>/mods/
    mode           — transfer method
    strip_prefixes — same set passed to build_filemap; used to locate source
                     files whose leading folder was stripped from the filemap
                     path (e.g. rel_str "Nautilus/Nautilus.dll" may live on
                     disk as "plugins/Nautilus/Nautilus.dll").
    per_mod_strip_prefixes — optional dict mapping mod name to list of
                     top-level folder names to prepend when resolving (user-
                     configured "ignore" folders for that mod).
    progress_fn    — optional callable(done: int, total: int) called after
                     each file is transferred.

    Returns:
        (count, placed_lower)
        placed_lower is the set of lowercased rel paths successfully placed —
        pass it to deploy_core() so it can skip files already provided by mods.
    """
    _log = log_fn or (lambda _: None)
    _strip = {p.lower() for p in strip_prefixes} if strip_prefixes else set()
    _per_mod = per_mod_strip_prefixes or {}
    overwrite_dir = staging_root.parent / "overwrite"

    already_seen: set[str] = set()
    tasks: list[tuple[Path, Path, str]] = []
    placed_lower: set[str] = set()
    nocase_cache: dict[Path, dict[str, Path]] = {}

    # Read all lines first so we know the total for progress reporting.
    raw_lines: list[str] = []
    with filemap_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if "\t" in line:
                raw_lines.append(line)

    total_lines = len(raw_lines)

    for line_idx, line in enumerate(raw_lines, 1):
        rel_str, mod_name = line.split("\t", 1)

        rel_lower = rel_str.lower()
        if rel_lower in already_seen:
            continue
        already_seen.add(rel_lower)

        mod_root = overwrite_dir if mod_name == _OVERWRITE_NAME else staging_root / mod_name
        src = mod_root / rel_str

        if not src.is_file():
            src = _resolve_nocase(mod_root, rel_str, cache=nocase_cache)

        # If still not found, try re-adding stripped prefixes — the filemap
        # stripped them during build but the file on disk still has them.
        # First try global strip_prefixes (1 or 2 segments).
        if src is None and _strip:
            prefixes = sorted(_strip)  # deterministic order
            for p1 in prefixes:
                candidate = _resolve_nocase(
                    mod_root, p1 + "/" + rel_str, cache=nocase_cache)
                if candidate is not None:
                    src = candidate
                    break
                for p2 in prefixes:
                    candidate = _resolve_nocase(
                        mod_root, p1 + "/" + p2 + "/" + rel_str, cache=nocase_cache)
                    if candidate is not None:
                        src = candidate
                        break
                if src is not None:
                    break

        # Then try per-mod: full path prefixes first, then segment-name chain
        if src is None and mod_name != _OVERWRITE_NAME:
            mod_strip = _per_mod.get(mod_name)
            if mod_strip:
                path_prefixes = [p for p in mod_strip if "/" in p]
                for p in path_prefixes:
                    candidate = _resolve_nocase(
                        mod_root, p + "/" + rel_str, cache=nocase_cache)
                    if candidate is not None:
                        src = candidate
                        break
                if src is None:
                    segment_list = [p for p in mod_strip if "/" not in p]
                    prefix_path = ""
                    for seg in segment_list:
                        prefix_path = prefix_path + seg + "/" if prefix_path else seg + "/"
                        candidate = _resolve_nocase(
                            mod_root, prefix_path + rel_str, cache=nocase_cache)
                        if candidate is not None:
                            src = candidate
                            break

        if src is None:
            _log(f"  WARN: source not found — {rel_str} ({mod_name})")
            continue

        dst = deploy_dir / rel_str
        tasks.append((src, dst, rel_lower))

        if progress_fn is not None and line_idx % 500 == 0:
            progress_fn(line_idx, total_lines)

    total = len(tasks)
    if total == 0:
        return 0, placed_lower

    linked = 0

    for done_count, (src, dst, rel_lower) in enumerate(tasks, 1):
        try:
            _transfer(src, dst, mode)
            linked += 1
            placed_lower.add(rel_lower)
        except OSError as e:
            _log(f"  WARN: could not transfer {dst.relative_to(deploy_dir)}: {e}")
        if progress_fn is not None and (done_count % 200 == 0 or done_count == total):
            progress_fn(done_count, total)

    return linked, placed_lower


# ---------------------------------------------------------------------------
# Step 3 — fill gaps with vanilla files from the backup
# ---------------------------------------------------------------------------

def deploy_core(
    deploy_dir: Path,
    already_placed: set[str],
    core_dir: Path | None = None,
    mode: LinkMode = LinkMode.HARDLINK,
    log_fn=None,
    progress_fn=None,
) -> int:
    """Transfer files from core_dir into deploy_dir for any path not already
    covered by a mod.

    deploy_dir     — destination (e.g. <game_path>/Data)
    already_placed — lowercased rel paths already placed by deploy_filemap()
    core_dir       — vanilla backup directory; defaults to Data_Core/ sibling
    progress_fn    — optional callable(done: int, total: int)
    Returns the number of files transferred.
    """
    _log = log_fn or (lambda _: None)
    core_dir = core_dir or _default_core(deploy_dir)

    if not core_dir.is_dir():
        return 0

    linked = 0
    files = [s for s in core_dir.rglob("*") if s.is_file()]
    total = len(files)

    for done_count, src in enumerate(files, 1):
        rel = src.relative_to(core_dir)
        rel_lower = str(rel).replace("\\", "/").lower()
        if rel_lower in already_placed:
            continue
        dst = deploy_dir / rel
        try:
            _transfer(src, dst, mode)
            linked += 1
        except OSError as e:
            _log(f"  WARN: could not transfer {rel}: {e}")
        if progress_fn is not None:
            progress_fn(done_count, total)

    return linked


# ---------------------------------------------------------------------------
# Root folder — transfer files into the game's root directory
# ---------------------------------------------------------------------------

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
    _log = log_fn or (lambda _: None)

    if not root_folder_dir.is_dir():
        return 0

    # Collect all source files first; bail early if none.
    sources: list[tuple[Path, Path]] = []   # (src, rel)
    for src in root_folder_dir.rglob("*"):
        if src.is_file():
            sources.append((src, src.relative_to(root_folder_dir)))

    if not sources:
        return 0

    backup_dir = root_folder_dir.parent / _ROOT_BACKUP_NAME
    log_path   = root_folder_dir.parent / _ROOT_LOG_NAME
    placed: list[str] = []

    # Track which top-level directories we are creating so restore can wipe
    # them entirely — including any game-generated files written into them
    # after deploy (e.g. BepInEx cache/config/log files).
    created_dirs: set[str] = set()

    for src, rel in sources:
        dst = game_root / rel
        # Record the top-level directory we're about to create (if new).
        top = rel.parts[0] if len(rel.parts) > 1 else None
        if top and not (game_root / top).exists():
            created_dirs.add(top)

        # Back up any pre-existing file so restore can put it back.
        if dst.exists() and not dst.is_symlink():
            bak = backup_dir / rel
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(bak))
            _log(f"  Backed up existing {rel} → Root_Backup/")
        elif dst.is_symlink():
            dst.unlink()

        try:
            _transfer(src, dst, mode)
            placed.append(str(rel).replace("\\", "/"))
        except OSError as e:
            _log(f"  WARN: could not transfer root file {rel}: {e}")

    # Write the deployment log: files on the first line block, then a
    # separator, then directories we created that should be fully removed on
    # restore (deepest first so rmtree on each is safe).
    with log_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(placed))
        if created_dirs:
            f.write("\n---dirs---\n")
            f.write("\n".join(sorted(created_dirs)))

    _log(f"  Root Folder: {len(placed)} file(s) transferred to game root.")
    return len(placed)


def restore_root_folder(
    root_folder_dir: Path,
    game_root: Path,
    log_fn=None,
) -> int:
    """Undo a deploy_root_folder() operation.

    Reads the log written by deploy_root_folder(), removes every file that
    was placed into game_root, restores any backed-up originals from
    Root_Backup/, then removes the log and any empty directories left behind.

    root_folder_dir — Profiles/<game>/Root_Folder/  (used to locate the log)
    game_root       — the game's install directory
    Returns the number of files removed from game_root.
    Silently does nothing if the log file is absent (no prior deploy).
    """
    _log = log_fn or (lambda _: None)

    log_path   = root_folder_dir.parent / _ROOT_LOG_NAME
    backup_dir = root_folder_dir.parent / _ROOT_BACKUP_NAME

    if not log_path.is_file():
        return 0

    # Parse log: files section and optional ---dirs--- section.
    content = log_path.read_text(encoding="utf-8")
    if "---dirs---" in content:
        files_section, dirs_section = content.split("---dirs---", 1)
    else:
        files_section, dirs_section = content, ""
    placed      = [p for p in files_section.splitlines() if p]
    created_dirs = [d for d in dirs_section.splitlines() if d]
    removed = 0

    # Remove files we placed.
    for rel_str in placed:
        dst = game_root / rel_str
        if not _path_under_root(dst, game_root):
            _log(f"  SKIP: path traversal blocked — {rel_str}")
            continue
        if dst.is_file() or dst.is_symlink():
            dst.unlink()
            removed += 1

    # Restore backed-up originals if any.
    if backup_dir.is_dir():
        for bak_src in backup_dir.rglob("*"):
            if not bak_src.is_file():
                continue
            rel = bak_src.relative_to(backup_dir)
            orig = game_root / rel
            if not _path_under_root(orig, game_root):
                _log(f"  SKIP: path traversal blocked — {rel}")
                continue
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(bak_src), str(orig))
            _log(f"  Restored {rel} from Root_Backup/")
        shutil.rmtree(backup_dir, ignore_errors=True)

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
    # Walk deepest-first so parent dirs are checked after their children.
    dirs_to_check: set[Path] = set()
    for rel_str in placed:
        p = (game_root / rel_str).parent
        while p != game_root and p != game_root.parent:
            dirs_to_check.add(p)
            p = p.parent
    for d in sorted(dirs_to_check, key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()  # Only succeeds if the directory is empty
        except OSError:
            pass

    _log(f"  Root Folder restore: removed {removed} file(s) from game root.")
    return removed


# ---------------------------------------------------------------------------
# Restore — undo a deploy
# ---------------------------------------------------------------------------

def restore_data_core(
    deploy_dir: Path,
    core_dir: Path | None = None,
    overwrite_dir: Path | None = None,
    log_fn=None,
) -> int:
    """Undo a deploy: clear deploy_dir and move core_dir contents back.

    deploy_dir    — directory to restore (e.g. <game_path>/Data)
    core_dir      — vanilla backup to restore from; defaults to Data_Core/ sibling
    overwrite_dir — if given, any file in deploy_dir that is not a deployed mod
                    file and not present in core_dir (i.e. created at runtime by
                    the game or a mod) is moved here before clearing, preserving
                    its relative path.  Existing files in overwrite_dir are
                    overwritten.  Pass Profiles/<game>/overwrite/.
    Returns the number of files restored.

    If core_dir does not exist (e.g. the deploy dir was empty at deploy time
    so move_to_core skipped creating it), the deploy dir is simply cleared and
    0 is returned — no error is raised.
    """
    _log = log_fn or (lambda _: None)
    core_dir = core_dir or _default_core(deploy_dir)

    if not core_dir.is_dir():
        _log(f"  No {core_dir.name}/ found — nothing to restore (skipping).")
        return 0

    # Rescue runtime-created files into overwrite/ before wiping deploy_dir.
    # A file is runtime-created if it:
    #   - is not a symlink (symlinks are deployed mod files)
    #   - has a single hard-link count (nlink > 1 means it is a deployed hardlink)
    #   - is not present in core_dir (not a vanilla file)
    #   - is not listed in filemap.txt (copied mod files have nlink==1 when their
    #     staging copy was replaced after deploy, breaking the hardlink)
    #   - is not listed in modindex.txt (catches cross-profile mod files not in
    #     the current filemap.txt — e.g. when switching profiles)
    if overwrite_dir is not None and deploy_dir.is_dir():
        core_lower: set[str] = {
            f.relative_to(core_dir).as_posix().lower()
            for f in core_dir.rglob("*") if f.is_file()
        }
        filemap_lower: set[str] = set()
        filemap_path = overwrite_dir.parent / "filemap.txt"
        if filemap_path.is_file():
            with filemap_path.open(encoding="utf-8") as _fm:
                for _line in _fm:
                    _line = _line.rstrip("\n")
                    if "\t" in _line:
                        filemap_lower.add(_line.split("\t", 1)[0].lower())
        # Build a set of every file known to any mod in the index (all profiles,
        # all mods, enabled or disabled).  Runtime-created files won't appear here,
        # so any hit means "this is a mod file, don't rescue it".
        modindex_lower: set[str] = set()
        try:
            from Utils.filemap import read_mod_index
            _index = read_mod_index(overwrite_dir.parent / "modindex.txt")
            if _index:
                for _mod_name, (_normal, _root) in _index.items():
                    modindex_lower.update(_normal.keys())
        except Exception:
            pass
        rescued = 0
        for src in deploy_dir.rglob("*"):
            if not src.is_file():
                continue
            if src.is_symlink():
                continue  # deployed mod symlink
            if src.stat().st_nlink > 1:
                continue  # deployed mod hardlink
            rel = src.relative_to(deploy_dir)
            rel_lower = rel.as_posix().lower()
            if rel_lower in core_lower:
                continue  # vanilla file — will be restored from core
            if rel_lower in filemap_lower:
                continue  # known mod file whose hardlink was broken by a staging update
            if rel_lower in modindex_lower:
                continue  # known mod file (any mod/profile) — not runtime-created
            dst = overwrite_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            rescued += 1
        if rescued:
            _log(f"  Rescued {rescued} runtime-created file(s) → overwrite/.")
            # Update modindex.txt so the next build_filemap call immediately
            # sees the rescued files under [Overwrite] without a full rescan.
            try:
                from Utils.filemap import update_mod_index, read_mod_index, OVERWRITE_NAME
                index_path = overwrite_dir.parent / "modindex.txt"
                existing = read_mod_index(index_path) or {}
                existing_normal, existing_root = existing.get(OVERWRITE_NAME, ({}, {}))
                # Walk overwrite_dir to build the complete current file list.
                new_normal: dict[str, str] = dict(existing_normal)
                for f in overwrite_dir.rglob("*"):
                    if f.is_file() and not f.is_symlink():
                        rel = f.relative_to(overwrite_dir)
                        rel_str = rel.as_posix()
                        new_normal[rel_str.lower()] = rel_str
                update_mod_index(index_path, OVERWRITE_NAME, new_normal, existing_root)
            except Exception:
                pass

    removed = _clear_dir(deploy_dir) if deploy_dir.is_dir() else 0
    _log(f"  Removed {removed} file(s) from {deploy_dir.name}/.")

    restored = 0
    for src in core_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(core_dir)
        dst = deploy_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        restored += 1

    shutil.rmtree(core_dir)
    return restored


# ---------------------------------------------------------------------------
# Root-deploy — filemap-driven deploy/restore directly into the game root
# ---------------------------------------------------------------------------

# Log file written next to filemap.txt recording what was placed.
_FILEMAP_LOG_NAME   = "filemap_deployed.txt"
# Sibling directory used to back up files overwritten during root deploy.
_FILEMAP_BACKUP_DIR = "filemap_backup"


def deploy_filemap_to_root(
    filemap_path: Path,
    game_root: Path,
    staging_root: Path,
    mode: LinkMode = LinkMode.HARDLINK,
    strip_prefixes: set[str] | None = None,
    per_mod_strip_prefixes: dict[str, list[str]] | None = None,
    log_fn=None,
    progress_fn=None,
) -> tuple[int, set[str]]:
    """Deploy mod files directly into game_root, backing up any files they
    overwrite so restore_filemap_from_root() can undo the operation cleanly.

    Designed for games whose mods install into the game root itself (Cyberpunk,
    Witcher 3) rather than a dedicated subdirectory.  Unlike move_to_core() /
    restore_data_core(), this never touches vanilla files that mods don't
    overwrite — it only places mod files and backs up what they replace.

    filemap_path  — Profiles/<game>/filemap.txt
    game_root     — the game's install directory
    staging_root  — Profiles/<game>/mods/
    mode          — transfer method
    strip_prefixes — forwarded to source-file resolution (same as deploy_filemap)
    per_mod_strip_prefixes — optional dict mod name -> list of strip folders
    log_fn        — optional logging callable
    progress_fn   — optional callable(done: int, total: int)

    Writes a log file next to filemap.txt so restore_filemap_from_root() knows
    exactly which files to remove.

    Returns (count, placed_lower) — same shape as deploy_filemap().
    """
    _log = log_fn or (lambda _: None)
    _strip = {p.lower() for p in strip_prefixes} if strip_prefixes else set()
    _per_mod = per_mod_strip_prefixes or {}
    overwrite_dir = staging_root.parent / "overwrite"
    backup_dir    = filemap_path.parent / _FILEMAP_BACKUP_DIR
    log_path      = filemap_path.parent / _FILEMAP_LOG_NAME

    # Clear any stale backup from a previous deploy.
    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    already_seen: set[str] = set()
    placed_lower: set[str] = set()
    placed_log:   list[str] = []
    nocase_cache: dict[Path, dict[str, Path]] = {}

    raw_lines: list[str] = []
    with filemap_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if "\t" in line:
                raw_lines.append(line)

    total_lines = len(raw_lines)
    tasks: list[tuple[Path, Path, str]] = []

    for line_idx, line in enumerate(raw_lines, 1):
        rel_str, mod_name = line.split("\t", 1)
        rel_lower = rel_str.lower()
        if rel_lower in already_seen:
            continue
        already_seen.add(rel_lower)

        mod_root = overwrite_dir if mod_name == _OVERWRITE_NAME else staging_root / mod_name
        src = mod_root / rel_str
        if not src.is_file():
            src = _resolve_nocase(mod_root, rel_str, cache=nocase_cache)
        if src is None and _strip:
            prefixes = sorted(_strip)
            for p1 in prefixes:
                candidate = _resolve_nocase(mod_root, p1 + "/" + rel_str, cache=nocase_cache)
                if candidate is not None:
                    src = candidate
                    break
                for p2 in prefixes:
                    candidate = _resolve_nocase(mod_root, p1 + "/" + p2 + "/" + rel_str, cache=nocase_cache)
                    if candidate is not None:
                        src = candidate
                        break
                if src is not None:
                    break
        if src is None and mod_name != _OVERWRITE_NAME:
            mod_strip = _per_mod.get(mod_name)
            if mod_strip:
                path_prefixes = [p for p in mod_strip if "/" in p]
                for p in path_prefixes:
                    candidate = _resolve_nocase(
                        mod_root, p + "/" + rel_str, cache=nocase_cache)
                    if candidate is not None:
                        src = candidate
                        break
                if src is None:
                    segment_list = [p for p in mod_strip if "/" not in p]
                    prefix_path = ""
                    for seg in segment_list:
                        prefix_path = prefix_path + seg + "/" if prefix_path else seg + "/"
                        candidate = _resolve_nocase(
                            mod_root, prefix_path + rel_str, cache=nocase_cache)
                        if candidate is not None:
                            src = candidate
                            break
        if src is None:
            _log(f"  WARN: source not found — {rel_str} ({mod_name})")
            continue

        dst = game_root / rel_str
        tasks.append((src, dst, rel_lower, rel_str))

        if progress_fn is not None and line_idx % 500 == 0:
            progress_fn(line_idx, total_lines)

    total = len(tasks)
    linked = 0

    for done_count, (src, dst, rel_lower, rel_str) in enumerate(tasks, 1):
        # Back up any vanilla file we are about to overwrite.
        if dst.exists() and not dst.is_symlink():
            bak = backup_dir / rel_str
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(bak))
        elif dst.is_symlink():
            dst.unlink()

        try:
            _transfer(src, dst, mode)
            linked += 1
            placed_lower.add(rel_lower)
            placed_log.append(rel_str.replace("\\", "/"))
        except OSError as e:
            _log(f"  WARN: could not transfer {rel_str}: {e}")

        if progress_fn is not None and (done_count % 200 == 0 or done_count == total):
            progress_fn(done_count, total)

    # Write the deployment log so restore knows what to remove.
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(placed_log), encoding="utf-8")

    return linked, placed_lower


def restore_filemap_from_root(
    filemap_path: Path,
    game_root: Path,
    log_fn=None,
) -> int:
    """Undo a deploy_filemap_to_root() operation.

    Reads the log written by deploy_filemap_to_root(), removes every mod file
    that was placed into game_root, then restores any backed-up vanilla files
    from filemap_backup/.  Silently does nothing if the log is absent.

    filemap_path — Profiles/<game>/filemap.txt  (used to locate the log)
    game_root    — the game's install directory
    Returns the number of mod files removed.
    """
    _log = log_fn or (lambda _: None)
    log_path   = filemap_path.parent / _FILEMAP_LOG_NAME
    backup_dir = filemap_path.parent / _FILEMAP_BACKUP_DIR

    if not log_path.is_file():
        _log("  No filemap_deployed.txt found — nothing to restore.")
        return 0

    placed = [p for p in log_path.read_text(encoding="utf-8").splitlines() if p]
    removed = 0

    for rel_str in placed:
        dst = game_root / rel_str
        if not _path_under_root(dst, game_root):
            _log(f"  SKIP: path traversal blocked — {rel_str}")
            continue
        if dst.is_file() or dst.is_symlink():
            dst.unlink()
            removed += 1

    # Restore backed-up vanilla files.
    if backup_dir.is_dir():
        for bak_src in backup_dir.rglob("*"):
            if not bak_src.is_file():
                continue
            rel = bak_src.relative_to(backup_dir)
            orig = game_root / rel
            if not _path_under_root(orig, game_root):
                _log(f"  SKIP: path traversal blocked — {rel}")
                continue
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(bak_src), str(orig))
            _log(f"  Restored {rel} from filemap_backup/")
        shutil.rmtree(backup_dir, ignore_errors=True)

    # Remove any empty subdirectories left behind by the removed mod files.
    dirs_to_check: set[Path] = set()
    for rel_str in placed:
        p = (game_root / rel_str).parent
        while p != game_root and p != game_root.parent:
            dirs_to_check.add(p)
            p = p.parent
    for d in sorted(dirs_to_check, key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass

    log_path.unlink()
    _log(f"  Filemap restore: removed {removed} mod file(s) from game root.")
    return removed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _path_under_root(path: Path, root: Path) -> bool:
    """Return True if path resolves to a location under root (no path traversal)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve_nocase(root: Path, rel_str: str,
                    cache: dict[Path, dict[str, Path]] | None = None) -> Path | None:
    """Resolve a relative path case-insensitively under root.

    Each path segment is matched case-insensitively against the real
    filesystem entries so that a canonical rel_str (e.g. "Scripts/foo.pex")
    will find the actual file even if the mod folder uses "scripts/foo.pex".

    An optional *cache* dict maps directory Paths to {lowercase_name: entry}
    dicts so that repeated lookups in the same directory avoid re-scanning.

    Returns the resolved Path if it exists, or None.
    """
    if cache is None:
        cache = {}
    current = root
    parts = rel_str.replace("\\", "/").split("/")
    for part in parts:
        part_lower = part.lower()
        listing = cache.get(current)
        if listing is None:
            try:
                listing = {e.name.lower(): e for e in current.iterdir()}
            except OSError:
                return None
            cache[current] = listing
        matched = listing.get(part_lower)
        if matched is None:
            return None
        current = matched
    if current.is_file():
        return current
    return None