"""
deployment/standard.py
Standard-mode deployment (Data/ games: Bethesda, Stardew, Sims 4, OpenMW).

Originally extracted from the deployment façade during the 2026-04 refactor, with
behaviour preserved at the time of extraction.
"""

from __future__ import annotations

import errno
import inspect
import os
import re as _re
import shutil
import threading as _threading
import time as _time
import uuid
from pathlib import Path, PureWindowsPath

from Utils.app_log import safe_log as _safe_log, app_log as _app_log
from Utils.atomic_write import atomic_writer
from Utils.environment.paths import has_path_traversal as _has_traversal
from Utils.deployment.shared import (
    LinkMode,
    _OVERWRITE_NAME,
    _default_core,
    _do_link_ex,
    _fallback_snapshot,
    _report_fallbacks,
    _append_overwrite_log,
    _iter_map_batched,
    is_global_restore_ignored,
    _log_case_collisions,
    _map_batched,
    _mkdir_leaves,
    _move_crash_safe,
    _path_under_root,
    _resolve_root_path_str,
    _timer,
    _timing_print,
    _TRASH_INFIX,
)


def _report_mode_breakdown(_log, mode_counts: "dict[LinkMode, int]",
                           requested: "LinkMode") -> None:
    """Log how files were actually transferred, flagging hardlink fallbacks.

    Only prints a breakdown when the effective modes differ from what was
    requested - e.g. a HARDLINK deploy that silently fell back to symlink/copy
    because the game and staging are on different filesystems.
    """
    if not mode_counts:
        return
    used = {m for m, n in mode_counts.items() if n}
    if used == {requested}:
        return
    parts = ", ".join(
        f"{n} {m.name.lower()}"
        for m, n in sorted(mode_counts.items(), key=lambda kv: kv[0].name)
        if n
    )
    _log(f"  Transfer methods: {parts}.")
    if requested is LinkMode.HARDLINK and used - {LinkMode.HARDLINK}:
        _log("  Note: some files could not be hardlinked (game and mod "
             "staging are likely on different filesystems) - fell back to "
             "symlink/copy.")
    elif requested is LinkMode.SYMLINK and LinkMode.COPY in used:
        _log("  Note: some files could not be symlinked (the destination "
             "filesystem likely doesn't support symlinks, e.g. exFAT/FAT32) "
             "- fell back to copy.")


def _phase_progress(progress_fn):
    """Return a progress reporter which supports old two-argument callbacks.

    GUI deployment callbacks accept ``(done, total, phase)``.  The low-level
    deploy helpers historically documented ``(done, total)``, though, so keep
    direct callers working while allowing the popup to describe each phase.
    Signature inspection happens once per deployment, never in the hot loop.
    """
    if progress_fn is None:
        return None
    try:
        inspect.signature(progress_fn).bind(0, 1, "phase")
        accepts_phase = True
    except TypeError:
        accepts_phase = False
    except (ValueError, AttributeError):
        # Some extension/builtin callables do not expose a signature. The
        # application contract is the three-argument form, so prefer it.
        accepts_phase = True

    if accepts_phase:
        return lambda done, total, phase: progress_fn(done, total, phase)
    return lambda done, total, _phase: progress_fn(done, total)


class CoreBackupConflictError(RuntimeError):
    """Raised when move_to_core would overwrite a good vanilla backup with a
    deploy dir that still contains mod files - a sign of an interrupted or
    overlapping deploy. Aborting here protects the vanilla files."""


def _dir_has_deployed_mod_files(deploy_dir: Path, limit: int = 4096) -> bool:
    """Return True if deploy_dir contains files that look like deployed mod
    files (symlinks, or regular files with st_nlink > 1, i.e. hardlinks).

    Vanilla game files are plain regular files with a single link, so a Data/
    that contains symlinks/hardlinks has mods deployed into it. We cap the walk
    at *limit* files so this stays cheap on huge install dirs.
    """
    seen = 0
    stack = [str(deploy_dir)]
    while stack:
        cur = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            continue
        with it:
            for de in it:
                try:
                    if de.is_dir(follow_symlinks=False):
                        stack.append(de.path)
                        continue
                    if de.is_symlink():
                        return True
                    st = de.stat(follow_symlinks=False)
                    if st.st_nlink > 1:
                        return True
                except OSError:
                    continue
                seen += 1
                if seen >= limit:
                    return False
    return False


# ---------------------------------------------------------------------------
# Step 1 - back up the game install directory
# ---------------------------------------------------------------------------

# Marker dropped inside the deploy dir while mods are deployed.  Lets the
# unrestored-deploy guard in move_to_core fire even for COPY-mode deploys,
# which leave no symlinks or extra hardlinks for _dir_has_deployed_mod_files
# to detect.  Removed implicitly by restore_data_core's rmtree.
_DEPLOY_MARKER_NAME = ".mm_deployed"

# Restore's deferred delete renames the deploy dir to "Data.mm_trash-<ns>"
# (O(1), same filesystem), renames the core backup into place, and deletes
# the trash dir in a background thread.  The _TRASH_INFIX constant lives in
# deploy_shared (imported above) so the game-root walkers there - snapshot
# writer, runtime-file mover - skip trash dirs mid-delete.  Leftover trash
# from a crash is removed by sweep_deploy_trash().

# Trash dirs whose background delete is still running - sweep_deploy_trash
# skips these so a deploy right after a restore doesn't block on (or race)
# the deferred delete.  Guarded by _ACTIVE_TRASH_LOCK.
_ACTIVE_TRASH: "set[str]" = set()
_ACTIVE_TRASH_LOCK = _threading.Lock()


def _delete_trash_in_background(trash_str: str) -> None:
    """Spawn a daemon thread that rmtree-s *trash_str* and untracks it."""
    with _ACTIVE_TRASH_LOCK:
        _ACTIVE_TRASH.add(trash_str)

    def _run() -> None:
        try:
            shutil.rmtree(trash_str, ignore_errors=True)
        finally:
            with _ACTIVE_TRASH_LOCK:
                _ACTIVE_TRASH.discard(trash_str)

    _threading.Thread(target=_run, name="mm-restore-trash", daemon=True).start()


def sweep_deploy_trash(parent: "Path | str", log_fn=None) -> int:
    """Remove leftover '<name>.mm_trash-*' dirs under *parent*.

    These only exist when a background delete from a previous restore was
    interrupted (crash / app close).  Trash dirs still being deleted by a
    live background thread are skipped.  Returns the number of dirs removed.
    """
    removed = 0
    try:
        it = os.scandir(parent)
    except OSError:
        return 0
    with it:
        with _ACTIVE_TRASH_LOCK:
            _active = set(_ACTIVE_TRASH)
        for de in it:
            if _TRASH_INFIX not in de.name or de.path in _active:
                continue
            try:
                if not de.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            shutil.rmtree(de.path, ignore_errors=True)
            removed += 1
            _safe_log(log_fn)(
                f"  Removed leftover {de.name}/ from an interrupted cleanup.")
    return removed

# Per-file (size, mtime_ns) record of everything deploy_filemap placed in the
# main deploy dir, written to Profiles/<game>/.  restore_data_core uses it to
# tell "still exactly the file we deployed" (safe to discard - staging holds
# the data, or the mod was replaced and this copy is stale) apart from files
# the game or an external tool wrote after deploy (must be rescued).  Without
# it, replacing a deployed mod with a new version that drops a file leaves the
# old hardlink in Data with nlink==1 and no filemap/modindex entry, and
# restore wrongly rescues it into overwrite/.
_DEPLOY_STATS_NAME = "deploy_stats.txt"
_DEPLOY_STATS_DELTA_NAME = "deploy_stats_delta.txt"

# Slack when comparing mtimes across filesystems: FAT stores mtimes at 2s
# resolution, exFAT at 10ms, so a copy2-preserved timestamp read back from
# the game drive may differ from the staging original by up to 2s.
_MTIME_TOLERANCE_NS = 2_000_000_000


def _write_deploy_stats(stats_path: Path, entries: "list[str]", log_fn=None) -> None:
    """Atomically write deploy_stats.txt from pre-formatted lines."""
    try:
        revision = uuid.uuid4().hex
        # surrogateescape: the rel-path column derives from on-disk filenames
        # whose non-UTF-8 bytes decode to surrogate code points; symmetric with
        # _load_deploy_stats below so the round-trip never raises.
        with atomic_writer(stats_path, "w", errors="surrogateescape") as fh:
            fh.write(f"# deploy_stats v2 {revision}\n")
            for line in entries:
                fh.write(line)
        # A full deployment is a new baseline.  The revision in the new
        # header also makes a leftover delta harmless if unlinking fails.
        try:
            stats_path.with_name(_DEPLOY_STATS_DELTA_NAME).unlink(missing_ok=True)
        except OSError as exc:
            _safe_log(log_fn)(f"  WARN: could not clear deploy stats delta: {exc}")
    except OSError as exc:
        _safe_log(log_fn)(f"  WARN: could not write deploy stats: {exc}")


def _deploy_stats_revision(stats_path: Path) -> str | None:
    """Return a stable identity for the current full statistics baseline."""
    try:
        with stats_path.open(
                encoding="utf-8", errors="surrogateescape") as fh:
            header = fh.readline().rstrip("\n")
        prefix = "# deploy_stats v2 "
        if header.startswith(prefix):
            return header[len(prefix):]
        # Existing v1 files have no revision.  Their inode identity changes
        # when atomic_writer replaces them, invalidating any old delta.
        st = stats_path.stat()
        return (
            f"legacy:{st.st_dev:x}:{st.st_ino:x}:{st.st_size:x}:"
            f"{st.st_mtime_ns:x}"
        )
    except OSError:
        return None


def _load_deploy_stats(stats_path: Path) -> "dict[str, tuple[int, int]]":
    """Read the full deploy-stat baseline plus its small incremental delta."""
    stats: dict[str, tuple[int, int]] = {}
    revision = _deploy_stats_revision(stats_path)
    try:
        with stats_path.open(encoding="utf-8", errors="surrogateescape") as fh:
            for line in fh:
                if line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 3:
                    try:
                        stats[parts[0].lower()] = (int(parts[1]), int(parts[2]))
                    except ValueError:
                        pass
    except OSError:
        pass
    if revision is None:
        return stats

    delta_path = stats_path.with_name(_DEPLOY_STATS_DELTA_NAME)
    try:
        with delta_path.open(
                encoding="utf-8", errors="surrogateescape") as fh:
            if fh.readline().rstrip("\n") != (
                    f"# deploy_stats_delta v1 {revision}"):
                return stats
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 3:
                    continue
                key = parts[0].lower()
                if parts[1:] == ["-", "-"]:
                    stats.pop(key, None)
                    continue
                try:
                    stats[key] = (int(parts[1]), int(parts[2]))
                except ValueError:
                    pass
    except OSError:
        pass
    return stats


