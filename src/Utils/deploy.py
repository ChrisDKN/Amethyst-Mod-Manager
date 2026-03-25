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

import concurrent.futures
import json
import os
import shutil
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path


def load_per_mod_strip_prefixes(profile_dir: Path) -> dict[str, list[str]]:
    """Load per-mod strip prefixes from profile_state.json (falls back to legacy file)."""
    from Utils.profile_state import read_mod_strip_prefixes
    return read_mod_strip_prefixes(profile_dir)


def load_separator_deploy_paths(profile_dir: Path) -> dict[str, dict]:
    """Load separator deploy paths from profile_state.json (falls back to legacy file)."""
    from Utils.profile_state import read_separator_deploy_paths
    return read_separator_deploy_paths(profile_dir)


def expand_separator_deploy_paths(
    sep_paths: dict[str, dict],
    entries,
) -> dict[str, Path]:
    """Convert sep_paths → {mod_name: Path} using modlist order.

    Only mods whose separator has a non-empty path override are included.
    entries — list[ModEntry] from read_modlist()
    """
    result: dict[str, Path] = {}
    current_override: Path | None = None
    for entry in entries:
        if entry.is_separator:
            raw_path = sep_paths.get(entry.name, {}).get("path", "")
            current_override = Path(raw_path) if raw_path else None
        else:
            if current_override is not None:
                result[entry.name] = current_override
    return result


def expand_separator_raw_deploy(
    sep_paths: dict[str, dict],
    entries,
) -> set[str]:
    """Return the set of mod names whose separator has 'raw deploy' enabled.

    When raw deploy is on, deployment rules (routing, strip) are ignored and
    files are placed as-is relative to the custom deploy directory.
    entries — list[ModEntry] from read_modlist()
    """
    result: set[str] = set()
    current_raw = False
    for entry in entries:
        if entry.is_separator:
            info = sep_paths.get(entry.name, {})
            current_raw = bool(info.get("raw", False))
        else:
            if current_raw:
                result.add(entry.name)
    return result


def cleanup_custom_deploy_dirs(
    profile_dir: "Path | None",
    entries,
    log_fn=None,
    filemap_path: "Path | None" = None,
) -> int:
    """Remove files deployed to custom separator locations and restore originals.

    Reads custom_deploy_log.txt written by deploy_filemap() and deletes every
    file listed there, then restores any originals from custom_deploy_backup/.

    Returns the number of files removed.
    """
    _log = log_fn or (lambda _: None)

    if profile_dir is None:
        return 0

    # Locate log: search profile_dir and two levels up (profile root)
    log_path: Path | None = None
    for candidate_dir in (profile_dir, profile_dir.parent.parent):
        c = candidate_dir / "custom_deploy_log.txt"
        if c.is_file():
            log_path = c
            break

    if log_path is None:
        return 0

    file_list = [p for p in log_path.read_text(encoding="utf-8").splitlines() if p]
    backup_dir = log_path.parent / "custom_deploy_backup"

    removed = 0
    dirs_to_prune: set[Path] = set()
    stop_dirs: set[Path] = set()

    for abs_str in file_list:
        target = Path(abs_str)
        if target.is_file() or target.is_symlink():
            try:
                target.unlink()
                removed += 1
                dirs_to_prune.add(target.parent)
                stop_dirs.add(target.parent)
            except OSError as exc:
                _log(f"  WARN: could not remove custom-deployed {target}: {exc}")

    # Restore any originals that were backed up before deployment.
    restored = 0
    if backup_dir.is_dir():
        for bak_src in backup_dir.rglob("*"):
            if not bak_src.is_file():
                continue
            # Reconstruct original absolute path: backup mirrors the full
            # absolute path of the original (minus leading slash/anchor).
            rel = bak_src.relative_to(backup_dir)
            orig = Path("/") / rel
            try:
                orig.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(bak_src), str(orig))
                restored += 1
                _log(f"  Restored {orig.name} from custom_deploy_backup/")
            except OSError as exc:
                _log(f"  WARN: could not restore {orig}: {exc}")
        shutil.rmtree(backup_dir, ignore_errors=True)

    _prune_empty_dirs(dirs_to_prune, stop_dirs)

    try:
        log_path.unlink()
    except OSError:
        pass

    if removed:
        _log(f"  Removed {removed} file(s) from custom deployment location(s).")
    if restored:
        _log(f"  Restored {restored} original file(s) to custom deployment location(s).")
    return removed


def restore_custom_deploy_backup_for_path(
    filemap_path: "Path | None",
    custom_path: Path,
    log_fn=None,
) -> int:
    """Restore backed-up originals whose location is under custom_path.

    Called when a separator with a custom deploy location is removed while the
    game is still deployed — the backup files for that location must be put back
    immediately rather than waiting for the next full restore.

    Also removes the corresponding entries from custom_deploy_log.txt so that
    the full cleanup later does not try to delete the restored originals.

    Returns the number of files restored.
    """
    _log = log_fn or (lambda _: None)

    if filemap_path is None:
        return 0

    profile_dir = filemap_path.parent
    backup_dir  = profile_dir / "custom_deploy_backup"
    log_path    = profile_dir / "custom_deploy_log.txt"

    if not backup_dir.is_dir():
        return 0

    # The backup mirrors absolute paths: backup_dir / <abs-path-minus-anchor>
    # Files whose original location is under custom_path will be under:
    #   backup_dir / custom_path.relative_to(custom_path.anchor)
    try:
        backup_subtree = backup_dir / custom_path.relative_to(custom_path.anchor)
    except ValueError:
        return 0

    if not backup_subtree.is_dir():
        return 0

    restored = 0
    for bak_src in backup_subtree.rglob("*"):
        if not bak_src.is_file():
            continue
        rel = bak_src.relative_to(backup_dir)
        orig = Path("/") / rel
        try:
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(bak_src), str(orig))
            restored += 1
            _log(f"  Restored {orig.name} from custom_deploy_backup/")
        except OSError as exc:
            _log(f"  WARN: could not restore {orig}: {exc}")

    # Clean up the now-empty backup subtree.
    shutil.rmtree(backup_subtree, ignore_errors=True)

    # Remove entries for this path from the deploy log so full cleanup won't
    # try to delete the now-restored originals.
    if log_path.is_file() and restored:
        try:
            lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l]
            kept = [l for l in lines if not Path(l).is_relative_to(custom_path)]
            if len(kept) < len(lines):
                if kept:
                    log_path.write_text("\n".join(kept), encoding="utf-8")
                else:
                    log_path.unlink()
        except OSError:
            pass

    if restored:
        _log(f"  Restored {restored} original file(s) for removed separator.")
    return restored


def _prune_empty_dirs(dirs: "set[Path]", stop_dirs: "set[Path] | None" = None) -> None:
    """Remove empty directories bottom-up, stopping at (and never removing) stop_dirs."""
    _stop = stop_dirs or set()
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        current = d
        while current not in _stop:
            try:
                if current.is_dir() and not any(current.iterdir()):
                    current.rmdir()
                    current = current.parent
                else:
                    break
            except OSError:
                break


class LinkMode(Enum):
    HARDLINK = auto()
    SYMLINK  = auto()
    COPY     = auto()


