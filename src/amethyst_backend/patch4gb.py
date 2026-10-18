from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

from .jobs import JobCancelled, JobError
from .tool_install import _selected_game, _token


def _checkpoint(cancel):
    if cancel.is_set():
        raise JobCancelled("4GB patch operation cancelled.")


def _configuration(game, profile):
    paths = (game._paths_file, profile / "profile_state.json",
             profile / "profile_settings.json")
    states = {str(path): _token(path) for path in paths}
    if any(value is not None and not stat.S_ISREG(value[2]) for value in states.values()):
        raise JobError("unsafe_patch4gb_configuration", "A game or profile configuration file is not regular.")
    return states


def _selection(game, profile):
    _configuration(game, profile)
    selected = _selected_game(game, profile)
    if not selected.is_configured():
        raise JobError("game_not_configured", "Configure the game before using the 4GB patch wizard.")
    root = selected.get_game_path()
    if root is None or not root.is_dir():
        raise JobError("patch4gb_game_unavailable", "The configured game folder is unavailable.")
    return selected, Path(root).resolve()


def _report(game, profile, cancel):
    from Utils.bethesda.patch4gb import inspect_files, patcher_for_game

    selected, root = _selection(game, profile)
    configuration = _configuration(selected, profile)
    deployment_file = selected._deploy_state_file
    deployment = _token(deployment_file)
    if deployment is not None and not stat.S_ISREG(deployment[2]):
        raise JobError("unsafe_patch4gb_configuration", "The deployment state is not a regular file.")
    if deployment is not None:
        try:
            if deployment[3] > 65536:
                raise ValueError("The deployment state is too large.")
            saved = json.loads(deployment_file.read_text(encoding="utf-8"))
            if (not isinstance(saved, dict)
                    or "deploy_active" in saved and type(saved["deploy_active"]) is not bool):
                raise ValueError("Invalid deployment state")
            name = saved.get("last_deployed")
            if name is not None and (not isinstance(name, str) or not name.strip()
                                     or Path(name).name != name or name in (".", "..")):
                raise ValueError("Invalid deployed profile")
        except (OSError, ValueError) as exc:
            raise JobError("patch4gb_deployment_unavailable", "The saved deployment state cannot be read.") from exc
    active = selected.get_deploy_active()
    patcher = patcher_for_game(selected)
    info, files = inspect_files(patcher, root, lambda: _checkpoint(cancel), allow_link=active)
    if configuration != _configuration(selected, profile) or deployment != _token(deployment_file):
        raise JobError("stale_patch4gb_plan", "Configuration or deployment state changed during inspection.")
    auto = getattr(selected, "_auto_4gb_active", lambda: False)()
    snapshot = {"game": str(selected.game_id), "profile": str(profile),
                "root": str(root), "root_identity": _token(root),
                "configuration": configuration, "deployment": deployment,
                "files": files, "automatic": auto}
    report = {
        "game": str(selected.game_id), "profile": profile.name,
        "game_path": str(root), "executable": str(root / patcher.EXE_NAME),
        "backup": str(root / patcher.BACKUP_NAME),
        "state": info["state"], "variant": info.get("variant"),
        "sha1": info.get("hash"), "sha256": files[0]["sha256"] if files[0] else None,
        "backup_exists": files[1] is not None,
        "backup_sha256": files[1]["sha256"] if files[1] else None,
        "diagnostic": info.get("error") or None,
        "apply_available": info["state"] == "patchable",
        "restore_available": files[1] is not None,
        "restore_before_mutation": active,
        "automatic_patch_enabled": auto,
        "loads_nvse": patcher.EXE_NAME == "FalloutNV.exe",
        "revision": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
    }
    _checkpoint(cancel)
    return selected, root, patcher, report, configuration


def inspect_runner(game, profile, cancel):
    def run(log, progress):
        from Utils.bethesda.patch4gb import PatchError
        from Utils.operations import acquire_game_operation

        progress(0, 1, "inspect_4gb_patch")
        try:
            selected, _root = _selection(game, profile)
            with acquire_game_operation(selected, "tools.4gb_patch.inspect"):
                report = _report(game, profile, cancel)[3]
        except PatchError as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except (OSError, ValueError) as exc:
            raise JobError("patch4gb_inspection_failed", "Could not inspect the executable.",
                           {"reason": str(exc)}) from exc
        progress(1, 1, "inspect_4gb_patch")
        return report
    return run