def _write_deploy_stats_delta(
    stats_path: Path,
    updates: "dict[str, tuple[str, int | None, int | None]]",
    log_fn=None,
) -> None:
    """Atomically update only deploy-stat records changed by one deployment."""
    if not updates:
        return
    revision = _deploy_stats_revision(stats_path)
    if revision is None:
        _safe_log(log_fn)("  WARN: deploy stats baseline disappeared; "
                          "incremental stats were not written")
        return

    delta_path = stats_path.with_name(_DEPLOY_STATS_DELTA_NAME)
    records: dict[str, tuple[str, int | None, int | None]] = {}
    try:
        with delta_path.open(
                encoding="utf-8", errors="surrogateescape") as fh:
            if fh.readline().rstrip("\n") == (
                    f"# deploy_stats_delta v1 {revision}"):
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 3:
                        continue
                    key = parts[0].lower()
                    if parts[1:] == ["-", "-"]:
                        records[key] = (parts[0], None, None)
                        continue
                    try:
                        records[key] = (
                            parts[0], int(parts[1]), int(parts[2]))
                    except ValueError:
                        pass
    except OSError:
        pass

    records.update(updates)
    try:
        with atomic_writer(
                delta_path, "w", errors="surrogateescape") as fh:
            fh.write(f"# deploy_stats_delta v1 {revision}\n")
            for display, size, mtime_ns in records.values():
                if size is None or mtime_ns is None:
                    fh.write(f"{display}\t-\t-\n")
                else:
                    fh.write(f"{display}\t{size}\t{mtime_ns}\n")
    except OSError as exc:
        _safe_log(log_fn)(f"  WARN: could not write deploy stats delta: {exc}")


# Records the relative paths deploy_core() placed as vanilla gap-fill files.
# Needed for the symlink-mode xEdit rescue: when vanilla files are deployed as
# symlinks into Data_Core/, an external tool (e.g. FO4Edit Quick Auto Clean)
# launched against Data/ can reach through the symlink and destroy/replace the
# Data_Core/ copy.  Once the core copy is gone, restore_data_core can no longer
# tell the edited plugin was vanilla from core_lower alone, so it would wrongly
# treat it as a runtime-created file and bury it in overwrite/.  This sidecar
# lets restore recognise such files and put the edited plugin back in Data/.
_VANILLA_DEPLOYED_NAME = "vanilla_deployed.txt"


def _write_vanilla_deployed(path: Path, rels: "list[str]", log_fn=None) -> None:
    """Atomically write the vanilla gap-fill manifest (one rel path per line)."""
    try:
        with atomic_writer(path, "w", errors="surrogateescape") as fh:
            fh.write("# vanilla_deployed v1\n")
            for rel in rels:
                fh.write(rel.replace("\\", "/") + "\n")
    except OSError as exc:
        _safe_log(log_fn)(f"  WARN: could not write vanilla manifest: {exc}")


def _load_vanilla_deployed(path: Path) -> "set[str]":
    """Read the vanilla gap-fill manifest into a set of lowercased rel paths."""
    rels: set[str] = set()
    try:
        with path.open(encoding="utf-8", errors="surrogateescape") as fh:
            for line in fh:
                if line.startswith("#"):
                    continue
                rel = line.rstrip("\n").replace("\\", "/")
                if rel:
                    rels.add(rel.lower())
    except OSError:
        pass
    return rels


# Plugin file extensions whose in-place edits (e.g. via xEdit / Quick Auto
# Clean) we want to surface on the mod row.  When restore_data_core moves a
# *modified* plugin back into its owning mod folder, it tags that mod's
# meta.ini so the GUI can show a "contains an xEdit-modified plugin" flag.
_PLUGIN_EXTS = (".esp", ".esm", ".esl")

# xEdit / QuickAutoClean writes the cleaned record to a temp file and queues a
# rename to the real plugin name "on shutdown" (e.g.
# ``AlternatePerspective.esp.save.2026_06_19_00_38_14`` -> ``…esp``).  If that
# deferred rename hasn't landed by the time we walk Data/ (it races with xEdit's
# own shutdown), the temp file is the only copy of the edit.  Recognise it so we
# can finish the rename to the base plugin name rather than burying the temp in
# overwrite/ as an unrecognised runtime file.  Matches ``.save.<timestamp>``
# where the timestamp is digits and underscores.
_XEDIT_SAVE_TEMP_RE = _re.compile(r"^(?P<base>.+)\.save\.[0-9_]+$", _re.IGNORECASE)


def _tag_mod_xedit_modified(mod_dir: Path, plugin_name: str) -> None:
    """Record *plugin_name* in the mod's ``meta.ini`` under
    ``[General] xeditModifiedPlugins`` (a semicolon-separated list).

    Called when an externally-edited plugin is moved back into this mod's
    staging folder during restore, so the modlist can flag the mod as
    containing a plugin modified in xEdit.  Preserves all other meta.ini
    content and is idempotent (a plugin already listed is not duplicated)."""
    import configparser
    meta = mod_dir / "meta.ini"
    cp = configparser.ConfigParser()
    if meta.is_file():
        try:
            cp.read(str(meta), encoding="utf-8")
        except Exception:
            cp = configparser.ConfigParser()
    if not cp.has_section("General"):
        cp.add_section("General")
    existing = cp["General"].get("xeditModifiedPlugins", "")
    names = [n.strip() for n in existing.split(";") if n.strip()]
    # Case-insensitive de-dup (plugin filesystem names are case-insensitive).
    lower = {n.lower() for n in names}
    if plugin_name.lower() not in lower:
        names.append(plugin_name)
    cp["General"]["xeditModifiedPlugins"] = ";".join(names)
    try:
        with open(meta, "w", encoding="utf-8") as fh:
            cp.write(fh)
    except Exception as exc:
        _app_log(f"  WARN: could not tag xEdit-modified plugin in {meta}: {exc}")


def _tree_has_files(root: Path) -> bool:
    """Early-exit check: does *root* contain at least one file anywhere?"""
    stack = [str(root)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for de in it:
                    if de.is_dir(follow_symlinks=False):
                        stack.append(de.path)
                    else:
                        return True
        except OSError:
            continue
    return False


def move_to_core(
    deploy_dir: Path,
    core_dir: Path | None = None,
    log_fn=None,
) -> bool:
    """Move the contents of deploy_dir into core_dir (the vanilla backup).

    deploy_dir - directory whose contents will be moved out (e.g. Data/)
    core_dir   - destination for the backup; defaults to Data_Core/ sibling
    Returns True when a backup move happened.  If deploy_dir is empty or
    missing, core_dir is still created (empty) so restore always finds a core
    folder and does not report "nothing to restore".

    Safety guards when core_dir already exists (i.e. a prior deploy's backup
    was never restored):
    - deploy_dir missing → a restore was interrupted between clearing
      deploy_dir and renaming core_dir back.  The backup is the only copy of
      the vanilla files: keep it and just recreate an empty deploy_dir.
    - deploy_dir still contains deployed mod files (marker file, symlinks or
      extra hardlinks) → unrestored or overlapping deploy; abort rather than
      overwrite the good backup.
    - deploy_dir empty → an earlier deploy was interrupted after clearing it;
      again keep the existing backup.
    Otherwise the stale core_dir is removed and rebuilt from deploy_dir.
    """
    _log = _safe_log(log_fn)
    core_dir = core_dir or _default_core(deploy_dir)
    marker = deploy_dir / _DEPLOY_MARKER_NAME
    sweep_deploy_trash(deploy_dir.parent, log_fn=log_fn)

    # Incremental fast path: the current deployment (and its core backup)
    # stays in place - deploy_filemap diffs into it instead.
    from Utils.deployment import incremental as _incr
    if _incr.active_for(deploy_dir) is not None:
        if marker.is_file() and core_dir.is_dir():
            _log(f"  Incremental deploy - keeping {deploy_dir.name}/ and the "
                 f"existing {core_dir.name}/ backup.")
            return False
        raise _incr.IncrementalFallback(
            f"deploy state changed under us - {deploy_dir.name}/ marker or "
            f"{core_dir.name}/ missing")

    if core_dir.exists():
        if not deploy_dir.is_dir():
            _log(f"  Interrupted restore detected - keeping existing "
                 f"{core_dir.name}/ backup.")
            deploy_dir.mkdir(parents=True, exist_ok=True)
            marker.touch()
            return False
        if marker.is_file() or _dir_has_deployed_mod_files(deploy_dir):
            raise CoreBackupConflictError(
                f"Refusing to overwrite vanilla backup {core_dir.name}/: "
                f"{deploy_dir.name}/ still contains deployed mod files "
                f"(interrupted or overlapping deploy?). Run Restore, then deploy again."
            )
        if not _tree_has_files(deploy_dir):
            _log(f"  {deploy_dir.name}/ is empty - keeping existing "
                 f"{core_dir.name}/ backup.")
            marker.touch()
            return False
        _log(f"  {core_dir.name} already exists - removing old backup first.")
        shutil.rmtree(core_dir)

    if not deploy_dir.is_dir():
        core_dir.mkdir(parents=True, exist_ok=True)
        return False

    # Drop any stale marker so it never lands inside the backup.
    try:
        marker.unlink()
    except OSError:
        pass

    if not _tree_has_files(deploy_dir):
        core_dir.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return False

    # Same filesystem → os.rename is a single instant syscall.
    # shutil.move falls back to copy+delete if cross-device.
    with _timer("move_to_core - rename dir"):
        core_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(deploy_dir), str(core_dir))

    # Recreate the (now-empty) deploy dir so downstream code finds it, and
    # mark it as managed until restore puts the backup back.
    deploy_dir.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return True


# ---------------------------------------------------------------------------
# Step 2 - link mod files listed in filemap.txt into the deploy directory
# ---------------------------------------------------------------------------

