"""
deploy_shared.py
Shared primitives used by the deploy_* mode modules.

Extracted from deploy.py during the 2026-04 refactor. No behaviour changes —
the original deploy.py re-exports everything here via `from ... import *`.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import time as _time
from contextlib import contextmanager as _contextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.path_utils import has_path_traversal as _has_traversal


def _mkdir_leaves(dirs: "set[str]") -> None:
    """Create all directories in *dirs*, skipping any that are a prefix of
    another (mkdir -p on a deep leaf also creates every ancestor).

    Reduces os.makedirs() calls by dropping redundant parents before the
    stat-heavy exist_ok=True check runs on each one.
    """
    if not dirs:
        return
    # A dir is redundant if any of its ancestor-or-self chain members
    # (besides itself) is also in *dirs* as a deeper path — equivalently,
    # any ancestor of a deeper dir is redundant. Build the set of all
    # strict ancestors of every dir, then keep only dirs not in that set.
    redundant: set[str] = set()
    for d in dirs:
        # Walk up: every parent of d is an ancestor and thus not a leaf
        # (if that parent also appears in dirs).
        p = d.rsplit("/", 1)[0]
        while p and p != d:
            if p in dirs:
                redundant.add(p)
            nxt = p.rsplit("/", 1)[0]
            if nxt == p:
                break
            p = nxt
    for d in dirs:
        if d not in redundant:
            os.makedirs(d, exist_ok=True)


def _deploy_workers() -> int:
    """Thread-pool size for parallel file transfers. Override with
    MOD_MANAGER_DEPLOY_WORKERS for quick benchmarking."""
    try:
        n = int(os.environ.get("MOD_MANAGER_DEPLOY_WORKERS", "16"))
        return max(1, n)
    except ValueError:
        return 16


@_contextmanager
def _timer(label: str):
    """Print elapsed wall-clock time for a labelled block to stderr."""
    t0 = _time.perf_counter()
    yield
    dt = _time.perf_counter() - t0
    print(f"  [TIMER] {label}: {dt:.3f}s")


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
    _log = _safe_log(log_fn)

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
        if _has_traversal(abs_str):
            _log(f"  WARN: skipping suspicious path in custom_deploy_log: {abs_str!r}")
            continue
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
    _log = _safe_log(log_fn)

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
    loose_only — when True, the rule only matches files that are not inside
                 any folder (i.e. files at the mod root with no directory
                 components in their relative path).  Default False.
    companion_extensions — lowercase file extensions (e.g. [".ini"]) whose
                 owners ride along with a primary match.  When this rule
                 matches a file, any sibling in the same folder with the same
                 basename stem and one of these extensions is also routed to
                 the same destination.  Used for formats like RDR2's ASI
                 plugins where "Foo.asi" and "Foo.ini" must both live at the
                 game root even though the ``.ini`` extension is too generic
                 to route unconditionally.

    Placement behaviour:
    - extension-only match: file placed as game_root/dest/<filename> (flat)
    - folder match (with or without extension): file placed as
      game_root/dest/<original rel_path> (full path preserved)
    - filename match: file placed flat as game_root/dest/<filename>
    - companion match: file placed using the same rule as its primary owner
    """
    dest: str
    extensions: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)
    filenames: list[str] = field(default_factory=list)
    loose_only: bool = False
    companion_extensions: list[str] = field(default_factory=list)


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


_OVERWRITE_NAME = "[Overwrite]"


def _resolve_source(
    mod_name: str,
    rel_str: str,
    rel_lower: str,
    overwrite_dir: Path,
    staging_root: Path,
    overwrite_str: str,
    staging_str: str,
    sorted_strip: list[str],
    per_mod_strip: dict[str, list[str]],
    nocase_cache: dict,
    mod_index_cache: "dict[Path, dict[str, Path]] | None" = None,
) -> str | None:
    """Resolve the on-disk source path for a filemap entry.

    Returns the source path as a string, or None if not found.
    Tries (in order): direct stat, mod-index O(1) lookup, case-insensitive
    walk, strip-prefix combinations, per-mod strip prefixes.
    """
    _isfile = os.path.isfile

    # Fast path: direct string join + stat
    if mod_name == _OVERWRITE_NAME:
        candidate = overwrite_str + "/" + rel_str
    else:
        candidate = staging_str + "/" + mod_name + "/" + rel_str
    if _isfile(candidate):
        return candidate

    # Slow path
    mod_root = overwrite_dir if mod_name == _OVERWRITE_NAME else staging_root / mod_name
    src: Path | None = None

    if mod_index_cache is not None:
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
        mod_strip = per_mod_strip.get(mod_name)
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

    return str(src) if src is not None else None


def _do_link(src: str, dst: str, mode: LinkMode) -> OSError | None:
    """Transfer a single file. Returns None on success, or the OSError."""
    try:
        if mode is LinkMode.HARDLINK:
            os.link(src, dst)
        elif mode is LinkMode.SYMLINK:
            os.symlink(src, dst)
        else:
            shutil.copy2(src, dst)
        return None
    except OSError as e:
        return e


def _restore_from_log(
    log_path: Path,
    target_root: Path,
    backup_dir: "Path | None",
    log_fn,
    *,
    prune_dirs: bool = True,
) -> int:
    """Shared restore logic: read log, delete placed files, restore backups.

    log_path    — file listing relative paths (one per line) that were deployed
    target_root — directory the files were deployed into
    backup_dir  — directory holding backed-up originals (or None)
    prune_dirs  — if True, remove empty directories left behind

    Returns the number of files removed from target_root.
    """
    _log = _safe_log(log_fn)

    if not log_path.is_file():
        return 0

    placed = [p for p in log_path.read_text(encoding="utf-8").splitlines() if p]
    removed = 0

    for rel_str in placed:
        dst = target_root / rel_str
        if not _path_under_root(dst, target_root):
            _log(f"  SKIP: path traversal blocked — {rel_str}")
            continue
        if dst.is_file() or dst.is_symlink():
            dst.unlink()
            removed += 1

    # Restore backed-up originals.
    if backup_dir is not None and backup_dir.is_dir():
        for bak_src in backup_dir.rglob("*"):
            if not bak_src.is_file():
                continue
            rel = bak_src.relative_to(backup_dir)
            orig = target_root / rel
            if not _path_under_root(orig, target_root):
                _log(f"  SKIP: path traversal blocked — {rel}")
                continue
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(bak_src), str(orig))
            _log(f"  Restored {rel} from {backup_dir.name}/")
        shutil.rmtree(backup_dir, ignore_errors=True)

    log_path.unlink()

    # Prune empty directories left behind.
    if prune_dirs:
        dirs_to_check: set[Path] = set()
        for rel_str in placed:
            p = (target_root / rel_str).parent
            while p != target_root and p != target_root.parent:
                dirs_to_check.add(p)
                p = p.parent
        for d in sorted(dirs_to_check, key=lambda x: len(x.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass

    return removed


def _prebuild_mod_indexes(
    tab_lines: list[str],
    overwrite_dir: Path,
    staging_root: Path,
    mod_index_cache: dict,
    *,
    profile_dir: "Path | None" = None,
    strip_prefixes: "set[str] | None" = None,
    per_mod_strip_prefixes: "dict[str, list[str]] | None" = None,
) -> None:
    """Pre-build per-mod file indexes for all mods referenced in the filemap.

    Fast path: load on-disk paths from Profiles/<game>/modindex.bin (already
    built by filemap.py) for mods whose files aren't behind a strip prefix —
    no filesystem walk needed.

    Slow path: os.walk each mod folder (for mods with strip prefixes, or
    when the index is missing/stale).
    """
    mod_names: set[str] = set()
    for ln in tab_lines:
        tab_pos = ln.find("\t")
        if tab_pos > 0:
            mod_names.add(ln[tab_pos + 1:])

    # Try to reuse the on-disk mod index written by filemap.py. It stores
    # stripped rel_str values — fine for mods with no strip prefixes, since
    # the on-disk path is just mod_root/rel_str. For mods that do have strip
    # prefixes, fall through to the os.walk path so _resolve_source can find
    # files under their actual folders.
    index_from_disk: dict | None = None
    if profile_dir is not None:
        try:
            from Utils.filemap import read_mod_index
            index_from_disk = read_mod_index(profile_dir / "modindex.bin")
        except Exception:
            index_from_disk = None

    _global_strip = bool(strip_prefixes)
    _per_mod = per_mod_strip_prefixes or {}

    for mn in mod_names:
        if _has_traversal(mn):
            continue
        mr = overwrite_dir if mn == _OVERWRITE_NAME else staging_root / mn
        if mr in mod_index_cache:
            continue

        has_strip = _global_strip or bool(_per_mod.get(mn))
        entry = index_from_disk.get(mn) if index_from_disk is not None else None

        if entry is not None and not has_strip:
            # Fast path: synthesize on-disk paths directly from the index.
            normal, root = entry
            mr_str = str(mr)
            built: dict[str, str] = {}
            for rel_lower, rel_str in normal.items():
                built[rel_lower] = mr_str + "/" + rel_str
            for rel_lower, rel_str in root.items():
                built[rel_lower] = mr_str + "/" + rel_str
            mod_index_cache[mr] = built
        else:
            mod_index_cache[mr] = _build_mod_index(mr)


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
        if dir_cache is not None:
            if current not in dir_cache:
                try:
                    if current.is_dir():
                        dir_cache[current] = {
                            e.name.lower(): e.name
                            for e in current.iterdir()
                            if e.is_dir()
                        }
                    else:
                        dir_cache[current] = {}
                except OSError:
                    dir_cache[current] = {}
            matched = dir_cache[current].get(part_lower)
        else:
            if current.is_dir():
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
                        if core_current.is_dir():
                            dir_cache[core_current] = {
                                e.name.lower(): e.name
                                for e in core_current.iterdir()
                                if e.is_dir()
                            }
                        else:
                            dir_cache[core_current] = {}
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


def _resolve_root_path_str(base_str: str, rel_str: str,
                           dir_listing_cache: "dict[str, dict[str, str]]",
                           core_base_str: "str | None" = None,
                           resolved_dir_cache: "dict[str, str] | None" = None) -> str:
    """Fast string-based variant of _resolve_root_path for bulk deploy.

    Instead of creating Path objects per call, works entirely with strings
    and caches the fully-resolved directory path so files sharing the same
    parent directory skip all resolution after the first.

    dir_listing_cache — maps dir_path_str → {lower_name: actual_name}
    resolved_dir_cache — maps (base_str + "!" + dir_parts_lower) → resolved_dir_str
    """
    # Split rel_str into directory part and filename
    slash_pos = rel_str.rfind("/")
    if slash_pos < 0:
        # No directory component — file directly under base
        return base_str + "/" + rel_str

    dir_part = rel_str[:slash_pos]
    filename = rel_str[slash_pos + 1:]
    dir_lower = dir_part.lower()

    # Check resolved dir cache first — covers the common case where many
    # files share the same directory.
    if resolved_dir_cache is not None:
        cache_key = dir_lower
        cached = resolved_dir_cache.get(cache_key)
        if cached is not None:
            return cached + "/" + filename

    # Walk each directory segment, resolving case
    parts = dir_part.split("/")
    current = base_str
    core_current = core_base_str
    _isdir = os.path.isdir
    _scandir = os.scandir

    for part in parts:
        part_lower = part.lower()
        matched: str | None = None

        listing = dir_listing_cache.get(current)
        if listing is None:
            listing = {}
            if _isdir(current):
                try:
                    with _scandir(current) as it:
                        for e in it:
                            if e.is_dir(follow_symlinks=False):
                                listing[e.name.lower()] = e.name
                except OSError:
                    pass
            dir_listing_cache[current] = listing
        matched = listing.get(part_lower)

        if matched is None and core_current is not None:
            core_listing = dir_listing_cache.get(core_current)
            if core_listing is None:
                core_listing = {}
                if _isdir(core_current):
                    try:
                        with _scandir(core_current) as it:
                            for e in it:
                                if e.is_dir(follow_symlinks=False):
                                    core_listing[e.name.lower()] = e.name
                    except OSError:
                        pass
                dir_listing_cache[core_current] = core_listing
            matched = core_listing.get(part_lower)

        chosen = matched if matched is not None else part
        current = current + "/" + chosen
        core_current = (core_current + "/" + chosen) if core_current is not None else None

    if resolved_dir_cache is not None:
        resolved_dir_cache[dir_lower] = current

    return current + "/" + filename


# ---------------------------------------------------------------------------
# Snapshot helpers (shared by root and game-root modes)
# ---------------------------------------------------------------------------

# Snapshot of the game root written at deploy time; consumed by restore to
# identify runtime-generated files (files that appeared after deploy).
_FILEMAP_SNAPSHOT_NAME = "deploy_snapshot.txt"


def _write_deploy_snapshot(
    game_root: Path,
    snapshot_path: Path,
    log_fn=None,
) -> int:
    """Walk game_root and record every file as rel_path\\tmtime_ns\\tsize.

    Written atomically via a .tmp sibling then renamed.  Returns the number
    of files recorded, or 0 on error (the deploy is never aborted).
    """
    _log = _safe_log(log_fn)
    tmp_path = snapshot_path.with_suffix(".tmp")
    count = 0
    game_root_str = str(game_root)
    prefix_len = len(game_root_str) + 1          # +1 for trailing separator
    try:
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write("# deploy_snapshot v1\n")
            stack = [game_root_str]
            while stack:
                cur = stack.pop()
                try:
                    with os.scandir(cur) as it:
                        for entry in it:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                rel = entry.path[prefix_len:]
                                st = entry.stat(follow_symlinks=False)
                                fh.write(f"{rel}\t{st.st_mtime_ns}\t{st.st_size}\n")
                                count += 1
                except OSError:
                    pass
        tmp_path.rename(snapshot_path)
        _log(f"  Snapshot: recorded {count} files in game root.")
    except OSError as exc:
        _log(f"  WARN: could not write deploy snapshot: {exc}")
        return 0
    return count


def _load_deploy_snapshot(snapshot_path: Path) -> set[str]:
    """Return a set of lowercased relative paths from a deploy snapshot file.

    Returns an empty set if the file is missing or unreadable — callers treat
    this as "no snapshot available" and skip runtime-file detection.
    """
    if not snapshot_path.is_file():
        return set()
    try:
        known: set[str] = set()
        with snapshot_path.open(encoding="utf-8") as fh:
            for line in fh:
                if line[0] == "#":
                    continue
                tab = line.find("\t")
                known.add(line[:tab].lower() if tab != -1 else line.rstrip("\n").lower())
        return known
    except OSError:
        return set()


def _move_runtime_files(
    game_root: Path,
    snapshot_path: Path,
    overwrite_dir: Path,
    log_fn=None,
) -> int:
    """Move files that appeared after deploy (runtime-generated) to overwrite_dir.

    Compares the current game_root contents against the deploy snapshot.
    Files present now but absent from the snapshot are moved to overwrite_dir
    preserving their relative path so they become part of the [Overwrite] mod.
    Vanilla files (present in snapshot) are left untouched.
    Symlinks are skipped entirely.

    Returns the number of files moved.
    """
    _log = _safe_log(log_fn)
    known = _load_deploy_snapshot(snapshot_path)
    if not known:
        _log("  WARN: deploy snapshot empty or unreadable — skipping runtime file detection.")
        return 0

    game_root_str = str(game_root)
    prefix_len = len(game_root_str) + 1
    overwrite_str = str(overwrite_dir)
    made_dirs: set[str] = set()
    moved = 0
    stack = [game_root_str]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        rel = entry.path[prefix_len:]
                        if rel.lower() in known:
                            continue
                        dst = overwrite_str + "/" + rel
                        if os.path.exists(dst):
                            _log(f"  WARN: overwrite/{rel} already exists — skipping.")
                            continue
                        dst_dir = os.path.dirname(dst)
                        if dst_dir not in made_dirs:
                            os.makedirs(dst_dir, exist_ok=True)
                            made_dirs.add(dst_dir)
                        shutil.move(entry.path, dst)
                        moved += 1
        except OSError:
            pass
    return moved


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
    hit = idx.get(rel_lower)
    if hit is None:
        for prefix in sorted(strip_prefixes):
            candidate = (prefix + "/" + rel_str).lower()
            hit = idx.get(candidate)
            if hit is not None:
                break
            for prefix2 in strip_prefixes:
                if prefix2 == prefix:
                    continue
                candidate2 = (prefix + "/" + prefix2 + "/" + rel_str).lower()
                hit = idx.get(candidate2)
                if hit is not None:
                    break
            if hit is not None:
                break
    return Path(hit) if hit is not None else None


def _staging_source_exists(mod_root: Path, rel_str: str, strip_prefixes: set[str]) -> bool:
    """Return True if the given file exists in the mod staging folder."""
    return _get_staging_source_path(mod_root, rel_str, strip_prefixes) is not None


def _build_mod_index(mod_root: Path) -> "dict[str, str | Path]":
    """Build a case-insensitive index of all files under mod_root.

    Returns dict mapping lowercase rel_path -> full path (str) for O(1) lookup.
    Uses os.walk to avoid stat() per file (walk separates dirs from files).
    """
    out: dict[str, str | Path] = {}
    _root_str = str(mod_root)
    _root_plen = len(_root_str) + 1  # +1 for the trailing "/"
    try:
        for dirpath, _dirnames, filenames in os.walk(_root_str):
            for name in filenames:
                full_str = dirpath + "/" + name
                rel_lower = full_str[_root_plen:].lower()
                out[rel_lower] = full_str
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


__all__ = [
    # Public classes/enums
    "LinkMode",
    "CustomRule",
    # Public helpers
    "load_per_mod_strip_prefixes",
    "load_separator_deploy_paths",
    "expand_separator_deploy_paths",
    "expand_separator_raw_deploy",
    "cleanup_custom_deploy_dirs",
    "restore_custom_deploy_backup_for_path",
    # Private helpers (re-exported via façade for back-compat)
    "_mkdir_leaves",
    "_deploy_workers",
    "_timer",
    "_prune_empty_dirs",
    "_default_core",
    "_transfer",
    "_clear_dir",
    "_OVERWRITE_NAME",
    "_resolve_source",
    "_do_link",
    "_restore_from_log",
    "_prebuild_mod_indexes",
    "_resolve_root_path",
    "_resolve_root_path_str",
    "_FILEMAP_SNAPSHOT_NAME",
    "_write_deploy_snapshot",
    "_load_deploy_snapshot",
    "_move_runtime_files",
    "_path_under_root",
    "_get_staging_source_path",
    "_staging_source_exists",
    "_build_mod_index",
    "_resolve_nocase",
    # Re-exported stdlib/project imports used by other deploy_* modules
    "_safe_log",
    "_has_traversal",
    "_time",
    "_contextmanager",
]