def mutate_runner(game, profile, action, revision, cancel):
    def run(log, progress):
        from Utils.bethesda.patch4gb import PatchError, file_identity, mutate_files
        from Utils.deployment.locking import DeploymentBusy, game_mutation_lock
        from Utils.deployment.pipeline import _run_restore_pipeline
        from Utils.operations import OperationBusy, acquire_game_operation
        from Utils.wizards.payload import PayloadInstallError

        restore_started = False
        persisted = False
        progress(0, 1, "prepare_4gb_patch")
        try:
            selected, root = _selection(game, profile)
            with acquire_game_operation(selected, f"tools.4gb_patch.{action}"):
                selected, root, patcher, before, configuration = _report(game, profile, cancel)
                if before["revision"] != revision:
                    raise JobError("stale_patch4gb_plan", "The executable, backup, or configuration changed; inspect again.")
                if not before[f"{action}_available"]:
                    raise JobError("patch4gb_not_patchable" if action == "apply" else "patch4gb_backup_missing",
                                   "The requested action is unavailable in the current executable state.",
                                   {"state": before["state"]})

                def checkpoint():
                    _checkpoint(cancel)
                    latest, current_root = _selection(game, profile)
                    if current_root != root or configuration != _configuration(latest, profile):
                        raise JobError("stale_patch4gb_plan", "Game or profile configuration changed during the operation.")

                checkpoint()
                if before["restore_before_mutation"]:
                    recovery = profile.parent / selected.get_last_deployed_profile()
                    if recovery.parent != profile.parent:
                        raise JobError("patch4gb_restore_target_mismatch", "The deployed profile path is unsafe.")
                    _recovery_game, recovery_root = _selection(game, recovery)
                    if recovery_root != root:
                        raise JobError("patch4gb_restore_target_mismatch",
                                       "The deployed profile uses a different game folder; restore that profile first.")
                    restore_started = True
                    progress(0, 1, "restore_before_4gb_patch")
                    if not _run_restore_pipeline(selected, profile.name, log_fn=log):
                        raise JobError("patch4gb_restore_failed", "Modlist Restore failed; the executable was not patched.")
                    checkpoint()
                    if selected.get_deploy_active():
                        raise JobError("patch4gb_restore_failed", "Modlist Restore left the game deployed.")

                with game_mutation_lock(selected):
                    checkpoint()
                    automatic_restore = (action == "restore" and restore_started
                                         and not (root / patcher.BACKUP_NAME).exists())
                    if automatic_restore:
                        exe = file_identity(root / patcher.EXE_NAME, checkpoint)
                        if exe is None or exe["sha256"] != before["backup_sha256"]:
                            raise JobError("stale_patch4gb_plan", "Restore consumed the backup without restoring its executable.")
                    else:
                        current = _report(game, profile, cancel)[3]

                        def before_write():
                            checkpoint()
                            if current["revision"] != _report(game, profile, cancel)[3]["revision"]:
                                raise JobError("stale_patch4gb_plan", "The executable or backup changed during preparation.")

                        progress(0, 1, f"{action}_4gb_patch")

                        def mark_published():
                            nonlocal persisted
                            persisted = True

                        mutate_files(patcher, root, action, checkpoint=checkpoint,
                                     before_write=before_write, log=log, on_published=mark_published)
                    persisted = True
                    after = _report(game, profile, cancel)[3]
                log(f"4GB patch: {action} completed for {patcher.EXE_NAME}.")
        except (PatchError, PayloadInstallError) as exc:
            raise JobError(exc.name, str(exc), {**exc.details, "restore_attempted": restore_started,
                           "state_persisted": persisted or restore_started or exc.details.get("state_persisted", False)}) from exc
        except (OperationBusy, DeploymentBusy) as exc:
            raise JobError("operation_busy", str(exc),
                           {**getattr(exc, "details", {}), "restore_attempted": restore_started,
                            "state_persisted": persisted or restore_started}) from exc
        except (JobError, JobCancelled) as exc:
            exc.details.update(restore_attempted=restore_started,
                               state_persisted=persisted or restore_started or exc.details.get("state_persisted", False))
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise JobError("patch4gb_mutation_failed", "Could not complete the executable patch operation.",
                           {"reason": str(exc), "restore_attempted": restore_started,
                            "state_persisted": persisted or restore_started}) from exc
        progress(1, 1, f"{action}_4gb_patch")
        return {"ok": True, "action": action, "restore_attempted": restore_started,
                "backup_restored_by_modlist_restore": automatic_restore, "report": after}
    return run