def deploy_filemap(
    filemap_path: Path,
    deploy_dir: Path,
    staging_root: Path,
    mode: LinkMode = LinkMode.HARDLINK,
    strip_prefixes: set[str] | None = None,
    per_mod_strip_prefixes: dict[str, list[str]] | None = None,
    per_mod_deploy_dirs: dict[str, Path] | None = None,
    per_mod_link_modes: dict[str, LinkMode] | None = None,
    log_fn=None,
    progress_fn=None,
    symlink_exts: set[str] | None = None,
    exclude: set[str] | None = None,
    core_dir: "Path | None" = None,
    flatten_extensions: set[str] | None = None,
    per_mod_subdirs: dict[str, str] | None = None,
    path_remap: dict[str, str] | None = None,
    replace_existing: bool = False,
    source_resolver=None,
) -> tuple[int, set[str]]:
    """Read filemap.txt and transfer every listed file into deploy_dir.

    filemap_path   - Profiles/<game>/filemap.txt
    deploy_dir     - destination directory (e.g. <game_path>/Data)
    staging_root   - Profiles/<game>/mods/
    mode           - transfer method
    strip_prefixes - same set passed to build_filemap; used to locate source
                     files whose leading folder was stripped from the filemap
                     path (e.g. rel_str "Nautilus/Nautilus.dll" may live on
                     disk as "plugins/Nautilus/Nautilus.dll").
    per_mod_strip_prefixes - optional dict mapping mod name to list of
                     top-level folder names to prepend when resolving (user-
                     configured "ignore" folders for that mod).
    progress_fn    - optional callable(done: int, total: int, phase: str)
                     reporting one combined preparation + transfer counter.
                     Legacy two-argument callbacks remain supported.
    flatten_extensions - lowercase extensions whose files are placed flat at
                     the top of the deploy dir (basename only), regardless of
                     their staging subfolder.  BG3 passes {".pak"} because the
                     game only loads paks at the top level of the Mods folder.
    per_mod_subdirs - optional mapping of mod name to one destination folder
                     below deploy_dir.  Applied only to the normal deploy_dir,
                     never separator/custom destinations.  BepInEx uses this
                     to isolate Thunderstore plugins below their versionless
                     package ID, matching r2modman's package layout.
    path_remap     - optional destination-only path-prefix replacements. Source
                     lookup always uses the original filemap path. This is the
                     standard-directory equivalent of deploy_filemap_to_root's
                     remapping support.
    replace_existing - unlink regular files/symlinks already present at normal
                     deploy destinations before transfer. Private VFS layer
                     construction uses this to preserve physical deploy's
                     ordering when an earlier custom route and a later normal
                     or remapped entry converge on one destination.
    source_resolver - optional handler callback for filemaps whose displayed
                     destination differs from the staged source layout. It is
                     called with keyword arguments ``staging_root``,
                     ``mod_name``, ``relative``, ``strip_prefixes``,
                     ``overwrite_dir``, and ``cache``. The normal resolver is
                     unchanged when this is omitted.

    Returns:
        (count, placed_lower)
        placed_lower is the set of lowercased rel paths successfully placed -
        pass it to deploy_core() so it can skip files already provided by mods.
    """
    _log = _safe_log(log_fn)
    _progress = _phase_progress(progress_fn)
    _strip = {p.lower() for p in strip_prefixes} if strip_prefixes else set()
    _per_mod = per_mod_strip_prefixes or {}
    _remap: list[tuple[str, str]] = []
    for old, new in (path_remap or {}).items():
        old_normalized = str(old).replace("\\", "/")
        new_normalized = str(new).replace("\\", "/")
        if (not old_normalized
                or _has_traversal(old_normalized)
                or Path(old_normalized).is_absolute()
                or PureWindowsPath(old_normalized).is_absolute()
                or _has_traversal(new_normalized)
                or Path(new_normalized).is_absolute()
                or PureWindowsPath(new_normalized).is_absolute()):
            raise RuntimeError(
                "Unsafe deployment path remap: "
                f"{old!r} -> {new!r}"
            )
        _remap.append((old_normalized.lower(), new_normalized))
    _flatten_exts = {e.lower() for e in flatten_extensions} if flatten_extensions else None
    _per_deploy = per_mod_deploy_dirs or {}
    _per_subdir: dict[str, str] = {}
    for _mod_name, _raw_subdir in (per_mod_subdirs or {}).items():
        _subdir = str(_raw_subdir).strip()
        if (not _subdir or _subdir in (".", "..") or "/" in _subdir
                or "\\" in _subdir or "\x00" in _subdir):
            _log(f"  WARN: ignoring unsafe deployment subdirectory "
                 f"{_raw_subdir!r} for {_mod_name!r}")
            continue
        _per_subdir[_mod_name] = _subdir
    _per_mode = per_mod_link_modes
    _per_merge: set[str] = set()
    try:
        from Utils.deployment.shared import (
            load_separator_deploy_paths as _lsdp,
            expand_separator_link_modes as _eslm,
            expand_separator_merge_dirs as _esmd,
        )
        from Utils.mods.modlist import read_modlist as _rml
        _sd = _lsdp(filemap_path.parent)
        _se = _rml(filemap_path.parent / "modlist.txt")
        if _per_mode is None:
            _per_mode = _eslm(_sd, _se)
        _per_merge = _esmd(_sd, _se)
    except Exception:
        if _per_mode is None:
            _per_mode = {}
    _per_mode = _per_mode or {}
    overwrite_dir = staging_root.parent / "overwrite"

    already_seen: set[str] = set()
    # Prefix remaps, flattening, and package subdirectories can make distinct
    # filemap paths converge on one destination. Keep the first effective
    # winner for each actual target, without conflating explicit separator
    # targets that happen to use the same relative path.
    already_seen_dst: set[tuple[str, str]] = set()
    tasks: list[tuple[Path, Path, str]] = []
    placed_lower: set[str] = set()
    _exclude: set[str] = exclude or set()
    _excluded_plan_keys: set[str] = set()
    # rel_lower -> (deployed rel_str, mod_name) for non-custom tasks - the
    # "effective deployed set" recorded in deployed_filemap.txt so the next
    # deploy can diff against it (incremental fast path).
    _deployed_rel_mod: dict[str, tuple[str, str]] = {}

    _overwrite_str = str(overwrite_dir)
    _staging_str   = str(staging_root)
    sorted_strip   = sorted(_strip) if _strip else []
    nocase_cache: dict[Path, dict[str, list[Path]]] = {}
    mod_index_cache: dict[Path, dict[str, Path]] = {}

    _t_resolve_start = _time.perf_counter()
    from Utils.filegraph.deploy import entries as filegraph_entries
    # Consume the typed plan directly. The compatibility projection used to
    # allocate thousands of ``"path\tmod"`` strings, split them again, and
    # build a second full source lookup before the real loop could start.
    _plan_entries = tuple(
        entry for entry in filegraph_entries()
        if entry.legacy_rel and entry.mod_name != "[Root_Folder]"
    )
    total_lines = len(_plan_entries)
    # The exact transfer count is known only after this pass has applied
    # exclusions, routing and destination de-duplication. Use two plan-sized
    # halves provisionally; at the phase boundary the denominator is corrected
    # to ``plan entries + concrete transfer tasks``. Because tasks cannot
    # outnumber entries here, that correction can only move the bar forward.
    _planning_total = total_lines * 2
    if _progress is not None and total_lines:
        _progress(0, _planning_total, "Resolving sources and destinations…")

    _timing_print(
        f"  [TIMER][CPU] deploy_filemap - prepare typed plan entries: "
        f"{_time.perf_counter() - _t_resolve_start:.3f}s")

    _t_resolve_loop = _time.perf_counter()
    _index_hits = 0
    _slow_hits = 0
    # Source roots are shared by all entries from one mod. Build source paths
    # as surrogate-safe strings so the hot loop does not allocate two Path
    # objects (join + str) per winner.
    _source_root_strings: dict[str, str] = {}
    # String-based caches for _resolve_root_path_str
    _deploy_dir_str = str(deploy_dir)
    _deploy_dir_key = _deploy_dir_str.casefold()
    _core_base_str = str(core_dir) if core_dir is not None else None
    _dir_listing_cache: dict[str, dict[str, str]] = {}
    _resolved_dir_cache: dict[str, str] = {}
    # Destination casing is deliberately resolved after Deploy is requested.
    # Keeping these caches local ensures toggles and moves do no speculative
    # filesystem work for a deployment which may never happen.
    # {custom_deploy_dir_str: {top_level_folder_name, ...}} - populated as we
    # build tasks, consumed by the folder-replace pass below.
    _custom_top_roots: dict[str, set[str]] = {}
    # {(custom_deploy_dir_str, top_folder_lower): owner_mod_name or None} -
    # records which mod owns each top-level folder. None means multiple mods
    # contribute to it (so we fall back to per-file deploy instead of a
    # single directory symlink). Folders ending up with exactly one owner are
    # candidates for the directory-symlink optimization.
    _top_folder_owner: dict[tuple[str, str], str | None] = {}
    # {(custom_deploy_dir_str, top_folder_lower): one (src_str, rel_str, dst_str)}
    # - used to derive the source- and destination-side top-level folder paths
    # for the symlink. Keeping the resolved dst guarantees the symlink path
    # uses the same casing as the per-file tasks it replaces.
    _top_folder_sample: dict[tuple[str, str], tuple[str, str, str]] = {}
    _mod_traversal: dict[str, bool] = {}
    for scan_idx, entry in enumerate(_plan_entries, 1):
        # Report entries inspected rather than only entries which survive the
        # filters. This makes the preparation half reach its boundary even when
        # many winners are excluded or converge on one destination.
        if (_progress is not None
                and (scan_idx % 500 == 0 or scan_idx == total_lines)):
            _progress(scan_idx, _planning_total,
                      "Resolving sources and destinations…")
        rel_str = entry.legacy_rel
        mod_name = entry.mod_name
        # Guard against path traversal in filemap entries. The mod-name
        # verdict is cached per unique mod (~hundreds) instead of per line.
        _bad_mod = _mod_traversal.get(mod_name)
        if _bad_mod is None:
            _bad_mod = _has_traversal(mod_name)
            _mod_traversal[mod_name] = _bad_mod
        _bad_rel = (_has_traversal(rel_str)
                    or (len(rel_str) >= 2 and rel_str[1] == ":"
                        and rel_str[0].isalpha()))
        if _bad_mod or _bad_rel:
            _log(f"  WARN: skipping suspicious filemap entry - rel={rel_str!r} mod={mod_name!r}")
            continue
        rel_lower = rel_str.lower()
        if rel_lower in already_seen:
            continue
        already_seen.add(rel_lower)
        if rel_lower in _exclude:
            # Keep the incremental Data projection aligned with the standard
            # handler. Custom routing may place a file back *under* Data at a
            # different flattened path (e.g. Skyrim .jslot presets), even
            # though this normal pass correctly excludes its legacy path.
            _entry_destination = entry.destination.replace("\\", "/")
            _data_prefix = deploy_dir.name.lower() + "/"
            if _entry_destination.lower().startswith(_data_prefix):
                _excluded_plan_keys.add(
                    _entry_destination[len(_data_prefix):].lower())
            else:
                _excluded_plan_keys.add(rel_lower)
            continue
        source_root = entry.source_root
        if source_root is None:
            src_str = None
        else:
            source_root_str = _source_root_strings.get(mod_name)
            if source_root_str is None:
                source_root_str = str(source_root)
                _source_root_strings[mod_name] = source_root_str
            src_str = source_root_str + "/" + os.fsdecode(entry.source_rel)
        if src_str is not None:
            _index_hits += 1
        if src_str is None:
            _log(f"  WARN: source not found - {rel_str} ({mod_name})")
            continue

        # Destination remapping deliberately happens after source resolution:
        # staged files retain their original filemap layout. This is used by
        # Cyberpunk, for example, to expose archive/pc/patch payloads below
        # archive/pc/mod in the resolved view.
        dst_rel = rel_str
        for old_prefix, new_prefix in _remap:
            if rel_lower.startswith(old_prefix):
                dst_rel = new_prefix + rel_str[len(old_prefix):]
                break
        dst_rel_lower = dst_rel.lower()

        # Flatten matching files to the top of the deploy dir. Source
        # resolution above used the original rel path; only the destination
        # changes. Collisions on the flattened name keep the first entry.
        if _flatten_exts is not None and "/" in rel_str \
                and os.path.splitext(rel_str)[1].lower() in _flatten_exts:
            dst_rel = rel_str.rsplit("/", 1)[1]
            dst_rel_lower = dst_rel.lower()
            if dst_rel_lower in already_seen:
                _log(f"  WARN: flattened name collision - skipping "
                     f"{rel_str} ({mod_name})")
                continue
            already_seen.add(dst_rel_lower)

        effective_dir = _per_deploy.get(mod_name, deploy_dir)
        # r2modman installs each Thunderstore plugin package below its full,
        # versionless package ID (e.g. RiskofThunder-RoR2BepInExPack). Apart
        # from preventing package collisions, that keeps current payloads away
        # from legacy paths which a loader may intentionally remove. Apply the
        # namespace only to the normal destination: separator/custom deploy
        # paths are explicit user choices and must retain their exact layout.
        _package_subdir = _per_subdir.get(mod_name)
        if _package_subdir and effective_dir is deploy_dir:
            _first_segment = dst_rel.split("/", 1)[0]
            if _first_segment.casefold() != _package_subdir.casefold():
                dst_rel = f"{_package_subdir}/{dst_rel}"
                dst_rel_lower = dst_rel.lower()

        # ``rel_str`` came from the validated native candidate. Only redo the
        # full remap safety check when this handler actually changed it; the
        # old code constructed Path and PureWindowsPath for every winner.
        if (dst_rel != rel_str and (
                _has_traversal(dst_rel)
                or Path(dst_rel).is_absolute()
                or PureWindowsPath(dst_rel).is_absolute())):
            _log(f"  WARN: skipping unsafe remapped destination - "
                 f"{dst_rel!r} ({mod_name})")
            continue

        _is_normal_dir = effective_dir is deploy_dir
        _eff_s = _deploy_dir_str if _is_normal_dir else str(effective_dir)
        _dst_key = (
            _deploy_dir_key if _is_normal_dir else _eff_s.casefold(),
            dst_rel_lower,
        )
        if _dst_key in already_seen_dst:
            _log(f"  WARN: remapped destination collision - skipping "
                 f"{rel_str} ({mod_name})")
            continue
        already_seen_dst.add(_dst_key)
        _core_s = _core_base_str if _is_normal_dir else None
        # Inline _resolve_root_path_str's two O(1) outcomes (no dir part /
        # resolved-dir cache hit) - most files share their parent dir, so
        # this skips a function call per line. Must mirror its key exactly:
        # base + "\x00" + dir-part-of-original-rel lowercased (lowercasing
        # can change string length, so never slice the lowered rel instead).
        _sp = dst_rel.rfind("/")
        if _sp < 0:
            dst_str = _eff_s + "/" + dst_rel
        else:
            _rd = _resolved_dir_cache.get(
                _eff_s + "\x00" + dst_rel[:_sp].lower())
            if _rd is not None:
                dst_str = _rd + "/" + dst_rel[_sp + 1:]
            else:
                dst_str = _resolve_root_path_str(_eff_s, dst_rel,
                                                 _dir_listing_cache,
                                                 core_base_str=_core_s,
                                                 resolved_dir_cache=_resolved_dir_cache)
        use_symlink = symlink_exts is not None and os.path.splitext(src_str)[1].lower() in symlink_exts
        override_mode = _per_mode.get(mod_name)
        is_custom_task = not _is_normal_dir
        tasks.append((src_str, dst_str, dst_rel_lower, is_custom_task, use_symlink, override_mode))
        if not is_custom_task:
            _deployed_rel_mod[dst_rel_lower] = (dst_rel, mod_name)
        # Track top-level folder roots that custom-deploy mods are writing into,
        # so we can wholesale-replace any same-named folder at the destination
        # (with backup) before the per-file deploy runs. Files that the mod
        # ships at the root (no folder component in rel_str) are excluded -
        # those still get the existing file-by-file backup-and-replace path.
        # Mods whose separator opted into "merge folders" are skipped here so
        # their top-level folders are merged with the target instead of
        # wholesale-replaced; per-file backup-and-replace still applies.
        #
        # The wholesale-replace is only safe to apply when the folder will be
        # dir-symlinked afterwards (symlink-effective mode): the symlink covers
        # every file under the folder, including any a custom routing rule
        # deployed there (Step 0). Under hardlink/copy there is no symlink to
        # repopulate it, and custom-routed files are excluded from the per-file
        # deploy - wiping the folder would silently lose them. So for non-symlink
        # modes we skip the wholesale-replace and let per-file backup-and-replace
        # handle each file, leaving co-located custom-routed files intact.
        _eff_mode = override_mode if override_mode is not None else mode
        if is_custom_task and "/" in dst_rel and mod_name not in _per_merge \
                and _eff_mode is LinkMode.SYMLINK:
            _top = dst_rel.split("/", 1)[0]
            _custom_top_roots.setdefault(_eff_s, set()).add(_top)
            _key = (_eff_s, _top.lower())
            _existing_owner = _top_folder_owner.get(_key, "__unset__")
            if _existing_owner == "__unset__":
                _top_folder_owner[_key] = mod_name
                _top_folder_sample[_key] = (src_str, dst_rel, dst_str)
            elif _existing_owner != mod_name:
                _top_folder_owner[_key] = None  # multi-owner → no dir-symlink

    _timing_print(
        f"  [TIMER][CPU + FS METADATA] deploy_filemap - resolve loop: "
        f"{_time.perf_counter() - _t_resolve_loop:.3f}s "
        f"(index={_index_hits}, slow={_slow_hits})")

    # Incremental fast path: when the deploy pipeline activated a plan for
    # this deploy dir, diff the new task set against the previous deploy and
    # only unlink/link what changed (raises IncrementalFallback on anomaly -
    # the pipeline catches it and reruns the full restore + deploy).
    from Utils.deployment import incremental as _incr
    _incr_plan = _incr.active_for(deploy_dir)
    if _incr_plan is not None:
        if _progress is not None:
            _progress(total_lines, total_lines + len(tasks),
                      "Applying deployment changes…")
        _incremental_total = 0

        def _incremental_progress(done, total, _phase=None):
            nonlocal _incremental_total
            _incremental_total = total
            if _progress is not None:
                _progress(total_lines + done, total_lines + total,
                          "Applying deployment changes…")

        result = _incr.apply_incremental(
            _incr_plan, tasks, _deployed_rel_mod,
            deploy_dir=deploy_dir,
            core_dir=core_dir if core_dir is not None else _default_core(deploy_dir),
            overwrite_dir=overwrite_dir,
            mode=mode,
            state_dir=filemap_path.parent,
            staging_root=staging_root,
            excluded_plan_keys=_excluded_plan_keys,
            log_fn=log_fn,
            progress_fn=(_incremental_progress
                         if _progress is not None else None),
        )
        if _progress is not None:
            _progress(total_lines + _incremental_total,
                      total_lines + _incremental_total,
                      "Deployment changes applied")
        return result

    from Utils.filegraph.deploy import current as current_deployment, mark_phase
    if current_deployment() is not None:
        mark_phase("placing")

    total = len(tasks)
    if total == 0:
        if _progress is not None and total_lines:
            _progress(total_lines, total_lines, "Deployment plan complete")
        # Still clear any stale stats/deploy record from a previous deploy.
        _write_deploy_stats(filemap_path.parent / _DEPLOY_STATS_NAME, [],
                            log_fn=log_fn)
        return 0, placed_lower

    _custom_backup_dir = filemap_path.parent / "custom_deploy_backup"
    _custom_log_path   = filemap_path.parent / "custom_deploy_log.txt"

    # Self-heal: a leftover custom_deploy_log.txt means the previous deploy
    # was never restored (crashed or failed restore).  Restore it now -
    # otherwise the rmtree below would destroy the backed-up originals.
    if _custom_log_path.is_file():
        _log("  Previous custom-deploy log still present - restoring it before redeploying.")
        from Utils.deployment.shared import cleanup_custom_deploy_dirs
        cleanup_custom_deploy_dirs(
            filemap_path.parent, [], log_fn=log_fn, filemap_path=filemap_path,
        )

    # Clear any stale backup from a previous deploy before we start, so we
    # never mix old backed-up originals with new ones (same pattern as
    # deploy_filemap_to_root).
    if _custom_backup_dir.exists():
        shutil.rmtree(_custom_backup_dir)

    def _write_custom_log(paths: "list[str]") -> None:
        try:
            if paths:
                # surrogateescape: custom-deploy log holds absolute dest paths
                # that carry surrogate-escaped on-disk filename bytes.
                _custom_log_path.write_text("\n".join(paths), encoding="utf-8",
                                            errors="surrogateescape")
            elif _custom_log_path.exists():
                _custom_log_path.unlink()
        except OSError:
            pass

    # Write the custom-deploy log BEFORE the first on-disk mutation (the
    # wholesale-replace pass below): if the deploy is interrupted, cleanup
    # still knows every custom location we may have touched.  Re-written
    # once the dir-symlink pass settles, and again after the transfers.
    _write_custom_log([dst for _src, dst, _r, is_c, _u, _o in tasks if is_c])

    import stat as _stat_module

    # Wholesale-replace pass: for every top-level folder that custom-deploy
    # mods are writing into, move the existing folder at the destination
    # (if any) into custom_deploy_backup/, mirroring the absolute path so
    # restore can put it back. This is the "Saves/ should replace, not
    # merge" rule for custom-deploy separators. Symlinks at that path are
    # unlinked instead of moved (they're our own from a previous deploy).
    _folders_replaced = 0
    for _eff_dir_s, _top_names in _custom_top_roots.items():
        # Resolve each top folder's actual on-disk casing so e.g. mod "Saves"
        # lands on disk-side "saves" if that's what already exists there.
        # (_resolve_root_path_str only case-resolves *directory* segments, so
        # a bare top-level name needs its own listing lookup.)
        _eff_listing = _dir_listing_cache.get(_eff_dir_s)
        if _eff_listing is None:
            _eff_listing = {}
            if os.path.isdir(_eff_dir_s):
                try:
                    with os.scandir(_eff_dir_s) as _it:
                        for _e in _it:
                            if _e.is_dir(follow_symlinks=False):
                                _eff_listing[_e.name.lower()] = _e.name
                except OSError:
                    pass
            _dir_listing_cache[_eff_dir_s] = _eff_listing
        for _top in _top_names:
            _existing_dir_str = (
                _eff_dir_s + "/" + _eff_listing.get(_top.lower(), _top)
            )
            try:
                _est = os.lstat(_existing_dir_str)
            except OSError:
                continue
            if _stat_module.S_ISLNK(_est.st_mode):
                # Stale symlink from a previous deploy - drop it.
                try:
                    os.unlink(_existing_dir_str)
                except OSError as exc:
                    _log(f"  WARN: could not remove stale symlink {_existing_dir_str}: {exc}")
                continue
            if not _stat_module.S_ISDIR(_est.st_mode):
                continue
            _existing_p = Path(_existing_dir_str)
            _bak_dir = _custom_backup_dir / _existing_p.relative_to(_existing_p.anchor)
            try:
                _move_crash_safe(_existing_dir_str, _bak_dir)
                _folders_replaced += 1
                _log(f"  Backed up existing folder {_existing_p.name}/ → custom_deploy_backup/")
            except OSError as exc:
                _log(f"  WARN: could not back up folder {_existing_dir_str}: {exc}")
            # Invalidate the caches so subsequent path resolution against this
            # destination doesn't reuse the now-moved entries.  Resolved-dir
            # keys are base + "\0" + rel_dir_lower; listing keys are absolute
            # directory paths.
            _rd_prefix = _eff_dir_s + "\x00" + _top.lower()
            for _k in [k for k in _resolved_dir_cache
                       if k == _rd_prefix or k.startswith(_rd_prefix + "/")]:
                del _resolved_dir_cache[_k]
            for _k in [k for k in _dir_listing_cache
                       if k == _existing_dir_str
                       or k.startswith(_existing_dir_str + "/")]:
                del _dir_listing_cache[_k]
            _eff_listing.pop(_top.lower(), None)

    # Directory-symlink pass: for every single-owner top-level folder we just
    # replaced above, drop a directory symlink <dest>/<top> → <staging>/<mod>/<src_top>.
    # New files written by the game land directly in the mod's staging dir
    # (no manual sync on restore needed). Tasks that fall under one of these
    # symlinked folders are excluded from the per-file deploy below.
    _dir_symlink_log: list[str] = []
    _skipped_task_prefixes: set[str] = set()
    for (_eff_dir_s, _top_lower), _owner in _top_folder_owner.items():
        if _owner is None:
            continue
        _owner_mode = _per_mode.get(_owner, mode)
        if _owner_mode is not LinkMode.SYMLINK:
            continue
        _sample = _top_folder_sample.get((_eff_dir_s, _top_lower))
        if _sample is None:
            continue
        _sample_src, _sample_rel, _sample_dst = _sample
        # Derive the source-side folder path: rel_str is e.g. "Saves/foo.ess"
        # and src_str ends in "<staging>/<mod>/<resolved>/Saves/foo.ess" (the
        # resolved part may include strip_prefix folders). Walk parents of
        # src_str up by the number of "/" components in rel_str minus one to
        # land on the source-side top-level folder.
        _rel_depth = _sample_rel.count("/")  # files-deep below the top folder
        _src_top = _sample_src
        for _ in range(_rel_depth):
            _src_top = os.path.dirname(_src_top)
        if not os.path.isdir(_src_top):
            # Couldn't resolve a real source directory - fall back to per-file.
            continue
        # Destination: derive from the sample task's resolved dst the same way,
        # so the symlink path casing matches the per-file tasks it replaces.
        _dst_top = _sample_dst
        for _ in range(_rel_depth):
            _dst_top = os.path.dirname(_dst_top)
        # The wholesale-replace pass above moved any vanilla folder away, so
        # the dest path should not exist; create the parent dir, then symlink.
        try:
            os.makedirs(os.path.dirname(_dst_top), exist_ok=True)
            # Defensive: drop a leftover empty dir or stale symlink at the spot
            try:
                _existing_st = os.lstat(_dst_top)
                if _stat_module.S_ISLNK(_existing_st.st_mode):
                    os.unlink(_dst_top)
                elif _stat_module.S_ISDIR(_existing_st.st_mode):
                    try:
                        os.rmdir(_dst_top)
                    except OSError:
                        # Non-empty - bail; per-file deploy will handle it.
                        continue
            except OSError:
                pass
            os.symlink(_src_top, _dst_top)
            _dir_symlink_log.append(_dst_top)
            _skipped_task_prefixes.add(_dst_top.rstrip("/") + "/")
            _log(f"  Symlinked folder {os.path.basename(_dst_top)}/ → {_src_top}")
        except OSError as exc:
            _log(f"  WARN: could not symlink folder {_dst_top}: {exc}")

    # Filter out tasks whose destination falls under a directory-symlinked
    # folder - they're already covered by the symlink. Their rel paths are
    # marked as "placed" so deploy_core() doesn't try to provide a vanilla
    # fallback for them.
    if _skipped_task_prefixes:
        def _under_symlinked(dst: str) -> bool:
            for _pfx in _skipped_task_prefixes:
                if dst.startswith(_pfx):
                    return True
            return False
        before_count = len(tasks)
        kept_tasks: list[tuple[str, str, str, bool, bool, "LinkMode | None"]] = []
        for t in tasks:
            if _under_symlinked(t[1]):
                placed_lower.add(t[2])
            else:
                kept_tasks.append(t)
        tasks = kept_tasks
        _timing_print(
            f"  [TIMER][FS I/O] deploy_filemap - directory-symlink pass: skipped "
            f"{before_count - len(tasks)} per-file task(s) under "
            f"{len(_skipped_task_prefixes)} folder symlink(s).")
    if _dir_symlink_log or _skipped_task_prefixes:
        # Refresh the early log now that the dir-symlink pass settled the
        # final custom task list (and created symlinks cleanup must remove).
        _write_custom_log(
            [dst for _src, dst, _r, is_c, _u, _o in tasks if is_c]
            + _dir_symlink_log
        )
    total = len(tasks)

    if _progress is not None:
        _progress(total_lines, total_lines + total,
                  "Preparing destination folders…")

    # Up-front free-space check for explicit copy-mode tasks - abort before
    # touching the game dir rather than filling the drive mid-deploy.
    # (Hardlink/symlink fallbacks that end in copy are caught by the ENOSPC
    # abort in the transfer loop instead.)
    _copy_bytes = 0
    for _src_s, _dst_s, _rl, _ic, _use_sym, _ov in tasks:
        _eff = LinkMode.SYMLINK if _use_sym else (_ov if _ov is not None else mode)
        if _eff is LinkMode.COPY:
            try:
                _copy_bytes += os.stat(_src_s).st_size
            except OSError:
                pass
    if _copy_bytes:
        try:
            _vfs = os.statvfs(str(deploy_dir))
            _free = _vfs.f_frsize * _vfs.f_bavail
        except OSError:
            _free = None
        if _free is not None and _copy_bytes > _free:
            raise OSError(
                errno.ENOSPC,
                f"Not enough free space on the game drive: this deploy needs "
                f"~{_copy_bytes // (1024 * 1024)} MB copied but only "
                f"{_free // (1024 * 1024)} MB is free. Free up space, then "
                f"deploy again (or run Restore).",
            )

    _log_case_collisions(_dir_listing_cache, _log)

    # Pre-create all destination directories up front (single-threaded) to
    # avoid mkdir races inside the thread pool.
    with _timer("deploy_filemap - mkdir"):
        needed_dirs: set[str] = {os.path.dirname(dst) for _, dst, _, _is_custom, _, _ in tasks}
        _mkdir_leaves(needed_dirs)

    # Back up any pre-existing files at custom deploy locations so restore can
    # put the originals back.  Mirror each dst's absolute path as a relative
    # path inside _custom_backup_dir (strip leading slash) so structure is
    # preserved and files with the same name in different dirs never collide.
    # One lstat per task instead of islink+isfile (two stat-equivalent calls).
    # Files whose top-level folder was already wholesale-replaced above will
    # no longer exist here - the lstat just no-ops and the loop moves on.
    for _src_s, dst_s, _rel_lower, is_custom, _use_sym, _ov in tasks:
        if not is_custom:
            continue
        try:
            _st = os.lstat(dst_s)
        except OSError:
            continue
        if _stat_module.S_ISLNK(_st.st_mode):
            os.unlink(dst_s)
        elif _stat_module.S_ISREG(_st.st_mode):
            dst_p = Path(dst_s)
            bak = _custom_backup_dir / dst_p.relative_to(dst_p.anchor)
            _move_crash_safe(dst_s, bak)
            _log(f"  Backed up existing {os.path.basename(dst_s)} → custom_deploy_backup/")

    if replace_existing:
        # Custom-rule placement runs before ordinary filemap placement in both
        # the physical root flow and the VFS builder. Physical root deploy
        # replaces an earlier routed target; the synthetic layer must do the
        # same instead of letting os.link fail with EEXIST. Custom/external
        # destinations already use the backup-and-replace loop above.
        for _src_s, dst_s, _rel_lower, is_custom, _use_sym, _ov in tasks:
            if is_custom:
                continue
            try:
                _st = os.lstat(dst_s)
            except OSError:
                continue
            if (_stat_module.S_ISREG(_st.st_mode)
                    or _stat_module.S_ISLNK(_st.st_mode)):
                os.unlink(dst_s)

    linked = 0
    done_count = 0
    fallback_before = _fallback_snapshot()

    _stats_plen = len(_deploy_dir_str) + 1

    def _do_transfer(item: tuple[str, str, str, bool, bool, "LinkMode | None"]) -> tuple[str | None, "LinkMode | None", tuple[str, OSError] | None, str | None]:
        src, dst, rel_lower, _is_custom, use_symlink, override_mode = item
        if use_symlink:
            effective_mode = LinkMode.SYMLINK
        elif override_mode is not None:
            effective_mode = override_mode
        else:
            effective_mode = mode
        actual, err = _do_link_ex(src, dst, effective_mode)
        if err is None:
            # Capture the deploy-stats entry in-worker (parallel) instead of a
            # serial post-pass: one lstat per placed regular file in the main
            # deploy dir.  Symlinks are recognised by d_type on restore and
            # custom-location files are tracked by the custom log, so neither
            # needs a stats record (matches the old post-pass filters).
            stats_line: str | None = None
            if not _is_custom and actual is not LinkMode.SYMLINK:
                try:
                    _dst_st = os.lstat(dst)
                    if _stat_module.S_ISREG(_dst_st.st_mode):
                        stats_line = (f"{dst[_stats_plen:]}\t{_dst_st.st_size}"
                                      f"\t{_dst_st.st_mtime_ns}\n")
                except OSError:
                    pass
            return rel_lower, actual, None, stats_line
        return None, None, (dst, err), None

    # Per-mode tally so we can report when files were copied/symlinked instead
    # of hardlinked (a common cause of "mods not loading" when game and staging
    # live on different filesystems).
    mode_counts: dict[LinkMode, int] = {}
    _stats_entries: list[str] = []
    _t_transfer = _time.perf_counter()
    def _transfer_fatal(result) -> bool:
        err = result[2]
        return (err is not None
                and getattr(err[1], "errno", None) == errno.ENOSPC)

    for result, actual, err, stats_line in _iter_map_batched(
            _do_transfer, tasks, stop_on=_transfer_fatal):
        done_count += 1
        if result is not None:
            placed_lower.add(result)
            linked += 1
            if actual is not None:
                mode_counts[actual] = mode_counts.get(actual, 0) + 1
            if stats_line is not None:
                _stats_entries.append(stats_line)
        elif err is not None:
            dst_err, exc = err
            if getattr(exc, "errno", None) == errno.ENOSPC:
                # Drive full - stop immediately instead of spamming a
                # WARN per remaining file and "succeeding" half-deployed.
                _log(f"  ERROR: game drive is full - aborting deploy "
                     f"(failed at {dst_err}). Free up space, then run "
                     f"Restore and deploy again.")
                raise OSError(errno.ENOSPC,
                              f"Game drive full while deploying {dst_err}")
            _log(f"  WARN: could not transfer {dst_err}: {exc}")
        if (_progress is not None
                and (done_count % 200 == 0 or done_count == total)):
            _progress(total_lines + done_count, total_lines + total,
                      "Transferring mod files…")
    _timing_print(
        f"  [TIMER][FS I/O] deploy_filemap - transfer {total} files: "
        f"{_time.perf_counter() - _t_transfer:.3f}s")

    _report_mode_breakdown(_log, mode_counts, mode)
    _report_fallbacks(_log, fallback_before)

    # Deploy-stats record (size, mtime_ns) of every regular file placed in the
    # main deploy dir - captured in-worker during the transfer above - so
    # restore_data_core can tell superseded deployed copies (mod replaced/
    # removed while deployed) apart from files written after deploy.
    _write_deploy_stats(filemap_path.parent / _DEPLOY_STATS_NAME,
                        _stats_entries, log_fn=log_fn)

    # Write a log of files placed in custom locations so cleanup knows what to
    # remove.  Each line is the absolute path of a deployed file (or a
    # directory symlink we created via the dir-symlink pass).
    custom_deployed = [
        dst
        for _src, dst, rel_lower, is_custom, _use_sym, _ov in tasks
        if is_custom and rel_lower in placed_lower
    ]
    custom_deployed.extend(_dir_symlink_log)
    _write_custom_log(custom_deployed)

    return linked, placed_lower


# ---------------------------------------------------------------------------
# Step 3 - fill gaps with vanilla files from the backup
# ---------------------------------------------------------------------------

def deploy_core(
    deploy_dir: Path,
    already_placed: set[str],
    core_dir: Path | None = None,
    mode: LinkMode = LinkMode.HARDLINK,
    log_fn=None,
    progress_fn=None,
    manifest_dir: Path | None = None,
) -> int:
    """Transfer files from core_dir into deploy_dir for any path not already
    covered by a mod.

    deploy_dir     - destination (e.g. <game_path>/Data)
    already_placed - lowercased rel paths already placed by deploy_filemap()
    core_dir       - vanilla backup directory; defaults to Data_Core/ sibling
    progress_fn    - optional callable(done: int, total: int)
    manifest_dir   - directory to write vanilla_deployed.txt into (the profile
                     root, alongside filemap.txt). When None the manifest is
                     skipped - pass it for games whose Data/ is symlink-deployed
                     so restore_data_core can rescue externally-edited vanilla
                     files (see _VANILLA_DEPLOYED_NAME).
    Returns the number of files transferred.
    """
    _log = _safe_log(log_fn)
    core_dir = core_dir or _default_core(deploy_dir)

    # Incremental fast path: the diff in deploy_filemap already refilled any
    # vanilla gaps (and owns the vanilla_deployed.txt manifest) - skip.
    from Utils.deployment import incremental as _incr
    if _incr.active_for(deploy_dir) is not None:
        return 0

    if not core_dir.is_dir():
        return 0

    # Use os.walk to collect files - avoids per-file stat() that rglob+is_file does.
    _core_str = str(core_dir)
    _core_prefix_len = len(_core_str) + 1  # +1 for the trailing separator

    _t_core_walk = _time.perf_counter()
    tasks_core: list[tuple[str, str]] = []  # (src_str, rel_str)
    for dirpath, _dirnames, filenames in os.walk(_core_str):
        for fname in filenames:
            src_str = dirpath + "/" + fname
            rel_str = src_str[_core_prefix_len:]
            if rel_str.replace("\\", "/").lower() not in already_placed:
                tasks_core.append((src_str, rel_str))
    _timing_print(
        f"  [TIMER][FS I/O + CPU] deploy_core - walk + filter: "
        f"{_time.perf_counter() - _t_core_walk:.3f}s")

    if not tasks_core:
        return 0

    total = len(tasks_core)

    # Resolve destination paths using case-insensitive directory matching so
    # that core files (e.g. Data_Core/Scripts/) merge into any same-name
    # directory already created by mods (e.g. Data/scripts/) rather than
    # producing a duplicate folder with different casing.
    _deploy_dir_str = str(deploy_dir)
    _dir_listing_cache: dict[str, dict[str, str]] = {}
    _resolved_dir_cache: dict[str, str] = {}
    resolved_tasks: list[tuple[str, str]] = []  # (src_str, dst_str)
    for src_str, rel_str in tasks_core:
        dst_str = _resolve_root_path_str(_deploy_dir_str, rel_str,
                                         _dir_listing_cache,
                                         resolved_dir_cache=_resolved_dir_cache)
        resolved_tasks.append((src_str, dst_str))

    # Deduplicate destination directories with a set before creating them.
    needed_dirs: set[str] = set()
    for _, dst_str in resolved_tasks:
        needed_dirs.add(os.path.dirname(dst_str))
    _mkdir_leaves(needed_dirs)

    linked = 0
    done_count = 0
    fallback_before = _fallback_snapshot()

    def _do_core(item: tuple[str, str]) -> tuple["LinkMode | None", str, OSError | None]:
        src, dst_str = item
        actual, err = _do_link_ex(src, dst_str, mode)
        return (actual, dst_str, None) if err is None else (None, dst_str, err)

    mode_counts: dict[LinkMode, int] = {}
    _deploy_plen = len(_deploy_dir_str) + 1
    # Vanilla files placed as symlinks point straight into Data_Core/.  An
    # external tool editing Data/ (e.g. xEdit) can follow the symlink and
    # mangle the core copy, so we record these paths for the restore-side
    # rescue.  Hardlink/copy placements own an independent inode and don't
    # need recording (restore's core_lower/inode checks already cover them).
    _vanilla_symlinked: list[str] = []
    _t_core_transfer = _time.perf_counter()
    def _core_fatal(result) -> bool:
        return getattr(result[2], "errno", None) == errno.ENOSPC

    for actual, dst_str, exc in _iter_map_batched(
            _do_core, resolved_tasks, stop_on=_core_fatal):
        done_count += 1
        if actual is not None:
            linked += 1
            mode_counts[actual] = mode_counts.get(actual, 0) + 1
            if actual == LinkMode.SYMLINK:
                _vanilla_symlinked.append(dst_str[_deploy_plen:])
        else:
            if getattr(exc, "errno", None) == errno.ENOSPC:
                _log(f"  ERROR: game drive is full - aborting deploy "
                     f"(failed at {dst_str}). Free up space, then run "
                     f"Restore and deploy again.")
                raise OSError(errno.ENOSPC,
                              f"Game drive full while deploying {dst_str}")
            _log(f"  WARN: could not transfer {dst_str}: {exc}")
        if progress_fn is not None:
            progress_fn(done_count, total)
    _timing_print(
        f"  [TIMER][FS I/O] deploy_core - transfer {total} files: "
        f"{_time.perf_counter() - _t_core_transfer:.3f}s")

    # The manifest must land in the profile root (beside filemap.txt) where
    # restore_data_core looks for it - NOT next to deploy_dir, which lives in
    # the game install.  Only written when the caller opts in via manifest_dir.
    if manifest_dir is not None:
        _write_vanilla_deployed(
            manifest_dir / _VANILLA_DEPLOYED_NAME, _vanilla_symlinked, log_fn=log_fn)

    _report_mode_breakdown(_log, mode_counts, mode)
    _report_fallbacks(_log, fallback_before)

    return linked


