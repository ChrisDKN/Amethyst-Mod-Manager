"""
Shared deploy orchestration used by the Deploy button, Run EXE (Play),
the BodySlide / DynDOLOD wizards, and the CLI.

`run_deploy_pipeline` performs the full restore → build_filemap → deploy →
wine-dll → root-folder → root-flagged → swap_launcher sequence. UI-specific
concerns (button enable/disable, status bar, mod panel reload) stay at the
call site.
"""

from __future__ import annotations

import traceback
import time
from pathlib import Path
from typing import Callable, Optional

from Utils.deploy import (
    LinkMode,
    deploy_root_folder,
    deploy_root_flagged_mods,
    load_per_mod_strip_prefixes,
    restore_root_folder_for_game,
)
from Utils.deploy_shared import RestoreIncompleteError, _FILEMAP_SNAPSHOT_NAME
from Utils.profile_backup import create_backup
from Utils.wine_dll_config import deploy_game_wine_dll_overrides


LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, Optional[str]], None]


class _DeployTimeline:
    """Opt-in top-level timing for one user-visible deployment.

    Enabled by ``MM_PERFTRACE``. Each mark is written both to the source
    terminal and the normal operation log.
    """

    def __init__(self, log_fn: LogFn, origin: "float | None" = None):
        now = time.perf_counter()
        self.origin = now if origin is None else float(origin)
        self.previous = self.origin
        self.log_fn = log_fn
        from Utils import perftrace
        self.enabled = perftrace.is_enabled()

    def mark(self, label: str, *, work: str | None = None) -> None:
        if not self.enabled:
            return
        now = time.perf_counter()
        tagged_label = f"[{work}] {label}" if work else label
        message = (
            f"[DEPLOY-TIMING] +{now - self.origin:8.3f}s "
            f"(step {now - self.previous:7.3f}s) {tagged_label}"
        )
        self.previous = now
        from Utils.app_log import safe_print
        safe_print(message, flush=True)
        try:
            self.log_fn(message)
        except Exception:
            pass


def check_paths_mounted(game) -> "str | None":
    """Return an error message if the game or staging drive looks unmounted.

    Guards against deploying into (or restoring under) a dead mountpoint:
    mkdir(parents=True) would silently recreate the game tree on the root
    filesystem and every file would land on the wrong drive.
    """
    import os

    game_root = _safe(game.get_game_path)
    if game_root:
        p = Path(game_root)
        if not p.is_dir():
            return (f"game folder not found: {p} - is the drive mounted?")
        try:
            with os.scandir(p) as it:
                if next(it, None) is None:
                    return (f"game folder is empty: {p} - is the drive mounted?")
        except OSError as exc:
            return f"game folder not accessible: {p} ({exc})"

    profile_root = _safe(game.get_profile_root)
    if profile_root is not None:
        pr = Path(profile_root)
        if not pr.is_dir():
            return (f"mod staging/profile folder not found: {pr} - "
                    f"is the drive mounted?")
        try:
            with os.scandir(pr) as it:
                if next(it, None) is None:
                    return (f"mod staging/profile folder is empty: {pr} - "
                            f"is the drive mounted?")
        except OSError as exc:
            return f"mod staging/profile folder not accessible: {pr} ({exc})"

    return None


def finalize_filegraph_recovery(
    game,
    profile_dir: Path,
    *,
    log_fn: LogFn,
) -> int:
    """Close interrupted journals after their filesystem restore succeeded."""
    from Utils.filegraph_service import FileGraphService

    profile_dir = Path(profile_dir)
    library = FileGraphService.open_library(
        game, profile_dir, log_fn=log_fn)
    session = library.open_profile(profile_dir)
    operations = session.incomplete_operations()
    for operation in operations:
        session.fail_deployment(operation.operation_id)
    if operations:
        log_fn(
            f"Recovered {len(operations)} interrupted deployment "
            "operation(s); the prior committed deployed state remains "
            "authoritative."
        )
    return len(operations)