@dataclass
class CustomRule:
    """A file-routing rule that sends matched files to a game-root-relative
    destination directory.

    Matching is by extension, leading folder name, or both (both must match).

    dest       — path relative to the game install root (e.g. "pak_mods", "")
    extensions — lowercase file extensions to match (e.g. [".pak"]).
                 Empty list means no extension filter.
    folders    — lowercase first-path-segment names to match (e.g. ["natives"]).
                 Empty list means no folder filter.

    Placement behaviour:
    - extension-only match: file placed as game_root/dest/<filename> (flat)
    - folder match (with or without extension): file placed as
      game_root/dest/<original rel_path> (full path preserved)
    """
    dest: str
    extensions: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)


_CUSTOM_RULES_LOG_NAME = "custom_rules_deployed.txt"
_CUSTOM_RULES_BACKUP_DIR = "custom_rules_backup"


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

    Uses shutil.rmtree + recreate rather than per-file unlink — significantly
    faster for large directories (avoids repeated stat/unlink/rmdir syscalls).
    """
    if not directory.is_dir():
        return 0
    files = [p for p in directory.rglob("*") if p.is_file()]
    count = len(files)
    if count == 0:
        return 0
    shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return count


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

    if not deploy_dir.is_dir():
        core_dir.mkdir(parents=True, exist_ok=True)
        return 0

    # Count files before the move so we can report the number moved.
    count = sum(1 for _ in deploy_dir.rglob("*") if _.is_file())
    if not count:
        core_dir.mkdir(parents=True, exist_ok=True)
        return 0

    # Same filesystem → os.rename is a single instant syscall.
    # shutil.move falls back to copy+delete if cross-device.
    core_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(deploy_dir), str(core_dir))

    # Recreate the (now-empty) deploy dir so downstream code finds it.
    deploy_dir.mkdir(parents=True, exist_ok=True)
    return count


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
    per_mod_deploy_dirs: dict[str, Path] | None = None,
    log_fn=None,
    progress_fn=None,
    symlink_exts: set[str] | None = None,
    exclude: set[str] | None = None,
    core_dir: "Path | None" = None,
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
    _per_deploy = per_mod_deploy_dirs or {}
    overwrite_dir = staging_root.parent / "overwrite"

    already_seen: set[str] = set()
    tasks: list[tuple[Path, Path, str]] = []
    placed_lower: set[str] = set()
    _exclude: set[str] = exclude or set()

    # Pre-compute string roots so the hot loop avoids Path construction.
    _overwrite_str = str(overwrite_dir)
    _staging_str   = str(staging_root)
    _isfile        = os.path.isfile
    sorted_strip   = sorted(_strip) if _strip else []
    nocase_cache: dict[Path, dict[str, list[Path]]] = {}
    mod_index_cache: dict[Path, dict[str, Path]] = {}
    dst_dir_cache: dict[Path, dict[str, str]] = {}

    # Single-pass: read file once, then process. Avoids second disk read.
    # Using os.path.isfile (one C-level stat) instead of Path.is_file()
    # avoids the overhead of constructing Path objects for the fast path.
    with filemap_path.open(encoding="utf-8") as f:
        _tab_lines = [ln.rstrip("\n") for ln in f if "\t" in ln]
    total_lines = len(_tab_lines)
    line_idx = 0

    for line in _tab_lines:
        rel_str, mod_name = line.split("\t", 1)
        rel_lower = rel_str.lower()
        if rel_lower in already_seen:
            continue
        already_seen.add(rel_lower)
        if rel_lower in _exclude:
            continue
        line_idx += 1

        # Fast path: direct string join + stat via os.path.isfile
        if mod_name == _OVERWRITE_NAME:
            candidate_str = _overwrite_str + "/" + rel_str
        else:
            candidate_str = _staging_str + "/" + mod_name + "/" + rel_str
        if _isfile(candidate_str):
            src_str = candidate_str
        else:
            # Slow path: use per-mod index for O(1) lookup when available
            mod_root = overwrite_dir if mod_name == _OVERWRITE_NAME else staging_root / mod_name
            if mod_root not in mod_index_cache:
                mod_index_cache[mod_root] = _build_mod_index(mod_root)
            src = mod_index_cache[mod_root].get(rel_lower)
            if src is None:
                src = _resolve_nocase(mod_root, rel_str, cache=nocase_cache)
            if src is None and sorted_strip:
                for p1 in sorted_strip:
                    src = _resolve_nocase(mod_root, p1 + "/" + rel_str, cache=nocase_cache)
                    if src is not None:
                        break
                    for p2 in sorted_strip:
                        src = _resolve_nocase(mod_root, p1 + "/" + p2 + "/" + rel_str, cache=nocase_cache)
                        if src is not None:
                            break
                    if src is not None:
                        break
            if src is None and mod_name != _OVERWRITE_NAME:
                mod_strip = _per_mod.get(mod_name)
                if mod_strip:
                    path_prefixes = [p for p in mod_strip if "/" in p]
                    for p in path_prefixes:
                        src = _resolve_nocase(mod_root, p + "/" + rel_str, cache=nocase_cache)
                        if src is not None:
                            break
                    if src is None:
                        segment_list = [p for p in mod_strip if "/" not in p]
                        prefix_path = ""
                        for seg in segment_list:
                            prefix_path = prefix_path + seg + "/" if prefix_path else seg + "/"
                            src = _resolve_nocase(mod_root, prefix_path + rel_str, cache=nocase_cache)
                            if src is not None:
                                break
            if src is None:
                _log(f"  WARN: source not found — {rel_str} ({mod_name})")
                continue
            src_str = str(src)

        effective_dir = _per_deploy.get(mod_name, deploy_dir)
        _core = core_dir if effective_dir is deploy_dir else None
        dst_path = _resolve_root_path(effective_dir, Path(rel_str), dir_cache=dst_dir_cache, core_base=_core)
        dst_str = str(dst_path)
        use_symlink = symlink_exts is not None and os.path.splitext(src_str)[1].lower() in symlink_exts
        tasks.append((src_str, dst_str, rel_lower, effective_dir is not deploy_dir, use_symlink))

        if progress_fn is not None and line_idx % 500 == 0:
            progress_fn(line_idx, total_lines)

    total = len(tasks)
    if total == 0:
        return 0, placed_lower

    _custom_backup_dir = filemap_path.parent / "custom_deploy_backup"
    _custom_log_path   = filemap_path.parent / "custom_deploy_log.txt"

    # Clear any stale backup from a previous deploy before we start, so we
    # never mix old backed-up originals with new ones (same pattern as
    # deploy_filemap_to_root).
    if _custom_backup_dir.exists():
        shutil.rmtree(_custom_backup_dir)

    # Pre-create all destination directories up front (single-threaded) to
    # avoid mkdir races inside the thread pool.
    needed_dirs: set[str] = {os.path.dirname(dst) for _, dst, _, _is_custom, _ in tasks}
    for d in needed_dirs:
        os.makedirs(d, exist_ok=True)

    # Back up any pre-existing files at custom deploy locations so restore can
    # put the originals back.  Mirror each dst's absolute path as a relative
    # path inside _custom_backup_dir (strip leading slash) so structure is
    # preserved and files with the same name in different dirs never collide.
    _custom_backup_str = str(_custom_backup_dir)
    for _src_s, dst_s, _rel_lower, is_custom, _use_sym in tasks:
        if not is_custom:
            continue
        if os.path.islink(dst_s):
            os.unlink(dst_s)
        elif os.path.isfile(dst_s):
            dst_p = Path(dst_s)
            bak = _custom_backup_dir / dst_p.relative_to(dst_p.anchor)
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(dst_s, str(bak))
            _log(f"  Backed up existing {os.path.basename(dst_s)} → custom_deploy_backup/")

    linked = 0
    done_count = 0

    def _do_transfer(item: tuple[str, str, str, bool, bool]) -> tuple[str | None, tuple[str, OSError] | None]:
        src, dst, rel_lower, _is_custom, use_symlink = item
        try:
            effective_mode = LinkMode.SYMLINK if use_symlink else mode
            if effective_mode is LinkMode.HARDLINK:
                os.link(src, dst)
            elif effective_mode is LinkMode.SYMLINK:
                os.symlink(src, dst)
            else:
                shutil.copy2(src, dst)
            return rel_lower, None
        except OSError as e:
            return None, (dst, e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for result, err in pool.map(_do_transfer, tasks):
            done_count += 1
            if result is not None:
                placed_lower.add(result)
                linked += 1
            elif err is not None:
                dst_err, exc = err
                _log(f"  WARN: could not transfer {dst_err}: {exc}")
            if progress_fn is not None and (done_count % 200 == 0 or done_count == total):
                progress_fn(done_count, total)

    # Write a log of files placed in custom locations so cleanup knows what to
    # remove.  Each line is the absolute path of a deployed file.
    custom_deployed = [
        dst
        for _src, dst, rel_lower, is_custom, _use_sym in tasks
        if is_custom and rel_lower in placed_lower
    ]
    try:
        if custom_deployed:
            _custom_log_path.write_text("\n".join(custom_deployed), encoding="utf-8")
        elif _custom_log_path.exists():
            _custom_log_path.unlink()
    except OSError:
        pass

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

    # Use os.walk to collect files — avoids per-file stat() that rglob+is_file does.
    _core_str = str(core_dir)
    _core_prefix_len = len(_core_str) + 1  # +1 for the trailing separator

    tasks_core: list[tuple[str, str]] = []  # (src_str, rel_str)
    for dirpath, _dirnames, filenames in os.walk(_core_str):
        for fname in filenames:
            src_str = dirpath + "/" + fname
            rel_str = src_str[_core_prefix_len:]
            if rel_str.replace("\\", "/").lower() not in already_placed:
                tasks_core.append((src_str, rel_str))

    if not tasks_core:
        return 0

    total = len(tasks_core)

    # Resolve destination paths using case-insensitive directory matching so
    # that core files (e.g. Data_Core/Scripts/) merge into any same-name
    # directory already created by mods (e.g. Data/scripts/) rather than
    # producing a duplicate folder with different casing.
    dst_dir_cache: dict[Path, dict[str, str]] = {}
    resolved_tasks: list[tuple[str, str]] = []  # (src_str, dst_str)
    for src_str, rel_str in tasks_core:
        dst_path = _resolve_root_path(deploy_dir, Path(rel_str), dir_cache=dst_dir_cache)
        resolved_tasks.append((src_str, str(dst_path)))

    # Deduplicate destination directories with a set before creating them.
    needed_dirs: set[str] = set()
    for _, dst_str in resolved_tasks:
        needed_dirs.add(os.path.dirname(dst_str))
    for d in needed_dirs:
        os.makedirs(d, exist_ok=True)

    linked = 0
    done_count = 0

    def _do_core(item: tuple[str, str]) -> tuple[bool, str, OSError | None]:
        src, dst_str = item
        try:
            if mode is LinkMode.HARDLINK:
                os.link(src, dst_str)
            elif mode is LinkMode.SYMLINK:
                os.symlink(src, dst_str)
            else:
                shutil.copy2(src, dst_str)
            return True, dst_str, None
        except OSError as e:
            return False, dst_str, e

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for ok, rel_str, exc in pool.map(_do_core, resolved_tasks):
            done_count += 1
            if ok:
                linked += 1
            else:
                _log(f"  WARN: could not transfer {rel_str}: {exc}")
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


def _resolve_root_path(base: Path, rel: Path,
                       dir_cache: "dict[Path, dict[str, str]] | None" = None,
                       core_base: "Path | None" = None) -> Path:
    """Resolve *rel* under *base*, matching existing directory names
    case-insensitively so that mod folders (e.g. ``R6/``) merge into whatever
    casing the game already has on disk (e.g. ``r6/``).  Segments that don't
    yet exist use the casing from the filemap.

    Only directory segments are normalised; the final filename is kept as-is.
    dir_cache maps parent Path → {lower_name: actual_name} to avoid repeated
    iterdir() calls across many files with the same directory structure.

    core_base — optional sibling backup dir (e.g. Data_Core/) consulted when
    a segment isn't found in *base*.  This preserves vanilla folder casing
    (e.g. ``Scripts/``) even when *base* is empty at deploy time.
    """
    current = base
    core_current = core_base
    parts = rel.parts
    for part in parts[:-1]:   # directory segments only
        part_lower = part.lower()
        # Check if a directory with this name (any case) already exists.
        matched: str | None = None
        if current.is_dir():
            if dir_cache is not None:
                if current not in dir_cache:
                    try:
                        dir_cache[current] = {
                            e.name.lower(): e.name
                            for e in current.iterdir()
                            if e.is_dir()
                        }
                    except OSError:
                        dir_cache[current] = {}
                matched = dir_cache[current].get(part_lower)
            else:
                try:
                    for entry in current.iterdir():
                        if entry.is_dir() and entry.name.lower() == part_lower:
                            matched = entry.name
                            break
                except OSError:
                    pass
        # Fall back to core_base to preserve vanilla folder casing
        # (e.g. Data_Core/Scripts → use "Scripts" not "scripts").
        if matched is None and core_current is not None:
            if dir_cache is not None:
                if core_current not in dir_cache:
                    try:
                        dir_cache[core_current] = {
                            e.name.lower(): e.name
                            for e in core_current.iterdir()
                            if e.is_dir()
                        }
                    except OSError:
                        dir_cache[core_current] = {}
                matched = dir_cache[core_current].get(part_lower)
            else:
                try:
                    for entry in core_current.iterdir():
                        if entry.is_dir() and entry.name.lower() == part_lower:
                            matched = entry.name
                            break
                except OSError:
                    pass
        chosen = matched if matched is not None else part
        current = current / chosen
        core_current = (core_current / chosen) if core_current is not None else None
    return current / parts[-1]


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
        dst = _resolve_root_path(game_root, rel)
        # Record the top-level directory we're about to create (if new).
        # Use the resolved (possibly case-corrected) top-level name.
        top = dst.relative_to(game_root).parts[0] if len(rel.parts) > 1 else None
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
    staging_root: Path | None = None,
    strip_prefixes: set[str] | None = None,
    log_fn=None,
) -> int:
    """Undo a deploy: clear deploy_dir and move core_dir contents back.

    deploy_dir     — directory to restore (e.g. <game_path>/Data)
    core_dir       — vanilla backup to restore from; defaults to Data_Core/ sibling
    overwrite_dir  — if given, any file in deploy_dir that is not a deployed mod
                     file and not present in core_dir (i.e. created at runtime by
                     the game or a mod) is moved here before clearing, preserving
                     its relative path.  Existing files in overwrite_dir are
                     overwritten.  Pass Profiles/<game>/overwrite/.
    staging_root   — if given with strip_prefixes, files listed in filemap/modindex
                     whose staging source no longer exists (e.g. xEdit deleted the
                     original after saving an edited plugin) are rescued to
                     overwrite/ instead of being removed.  Pass the mod staging
                     root (e.g. Profiles/<game>/mods/).
    strip_prefixes — top-level folder names to try when resolving staging paths
                     (e.g. {"Data"} for Bethesda games).
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
    #
    # Exception: If staging_root and strip_prefixes are provided, files that ARE
    # in filemap/modindex are still rescued when their staging source is missing.
    # This handles the xEdit flow: user edits plugin → xEdit saves → xEdit closes
    # and deletes the original from both Data and staging → the edited copy in
    # Data is the only remaining version and must be rescued to overwrite.
    if overwrite_dir is not None and deploy_dir.is_dir():
        # Build core_lower using os.walk — avoids per-file stat() from rglob+is_file.
        _core_str = str(core_dir)
        _core_plen = len(_core_str) + 1
        core_lower: set[str] = set()
        for _dp, _dns, _fns in os.walk(_core_str):
            for _fn in _fns:
                core_lower.add((_dp + "/" + _fn)[_core_plen:].lower())
        filemap_path = overwrite_dir.parent / "filemap.txt"
        filemap_lower: set[str] = set()
        filemap_rel_to_mod: dict[str, str] = {}
        if filemap_path.is_file():
            with filemap_path.open(encoding="utf-8") as _fm:
                for _line in _fm:
                    _line = _line.rstrip("\n")
                    if "\t" in _line:
                        rel_str, mod_name = _line.split("\t", 1)
                        rel_lower = rel_str.lower()
                        filemap_lower.add(rel_lower)
                        filemap_rel_to_mod[rel_lower] = mod_name
        # Build a set of every file known to any mod in the index (all profiles,
        # all mods, enabled or disabled).  Runtime-created files won't appear here,
        # so any hit means "this is a mod file, don't rescue it".
        modindex_lower: set[str] = set()
        modindex_rel_to_mods: dict[str, list[str]] = {}
        try:
            from Utils.filemap import read_mod_index
            _index = read_mod_index(overwrite_dir.parent / "modindex.bin")
            if _index:
                for _mod_name, (_normal, _root) in _index.items():
                    if _mod_name == _OVERWRITE_NAME:
                        continue
                    for rel_key in _normal.keys():
                        modindex_lower.add(rel_key)
                        modindex_rel_to_mods.setdefault(rel_key, []).append(_mod_name)
        except Exception:
            pass
        _strip = {p.lower() for p in (strip_prefixes or set())}
        _staging = staging_root
        rescued = 0
        rescued_to_mod = 0
        rescued_to_overwrite = 0
        _deploy_str = str(deploy_dir)
        _deploy_plen = len(_deploy_str) + 1
        _overwrite_str = str(overwrite_dir)
        _staging_str = str(_staging) if _staging else ""
        _lstat = os.lstat
        import stat as _stat_mod
        for dirpath, _dirnames, filenames in os.walk(_deploy_str, followlinks=False):
            for fname in filenames:
                src_str = dirpath + "/" + fname
                try:
                    st = _lstat(src_str)
                except OSError:
                    continue
                if _stat_mod.S_ISLNK(st.st_mode):
                    continue  # deployed mod symlink
                if not _stat_mod.S_ISREG(st.st_mode):
                    continue
                if st.st_nlink > 1:
                    continue  # deployed mod hardlink
                rel_str = src_str[_deploy_plen:]
                rel_lower = rel_str.lower()
                if rel_lower in core_lower:
                    continue  # vanilla file — will be restored from core
                # Check if we would skip as a known mod file
                in_filemap = rel_lower in filemap_lower
                in_modindex = rel_lower in modindex_lower
                if in_filemap or in_modindex:
                    # xEdit orphan check: if staging source is missing, rescue the
                    # edited file (e.g. xEdit deleted original from staging on close)
                    if _staging and _strip:
                        mods_to_check: list[str] = []
                        if in_filemap:
                            m = filemap_rel_to_mod.get(rel_lower)
                            if m:
                                mods_to_check.append(m)
                        if in_modindex:
                            for m in modindex_rel_to_mods.get(rel_lower, []):
                                if m and m not in mods_to_check:
                                    mods_to_check.append(m)
                        staging_path: Path | None = None
                        target_mod: str | None = None
                        for mod_name in mods_to_check:
                            if mod_name == _OVERWRITE_NAME:
                                mod_root = overwrite_dir
                            else:
                                mod_root = _staging / mod_name
                            found = _get_staging_source_path(mod_root, rel_str, _strip)
                            if found is not None:
                                staging_path = found
                                target_mod = mod_name
                                break
                        if staging_path is not None and target_mod is not None:
                            staging_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(src_str, str(staging_path))
                            rescued += 1
                            rescued_to_mod += 1
                            continue
                        # xEdit orphan: staging missing — put file back in original mod or overwrite
                        target_mod = (
                            filemap_rel_to_mod.get(rel_lower)
                            or (modindex_rel_to_mods.get(rel_lower) or [None])[0]
                        )
                        if target_mod:
                            if target_mod == _OVERWRITE_NAME:
                                dst_str = _overwrite_str + "/" + rel_str
                                rescued_to_overwrite += 1
                            else:
                                dst_str = _staging_str + "/" + target_mod + "/" + rel_str
                                rescued_to_mod += 1
                            os.makedirs(os.path.dirname(dst_str), exist_ok=True)
                            shutil.move(src_str, dst_str)
                            rescued += 1
                            continue
                    else:
                        continue  # no staging check — skip as before
                # Genuine runtime-generated file (never in a mod) — goes to overwrite
                dst_str = _overwrite_str + "/" + rel_str
                os.makedirs(os.path.dirname(dst_str), exist_ok=True)
                shutil.move(src_str, dst_str)
                rescued += 1
                rescued_to_overwrite += 1
        if rescued:
            if rescued_to_mod:
                _log(f"  Rescued {rescued_to_mod} file(s) back to mod folder(s).")
            if rescued_to_overwrite:
                _log(f"  Rescued {rescued_to_overwrite} runtime-created file(s) → overwrite/.")
            # Update modindex.bin so the next build_filemap call immediately
            # sees the rescued files under [Overwrite] without a full rescan.
            if rescued_to_overwrite:
                try:
                    from Utils.filemap import update_mod_index, read_mod_index
                    index_path = overwrite_dir.parent / "modindex.bin"
                    existing = read_mod_index(index_path) or {}
                    existing_normal, existing_root = existing.get(_OVERWRITE_NAME, ({}, {}))
                    # Walk overwrite_dir to build the complete current file list.
                    new_normal: dict[str, str] = dict(existing_normal)
                    for f in overwrite_dir.rglob("*"):
                        if f.is_file() and not f.is_symlink():
                            rel = f.relative_to(overwrite_dir)
                            rel_str = rel.as_posix()
                            new_normal[rel_str.lower()] = rel_str
                    update_mod_index(index_path, _OVERWRITE_NAME, new_normal, existing_root)
                except Exception:
                    pass

    removed = _clear_dir(deploy_dir) if deploy_dir.is_dir() else 0
    _log(f"  Removed {removed} file(s) from {deploy_dir.name}/.")

    # Count files before moving so we can report the number restored.
    restored = sum(1 for _ in core_dir.rglob("*") if _.is_file())

    # After _clear_dir, deploy_dir is empty — remove it and rename core_dir
    # in its place.  This is O(1) on the same filesystem vs O(n) per-file moves.
    if deploy_dir.is_dir():
        shutil.rmtree(deploy_dir)
    shutil.move(str(core_dir), str(deploy_dir))

    return restored