# ---------------------------------------------------------------------------
# Restore - undo a deploy
# ---------------------------------------------------------------------------

def restore_data_core(
    deploy_dir: Path,
    core_dir: Path | None = None,
    overwrite_dir: Path | None = None,
    staging_root: Path | None = None,
    strip_prefixes: set[str] | None = None,
    index_path: Path | None = None,
    log_fn=None,
    restore_whitelist=None,
    game=None,
    profile_dir: Path | None = None,
) -> int:
    """Undo a deploy: clear deploy_dir and move core_dir contents back.

    deploy_dir     - directory to restore (e.g. <game_path>/Data)
    core_dir       - vanilla backup to restore from; defaults to Data_Core/ sibling
    overwrite_dir  - if given, any file in deploy_dir that is not a deployed mod
                     file and not present in core_dir (i.e. created at runtime by
                     the game or a mod) is moved here before clearing, preserving
                     its relative path.  Existing files in overwrite_dir are
                     overwritten.  Pass Profiles/<game>/overwrite/.
    staging_root, strip_prefixes, and index_path are retained temporarily for
                     handler API compatibility. Restore ownership and exact
                     staged source paths come exclusively from the last
                     committed Filegraph deployment generation.
    restore_whitelist - optional matcher (over lowercased deploy_dir-relative
                     paths) whose matches are kept in the game folder: moved
                     into core_dir so the swap restores them in place, never
                     rescued to overwrite/ or a mod folder.
    Returns the number of files restored.

    If core_dir does not exist (e.g. the deploy dir was empty at deploy time
    so move_to_core skipped creating it), the deploy dir is simply cleared and
    0 is returned - no error is raised.
    """
    _log = _safe_log(log_fn)
    core_dir = core_dir or _default_core(deploy_dir)
    sweep_deploy_trash(deploy_dir.parent, log_fn=log_fn)

    if not core_dir.is_dir():
        _log(f"  No {core_dir.name}/ found - nothing to restore (skipping).")
        return 0

    # Rescue runtime-created files into overwrite/ before wiping deploy_dir.
    # A file is runtime-created if it:
    #   - is not a symlink (symlinks are deployed mod files)
    #   - has a single hard-link count (nlink > 1 means it is a deployed hardlink)
    #   - no longer matches the (size, mtime) recorded for it in
    #     deploy_stats.txt at deploy time (a still-matching file is the
    #     untouched deployed copy - discarded even when its mod was replaced
    #     or removed while deployed, so dropped files don't pollute overwrite/)
    #   - is not present in core_dir (not a vanilla file)
    #   - is not owned by the committed Filegraph deployment generation
    #
    # Exception: committed deployment files are rescued to their exact catalogued
    # source when an external tool edits the deployed copy or removes its staged
    # source. This handles stripped wrapper folders without reconstructing paths.
    # If the rescue walk runs it builds core_path as a side-effect, which
    # gives us the file count for free.  -1 is the sentinel for "rescue walk
    # didn't run, fall back to a dedicated count walk below".
    restored = -1
    if overwrite_dir is not None and deploy_dir.is_dir():
        # Build core_lower/core_path using os.walk. Core metadata is loaded
        # lazily below only for a single-link vanilla-path candidate: normal
        # hardlinks/symlinks never need it, so eagerly lstat-ing every vanilla
        # file made an ordinary restore pay for thousands of unused syscalls.
        _t_rescue_start = _time.perf_counter()

        def _lstat_or_none(p: str) -> "os.stat_result | None":
            try:
                return os.lstat(p)
            except OSError:
                return None

        _core_str = str(core_dir)
        _core_plen = len(_core_str) + 1
        core_lower: set[str] = set()
        core_stat: dict[str, tuple[int, int, int]] = {}
        core_path: dict[str, str] = {}
        for _dp, _dns, _fns in os.walk(_core_str):
            for _fn in _fns:
                _cp = _dp + "/" + _fn
                _rel = _cp[_core_plen:].lower()
                core_lower.add(_rel)
                core_path[_rel] = _cp
        _t_phase = _time.perf_counter()
        _timing_print(
            f"  [RESTORE-TIMING][FS I/O] core inventory: "
            f"{_t_phase - _t_rescue_start:.3f}s ({len(core_path)} files)")
        catalog_lower: set[str] = set()
        # Keep raw source bytes here and construct a Path only for the rare
        # modified/single-link file that actually needs rescuing. Eagerly
        # materialising one Path for every deployed winner was pure overhead
        # for the normal hardlink restore, where nlink>1 rejects all of them.
        catalog_sources: dict[str, tuple[str, bytes]] = {}
        catalog_root_lower: set[str] = set()
        _catalog_loaded = False

        def _ensure_catalog() -> None:
            """Load ownership only after a genuinely ambiguous file appears."""
            nonlocal _catalog_loaded
            if _catalog_loaded:
                return
            _catalog_loaded = True
            _t_catalog_start = _time.perf_counter()
            if game is not None and profile_dir is not None:
                try:
                    from Utils.filegraph.deploy import (
                        deployed_entries_for, entry_relative_to)
                    _catalog_target_roots: dict[str, str] = {}
                    for _entry in deployed_entries_for(game, profile_dir):
                        _relative = entry_relative_to(
                            game, _entry, deploy_dir, _catalog_target_roots)
                        if _relative is None:
                            continue
                        _relative_lower = _relative.lower()
                        if _entry.provider_kind == "root":
                            catalog_root_lower.add(_relative_lower)
                        else:
                            catalog_lower.add(_relative_lower)
                            catalog_sources[_relative_lower] = (
                                _entry.mod_name,
                                _entry.source_rel,
                            )
                except Exception as _catalog_error:
                    _log(f"  WARN: could not load deployed catalog state: "
                         f"{_catalog_error}")
            _timing_print(
                f"  [RESTORE-TIMING][DB I/O + CPU] deployed catalog projection (lazy): "
                f"{_time.perf_counter() - _t_catalog_start:.3f}s "
                f"({len(catalog_lower)} Data entries, "
                f"{len(catalog_root_lower)} root entries)")
        # Files owned by root-flagged mods may target paths
        # *inside* deploy_dir - e.g. a root-flagged mod that ships its own
        # Data/Fallout4.esm deploys to <game>/Data/Fallout4.esm, the same path
        # a vanilla file occupies.  Those files are backed up to and restored
        # from Root_Backup/ by restore_root_folder(); they are NOT edited
        # vanilla.  Without this set the rescue walk below sees a regular file
        # (nlink==1 under copy/cross-device deploy) sitting at a core_lower path
        # whose inode/size differs from the vanilla backup, mistakes it for an
        # xEdit-cleaned vanilla plugin, and os.replace()s it OVER the vanilla
        # copy in core_dir - destroying the only good backup.  Record their
        # deploy-dir-relative paths so the walk leaves them for the root
        # restore. Root destinations are full game-root paths (e.g.
        # "Data/Fallout4.esm"); strip the deploy_dir's name to match rel_lower.
        # Vanilla files deployed as symlinks into core_dir.  If such a file was
        # edited in place by an external tool (xEdit), the tool may have
        # destroyed core_dir's copy via the symlink, so it won't be in
        # core_lower anymore.  This manifest lets us still recognise it as
        # edited vanilla and put it back in deploy_dir instead of overwrite/.
        vanilla_symlinked = _load_vanilla_deployed(
            overwrite_dir.parent / _VANILLA_DEPLOYED_NAME)
        # Deploy-time stat record - a regular file still matching its entry is
        # exactly what we deployed, so the staging side (or nothing, if the
        # mod was replaced/removed since) owns the data and the copy in
        # deploy_dir is safe to discard with the rmtree below.  Checked before
        # the filemap/modindex tests so a mod version swapped out while
        # deployed doesn't leave its dropped files rescued into overwrite/.
        deploy_stats = _load_deploy_stats(
            overwrite_dir.parent / _DEPLOY_STATS_NAME)
        _t_aux = _time.perf_counter()
        _timing_print(
            f"  [RESTORE-TIMING][FS I/O] restore manifests/stats: "
            f"{_t_aux - _t_phase:.3f}s")
        rescued = 0
        rescued_to_mod = 0
        rescued_to_overwrite = 0
        rescued_edited_vanilla = 0
        kept_whitelisted = 0
        discarded_empty = 0
        # Track rescued overwrite paths so the catalog can be refreshed once.
        rescued_overwrite_rels: list[str] = []
        _deploy_str = str(deploy_dir)
        _deploy_plen = len(_deploy_str) + 1
        _overwrite_str = str(overwrite_dir)
        _lstat = os.lstat
        # Phase 1 - collect candidates with an os.scandir walk.  DirEntry
        # is_symlink()/is_file() use d_type from readdir on Linux - no extra
        # syscall - so symlinks (deployed mod files) are skipped for free.
        # Only non-symlink regular files need a real lstat() to check
        # st_nlink; in hardlink mode that is every remaining file in the
        # deploy dir, so phase 2 runs those stats in parallel.
        _scandir = os.scandir
        _walk_stack = [_deploy_str]
        _candidates: list[str] = []
        while _walk_stack:
            _cur_dir = _walk_stack.pop()
            try:
                _scan_it = _scandir(_cur_dir)
            except OSError:
                continue
            with _scan_it:
                for _de in _scan_it:
                    if _de.is_dir(follow_symlinks=False):
                        _walk_stack.append(_de.path)
                        continue
                    if _de.is_symlink():
                        continue  # deployed mod symlink - free check via d_type
                    if not _de.is_file(follow_symlinks=False):
                        continue
                    _candidates.append(_de.path)
        _t_scan = _time.perf_counter()
        _timing_print(
            f"  [RESTORE-TIMING][FS I/O] deployed directory scan: "
            f"{_t_scan - _t_aux:.3f}s ({len(_candidates)} regular files)")

        # Phase 2 - parallel lstat over the collected candidates (batched:
        # per-item pool dispatch costs more than the lstat itself).
        _stat_pairs: "list[tuple[str, os.stat_result]]" = []
        for _cand, _cst in zip(_candidates,
                               _map_batched(_lstat_or_none, _candidates)):
            if _cst is not None:
                _stat_pairs.append((_cand, _cst))
        _t_stat = _time.perf_counter()
        _timing_print(
            f"  [RESTORE-TIMING][FS I/O] deployed lstat batch: "
            f"{_t_stat - _t_scan:.3f}s ({len(_stat_pairs)} files)")

        # Phase 3 - serial decision loop (rescues/moves are rare; the logic
        # below is unchanged from the interleaved walk it replaces).
        for src_str, st in _stat_pairs:
            if st.st_nlink > 1:
                continue  # deployed mod hardlink
            rel_str = src_str[_deploy_plen:]
            if rel_str == _DEPLOY_MARKER_NAME:
                continue  # our own deploy marker - removed with deploy_dir
            rel_lower = rel_str.lower()
            # xEdit deferred-save temp (…esp.save.<timestamp>): its queued
            # rename to the real plugin name lost the race with our walk.
            # Re-point rel_str/rel_lower at the base plugin so the same
            # committed ownership/source logic below routes the cleaned file
            # back to its owning mod, and finish xEdit's rename ourselves.
            _save_m = _XEDIT_SAVE_TEMP_RE.match(rel_str)
            if _save_m is not None:
                _base_rel = _save_m.group("base")
                _base_lower = _base_rel.lower()
                # Only adopt the base name when it's a plugin we recognise
                # (mod-owned or vanilla) - otherwise leave the temp alone
                # for the normal runtime-file handling.
                _base_known = (_base_lower in core_lower
                               or _base_lower in vanilla_symlinked)
                if not _base_known:
                    _ensure_catalog()
                    _base_known = _base_lower in catalog_lower
                if _base_known:
                    _base_dst = _deploy_str + "/" + _base_rel
                    try:
                        # Complete the deferred rename. os.replace
                        # overwrites any half-state at the base path
                        # (e.g. a stale symlink xEdit left behind).
                        os.replace(src_str, _base_dst)
                        src_str = _base_dst
                        st = _lstat(src_str)
                        rel_str = _base_rel
                        rel_lower = _base_lower
                    except OSError:
                        pass
            _ds = deploy_stats.get(rel_lower)
            if (_ds is not None and st.st_size == _ds[0]
                    and abs(st.st_mtime_ns - _ds[1]) <= _MTIME_TOLERANCE_NS):
                continue  # unmodified deployed file - discard, don't rescue
            _ensure_catalog()
            if rel_lower in catalog_root_lower:
                # Root-flagged mod file deployed into deploy_dir (e.g. a
                # mod shipping its own Data/Fallout4.esm).  Owned by
                # restore_root_folder() via Root_Backup/ - never treat
                # as edited vanilla (which would overwrite core_dir's
                # real backup).  Leave it for the rmtree below; the root
                # restore puts the genuine vanilla copy back.
                continue
            if rel_lower in core_lower:
                # Vanilla path - but the file might have been replaced by
                # an external tool (e.g. xEdit Quick Auto Clean deletes
                # the symlink/hardlink and writes a fresh file).  If the
                # on-disk file no longer matches the core backup by inode
                # or by (size, mtime), overwrite the core copy with the
                # edited file so the rmtree+rename below restores the
                # edited vanilla plugin back into Data/.
                _cs = core_stat.get(rel_lower)
                if _cs is None:
                    _core_src = core_path.get(rel_lower)
                    _raw_cs = (_lstat_or_none(_core_src)
                               if _core_src is not None else None)
                    if _raw_cs is not None:
                        _cs = (_raw_cs.st_ino, _raw_cs.st_size,
                               _raw_cs.st_mtime_ns)
                        core_stat[rel_lower] = _cs
                if _cs is not None:
                    _core_ino, _core_sz, _core_mt = _cs
                    if (st.st_ino == _core_ino or
                        (st.st_size == _core_sz and st.st_mtime_ns == _core_mt)):
                        continue  # untouched vanilla - restore from core
                    if st.st_size == 0 and _core_sz > 0:
                        # A 0-byte file is never a legitimate edit (xEdit
                        # can't save an empty plugin)
                        _log(f"  WARN: deployed {rel_str} is 0 bytes "
                             f"(runtime-damaged?) - discarded; keeping the "
                             f"vanilla backup.")
                        discarded_empty += 1
                        continue
                    core_dst = core_path.get(rel_lower)
                    if core_dst is not None:
                        try:
                            os.replace(src_str, core_dst)
                            rescued += 1
                            rescued_edited_vanilla += 1
                        except OSError:
                            pass
                    continue
                continue  # vanilla file - will be restored from core
            if rel_lower in vanilla_symlinked:
                # Symlink-mode vanilla file edited in place by an external
                # tool: the symlink let the tool reach through and destroy
                # core_dir's copy, so it's no longer in core_lower.  The
                # regular file now sitting here IS the edited vanilla
                # plugin - move it into core_dir at its rel path so the
                # rmtree+rename below restores it to deploy_dir rather
                # than burying it in overwrite/.
                if st.st_size == 0:
                    # Never restore a 0-byte "edited vanilla" - that's
                    # runtime damage, not an edit (see GH#307 note above).
                    _log(f"  WARN: deployed {rel_str} is 0 bytes "
                         f"(runtime-damaged?) - discarded; verify game "
                         f"files to restore the vanilla copy.")
                    discarded_empty += 1
                    continue
                core_dst = _core_str + "/" + rel_str
                try:
                    os.makedirs(os.path.dirname(core_dst), exist_ok=True)
                    os.replace(src_str, core_dst)
                    rescued += 1
                    rescued_edited_vanilla += 1
                    # Keep core_path in sync so the len()-based restore
                    # count below includes this re-added file.
                    core_path[rel_lower] = core_dst
                except OSError:
                    pass
                continue
            if (is_global_restore_ignored(rel_lower)
                    or (restore_whitelist is not None
                        and restore_whitelist(rel_lower))):
                # Keep whitelisted files in the game folder by moving them into
                # core_dir so the swap below restores them in place (skipping
                # would delete them with deploy_dir).  Sync core_path so the
                # len()-based restore count includes them.
                core_dst = _core_str + "/" + rel_str
                try:
                    os.makedirs(os.path.dirname(core_dst), exist_ok=True)
                    os.replace(src_str, core_dst)
                    core_path[rel_lower] = core_dst
                    kept_whitelisted += 1
                except OSError:
                    pass
                continue
            source_info = catalog_sources.get(rel_lower)
            if rel_lower in catalog_lower and source_info is not None:
                target_mod, source_rel = source_info
                from Utils.filegraph.service import source_path
                staging_path = source_path(game, target_mod, source_rel)
                try:
                    _sst = os.lstat(staging_path)
                    # Tolerate coarse-timestamp filesystems truncating the
                    # mtime preserved by a copy-mode deployment.
                    if (_sst.st_size == st.st_size
                            and abs(_sst.st_mtime_ns - st.st_mtime_ns)
                            <= _MTIME_TOLERANCE_NS):
                        continue
                except OSError:
                    pass
                if st.st_size == 0:
                    _log(f"  WARN: deployed {rel_str} is 0 bytes "
                         f"(runtime-damaged?) - discarded; keeping any "
                         f"catalogued staging copy in '{target_mod}'.")
                    discarded_empty += 1
                    continue
                _move_crash_safe(src_str, staging_path)
                rescued += 1
                if target_mod == _OVERWRITE_NAME:
                    rescued_to_overwrite += 1
                    rescued_overwrite_rels.append(rel_str)
                else:
                    rescued_to_mod += 1
                    if rel_str.lower().endswith(_PLUGIN_EXTS):
                        _tag_mod_xedit_modified(
                            Path(game.get_effective_mod_staging_path()) / target_mod,
                            os.path.basename(rel_str),
                        )
                continue
            if rel_lower in catalog_lower:
                # The committed generation owns this path but its source could
                # not be decoded. Preserve the deployed-file discard behavior.
                continue
            # Genuine runtime-generated file (never in a mod) - goes to overwrite
            dst_str = _overwrite_str + "/" + rel_str
            _move_crash_safe(src_str, dst_str)
            rescued += 1
            rescued_to_overwrite += 1
            rescued_overwrite_rels.append(rel_str)
        _t_decide = _time.perf_counter()
        _timing_print(
            f"  [RESTORE-TIMING][CPU + FS I/O] rescue decisions/moves: "
            f"{_t_decide - _t_stat:.3f}s")
        if not _catalog_loaded:
            _timing_print(
                "  [RESTORE-TIMING][DB I/O] deployed catalog projection: skipped "
                "(no ambiguous files)")
        if rescued:
            if rescued_to_mod:
                _log(f"  Rescued {rescued_to_mod} file(s) back to mod folder(s).")
            if rescued_to_overwrite:
                _log(f"  Rescued {rescued_to_overwrite} runtime-created file(s) → overwrite/.")
            if rescued_edited_vanilla:
                _log(f"  Preserved {rescued_edited_vanilla} edited vanilla file(s) (e.g. xEdit-cleaned).")
            if rescued_overwrite_rels:
                _append_overwrite_log(overwrite_dir, rescued_overwrite_rels, _log)
            if rescued_overwrite_rels:
                try:
                    if game is not None and profile_dir is not None:
                        from Utils.filegraph.service import FileGraphService
                        _library = FileGraphService.open_library(
                            game, profile_dir, log_fn=_log)
                        _library.update_mod_from_disk_if_changed(
                            profile_dir, _OVERWRITE_NAME)
                except Exception as _catalog_error:
                    _log(f"  WARN: could not refresh rescued overwrite files "
                         f"in the catalog: {_catalog_error}")
        if kept_whitelisted:
            _log(f"  Left {kept_whitelisted} whitelisted file(s) in the game folder.")
        if discarded_empty:
            _log(f"  Discarded {discarded_empty} runtime-damaged 0-byte file(s) "
                 f"(kept the good staging/backup copies).")
        _timing_print(
            f"  [TIMER][CPU + FS I/O] restore - rescue walk: "
            f"{_time.perf_counter() - _t_rescue_start:.3f}s")
        # core_path was populated by the rescue walk above - one entry per
        # core file, so len() is our return-value count without a second walk.
        restored = len(core_path)

    # Fallback count walk - only runs when the rescue walk above was skipped
    # (overwrite_dir is None, or deploy_dir doesn't exist).
    if restored < 0:
        with _timer("restore - count core files"):
            _core_str2 = str(core_dir)
            restored = 0
            for _dp2, _dns2, _fns2 in os.walk(_core_str2):
                restored += len(_fns2)

    # Swap-and-defer: rename deploy_dir to a unique trash sibling (O(1), same
    # filesystem), rename core_dir into place, then delete the trash in a
    # background thread - restore latency stops scaling with the number of
    # deployed files.  Leftover trash from a crash is removed by
    # sweep_deploy_trash (start of move_to_core/restore, and app startup).
    with _timer("restore - swap dirs"):
        merge_needed = False
        if deploy_dir.is_dir():
            trash = deploy_dir.parent / (
                f"{deploy_dir.name}{_TRASH_INFIX}{_time.time_ns()}")
            try:
                os.rename(deploy_dir, trash)
                _delete_trash_in_background(str(trash))
                _log(f"  Cleared {deploy_dir.name}/ (old files are deleted "
                     f"in the background).")
            except OSError:
                # Rename refused (e.g. exotic mount) - fall back to the old
                # foreground rmtree.  A partial rmtree failure (EACCES/EBUSY)
                # must NOT escape here: that would abort the restore with a
                # half-deleted deploy dir AND the vanilla files still
                # stranded in core_dir.  Merge them back over the leftovers
                # instead.
                try:
                    shutil.rmtree(deploy_dir)
                    _log(f"  Cleared {deploy_dir.name}/.")
                except OSError as rm_err:
                    if deploy_dir.exists():
                        merge_needed = True
                        _log(f"  ERROR: could not fully clear "
                             f"{deploy_dir.name}/ ({rm_err}) - restoring the "
                             f"vanilla files over the leftovers.")
                    else:
                        _log(f"  Cleared {deploy_dir.name}/.")
        if merge_needed:
            _merge_restore_core(core_dir, deploy_dir, _log)
        else:
            shutil.move(str(core_dir), str(deploy_dir))

    return restored