def _fs_id(path: Path) -> "int | None":
    """Return the device id for *path* (or its nearest existing parent).

    Used to detect up-front when the game directory and the mod staging live
    on different filesystems - the single most common cause of hardlink
    deploys silently falling back to copy/symlink.
    """
    p = path
    for _ in range(40):
        try:
            return p.stat().st_dev
        except OSError:
            if p.parent == p:
                return None
            p = p.parent
    return None


def _count_enabled_mods(profile_dir: Path) -> "tuple[int, int]":
    """Return (enabled_mods, separators) from the profile's modlist.txt."""
    try:
        from Utils.modlist import read_modlist
        entries = read_modlist(profile_dir / "modlist.txt")
    except Exception:
        return (0, 0)
    enabled = sum(1 for e in entries if e.enabled and not e.is_separator)
    seps = sum(1 for e in entries if e.is_separator)
    return (enabled, seps)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _log_deploy_context(game, profile: str, profile_dir: Path,
                        deploy_mode: "LinkMode", *, log_fn: LogFn) -> None:
    """Emit a diagnostic header describing the full deploy environment.

    Logged once at the start of every deploy (all games) so a saved log
    contains everything needed to diagnose a failure without re-running:
    app version, game + paths, prefix, staging, deploy mode, profile, mod
    counts, and a same-filesystem check for hardlink viability.
    """
    try:
        from version import __version__ as app_version
    except Exception:
        app_version = "?"

    import platform

    game_root  = _safe(game.get_game_path)
    staging    = _safe(game.get_effective_mod_staging_path)
    filemap    = _safe(game.get_effective_filemap_path)
    data_path  = _safe(game.get_mod_data_path)
    prefix     = _safe(game.get_prefix_path)
    last_dep   = _safe(game.get_last_deployed_profile)
    enabled, seps = _count_enabled_mods(profile_dir)
    vfs_active = bool(getattr(game, "vfs_launch_enabled", False))
    method_name = "VFS" if vfs_active else deploy_mode.name

    log_fn("=" * 60)
    log_fn(f"Deploy: {game.name} - profile '{profile}'")
    log_fn(f"  Mod Manager {app_version} on {platform.system()} "
           f"{platform.release()}")
    log_fn(f"  Deploy mode:   {method_name}")
    log_fn(f"  Game path:     {game_root or '(not set)'}")
    if data_path is not None and data_path != game_root:
        log_fn(f"  Mod data dir:  {data_path}")
    if prefix:
        log_fn(f"  Proton prefix: {prefix}")
    log_fn(f"  Staging:       {staging or '(unknown)'}")
    log_fn(f"  Filemap:       {filemap or '(unknown)'}")
    log_fn(f"  Enabled mods:  {enabled}" +
           (f"  ({seps} separator(s))" if seps else ""))
    if last_dep and last_dep != profile:
        log_fn(f"  Last deployed: profile '{last_dep}'")

    # Hardlink viability: compare the filesystem of the deploy destination
    # against the staging folder. Different devices ⇒ hardlinks will fall
    # back to symlink/copy. Warn proactively rather than after-the-fact.
    if (not vfs_active and deploy_mode is LinkMode.HARDLINK
            and staging is not None):
        dest = data_path or game_root
        if dest is not None:
            dev_dest = _fs_id(Path(dest))
            dev_stg  = _fs_id(Path(staging))
            if dev_dest is not None and dev_stg is not None and dev_dest != dev_stg:
                log_fn("  WARNING: game and mod staging are on DIFFERENT "
                       "filesystems - hardlinks will fall back to "
                       "symlink/copy (uses extra disk space; symlinks can "
                       "break some games).")

    # Flatpak-sandboxed launchers can't read symlink targets outside their
    # own sandbox - symlinks into host-home staging look broken to the game.
    if not vfs_active and deploy_mode is LinkMode.SYMLINK and game_root:
        _app = flatpak_runtime_app(Path(game_root))
        if _app and (staging is None or flatpak_runtime_app(Path(staging)) != _app):
            log_fn(f"  NOTE: game runs inside the {_app} flatpak - sandbox "
                   f"access to the staging/profile folders is granted "
                   f"automatically so symlinked mods resolve. If mods still "
                   f"don't load, restart the launcher or run: flatpak "
                   f"override --user {_app} --filesystem='{staging}'")
    log_fn("=" * 60)