# ---------------------------------------------------------------------------
# Undeploy — remove a mod's deployed files from the game directory
# ---------------------------------------------------------------------------

def undeploy_mod_files(
    mod_names: list[str],
    deploy_dir: "Path | None",
    game_root: "Path | None",
    index_path: Path,
    log_fn=None,
) -> int:
    """Remove any files belonging to the given mods from the game's deploy
    directory and/or game root, using the modindex.bin to find them.

    Call this *before* deleting the staging folders so that hardlinks/copies
    that are still sitting in the game directory are cleaned up.  Without this
    step, restore_data_core() would classify the leftover files as
    runtime-generated and move them to overwrite/ as a false positive.

    mod_names  — list of mod folder names to undeploy
    deploy_dir — the game's mod data directory (e.g. <game_path>/Data/).
                 May be None if the game has no separate data dir.
    game_root  — the game's install root (used for root-deployed files).
                 May be None if unknown / game not configured.
    index_path — path to modindex.bin (typically <profile_root>/modindex.bin)
    log_fn     — optional logging callable

    Returns the total number of files removed.
    """
    _log = log_fn or (lambda _: None)

    # Load the index; nothing to do if it is absent.
    try:
        from Utils.filemap import read_mod_index
        index = read_mod_index(index_path)
    except Exception:
        index = None
    if not index:
        return 0

    removed = 0
    dirs_to_prune: set[Path] = set()

    for mod_name in mod_names:
        entry = index.get(mod_name)
        if entry is None:
            continue
        normal_files, root_files = entry

        # Normal files — deployed files live inside deploy_dir.
        if deploy_dir is not None and normal_files:
            for rel_str in normal_files.values():
                target = deploy_dir / rel_str
                if not _path_under_root(target, deploy_dir):
                    _log(f"  SKIP (path traversal): {rel_str}")
                    continue
                if target.is_file() or target.is_symlink():
                    try:
                        target.unlink()
                        removed += 1
                        dirs_to_prune.add(target.parent)
                    except OSError as exc:
                        _log(f"  WARN: could not remove deployed file {rel_str}: {exc}")

        # Root files — deployed files live inside game_root.
        if game_root is not None and root_files:
            for rel_str in root_files.values():
                target = game_root / rel_str
                if not _path_under_root(target, game_root):
                    _log(f"  SKIP (path traversal): {rel_str}")
                    continue
                if target.is_file() or target.is_symlink():
                    try:
                        target.unlink()
                        removed += 1
                        dirs_to_prune.add(target.parent)
                    except OSError as exc:
                        _log(f"  WARN: could not remove root-deployed file {rel_str}: {exc}")

    # Prune empty directories left behind (deepest first).
    roots = set()
    if deploy_dir is not None:
        roots.add(deploy_dir)
    if game_root is not None:
        roots.add(game_root)
    for d in sorted(dirs_to_prune, key=lambda x: len(x.parts), reverse=True):
        if d in roots:
            continue
        try:
            d.rmdir()  # Only removes if empty
        except OSError:
            pass

    if removed:
        _log(f"  Undeployed {removed} file(s) for {len(mod_names)} mod(s).")
    return removed


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
    exclude: set[str] | None = None,
    path_remap: dict[str, str] | None = None,
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
    path_remap    — optional dict of prefix replacements applied to dest paths
                    e.g. {"natives/x64/": "natives/STM/"} for RE2/RE3

    Writes a log file next to filemap.txt so restore_filemap_from_root() knows
    exactly which files to remove.

    Returns (count, placed_lower) — same shape as deploy_filemap().
    placed_lower contains the *remapped* paths (as deployed on disk).
    """
    _log = log_fn or (lambda _: None)
    _strip = {p.lower() for p in strip_prefixes} if strip_prefixes else set()
    _per_mod = per_mod_strip_prefixes or {}
    _remap: list[tuple[str, str]] = []
    if path_remap:
        for old, new in path_remap.items():
            _remap.append((old.lower(), new))
    overwrite_dir = staging_root.parent / "overwrite"
    backup_dir    = filemap_path.parent / _FILEMAP_BACKUP_DIR
    log_path      = filemap_path.parent / _FILEMAP_LOG_NAME

    # Clear any stale backup from a previous deploy.
    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    already_seen: set[str] = set()
    placed_lower: set[str] = set()
    placed_log:   list[str] = []
    tasks: list[tuple[str, str, str, str]] = []  # (src_str, dst_str, rel_lower, rel_str)

    # Pre-compute string roots so the hot loop avoids Path construction.
    _overwrite_str = str(overwrite_dir)
    _staging_str   = str(staging_root)
    _game_root_str = str(game_root)
    _isfile        = os.path.isfile
    sorted_strip   = sorted(_strip) if _strip else []
    nocase_cache: dict[Path, dict[str, list[Path]]] = {}
    dst_dir_cache: dict[Path, dict[str, str]] = {}

    # Single-pass read: slurp all tab-containing lines at once.
    with filemap_path.open(encoding="utf-8") as f:
        _tab_lines = [ln.rstrip("\n") for ln in f if "\t" in ln]
    total_lines = len(_tab_lines)
    line_idx = 0

    for line in _tab_lines:
        rel_str, mod_name = line.split("\t", 1)
        rel_lower = rel_str.lower()
        if rel_lower in already_seen:
            continue
        already_seen.add(rel_lower)
        if exclude and rel_lower in exclude:
            continue
        line_idx += 1
        # Apply path prefix remapping to destination only (e.g. natives/x64/ → natives/STM/).
        # Source lookup always uses the original rel_str (files on disk use the original path).
        dst_rel = rel_str
        if _remap:
            rel_lower_check = rel_lower
            for old_prefix, new_prefix in _remap:
                if rel_lower_check.startswith(old_prefix):
                    dst_rel = new_prefix + rel_str[len(old_prefix):]
                    break

        # Fast path: direct string join + stat via os.path.isfile
        if mod_name == _OVERWRITE_NAME:
            candidate_str = _overwrite_str + "/" + rel_str
        else:
            candidate_str = _staging_str + "/" + mod_name + "/" + rel_str
        if _isfile(candidate_str):
            src_str = candidate_str
        else:
            mod_root = overwrite_dir if mod_name == _OVERWRITE_NAME else staging_root / mod_name
            src = _resolve_nocase(mod_root, rel_str, cache=nocase_cache)
            if src is None and sorted_strip:
                for p1 in sorted_strip:
                    src = _resolve_nocase(mod_root, p1 + "/" + rel_str, cache=nocase_cache)
                    if src is not None:
                        break
                    for p2 in sorted_strip:
                        src = _resolve_nocase(mod_root, p1 + "/" + p2 + "/" + rel_str, cache=nocase_cache)
                        if src is not None:
                            break
                    if src is not None:
                        break
            if src is None and mod_name != _OVERWRITE_NAME:
                mod_strip = _per_mod.get(mod_name)
                if mod_strip:
                    path_prefixes = [p for p in mod_strip if "/" in p]
                    for p in path_prefixes:
                        src = _resolve_nocase(mod_root, p + "/" + rel_str, cache=nocase_cache)
                        if src is not None:
                            break
                    if src is None:
                        segment_list = [p for p in mod_strip if "/" not in p]
                        prefix_path = ""
                        for seg in segment_list:
                            prefix_path = prefix_path + seg + "/" if prefix_path else seg + "/"
                            src = _resolve_nocase(mod_root, prefix_path + rel_str, cache=nocase_cache)
                            if src is not None:
                                break
            if src is None:
                _log(f"  WARN: source not found — {rel_str} ({mod_name})")
                continue
            src_str = str(src)

        dst_path = _resolve_root_path(game_root, Path(dst_rel), dir_cache=dst_dir_cache)
        dst_str = str(dst_path)
        tasks.append((src_str, dst_str, dst_rel.lower(), str(dst_path.relative_to(game_root))))

        if progress_fn is not None and line_idx % 500 == 0:
            progress_fn(line_idx, total_lines)

    total = len(tasks)
    if total == 0:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        return 0, placed_lower

    # Pre-create all destination directories up front (single-threaded).
    needed_dirs: set[str] = {os.path.dirname(dst) for _, dst, _, _ in tasks}
    for d in needed_dirs:
        os.makedirs(d, exist_ok=True)

    # Back up any vanilla files we are about to overwrite (must be serial).
    _backup_str = str(backup_dir)
    for _src_s, dst_s, _rel_lower, rel_str in tasks:
        if os.path.islink(dst_s):
            os.unlink(dst_s)
        elif os.path.isfile(dst_s):
            bak_str = _backup_str + "/" + rel_str
            os.makedirs(os.path.dirname(bak_str), exist_ok=True)
            shutil.move(dst_s, bak_str)

    linked = 0
    done_count = 0

    def _do_transfer(item: tuple[str, str, str, str]) -> tuple[str, str, OSError | None]:
        src, dst, rel_lower, rel_str = item
        try:
            if mode is LinkMode.HARDLINK:
                os.link(src, dst)
            elif mode is LinkMode.SYMLINK:
                os.symlink(src, dst)
            else:
                shutil.copy2(src, dst)
            return rel_lower, rel_str, None
        except OSError as e:
            return rel_lower, rel_str, e

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for rel_lower, rel_str, exc in pool.map(_do_transfer, tasks):
            done_count += 1
            if exc is None:
                linked += 1
                placed_lower.add(rel_lower)
                placed_log.append(rel_str.replace("\\", "/"))
            else:
                _log(f"  WARN: could not transfer {rel_str}: {exc}")
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
# Custom routing rules — route files by extension to arbitrary game-root dirs
# ---------------------------------------------------------------------------

def deploy_custom_rules(
    filemap_path: Path,
    game_root: Path,
    staging_root: Path,
    rules: list[CustomRule],
    mode: LinkMode = LinkMode.HARDLINK,
    strip_prefixes: set[str] | None = None,
    log_fn=None,
    progress_fn=None,
) -> set[str]:
    """Deploy filemap entries that match a CustomRule to their designated dirs.

    Matching logic (first matching rule wins):
    - folder match: file's first path segment is in rule.folders (and extension
      matches rule.extensions if non-empty) → placed at game_root/dest/rel_path
      (full relative path preserved under dest)
    - extension-only match: extension in rule.extensions (and rule.folders empty)
      → placed flat as game_root/dest/<filename>

    Returns the set of lowercased rel_paths that were handled so the caller
    can exclude them from the normal deploy step.

    A log of placed absolute paths is written to
    filemap_path.parent / "custom_rules_deployed.txt" for use by
    restore_custom_rules().
    """
    if not rules:
        return set()

    _log = log_fn or (lambda _: None)
    _strip = {p.lower() for p in strip_prefixes} if strip_prefixes else set()
    overwrite_dir = staging_root.parent / "overwrite"
    _overwrite_str = str(overwrite_dir)
    _staging_str   = str(staging_root)
    _isfile        = os.path.isfile
    nocase_cache: dict[Path, dict[str, list[Path]]] = {}
    sorted_strip   = sorted(_strip) if _strip else []

    # Pre-process rules into normalised form for fast matching
    _rules: list[tuple[CustomRule, set[str], set[str]]] = []
    for rule in rules:
        _rules.append((
            rule,
            {f.lower() for f in rule.folders},
            {e.lower() for e in rule.extensions},
        ))

    def _match_rule(rel_lower: str) -> tuple[CustomRule, bool] | None:
        """Return (rule, folder_match) for the first matching rule, or None."""
        first_seg = rel_lower.split("/")[0]
        ext = os.path.splitext(rel_lower)[1]
        for rule, folders, exts in _rules:
            folder_match = bool(folders and first_seg in folders)
            ext_match    = bool(exts and ext in exts)
            if folder_match and (not exts or ext_match):
                return rule, True
            if ext_match and not folders:
                return rule, False
        return None

    already_seen: set[str] = set()
    tasks: list[tuple[Path, Path]] = []   # (src, dst)
    handled_lower: set[str] = set()

    with filemap_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if "\t" not in line:
                continue
            rel_str, mod_name = line.split("\t", 1)
            rel_lower = rel_str.lower()
            if rel_lower in already_seen:
                continue
            already_seen.add(rel_lower)

            match = _match_rule(rel_lower)
            if match is None:
                continue
            rule, is_folder_match = match

            # Resolve source file
            if mod_name == _OVERWRITE_NAME:
                candidate_str = _overwrite_str + "/" + rel_str
            else:
                candidate_str = _staging_str + "/" + mod_name + "/" + rel_str
            if _isfile(candidate_str):
                src = Path(candidate_str)
            else:
                mod_root = overwrite_dir if mod_name == _OVERWRITE_NAME else staging_root / mod_name
                src = _resolve_nocase(mod_root, rel_str, cache=nocase_cache)
                if src is None and sorted_strip:
                    for p1 in sorted_strip:
                        src = _resolve_nocase(mod_root, p1 + "/" + rel_str, cache=nocase_cache)
                        if src is not None:
                            break

            if src is None:
                _log(f"  WARN: source not found — {rel_str} ({mod_name})")
                continue

            dest_base = game_root / rule.dest if rule.dest else game_root
            if is_folder_match:
                # Preserve full relative path under dest
                dst = dest_base / rel_str
            else:
                # Extension-only: place flat (filename only)
                dst = dest_base / src.name
            tasks.append((src, dst))
            handled_lower.add(rel_lower)

    if not tasks:
        return handled_lower

    # Backup directory for vanilla files that will be overwritten
    backup_dir = filemap_path.parent / _CUSTOM_RULES_BACKUP_DIR
    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    # Create destination directories
    needed_dirs: set[Path] = {dst.parent for _, dst in tasks}
    for d in needed_dirs:
        d.mkdir(parents=True, exist_ok=True)

    placed_abs: list[str] = []
    total = len(tasks)
    done_count = 0
    _game_root = game_root

    for src, dst in tasks:
        done_count += 1
        # Back up any vanilla file we are about to overwrite
        if dst.exists() and not dst.is_symlink():
            try:
                rel = dst.relative_to(_game_root)
                bak = backup_dir / rel
                bak.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(bak))
            except (ValueError, OSError) as e:
                _log(f"  WARN: could not back up {dst}: {e}")
        elif dst.is_symlink():
            dst.unlink()

        try:
            if mode is LinkMode.HARDLINK:
                os.link(src, dst)
            elif mode is LinkMode.SYMLINK:
                os.symlink(src, dst)
            else:
                shutil.copy2(src, dst)
            placed_abs.append(str(dst))
        except OSError as e:
            _log(f"  WARN: could not transfer {dst}: {e}")

        if progress_fn is not None and (done_count % 200 == 0 or done_count == total):
            progress_fn(done_count, total)

    log_path = filemap_path.parent / _CUSTOM_RULES_LOG_NAME
    try:
        if placed_abs:
            log_path.write_text("\n".join(placed_abs), encoding="utf-8")
        elif log_path.exists():
            log_path.unlink()
    except OSError:
        pass

    _log(f"  Custom rules: placed {len(placed_abs)} file(s).")
    return handled_lower


def restore_custom_rules(
    filemap_path: Path,
    game_root: Path,
    rules: list[CustomRule],
    log_fn=None,
) -> int:
    """Remove files placed by deploy_custom_rules() and prune empty dest dirs.

    Reads filemap_path.parent / "custom_rules_deployed.txt", deletes every
    listed absolute path, then tries to rmdir each rule's destination directory
    (silently ignored if non-empty).  Returns the number of files removed.
    """
    _log = log_fn or (lambda _: None)
    log_path = filemap_path.parent / _CUSTOM_RULES_LOG_NAME
    backup_dir = filemap_path.parent / _CUSTOM_RULES_BACKUP_DIR

    if not log_path.is_file():
        return 0

    placed = [p for p in log_path.read_text(encoding="utf-8").splitlines() if p]
    removed = 0
    dirs_to_prune: set[Path] = set()
    _game_root_resolved = game_root.resolve()
    for abs_str in placed:
        p = Path(abs_str)
        # Use the unresolved path for the under-root check so symlinks
        # (whose targets live outside game_root) are not incorrectly blocked.
        try:
            p.relative_to(game_root)
        except ValueError:
            try:
                p.resolve().relative_to(_game_root_resolved)
            except ValueError:
                _log(f"  SKIP: path traversal blocked — {abs_str}")
                continue
        if p.is_file() or p.is_symlink():
            p.unlink()
            removed += 1
        # Collect parent dirs for pruning (stop at game_root, unresolved check)
        parent = p.parent
        while parent != game_root:
            try:
                parent.relative_to(game_root)
            except ValueError:
                break
            dirs_to_prune.add(parent)
            parent = parent.parent

    # Restore backed-up vanilla files
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
            _log(f"  Restored {rel} from custom_rules_backup/")
        shutil.rmtree(backup_dir, ignore_errors=True)

    # Prune empty subdirectories deepest-first; never touch game_root itself
    for d in sorted(dirs_to_prune, key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass

    log_path.unlink()
    _log(f"  Custom rules restore: removed {removed} file(s).")
    return removed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _path_under_root(path: Path, root: Path) -> bool:
    """Return True if path is under root (no path traversal).

    Checks the unresolved path first so that symlinks whose targets live
    outside root (e.g. symlinks into staging) are not incorrectly blocked.
    """
    try:
        path.relative_to(root)
        return True
    except ValueError:
        pass
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _get_staging_source_path(mod_root: Path, rel_str: str, strip_prefixes: set[str]) -> Path | None:
    """Return the path of the given file in the mod staging folder, or None if absent.

    Tries rel_str directly, then strip_prefix/rel_str for each prefix (e.g.
    mods/ModName/Data/Plugin.esp when rel_str is Plugin.esp and strip has "data").
    """
    if not mod_root.is_dir():
        return None
    rel_lower = rel_str.lower()
    idx = _build_mod_index(mod_root)
    if rel_lower in idx:
        return idx[rel_lower]
    for prefix in sorted(strip_prefixes):
        candidate = (prefix + "/" + rel_str).lower()
        if candidate in idx:
            return idx[candidate]
        for prefix2 in strip_prefixes:
            if prefix2 == prefix:
                continue
            candidate2 = (prefix + "/" + prefix2 + "/" + rel_str).lower()
            if candidate2 in idx:
                return idx[candidate2]
    return None


def _staging_source_exists(mod_root: Path, rel_str: str, strip_prefixes: set[str]) -> bool:
    """Return True if the given file exists in the mod staging folder."""
    return _get_staging_source_path(mod_root, rel_str, strip_prefixes) is not None


def _build_mod_index(mod_root: Path) -> dict[str, Path]:
    """Build a case-insensitive index of all files under mod_root.

    Returns dict mapping lowercase rel_path -> Path for O(1) lookup.
    Uses os.walk to avoid stat() per file (walk separates dirs from files).
    """
    out: dict[str, Path] = {}
    try:
        for dirpath, _dirnames, filenames in os.walk(mod_root):
            for name in filenames:
                full = Path(dirpath) / name
                rel = full.relative_to(mod_root)
                out[str(rel).replace("\\", "/").lower()] = full
    except OSError:
        pass
    return out


def _resolve_nocase(root: Path, rel_str: str,
                    cache: dict[Path, dict[str, list[Path]]] | None = None) -> Path | None:
    """Resolve a relative path case-insensitively under root.

    Each path segment is matched case-insensitively against the real
    filesystem entries so that a canonical rel_str (e.g. "Scripts/foo.pex")
    will find the actual file even if the mod folder uses "scripts/foo.pex".

    When a directory contains multiple entries whose names differ only in case
    (e.g. both "Textures/" and "textures/"), *all* are explored so the correct
    file is found regardless of which casing the filemap recorded.

    An optional *cache* dict maps directory Paths to {lowercase_name: [entries]}
    dicts so that repeated lookups in the same directory avoid re-scanning.

    Returns the resolved Path if it exists, or None.
    """
    if cache is None:
        cache = {}
    parts = rel_str.replace("\\", "/").split("/")
    # Stack entries: (current_dir, parts_index)
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, idx = stack.pop()
        if idx == len(parts):
            if current.is_file():
                return current
            continue
        part_lower = parts[idx].lower()
        listing = cache.get(current)
        if listing is None:
            listing = {}
            try:
                for e in current.iterdir():
                    key = e.name.lower()
                    if key not in listing:
                        listing[key] = []
                    listing[key].append(e)
            except OSError:
                pass
            cache[current] = listing
        candidates = listing.get(part_lower)
        if not candidates:
            continue
        for candidate in candidates:
            # Skip directory symlinks to prevent traversal outside root
            if idx + 1 < len(parts) and candidate.is_symlink():
                continue
            stack.append((candidate, idx + 1))
    return None


# ---------------------------------------------------------------------------
# Emergency cleanup — remove hardlinked / symlinked mod files
# ---------------------------------------------------------------------------

def remove_deployed_files(game_dir: Path, log_fn=None) -> int:
    """Remove all hardlinked and symlinked files from *game_dir* recursively,
    then rename any ``*_Core`` vanilla-backup directories back to their
    original names and prune empty directories.

    This is an emergency recovery tool for situations where the mod manager's
    own restore cannot run (e.g. the profile was deleted or the modlist is
    missing).  It works by detecting files that were placed by the deploy step:

    * **Symlinks** — trivially identifiable; always removed.
    * **Hardlinks** — identified by ``st_nlink > 1``: the file has more than
      one directory entry pointing at it, meaning the mod staging copy and the
      game-folder copy share the same inode.  A vanilla file that was never
      hardlinked will have ``st_nlink == 1``.

    After removing deployed files, any sibling ``*_Core`` directory (e.g.
    ``Data_Core/``) is renamed back to its original name (``Data/``), merging
    into any remaining content.  The same rename pass is also applied to
    ``*_Core`` subdirectories found directly inside *game_dir* (covers UE5
    games where the game root itself is scanned).

    Empty sub-directories left behind after removal are pruned.

    Returns the number of files removed.
    """
    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    removed = 0
    if not game_dir.is_dir():
        _log(f"Directory not found: {game_dir}")
        return 0

    # --- Step 1: remove deployed files ---
    for root, dirs, files in os.walk(game_dir, topdown=False, followlinks=False):
        root_path = Path(root)
        for fname in files:
            fpath = root_path / fname
            try:
                if fpath.is_symlink():
                    fpath.unlink()
                    _log(f"Removed symlink: {fpath}")
                    removed += 1
                elif fpath.stat().st_nlink > 1:
                    fpath.unlink()
                    _log(f"Removed hardlink: {fpath}")
                    removed += 1
            except OSError as exc:
                _log(f"Could not remove {fpath}: {exc}")
        # Prune empty directories (skip the root itself)
        if root_path != game_dir:
            try:
                root_path.rmdir()   # only succeeds if empty
            except OSError:
                pass

    _log(f"Removed {removed} deployed file(s) from {game_dir}")

    # --- Step 2: rename *_Core backup dirs back to their original names ---
    # Collect candidate directories to check:
    #   • Siblings of game_dir whose name ends with "_Core" and whose stripped
    #     name matches game_dir's name (e.g. Data_Core → Data).
    #   • Direct children of game_dir ending with "_Core" (UE5 / game-root scan).
    def _rename_core_dirs(search_parent: Path) -> None:
        try:
            entries = list(search_parent.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            name = entry.name
            if not name.endswith("_Core"):
                continue
            original_name = name[: -len("_Core")]
            target = entry.parent / original_name
            if target.exists():
                # Merge: move contents of core dir into target, then remove core dir
                for src in list(entry.rglob("*")):
                    if not src.is_file():
                        continue
                    rel = src.relative_to(entry)
                    dst = target / rel
                    if not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(dst))
                        _log(f"Restored vanilla file: {dst}")
                shutil.rmtree(entry, ignore_errors=True)
                _log(f"Merged {entry.name}/ into {original_name}/")
            else:
                try:
                    entry.rename(target)
                    _log(f"Renamed {entry.name}/ → {original_name}/")
                except OSError as exc:
                    _log(f"Could not rename {entry}: {exc}")

    # Siblings (Bethesda / BepInEx style: Data_Core/ next to Data/)
    _rename_core_dirs(game_dir.parent)
    # Children (UE5 style: game root contains subdirs that may have _Core siblings)
    _rename_core_dirs(game_dir)

    # --- Step 3: prune empty directories left inside game_dir ---
    for root, dirs, files in os.walk(game_dir, topdown=False, followlinks=False):
        root_path = Path(root)
        if root_path != game_dir and not files and not dirs:
            try:
                root_path.rmdir()
            except OSError:
                pass

    return removed


# ---------------------------------------------------------------------------
# Wine / Proton prefix helpers
# ---------------------------------------------------------------------------

def apply_wine_dll_overrides(
    prefix_path: Path,
    overrides: dict[str, str],
    log_fn=None,
) -> None:
    """Write DLL override entries into the Proton prefix's user.reg.

    *prefix_path* is the ``pfx/`` directory (the one that contains
    ``drive_c/`` and ``user.reg``).

    *overrides* maps DLL name → load order string, e.g.
    ``{"winhttp": "native,builtin"}``.

    The function locates (or creates) the
    ``[Software\\\\Wine\\\\DllOverrides]`` section in ``user.reg`` and
    inserts/updates each key.  The file is written atomically so a crash
    mid-write cannot corrupt the prefix.

    If *prefix_path* does not exist or ``user.reg`` cannot be read the
    call is a silent no-op (logged as a warning).
    """
    _log = log_fn or (lambda _: None)

    if not overrides:
        return

    # Accept either the pfx/ directory directly or its parent (compatdata/<id>/)
    if not (prefix_path / "user.reg").is_file() and (prefix_path / "pfx" / "user.reg").is_file():
        prefix_path = prefix_path / "pfx"
    user_reg = prefix_path / "user.reg"
    if not user_reg.is_file():
        _log(f"Warning: user.reg not found at {user_reg}; skipping DLL overrides.")
        return

    try:
        text = user_reg.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log(f"Warning: could not read user.reg: {exc}")
        return

    lines = text.splitlines(keepends=True)
    section_header = "[Software\\\\Wine\\\\DllOverrides]"

    # Locate the section (case-insensitive header match)
    section_start: int | None = None
    section_end: int | None = None  # index of first line after this section
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower().startswith(section_header.lower()):
            section_start = i
        elif section_start is not None and stripped.startswith("["):
            section_end = i
            break

    # Windows FILETIME: 100ns ticks since 1601-01-01; epoch offset = 11644473600s
    _filetime_hex = format(int((_time.time() + 11644473600) * 1e7), "x")

    if section_start is None:
        # Section doesn't exist — append it at the end
        _log(f"[Software\\\\Wine\\\\DllOverrides] not found; appending to user.reg.")
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n")
        lines.append(f"{section_header} {_filetime_hex}\n")
        lines.append(f"#time={_filetime_hex}\n")
        for dll, value in overrides.items():
            lines.append(f'"{dll}"="{value}"\n')
            _log(f"  DLL override set: {dll} = {value}")
    else:
        # Section exists — find existing keys and add/update
        body_start = section_start + 1
        body_end = section_end if section_end is not None else len(lines)
        key_lines = lines[body_start:body_end]

        # Update the section header line's timestamp and the #time= line
        lines[section_start] = f"{section_header} {_filetime_hex}\n"
        for j, kline in enumerate(key_lines):
            if kline.lower().startswith("#time="):
                key_lines[j] = f"#time={_filetime_hex}\n"
                break

        for dll, value in overrides.items():
            key_lower = f'"{dll.lower()}"'
            found = False
            for j, kline in enumerate(key_lines):
                if kline.lower().startswith(key_lower + "="):
                    key_lines[j] = f'"{dll}"="{value}"\n'
                    found = True
                    _log(f"  DLL override updated: {dll} = {value}")
                    break
            if not found:
                key_lines.append(f'"{dll}"="{value}"\n')
                _log(f"  DLL override set: {dll} = {value}")

        lines[body_start:body_end] = key_lines

    # Atomic write via temp file → rename
    tmp = user_reg.with_suffix(".reg.tmp")
    try:
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.replace(user_reg)
    except OSError as exc:
        _log(f"Warning: could not write user.reg: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass