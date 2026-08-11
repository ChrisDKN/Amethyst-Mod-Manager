"""
deploy_game_root.py
Game-root filemap deployment (Cyberpunk, Witcher 3, RE games, Darktide).

Originally extracted from deploy.py during the 2026-04 refactor, with
behaviour preserved at the time of extraction.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.deploy_shared import (
    LinkMode,
    _FILEMAP_SNAPSHOT_NAME,
    _OVERWRITE_NAME,
    _do_link,
    _iter_map_batched,
    _log_case_collisions,
    _mkdir_leaves,
    _move_crash_safe,
    _move_runtime_files,
    _prebuild_mod_indexes,
    _resolve_root_path_str,
    _resolve_source,
    _restore_from_log,
    _write_deploy_snapshot,
)


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
    ext_remap: dict[str, str] | None = None,
    file_transform=None,
) -> tuple[int, set[str]]:
    """Deploy mod files directly into game_root, backing up any files they
    overwrite so restore_filemap_from_root() can undo the operation cleanly.

    Designed for games whose mods install into the game root itself (Cyberpunk,
    Witcher 3) rather than a dedicated subdirectory.  Unlike move_to_core() /
    restore_data_core(), this never touches vanilla files that mods don't
    overwrite - it only places mod files and backs up what they replace.

    filemap_path  - Profiles/<game>/filemap.txt
    game_root     - the game's install directory
    staging_root  - Profiles/<game>/mods/
    mode          - transfer method
    strip_prefixes - forwarded to source-file resolution (same as deploy_filemap)
    per_mod_strip_prefixes - optional dict mod name -> list of strip folders
    log_fn        - optional logging callable
    progress_fn   - optional callable(done: int, total: int)
    path_remap    - optional dict of prefix replacements applied to dest paths
                    e.g. {"natives/x64/": "natives/STM/"} for RE2/RE3
    ext_remap     - optional dict of file extension substitutions applied to
                    dest paths, e.g. {".tex.10": ".tex.34"} for RTX-updated RE
    file_transform - optional callable(src_path: str, dst_path: str) -> str | None
                     If it returns a new path, that path is used as the source
                     for the transfer instead (e.g. for TEX format conversion).

    Writes a log file next to filemap.txt so restore_filemap_from_root() knows
    exactly which files to remove.

    Returns (count, placed_lower) - same shape as deploy_filemap().
    placed_lower contains the *remapped* paths (as deployed on disk).
    """
    _log = _safe_log(log_fn)
    _strip = {p.lower() for p in strip_prefixes} if strip_prefixes else set()
    _per_mod = per_mod_strip_prefixes or {}
    _remap: list[tuple[str, str]] = []
    if path_remap:
        for old, new in path_remap.items():
            _remap.append((old.lower(), new))
    _ext_remap: list[tuple[str, str]] = []
    if ext_remap:
        for old_ext, new_ext in ext_remap.items():
            _ext_remap.append((old_ext.lower(), new_ext))
    overwrite_dir = staging_root.parent / "overwrite"
    backup_dir    = filemap_path.parent / _FILEMAP_BACKUP_DIR
    log_path      = filemap_path.parent / _FILEMAP_LOG_NAME

    # Self-heal: a leftover deploy log means the previous deploy was never
    # restored (crashed or failed restore).  Restore it now - otherwise the
    # rmtree below would destroy the backed-up vanilla originals.
    if log_path.is_file():
        _log("  Previous deploy log still present - restoring it before redeploying.")
        _restore_from_log(log_path, game_root, backup_dir, log_fn)

    # Clear any stale backup from a previous deploy.
    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    already_seen: set[str] = set()
    already_seen_dst: set[str] = set()  # dedup by final destination path (after all remaps)
    placed_lower: set[str] = set()
    placed_log:   list[str] = []
    tasks: list[tuple[str, str, str, str]] = []  # (src_str, dst_str, rel_lower, rel_str)

    _overwrite_str = str(overwrite_dir)
    _staging_str   = str(staging_root)
    sorted_strip   = sorted(_strip) if _strip else []
    nocase_cache: dict[Path, dict[str, list[Path]]] = {}
    mod_index_cache: dict[Path, dict[str, str]] = {}
    _mod_root_cache: dict[str, Path] = {}
    # String-based caches for _resolve_root_path_str
    _game_root_str = str(game_root)
    _game_root_str_len = len(_game_root_str) + 1  # +1 for trailing "/"
    _dir_listing_cache: dict[str, dict[str, str]] = {}
    _resolved_dir_cache: dict[str, str] = {}

    # surrogateescape throughout: filemap.txt entries and the deploy log carry
    # filesystem-derived relative paths whose non-UTF-8 bytes decode to
    # surrogate code points; a plain utf-8 read/write raises on them.
    with filemap_path.open(encoding="utf-8", errors="surrogateescape") as f:
        _tab_lines = [ln.rstrip("\n") for ln in f if "\t" in ln]
    total_lines = len(_tab_lines)
    line_idx = 0

    _prebuild_mod_indexes(
        _tab_lines, overwrite_dir, staging_root, mod_index_cache,
        index_dir=filemap_path.parent,
        strip_prefixes=strip_prefixes,
        per_mod_strip_prefixes=per_mod_strip_prefixes,
    )

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
        # Apply file extension remapping (e.g. .tex.10 → .tex.34).
        if _ext_remap:
            dst_rel_lower = dst_rel.lower()
            for old_ext, new_ext in _ext_remap:
                if dst_rel_lower.endswith(old_ext):
                    dst_rel = dst_rel[: len(dst_rel) - len(old_ext)] + new_ext
                    break

        # Skip if a higher-priority entry already resolved to the same destination
        # (e.g. a mod ships both a .tex.34 and a .tex.10 for the same file, or two
        # mods cover the same path after prefix/extension remapping).
        _dst_rel_lower = dst_rel.lower()
        if _dst_rel_lower in already_seen_dst:
            continue
        already_seen_dst.add(_dst_rel_lower)

        # --- Fast path: O(1) mod-index lookup (no syscall) ---
        _mr = _mod_root_cache.get(mod_name)
        if _mr is None:
            _mr = overwrite_dir if mod_name == _OVERWRITE_NAME else staging_root / mod_name
            _mod_root_cache[mod_name] = _mr
        _idx = mod_index_cache.get(_mr)
        src_str: str | None = None
        if _idx is not None:
            _hit = _idx.get(rel_lower)
            if _hit is not None:
                src_str = _hit if isinstance(_hit, str) else str(_hit)
        if src_str is None:
            # Fall back to full resolve (stat-based)
            src_str = _resolve_source(
                mod_name, rel_str, rel_lower, overwrite_dir, staging_root,
                _overwrite_str, _staging_str, sorted_strip, _per_mod,
                nocase_cache, mod_index_cache,
            )
        if src_str is None:
            _log(f"  WARN: source not found - {rel_str} ({mod_name})")
            continue

        # Apply file transform (e.g. TEX v10→v34 conversion).  The callback
        # may return a new source path (pointing at a converted temp file).
        if file_transform is not None:
            transformed = file_transform(src_str, dst_rel)
            if transformed is not None:
                src_str = transformed

        dst_str = _resolve_root_path_str(_game_root_str, dst_rel,
                                         _dir_listing_cache,
                                         resolved_dir_cache=_resolved_dir_cache)
        # Compute rel_str for the log: strip game_root prefix
        dst_rel_for_log = dst_str[_game_root_str_len:]
        tasks.append((src_str, dst_str, dst_rel.lower(), dst_rel_for_log))

        if progress_fn is not None and line_idx % 500 == 0:
            progress_fn(line_idx, total_lines)

    _log_case_collisions(_dir_listing_cache, _log)

    total = len(tasks)
    if total == 0:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8", errors="surrogateescape")
        snapshot_path = filemap_path.parent / _FILEMAP_SNAPSHOT_NAME
        try:
            _write_deploy_snapshot(game_root, snapshot_path, log_fn=_log)
        except Exception as exc:
            _log(f"  WARN: could not write deploy snapshot: {exc}")
        return 0, placed_lower

    # Write the deployment log BEFORE touching the game dir: if the deploy is
    # interrupted mid-transfer, restore still knows everything we may have
    # placed (a listed file that never landed is a harmless no-op on restore).
    # It is re-written with the actually-placed list after the transfers.
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(t[3].replace("\\", "/") for t in tasks), encoding="utf-8",
        errors="surrogateescape"
    )

    # Pre-create all destination directories up front (single-threaded).
    needed_dirs: set[str] = {os.path.dirname(dst) for _, dst, _, _ in tasks}
    _mkdir_leaves(needed_dirs)

    # Back up any vanilla files we are about to overwrite (must be serial).
    # One lstat per task instead of islink+isfile (two stat-equivalent calls).
    import stat as _stat
    _backup_str = str(backup_dir)
    for _src_s, dst_s, _rel_lower, rel_str in tasks:
        try:
            _st = os.lstat(dst_s)
        except OSError:
            continue
        if _stat.S_ISLNK(_st.st_mode):
            os.unlink(dst_s)
        elif _stat.S_ISREG(_st.st_mode):
            bak_str = _backup_str + "/" + rel_str
            _move_crash_safe(dst_s, bak_str)

    linked = 0
    done_count = 0

    def _do_transfer(item: tuple[str, str, str, str]) -> tuple[str, str, OSError | None]:
        src, dst, rel_lower, rel_str = item
        return rel_lower, rel_str, _do_link(src, dst, mode)

    for rel_lower, rel_str, exc in _iter_map_batched(_do_transfer, tasks):
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
    log_path.write_text("\n".join(placed_log), encoding="utf-8",
                        errors="surrogateescape")

    # Snapshot the game root so restore can identify runtime-generated files.
    snapshot_path = filemap_path.parent / _FILEMAP_SNAPSHOT_NAME
    try:
        _write_deploy_snapshot(game_root, snapshot_path, log_fn=_log)
    except Exception as exc:
        _log(f"  WARN: could not write deploy snapshot: {exc}")

    return linked, placed_lower


def restore_filemap_from_root(
    filemap_path: Path,
    game_root: Path,
    log_fn=None,
    *,
    move_runtime_files: bool = True,
    restore_whitelist=None,
) -> int:
    """Undo a deploy_filemap_to_root() operation.

    Reads the log written by deploy_filemap_to_root(), removes every mod file
    that was placed into game_root, then restores any backed-up vanilla files
    from filemap_backup/.  Silently does nothing if the log is absent.

    If move_runtime_files is True (the default) and a deploy_snapshot.txt
    exists, any file in game_root that was not present at deploy time is moved
    to the profile's overwrite/ directory so it is preserved across redeploys.
    Pass move_runtime_files=False for migration helpers that are not game
    restore operations.

    restore_whitelist - optional matcher from build_restore_whitelist_matcher;
    matching runtime files are left in the game folder instead of moved.

    filemap_path - Profiles/<game>/filemap.txt  (used to locate the log)
    game_root    - the game's install directory
    Returns the number of mod files removed.
    """
    _log = _safe_log(log_fn)
    log_path   = filemap_path.parent / _FILEMAP_LOG_NAME
    backup_dir = filemap_path.parent / _FILEMAP_BACKUP_DIR

    if not log_path.is_file():
        _log("  No filemap_deployed.txt found - nothing to restore.")
        return 0

    removed = _restore_from_log(log_path, game_root, backup_dir, log_fn)
    _log(f"  Filemap restore: removed {removed} mod file(s) from game root.")

    snapshot_path = filemap_path.parent / _FILEMAP_SNAPSHOT_NAME
    if move_runtime_files and snapshot_path.is_file():
        overwrite_dir = filemap_path.parent / "overwrite"
        _log("  Scanning game root for runtime-generated files ...")
        moved = _move_runtime_files(game_root, snapshot_path, overwrite_dir, log_fn,
                                    restore_whitelist=restore_whitelist)
        _log(f"  Moved {moved} runtime-generated file(s) to overwrite/.")
        try:
            snapshot_path.unlink()
        except OSError:
            pass

    return removed


__all__ = [
    "_FILEMAP_LOG_NAME",
    "_FILEMAP_BACKUP_DIR",
    "deploy_filemap_to_root",
    "restore_filemap_from_root",
]
