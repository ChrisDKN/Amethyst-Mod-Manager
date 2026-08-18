"""
deploy_root.py
Root-folder deployment (BepInEx, UE5, Mewgenics, Bannerlord, KCD2, BG3).

Originally extracted from deploy.py during the 2026-04 refactor, with
behaviour preserved at the time of extraction.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import stat as _stat
import time as _time
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.atomic_write import write_atomic_text
from Utils.deploy_shared import (
    LinkMode,
    OVERWRITE_LOG_NAME,
    _deploy_workers,
    _do_link_ex,
    _get_staging_source_path,
    _iter_map_batched,
    _mkdir_leaves,
    _move_crash_safe,
    _path_under_root,
    _prune_empty_dirs,
    _resolve_nocase,
    _resolve_root_path,
    _restore_backup_dir,
    load_per_mod_strip_prefixes,
)


# Name of the sibling directory used to back up pre-existing root files.
_ROOT_BACKUP_NAME = "Root_Backup"
# Name of the log file written next to Root_Folder/ recording what was placed.
_ROOT_LOG_NAME    = "root_folder_deployed.txt"
# Exact identities of regular files placed by the root deploy.  Unlike the
# live filemap/staging tree, this survives a mod being disabled or removed.
_ROOT_IDENTITY_NAME = "root_deploy_identities.json"


def _root_identity(path: Path, rel_str: str, owner: "str | None" = None) \
        -> "dict | None":
    """Return a stable identity for a deployed regular file."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not _stat.S_ISREG(st.st_mode):
        return None
    return {
        "path": rel_str,
        "owner": owner,
        "dev": st.st_dev,
        "ino": st.st_ino,
        "nlink": st.st_nlink,
        "ctime_ns": st.st_ctime_ns,
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
    }


def _write_root_identities(path: Path, records: "list[dict]", log_fn=None) -> None:
    """Atomically persist the identities of successfully deployed root files."""
    try:
        if not records:
            path.unlink(missing_ok=True)
            return
        payload = json.dumps(
            {"version": 1, "files": records},
            ensure_ascii=True,
            separators=(",", ":"),
        ) + "\n"
        write_atomic_text(path, payload, errors="surrogateescape")
    except OSError as exc:
        _safe_log(log_fn)(f"  WARN: could not write root deploy identities: {exc}")