def flatpak_runtime_app(path: Path) -> "str | None":
    """Return the flatpak app id whose sandbox data dir contains *path*."""
    var_app = Path.home() / ".var" / "app"
    try:
        rel = path.relative_to(var_app)
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def run_deploy_pipeline(
    game,
    profile: str,
    *,
    log_fn: LogFn,
    progress_fn: Optional[ProgressFn] = None,
    root_folder_enabled: bool = True,
    confirm_cet: Optional[Callable[[], bool]] = None,
    confirm_windows_fs: Optional[Callable[[], bool]] = None,
    confirm_downgrade: Optional[Callable[[], bool]] = None,
    do_backup: bool = True,
    on_pre_filemap: Optional[Callable[[], None]] = None,
    timing_origin: "float | None" = None,
) -> bool:
    """Run the standard deploy sequence for *game* / *profile*.

    Parameters
    ----------
    log_fn / progress_fn
        Sinks for human-readable log lines and progress ticks. Callers supply
        thread-safe wrappers when invoked from a worker thread.
    root_folder_enabled
        Honors the Mod List panel's Root_Folder toggle; always True off the GUI.
    confirm_cet
        Optional blocking confirmation prompt (Cyberpunk CET symlink check).
        Return False to abort the deploy. None means "always proceed".
    confirm_windows_fs
        Optional blocking advisory when deploy folders sit on a Windows
        filesystem (NTFS/exFAT - see Utils.fs_check; GH#307). Called before
        any state is touched, so returning False is a clean no-op cancel.
        None means "always proceed".
    confirm_downgrade
        Optional blocking advisory when Fallout 3's exe is the Anniversary
        build FOSE cannot load (see Utils.fo3_version_check). Called before any
        state is touched, so returning False is a clean no-op cancel - the GUI
        returns False when the user opts to open the Downgrade wizard instead.
        None means "always proceed".
    do_backup
        If True, run `create_backup` for the profile dir before deploy.
    on_pre_filemap
        Optional hook fired *after* the profile switch but *before* the
        filemap rebuild. Used by wizards (e.g. BodySlide output redirect)
        to materialize a placeholder mod that needs to be in the filemap.

    Returns True on success, False on user-cancel / error. The active profile
    is always reset to *profile* before returning, even on error.
    """
    timeline = _DeployTimeline(log_fn, timing_origin)
    timeline.mark("pipeline worker entered")
    game_root = game.get_game_path()

    mount_err = check_paths_mounted(game)
    if mount_err:
        log_fn(f"Deploy aborted: {mount_err}")
        return False

    if confirm_windows_fs is not None and not confirm_windows_fs():
        log_fn("Deploy: cancelled - deploy folders are on a Windows "
               "filesystem (NTFS/exFAT) and the warning was declined.")
        return False

    if confirm_downgrade is not None and not confirm_downgrade():
        log_fn("Deploy: cancelled - Fallout 3 is on the Anniversary Edition "
               "exe (1.7.0.4), which the script extender cannot load.")
        return False

    timeline.mark("preflight and confirmations complete")
    _t_start = timeline.origin
    _filegraph_deployment_started = False

    try:
        from Utils import deploy_incremental as _incr
        from Utils.deploy_incremental import IncrementalFallback
        recovery_profile = None
        recovery_operations = ()

        # Restore against the last-deployed profile so runtime files (saves,
        # ShaderCache, etc.) land in *that* profile's overwrite/ folder.
        last_deployed = game.get_last_deployed_profile()
        if last_deployed:
            game.set_active_profile_dir(
                game.get_profile_root() / "profiles" / last_deployed
            )
            # Reload so per-profile path overrides apply to the restore (the
            # last-deployed profile may target a different game folder/prefix).
            game.load_paths()
            game_root = game.get_game_path()
            try:
                from Utils.filegraph_service import FileGraphService
                recovery_dir = (
                    game.get_profile_root() / "profiles" / last_deployed)
                recovery_library = FileGraphService.open_library(
                    game, recovery_dir, log_fn=log_fn)
                recovery_profile = recovery_library.open_profile(recovery_dir)
                recovery_operations = recovery_profile.incomplete_operations()
                if recovery_operations:
                    log_fn(
                        "Recovering an interrupted deployment before any new "
                        "profile mutation or placement."
                    )
            except Exception as recovery_error:
                raise RuntimeError(
                    "Could not inspect Filegraph deployment recovery state: "
                    f"{recovery_error}"
                ) from recovery_error

        # Profile Group target: reconcile it against its members' current
        # state BEFORE the incremental probe / filemap build so both see the
        # post-reconcile modlist, links and index. Explicit-dir based - the
        # active profile still points at last_deployed for the restore.
        try:
            from Utils.profile_groups import materialize_if_group
            materialize_if_group(
                game, game.get_profile_root() / "profiles" / profile,
                log_fn=log_fn)
        except Exception as _pg_err:
            log_fn(f"Profile Group reconcile warning: {_pg_err}")
        timeline.mark("recovery inspection and profile-group reconcile complete")

        # A same-profile physical redeploy with an identical pinned plan has
        # no filesystem work to do. Check before Restore so a conflict-only
        # reorder does not tear down and rebuild the whole game tree.
        if (last_deployed == profile
                and on_pre_filemap is None
                and not getattr(game, "vfs_launch_enabled", False)
                and game.get_deploy_active()):
            try:
                preview_mode = (
                    game.get_deploy_mode()
                    if hasattr(game, "get_deploy_mode")
                    else LinkMode.HARDLINK
                )
                if game.get_last_deploy_mode() == preview_mode.name:
                    from Utils.filegraph_service import FileGraphService
                    preview_dir = game.get_profile_root() / "profiles" / profile
                    preview_library = FileGraphService.open_library(
                        game, preview_dir, log_fn=log_fn)
                    preview_library.ensure_ready(preview_dir)
                    preview_profile = preview_library.open_profile(preview_dir)
                    preview_profile.ensure_reconciled(
                        operation_hint={"kind": "deployment_preview"})
                    preview_generation = preview_profile.snapshot().generation
                    if _incr.deployment_unchanged(
                            preview_profile, preview_generation,
                            preview_mode.name.lower()):
                        timeline.mark("unchanged-plan check complete: no-op deploy")
                        log_fn(
                            "Deploy skipped: the committed Filegraph plan is "
                            "unchanged; no filesystem operations were needed."
                        )
                        return True
            except Exception as preview_error:
                log_fn(
                    "Deployment no-op check was unavailable "
                    f"({preview_error}); using the full path."
                )
        timeline.mark("same-profile unchanged-plan check complete")

        # Incremental fast path: redeploying the profile that is already
        # deployed with the same link mode → skip the restore and let the
        # standard primitives diff against the previous deploy instead.
        incr_plan = None
        vfs_redeploy = False
        if last_deployed == profile:
            if progress_fn is not None:
                progress_fn(0, 0, "Inspecting the current deployment…")
            probe_mode = (
                game.get_deploy_mode()
                if hasattr(game, "get_deploy_mode")
                else LinkMode.HARDLINK
            )
            incr_plan = _incr.plan_incremental(
                game, profile, probe_mode, recovery_profile, log_fn=log_fn)
            if incr_plan is None:
                vfs_redeploy = _incr.plan_vfs_redeploy(
                    game, profile, log_fn=log_fn)
        timeline.mark(
            "incremental eligibility and deployed-state projection complete",
            work="DB I/O + CPU")
        if incr_plan is not None:
            log_fn("Incremental deploy: existing deployment reused - "
                   "skipping restore.")
            # swap_launcher (end of pipeline) backs up the *current* launcher
            # over <stem>.bak.  Without the full restore that current file is
            # the script-extender copy from the last deploy, which would
            # clobber the vanilla backup.  Undo the swap now; it is re-applied
            # after the deploy as usual.
            if hasattr(game, "_restore_launcher"):
                try:
                    game._restore_launcher(log_fn)
                except Exception as exc:
                    log_fn(f"  WARN: launcher un-swap failed: {exc}")
            timeline.mark("incremental deployment retained; launcher prepared")
        elif vfs_redeploy:
            log_fn("Incremental VFS deploy: existing private view retained - "
                   "skipping restore.")
            timeline.mark("incremental VFS deployment retained")
        elif recovery_operations and not hasattr(game, "restore"):
            raise RestoreIncompleteError(
                "An interrupted deployment requires recovery, but this game "
                "handler does not provide Restore."
            )
        elif ((recovery_operations
               or getattr(game, "restore_before_deploy", True))
              and hasattr(game, "restore")):
            try:
                if progress_fn is not None:
                    game.restore(log_fn=log_fn, progress_fn=progress_fn)
                else:
                    game.restore(log_fn=log_fn)
            except RestoreIncompleteError:
                # Recovery state is still authoritative. Never place another
                # deployment over files/backups which Restore could not clear.
                raise
            except RuntimeError as restore_err:
                if recovery_operations:
                    raise RestoreIncompleteError(
                        "Interrupted deployment recovery did not complete: "
                        f"{restore_err}"
                    ) from restore_err
                # Expected on first deploy / unconfigured paths; the deploy
                # steps have their own leftover-deploy guards, so continue -
                # but never hide the failure from the log.
                log_fn(f"Restore before deploy failed: {restore_err} - continuing.")
            timeline.mark("pre-deploy restore complete", work="FS I/O")
        else:
            timeline.mark("pre-deploy restore not required")
        last_root_folder_dir = game.get_effective_root_folder_path()
        if last_root_folder_dir.is_dir() and game_root:
            # The persisted root-deploy identity lets restore remove a leftover
            # root payload under Data/ without risking the vanilla file that
            # Data_Core may just have restored at the same path.  This runs
            # against the last-deployed profile before switching to the target.
            restore_root_folder_for_game(
                game,
                root_folder_dir=last_root_folder_dir,
                game_root=game_root,
                log_fn=log_fn,
            )
        if recovery_operations:
            if recovery_profile is None:
                raise RestoreIncompleteError(
                    "Interrupted deployment recovery lost its catalog session."
                )
            for operation in recovery_operations:
                recovery_profile.fail_deployment(operation.operation_id)
            log_fn(
                f"Recovered {len(recovery_operations)} interrupted "
                "deployment operation(s); the prior committed deployed state "
                "remains authoritative."
            )
        timeline.mark(
            "root cleanup and recovery finalization complete", work="FS I/O")

        # Switch to the target profile before filemap + deploy.
        game.set_active_profile_dir(
            game.get_profile_root() / "profiles" / profile
        )
        # Reload so the deploy uses the target profile's path overrides.
        game.load_paths()
        game_root = game.get_game_path()

        deploy_preflight = getattr(game, "deployment_preflight_error", None)
        if callable(deploy_preflight):
            preflight_error = deploy_preflight()
            if preflight_error:
                raise RuntimeError(str(preflight_error))

        if on_pre_filemap is not None:
            on_pre_filemap()

        # Open/reconcile the required native catalog and pin the generation
        # that every deploy handler below will consume. No legacy map is built
        # or refreshed here.
        if progress_fn is not None:
            progress_fn(0, 0, "Reconciling the selected profile…")
        from Utils.filegraph_service import FileGraphService
        profile_dir = game.get_profile_root() / "profiles" / profile
        filegraph_library = FileGraphService.open_library(
            game, profile_dir, log_fn=log_fn)
        if on_pre_filemap is not None:
            # The hook is a manager-owned staging mutation (wizard output), so
            # update the catalog immediately rather than waiting for Refresh.
            filegraph_library.refresh(profile_dir)
        else:
            filegraph_library.ensure_ready(profile_dir)
        filegraph_profile = filegraph_library.open_profile(profile_dir)
        filegraph_profile.ensure_reconciled(
            operation_hint={"kind": "deployment"})
        filegraph_generation = filegraph_profile.snapshot().generation
        timeline.mark(
            "target profile load and Filegraph reconcile complete",
            work="DB I/O + CPU")

        if confirm_cet is not None and not confirm_cet():
            log_fn("Deploy: cancelled - CET requires Hardlink mode.")
            return False

        if do_backup:
            try:
                create_backup(profile_dir, log_fn)
            except Exception as backup_err:
                log_fn(f"Backup skipped: {backup_err}")
        timeline.mark("profile backup complete", work="FS I/O")

        deploy_mode = (
            game.get_deploy_mode()
            if hasattr(game, "get_deploy_mode")
            else LinkMode.HARDLINK
        )
        from Utils.filegraph_deploy import begin as begin_filegraph_deployment
        if progress_fn is not None:
            progress_fn(0, 0, "Building the deployment plan…")
        active_deployment = begin_filegraph_deployment(
            filegraph_profile, filegraph_generation, deploy_mode.name.lower())
        _filegraph_deployment_started = True

        if incr_plan is not None:
            incremental_error = None
            if incr_plan.mode is not deploy_mode:
                incremental_error = "link mode changed after profile switch"
            else:
                try:
                    _incr.bind_deployment_plan(
                        incr_plan,
                        active_deployment.plan,
                    )
                except IncrementalFallback as bind_error:
                    incremental_error = str(bind_error)
            if incremental_error is not None:
                log_fn(
                    "Incremental deploy unavailable after reconciliation "
                    f"({incremental_error}); restoring the full deployment."
                )
                incr_plan = None
                try:
                    if progress_fn is not None:
                        game.restore(log_fn=log_fn, progress_fn=progress_fn)
                    else:
                        game.restore(log_fn=log_fn)
                except RestoreIncompleteError:
                    raise
                except RuntimeError as restore_err:
                    log_fn(
                        f"Restore before deploy failed: {restore_err} - "
                        "continuing."
                    )
        timeline.mark(
            "deployment plan built and recovery journal started",
            work="DB I/O + CPU")
        # Games launched by a flatpak launcher (Heroic flatpak et al.) run in
        # its sandbox and can't follow symlinks whose targets aren't mounted
        # there - grant staging/profile access up front (GH#275).
        try:
            from Utils.flatpak_sandbox import (
                ensure_launcher_handoff_access,
                ensure_symlink_target_access,
            )
            ensure_symlink_target_access(
                game,
                game_root=Path(game_root) if game_root else None,
                staging=_safe(game.get_effective_mod_staging_path),
                profile_dir=profile_dir,
                log_fn=log_fn,
            )
            ensure_launcher_handoff_access(game, log_fn=log_fn)
        except Exception as exc:
            log_fn(f"  WARN: flatpak sandbox access check failed: {exc}")

        _log_deploy_context(game, profile, profile_dir, deploy_mode,
                            log_fn=log_fn)
        timeline.mark(
            "filesystem/sandbox deployment preparation complete", work="FS I/O")

        def _run_game_deploy():
            if progress_fn is not None:
                game.deploy(log_fn=log_fn, profile=profile,
                            progress_fn=progress_fn, mode=deploy_mode)
            else:
                game.deploy(log_fn=log_fn, profile=profile, mode=deploy_mode)

        from Utils.mod_files import excluded_raw_by_mod
        from Utils.deploy_shared import set_deploy_excluded_raw

        # Defer the handler's end-of-deploy game-root snapshot: the pipeline
        # writes it once after the root-folder files land (below), instead of
        # the handler walking the game root now and the pipeline walking it
        # again for the refresh.
        game.begin_deferred_runtime_snapshot()
        # A VFS-aware handler consumes Root_Folder itself while building its
        # private layer. Keep the session toggle available without widening
        # every game's long-standing deploy() signature.
        if getattr(game, "virtualizes_game_root", False):
            game._pipeline_root_folder_enabled = bool(root_folder_enabled)
        try:
            # Source resolution must never pick a disabled variant when two
            # staged files collapse onto one filemap key. Set inside the try so
            # the finally always clears it - a leak would follow into restore.
            set_deploy_excluded_raw(excluded_raw_by_mod(profile_dir) or None)
            from Utils.filegraph_deploy import mark_phase as mark_deploy_phase
            mark_deploy_phase("backing_up")
            if incr_plan is not None:
                _incr.activate(incr_plan)
                try:
                    _run_game_deploy()
                except IncrementalFallback as fb:
                    _incr.deactivate()
                    incr_plan = None
                    log_fn(f"Incremental deploy fell back to the full path: {fb}")
                    # restore_data_core recovers any partially-diffed Data/,
                    # then the classic full deploy runs.  Same profile, so no
                    # profile switch is needed around the restore.
                    try:
                        if progress_fn is not None:
                            game.restore(log_fn=log_fn, progress_fn=progress_fn)
                        else:
                            game.restore(log_fn=log_fn)
                    except RestoreIncompleteError:
                        raise
                    except RuntimeError as restore_err:
                        log_fn(f"Restore before deploy failed: {restore_err} "
                               f"- continuing.")
                    _run_game_deploy()
                finally:
                    _incr.deactivate()
            else:
                _run_game_deploy()
        finally:
            set_deploy_excluded_raw(None)
            try:
                delattr(game, "_pipeline_root_folder_enabled")
            except AttributeError:
                pass
            (generic_snapshot_requested,
             direct_snapshot_requests) = game.end_deferred_runtime_snapshot()
        timeline.mark(
            "game handler filesystem deployment complete",
            work="FS I/O + CPU")

        from Utils.filegraph_deploy import mark_phase as mark_deploy_phase
        mark_deploy_phase("post_deploy")

        pfx = game.get_prefix_path()
        if pfx and pfx.is_dir():
            deploy_game_wine_dll_overrides(
                game.name, pfx, game.wine_dll_overrides, log_fn=log_fn
            )

        method_name = (
            "VFS" if getattr(game, "vfs_launch_enabled", False)
            else deploy_mode.name
        )
        game.save_last_deployed_profile(profile, deploy_mode=method_name)
        timeline.mark(
            "Wine configuration and deployed-profile marker complete",
            work="FS I/O")

        target_rf = game.get_effective_root_folder_path()
        rf_allowed = (
            getattr(game, "root_folder_deploy_enabled", True)
            and not getattr(game, "virtualizes_game_root", False)
        )

        # Step A: shared Root_Folder must run first - its log file is what
        # Step B's root-flagged-mods deploy merges into.
        if rf_allowed and root_folder_enabled and target_rf.is_dir() and game_root:
            count = deploy_root_folder(
                target_rf, game_root, mode=deploy_mode, log_fn=log_fn
            )
            if count:
                log_fn("Root Folder: transferred files to game root.")

        # rf_allowed=False means this game never writes into the game folder at
        # all (its mods are served by an external loader), so per-mod root-flagged
        # files must be skipped too - not just the shared Root_Folder above.
        if game_root and rf_allowed:
            filemap_root_path = (
                game.get_effective_filemap_path().parent / "filemap_root.txt"
            )
            staging = game.get_effective_mod_staging_path()
            strip = getattr(game, "mod_folder_strip_prefixes", None)
            per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
            rf_count = deploy_root_flagged_mods(
                filemap_root_path, game_root, staging,
                mode=deploy_mode, strip_prefixes=strip,
                per_mod_strip_prefixes=per_mod_strip or None,
                excluded_raw=excluded_raw_by_mod(profile_dir) or None,
                log_fn=log_fn,
            )
            if rf_count:
                log_fn(f"Root-flagged mods: {rf_count} file(s) deployed to game root.")

            # Wine path-lookup optimisation (GH#374): empty stubs for
            # directories the engine probes but that never exist, then
            # case-variant aliases so its case-mismatched lookups hit Wine's
            # exact-case fast path instead of scanning a whole directory.
            # Stubs first so the alias pass gives them their case variants.
            # Before the snapshot refresh so both are recorded as
            # deploy-time state, never runtime files.  With the setting off,
            # deploy removes what it previously created so toggling it
            # cleans up without waiting for a restore.
            alias_dirs = getattr(game, "case_alias_dirs", None)
            stub_dirs = getattr(game, "probe_stub_dirs", None)
            if alias_dirs or stub_dirs:
                from Utils.deploy_shared import (create_probe_stub_dirs,
                                                 deploy_case_alias_links,
                                                 remove_case_alias_links,
                                                 remove_probe_stub_dirs)
                try:
                    if getattr(game, "case_alias_links", True):
                        create_probe_stub_dirs(Path(game_root), stub_dirs,
                                               log_fn=log_fn)
                        deploy_case_alias_links(Path(game_root), alias_dirs,
                                                log_fn=log_fn)
                    else:
                        remove_case_alias_links(Path(game_root), alias_dirs,
                                                log_fn=log_fn)
                        remove_probe_stub_dirs(Path(game_root), stub_dirs,
                                               log_fn=log_fn)
                except Exception as exc:
                    log_fn(f"  WARN: Wine path-lookup optimisation failed: {exc}")
        timeline.mark(
            "root files and path-lookup aliases complete", work="FS I/O")

        # Write runtime snapshots now that every game-root mutation has landed.
        # Direct requests carry their own roots and destinations, so flush them
        # even when the pipeline's generic game_root value is unavailable.
        snapshot_requests = list(direct_snapshot_requests)
        try:
            snapshot_path = (
                game.get_effective_filemap_path().parent / _FILEMAP_SNAPSHOT_NAME
            )
            # BaseGame requests use the handler-derived default path/exclusions.
            # The existing-file check preserves refresh behavior for older state.
            if generic_snapshot_requested or (
                    game_root and snapshot_path.is_file()):
                generic_root = game.get_game_path()
                if generic_root:
                    snapshot_requests.insert(0, (
                        Path(generic_root), snapshot_path, log_fn,
                        game.runtime_snapshot_exclude_dirs(),
                    ))
        except Exception as exc:
            log_fn(f"WARN: could not prepare generic deploy snapshot: {exc}")
        try:
            from Utils.deploy_shared import _flush_deferred_deploy_snapshots
            # Direct requests follow the generic one in the collection, so an
            # explicit handler request wins if both target the same path.
            _flush_deferred_deploy_snapshots(snapshot_requests)
        except Exception as exc:
            log_fn(f"WARN: could not refresh deploy snapshot: {exc}")
        timeline.mark("runtime snapshots complete", work="FS I/O")

        # Launcher swap last so SE/SKSE/etc. dlls are present first.
        if hasattr(game, "swap_launcher"):
            game.swap_launcher(log_fn)

        try:
            game.post_deploy(log_fn=log_fn)
        except Exception as pd_err:
            log_fn(f"post_deploy warning: {pd_err}")

        # External launchers retain one short per-game script. Refresh it after
        # every successful deploy (including silent Play/wizard deployments),
        # so an AppImage upgrade or moved source checkout cannot leave stale
        # implementation details hidden in the launcher's saved settings.
        try:
            from Utils.launch_handoff import refresh_launch_handoff_script
            refresh_launch_handoff_script(game, log_fn=log_fn)
        except Exception as handoff_err:
            log_fn(f"Launcher handoff warning: {handoff_err}")
        timeline.mark(
            "launcher and post-deploy hooks complete", work="FS I/O")

        if incr_plan is not None:
            _tag = " (incremental)"
        elif vfs_redeploy:
            _tag = " (incremental VFS rebuild)"
        else:
            _tag = ""
        from Utils.filegraph_deploy import finish as finish_filegraph_deployment
        finish_filegraph_deployment(success=True)
        _filegraph_deployment_started = False
        timeline.mark(
            "Filegraph deployed state committed", work="DB I/O + CPU")
        log_fn(f"Deploy finished OK in {time.perf_counter() - _t_start:.1f}s "
               f"- profile '{profile}'.{_tag}")
        return True
    except Exception as e:
        if _filegraph_deployment_started:
            try:
                from Utils.filegraph_deploy import finish as finish_filegraph_deployment
                finish_filegraph_deployment(success=False)
            except Exception as journal_error:
                log_fn(f"Deployment recovery journal warning: {journal_error}")
            _filegraph_deployment_started = False
        timeline.mark("deployment failed")
        log_fn(f"Deploy FAILED after {time.perf_counter() - _t_start:.1f}s: "
               f"{e}\n{traceback.format_exc()}")
        return False
    finally:
        game.set_active_profile_dir(
            game.get_profile_root() / "profiles" / profile
        )
        game.load_paths()
        timeline.mark("active profile paths restored; worker complete")