def _merge_restore_core(core_dir: Path, deploy_dir: Path, _log) -> None:
    """Move core_dir's files into a partially-cleared deploy_dir one by one.

    Fallback for when the whole-dir swap in restore_data_core could not run
    (deploy_dir could not be fully deleted).  Each vanilla file replaces any
    leftover at its path; files that cannot be moved stay safely in core_dir
    (which is only removed once it has been fully emptied) so a later restore
    can retry them.
    """
    failed = 0
    core_str = str(core_dir)
    deploy_str = str(deploy_dir)
    for dp, _dns, fns in os.walk(core_str):
        rel_dp = os.path.relpath(dp, core_str)
        dst_dp = deploy_str if rel_dp == "." else os.path.join(deploy_str, rel_dp)
        for fn in fns:
            src = os.path.join(dp, fn)
            dst = os.path.join(dst_dp, fn)
            try:
                os.makedirs(dst_dp, exist_ok=True)
                if os.path.isdir(dst) and not os.path.islink(dst):
                    # Leftover deployed dir shadowing a vanilla file path.
                    shutil.rmtree(dst)
                try:
                    os.replace(src, dst)
                except OSError:
                    shutil.move(src, dst)
            except (OSError, shutil.Error) as exc:
                failed += 1
                _log(f"  WARN: could not restore "
                     f"{os.path.relpath(src, core_str)}: {exc}")
    if failed:
        _log(f"  ERROR: {failed} vanilla file(s) could not be restored and "
             f"remain in {core_dir.name}/ - they will be retried on the "
             f"next restore.")
    else:
        # Everything moved - drop the now-empty core_dir tree.
        shutil.rmtree(core_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Undeploy - remove a mod's deployed files from the game directory
# ---------------------------------------------------------------------------


__all__ = [
    "move_to_core",
    "deploy_filemap",
    "deploy_core",
    "restore_data_core",
    "sweep_deploy_trash",
    "_DEPLOY_MARKER_NAME",
]