def _load_root_identities(path: Path) -> "dict[str, dict]":
    """Load deployed identities keyed by normalized game-root relative path."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="surrogateescape"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return {}
    out: dict[str, dict] = {}
    for record in raw.get("files", []):
        if not isinstance(record, dict):
            continue
        rel = record.get("path")
        if isinstance(rel, str):
            out[rel.replace("\\", "/").lower()] = record
    return out


def _matches_root_identity(st: "os.stat_result", record: dict) -> bool:
    """True only for the exact regular file observed immediately after deploy."""
    try:
        same_file = (
            _stat.S_ISREG(st.st_mode)
            and st.st_dev == int(record["dev"])
            and st.st_ino == int(record["ino"])
            and st.st_size == int(record["size"])
            and st.st_mtime_ns == int(record["mtime_ns"])
        )
        if not same_file:
            return False
        if st.st_ctime_ns == int(record["ctime_ns"]):
            return True
        # Removing the staging side of a hardlink changes ctime on the shared
        # inode.  A reduced link count with unchanged inode/size/mtime is still
        # the deployed artifact; other ctime changes stay conservatively kept.
        return int(record["nlink"]) > st.st_nlink >= 1
    except (KeyError, TypeError, ValueError):
        return False


def deploy_root_folder(
    root_folder_dir: Path,
    game_root: Path,
    mode: LinkMode = LinkMode.HARDLINK,
    log_fn=None,
) -> int:
    """Transfer files from root_folder_dir into game_root.

    root_folder_dir - Profiles/<game>/Root_Folder/
    game_root       - the game's install directory (the root, not Data/)
    mode            - transfer method (HARDLINK / SYMLINK / COPY)

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
    # file/dir split from readdir d_type - no stat per entry like rglob+is_file.
    sources: list[tuple[Path, Path]] = []   # (src, rel)
    _root_str = str(root_folder_dir)
    _root_plen = len(_root_str) + 1
    for dirpath, _dirnames, filenames in os.walk(_root_str):
        for fname in filenames:
            # Runtime-capture history belongs in overwrite/ but must never
            # become payload when overwrite is deployed to the game root.
            if fname == OVERWRITE_LOG_NAME:
                continue
            full = dirpath + "/" + fname
            sources.append((Path(full), Path(full[_root_plen:])))

    if not sources:
        return 0

    backup_dir = root_folder_dir.parent / _ROOT_BACKUP_NAME
    log_path   = root_folder_dir.parent / _ROOT_LOG_NAME
    identity_path = root_folder_dir.parent / _ROOT_IDENTITY_NAME

    # Resolve destinations case-insensitively against the game tree (shared
    # dir cache - one iterdir per directory instead of one per file) and
    # track which top-level directories we are creating so restore can wipe
    # them entirely - including any game-generated files written into them
    # after deploy (e.g. BepInEx cache/config/log files).
    _dir_cache: dict = {}
    _top_preexisted: dict[str, bool] = {}
    created_dirs: set[str] = set()
    tasks: list[tuple[Path, Path, Path, str]] = []  # (src, dst, rel, rel_posix)
    for src, rel in sources:
        dst = _resolve_root_path(game_root, rel, _dir_cache)
        if len(rel.parts) > 1:
            # Use the resolved (possibly case-corrected) top-level name.
            top = dst.relative_to(game_root).parts[0]
            pre = _top_preexisted.get(top)
            if pre is None:
                pre = (game_root / top).exists()
                _top_preexisted[top] = pre
            if not pre:
                created_dirs.add(top)
        tasks.append((src, dst, rel, str(rel).replace("\\", "/")))

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

    for rel_posix, err in _iter_map_batched(_do_root, tasks):
        if err is None:
            placed.append(rel_posix)
        else:
            _log(f"  WARN: could not transfer root file {rel_posix}: {err}")

    # Re-write the log with what actually landed.
    _write_log(placed)
    task_by_rel = {rel_posix: dst for _s, dst, _r, rel_posix in tasks}
    identities = [
        record
        for rel_posix in placed
        if (record := _root_identity(task_by_rel[rel_posix], rel_posix)) is not None
    ]
    _write_root_identities(identity_path, identities, _log)

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

    filemap_root_path      - Profiles/<game>/filemap_root.txt  (written by build_filemap)
    game_root              - the game's install directory (not Data/)
    staging_root           - the mod staging root (same as used by deploy_filemap)
    mode                   - HARDLINK / SYMLINK / COPY
    strip_prefixes         - shared top-level folder names stripped during staging
    per_mod_strip_prefixes - per-mod overrides for strip_prefixes (same as deploy_filemap)
    excluded_raw           - per-mod RAW excluded keys; skipped as sources, so a
                             collision deploys the variant the user kept

    Files are appended to the same root_folder_deployed.txt log and Root_Backup/ directory
    used by deploy_root_folder(), so restore_root_folder() undoes everything in one pass.

    Returns the number of files transferred.
    """
    _log = _safe_log(log_fn)

    if not filemap_root_path.is_file():
        return 0

    # Read filemap_root.txt - each line is "rel_str\tmod_name"
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
    identity_path = filemap_root_path.parent / _ROOT_IDENTITY_NAME

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
    existing_identities = _load_root_identities(identity_path)

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
        # the scan peeled off (per-mod, shared, and the stacked combination -
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
            # it that way - on a case-sensitive FS the literal path above then
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
        rel_posix = str(Path(rel_str)).replace("\\", "/")

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

    for rel_posix, err in _iter_map_batched(_do_flagged, tasks):
        if err is None:
            placed.append(rel_posix)
        else:
            _log(f"  WARN: could not transfer root-flagged file {rel_posix}: {err}")

    # Re-write the log with what actually landed.
    _write_log(placed)
    task_by_rel = {}
    for src, dst, rel_posix in tasks:
        try:
            owner = src.relative_to(staging_root).parts[0]
        except (ValueError, IndexError):
            owner = None
        task_by_rel[rel_posix] = (dst, owner)
    new_identities = []
    for rel_posix in placed:
        dst, owner = task_by_rel[rel_posix]
        record = _root_identity(dst, rel_posix, owner)
        if record is not None:
            new_identities.append(record)
    merged_identities = {
        **existing_identities,
        **{r["path"].replace("\\", "/").lower(): r for r in new_identities},
    }
    _write_root_identities(identity_path, list(merged_identities.values()), _log)

    _log(f"  Root-flagged mods: {len(placed)} file(s) transferred to game root.")
    return len(placed)


def restore_root_folder(
    root_folder_dir: Path,
    game_root: Path,
    log_fn=None,
    data_deploy_dirs: "set[str] | None" = None,
    staging_root: "Path | None" = None,
    filemap_root_path: "Path | None" = None,
    strip_prefixes: "set[str] | None" = None,
    per_mod_strip_prefixes: "dict[str, list[str]] | None" = None,
) -> int:
    """Undo a deploy_root_folder() operation.

    Reads the log written by deploy_root_folder(), removes every file that
    was placed into game_root, restores any backed-up originals from
    Root_Backup/, then removes the log and any empty directories left behind.

    root_folder_dir - Profiles/<game>/Root_Folder/  (used to locate the log)
    game_root       - the game's install directory
    data_deploy_dirs - top-level dir names (e.g. {"Data"}) that the standard
                     Data/ deploy also owns.  A placed file under one of these
                     dirs that is now a plain regular file with no Root_Backup/
                     original was a deployed-vanilla symlink/hardlink at deploy
                     time (so we never backed it up) and has since been restored
                     to genuine vanilla by restore_data_core() - leaving it
                     intact, not deleting it, keeps that vanilla file.  Defaults
                     to no protection; callers pass the game's
                     root_restore_protect_dirs() (e.g. {"Data"} for Bethesda).
                     The protection is lifted only when the file still has its
                     recorded deployment identity (or, for legacy deployments,
                     shares an inode with its staging source).
    staging_root   - the mod staging root (Profiles/<game>/mods/).  Together
                     with filemap_root_path it supports the safe shared-inode
                     fallback for deployments created before the identity
                     manifest existed.
    filemap_root_path - Profiles/<game>/filemap_root.txt, mapping a placed rel
                     path to the mod that owns it.  Defaults to the sibling of
                     root_folder_dir.
    strip_prefixes - top-level folder names stripped during staging (e.g.
                     {"Data"}), used to locate a rel path's staging source.
    per_mod_strip_prefixes - per-mod wrapper folders stripped during staging.
                     Used only for the safe legacy hardlink fallback when a
                     deployment predates root_deploy_identities.json.
    Returns the number of files removed from game_root.
    Silently does nothing if the log file is absent (no prior deploy).
    """
    _log = _safe_log(log_fn)
    _t_root_restore = _time.perf_counter()

    log_path   = root_folder_dir.parent / _ROOT_LOG_NAME
    backup_dir = root_folder_dir.parent / _ROOT_BACKUP_NAME
    identity_path = root_folder_dir.parent / _ROOT_IDENTITY_NAME

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
    # an original into Root_Backup/ - only its raw bytes live in Data_Core/.  By
    # the time this restore runs, restore_data_core() has already wiped Data/ and
    # renamed Data_Core/ back, so the path now holds the genuine vanilla file.
    # Unlinking it here (with nothing in Root_Backup/ to put back) destroys the
    # vanilla copy for good.  Rule: only remove a placed file when Root_Backup/
    # actually holds its original - otherwise the file is owned by the Data_Core
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

    # Owner lookup for the protection override below: rel path -> mod name.
    # Only built when we have both a staging root and a filemap_root.txt, and
    # only consulted for files the blanket rule would otherwise protect.
    _fmr_path = filemap_root_path
    if _fmr_path is None:
        _fmr_path = root_folder_dir.parent / "filemap_root.txt"
    _rel_to_mod: dict[str, str] = {}
    if staging_root is not None and _fmr_path.is_file():
        try:
            with _fmr_path.open(encoding="utf-8", errors="surrogateescape") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if "\t" not in line:
                        continue
                    _r, _m = line.split("\t", 1)
                    _rel_to_mod[_r.replace("\\", "/").lower()] = _m
        except OSError as exc:
            _log(f"  WARN: could not read {_fmr_path.name} for restore: {exc}")

    _strip = {p.lower() for p in (strip_prefixes or set())}
    _per_mod_strip = per_mod_strip_prefixes or {}
    _staging_index_cache: dict = {}
    _deployed_identities = _load_root_identities(identity_path)

    def _still_mod_payload(rel_str: str, st: "os.stat_result") -> bool:
        """True when *st* is provably the exact regular file we deployed.

        The blanket data_deploy_dirs protection assumes any regular file at a
        Data-deploy path is vanilla restored by restore_data_core().  That is
        wrong for a root-flagged mod that ships its own Data/ subtree (SKSE and
        friends ship Data/Scripts/*.pex): those files never get a Root_Backup/
        original - at deploy time the path held a vanilla gap-fill symlink - so
        the protection kept them forever, and the next move_to_core() renamed
        them into Data_Core/, promoting mod payload to permanent "vanilla".

        New deployments persist the destination's exact filesystem identity,
        which remains usable after the mod or live filemap is removed.  For a
        deployment created before that manifest existed, only a shared inode
        with the staging source is strong enough to lift protection.  Metadata
        or content equality alone cannot distinguish a restored vanilla file
        from a copied mod payload at the same path.
        """
        rel_key = rel_str.replace("\\", "/").lower()
        record = _deployed_identities.get(rel_key)
        if record is not None:
            return _matches_root_identity(st, record)
        mod_name = _rel_to_mod.get(rel_str.replace("\\", "/").lower())
        if not mod_name or staging_root is None:
            return False
        mod_strip = {p.lower() for p in _per_mod_strip.get(mod_name, [])}
        src = _get_staging_source_path(
            staging_root / mod_name, rel_str, _strip | mod_strip,
            index_cache=_staging_index_cache,
        )
        if src is None:
            return False
        try:
            sst = os.lstat(src)
        except OSError:
            return False
        if st.st_ino == sst.st_ino and st.st_dev == sst.st_dev:
            return True
        return False

    # Remove files we placed (parallelised - one lstat + one unlink per worker).
    safe_targets: list[tuple[str, bool, tuple[int, ...] | None]] = []
    _restore_dir_cache: dict = {}
    for rel_str in placed:
        # Deploy resolves existing directory segments case-insensitively (for
        # example Data/Scripts into an existing Data/sCrIpTs).  Older logs
        # retain the filemap spelling, so restore must perform the same lookup
        # instead of probing a parallel, nonexistent path on Linux.
        dst = _resolve_root_path(game_root, Path(rel_str), _restore_dir_cache)
        if not _path_under_root(dst, game_root):
            _log(f"  SKIP: path traversal blocked - {rel_str}")
            continue
        # Protect only Data-deploy paths with no Root_Backup original - those are
        # the files restore_data_core owns.  Pure root-folder mod files (winhttp
        # .dll, BepInEx/, etc.) are still removed even without a backup.
        protect = _under_data_deploy(rel_str) and not _has_backup(rel_str)
        verified_identity = None
        if protect:
            # Resolve ownership serially: _still_mod_payload memoizes a walk of
            # the whole mod folder, and only the rare protected-and-owned path
            # reaches it.  Doing it here keeps the parallel unlink below to one
            # lstat + one unlink per worker and the index cache single-threaded.
            dst_str = str(dst)
            try:
                st = os.lstat(dst_str)
            except OSError:
                st = None
            if st is not None and _stat.S_ISREG(st.st_mode) \
                    and _still_mod_payload(rel_str, st):
                protect = False
                verified_identity = (
                    st.st_dev, st.st_ino, st.st_ctime_ns,
                    st.st_mtime_ns, st.st_size,
                )
        safe_targets.append(
            (str(dst), protect, verified_identity)
        )

    def _unlink_one(item) -> int:
        p, protect, verified_identity = item
        try:
            st = os.lstat(p)
        except OSError:
            return 0
        if verified_identity is not None:
            current_identity = (
                st.st_dev, st.st_ino, st.st_ctime_ns,
                st.st_mtime_ns, st.st_size,
            )
            if current_identity != verified_identity:
                return 0
        # A real regular file at a protected Data path is the vanilla file that
        # restore_data_core put back - leave it (deleting it would lose vanilla).
        # Symlinks are always our own deploy artifacts: safe to drop.
        # Files matching their recorded deploy identity had `protect` cleared
        # above - they are our own payload, never restored vanilla.
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
    _restore_backup_dir(
        backup_dir, game_root, _log, resolve_dir_case=True)

    # Remove the log.
    log_path.unlink()
    identity_path.unlink(missing_ok=True)

    # Wipe entire top-level directories we freshly created - removes any
    # game-generated files written into them after deploy.
    for dir_name in created_dirs:
        if ".." in dir_name or "/" in dir_name or "\\" in dir_name:
            _log(f"  SKIP: path traversal blocked - {dir_name}/")
            continue
        d = game_root / dir_name
        if not _path_under_root(d, game_root):
            _log(f"  SKIP: path traversal blocked - {dir_name}/")
            continue
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            _log(f"  Removed created directory {dir_name}/")

    # Remove any empty subdirectories left behind inside pre-existing dirs
    # (e.g. BepInEx/patchers/Tobey/ left empty after our files were removed).
    dirs_to_check: set[Path] = {Path(path).parent for path, _p, _i in safe_targets}
    _prune_empty_dirs(dirs_to_check, stop_dirs={game_root})

    print(f"  [TIMER] restore_root_folder: {_time.perf_counter() - _t_root_restore:.3f}s")
    _log(f"  Root Folder restore: removed {removed} file(s) from game root.")
    return removed


def restore_root_folder_for_game(
    game,
    *,
    root_folder_dir: "Path | None" = None,
    game_root: "Path | None" = None,
    log_fn=None,
) -> int:
    """Restore root deployment state using all context exposed by *game*.

    Keeping this derivation in one place prevents UI, CLI, wizard, and profile
    cleanup paths from accidentally falling back to the context-free restore.
    """
    root_folder_dir = (
        root_folder_dir
        if root_folder_dir is not None
        else game.get_effective_root_folder_path()
    )
    game_root = game_root if game_root is not None else game.get_game_path()
    if not root_folder_dir.is_dir() or not game_root:
        return 0

    profile_dir = getattr(game, "_active_profile_dir", None)
    if profile_dir is None:
        last_deployed = game.get_last_deployed_profile()
        if last_deployed:
            profile_dir = game.get_profile_root() / "profiles" / last_deployed
    per_mod_strip = (
        load_per_mod_strip_prefixes(Path(profile_dir))
        if profile_dir is not None else None
    )
    filemap_path = game.get_effective_filemap_path()
    return restore_root_folder(
        Path(root_folder_dir), Path(game_root), log_fn=log_fn,
        data_deploy_dirs=(
            game.root_restore_protect_dirs()
            if hasattr(game, "root_restore_protect_dirs") else None
        ),
        staging_root=game.get_effective_mod_staging_path(),
        filemap_root_path=filemap_path.parent / "filemap_root.txt",
        strip_prefixes=getattr(game, "mod_folder_strip_prefixes", None),
        per_mod_strip_prefixes=per_mod_strip or None,
    )


__all__ = [
    "_ROOT_BACKUP_NAME",
    "_ROOT_IDENTITY_NAME",
    "_ROOT_LOG_NAME",
    "deploy_root_folder",
    "deploy_root_flagged_mods",
    "restore_root_folder",
    "restore_root_folder_for_game",
]
