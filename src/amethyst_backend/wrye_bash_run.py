from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .jobs import JobCancelled, JobError
from .lod_run import _application_binaries, _binding, _source_tree
from .tool_install import _selected_game
from .xedit_run import _identity, _inputs, _plugin_sources


def _error(exc):
    return JobError(exc.name.replace("xedit_", "wrye_bash_"), str(exc), exc.details)


def _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel):
    from Utils.profiles.state import read_only_profile_state

    with read_only_profile_state():
        return _build_plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)


def _build_plan(game, profile, tool_id, catalogue_revision, choice, request, cancel):
    from Utils.bethesda.wrye_bash import arguments, executable, game_link, saved_options
    from Utils.bethesda.xedit_run import preview_prefix, _check_prefix
    from Utils.profiles.groups import is_group
    from Utils.vfs import effective_tool_data_root, manifest_path
    from .tools import catalogue

    if cancel.is_set():
        raise JobCancelled("Wrye Bash planning cancelled.")
    selected = _selected_game(game, profile)
    if not selected.is_configured():
        raise JobError("game_not_configured", "Configure the selected game before running Wrye Bash.")
    if is_group(profile):
        raise JobError("profile_group_unsupported", "Run Wrye Bash from a concrete profile so its output has an owner.")
    if catalogue(game, profile)["revision"] != catalogue_revision:
        raise JobError("stale_tool_catalogue", "Refresh the selected profile's tools.list catalogue.")
    settings = saved_options(selected, choice)
    settings.update({key: value for key, value in request.items() if key in settings})
    exe = executable(selected, choice)
    prefix = preview_prefix(selected, exe, settings["proton_name"], settings["prefix_mode"])
    _check_prefix(prefix, settings["launch_env"])
    args = arguments(selected, prefix["prefix_path"], settings["launch_args"])
    link, _root = game_link(selected, prefix["prefix_path"])
    deploy = request.get("deploy", True)
    if selected.get_deploy_active():
        last = selected.get_last_deployed_profile()
        if not isinstance(last, str) or not last or Path(last).name != last or not (profile.parent / last).is_dir():
            raise JobError("wrye_bash_restore_target_mismatch", "Recover the unavailable deployed profile first.")
        previous = _selected_game(game, profile.parent / last)
        if _binding(previous)[:2] != _binding(selected)[:2]:
            raise JobError("wrye_bash_restore_target_mismatch", "Restore the deployed profile's different game/prefix first.")
        if not deploy and last != profile.name:
            raise JobError("wrye_bash_deployed_profile_mismatch", "Deploy this profile or select the currently deployed profile.")
    inputs = _inputs(selected, profile, exe)
    inputs[str(profile / "loadorder.txt")] = _identity(profile / "loadorder.txt")
    snapshot = {"game": str(game.game_id), "profile": str(profile), "tool_id": tool_id,
                "catalogue_revision": catalogue_revision, "choice": choice, "request": request,
                "settings": settings, "exe": [str(exe), _identity(exe)], "prefix": prefix,
                "prefix_files": {n: _identity(Path(prefix["prefix_path"]) / n) for n in ("user.reg", "system.reg", "userdef.reg")},
                "runner": _identity(Path(prefix["script"])), "inputs": inputs,
                "sources": _plugin_sources(selected, cancel, label="Wrye Bash"),
                "tool_data": _source_tree(effective_tool_data_root(selected), cancel, family="wrye_bash", label="Wrye Bash"),
                "vfs_manifest": _identity(manifest_path(selected)),
                "overwrite": _source_tree(Path(selected.get_effective_overwrite_path()), cancel, family="wrye_bash", label="Wrye Bash"),
                "application_binaries": _application_binaries(exe), "game_link": _identity(link)}
    if inputs != {path: _identity(Path(path)) for path in inputs}:
        raise JobError("stale_wrye_bash_run", "Profile or launch settings changed while planning.")
    return {"revision": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            "game": str(game.game_id), "profile": profile.name, "tool_id": tool_id,
            "settings": settings, "exe_path": str(exe), "prefix": prefix, "arguments": args,
            "game_link": str(link), "output_path": str(selected.get_effective_overwrite_path()),
            "deploy_before_run": deploy, "root_folder_enabled": request.get("root_folder_enabled", True),
            "save_settings": request.get("save_settings", False), "restore_after_run": True,
            "external_tool_ui": True}


def plan_runner(game, profile, tool_id, catalogue_revision, choice, request, cancel):
    def run(log, progress):
        from Utils.bethesda.tool_run import ToolRunError

        progress(0, 1, "plan_wrye_bash_run")
        try:
            result = _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)
        except ToolRunError as exc:
            raise _error(exc) from exc
        except (JobError, JobCancelled):
            raise
        except (OSError, RuntimeError) as exc:
            raise JobError("wrye_bash_plan_unavailable", "Could not read the Wrye Bash run inputs.", {"reason": str(exc)}) from exc
        progress(1, 1, "plan_wrye_bash_run")
        return result
    return run


def run_runner(game, profile, tool_id, catalogue_revision, choice, request, revision, cancel, *, on_started=lambda: None):
    def run(log, progress):
        from Utils.bethesda.wrye_bash import game_link, run_wrye_bash, saved_options
        from Utils.bethesda.tool_run import ToolRunCancelled, ToolRunError
        from Utils.deployment.locking import DeploymentBusy
        from Utils.executables import launch
        from Utils.operations import OperationBusy, OperationCancelled

        try:
            initial = _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)
            if initial["revision"] != revision:
                raise JobError("stale_wrye_bash_run", "Wrye Bash inputs changed; plan the run again.")
            selected = _selected_game(game, profile)
            binding = _binding(selected)
            exe = Path(initial["exe_path"])
            runner_path = Path(initial["prefix"]["script"])
            identities = [_identity(exe), _identity(runner_path)]
            binaries = _application_binaries(exe)
            inputs = _inputs(selected, profile, exe)
            inputs.pop(str(profile / "plugins.txt"), None)

            def check_binding():
                if _binding(_selected_game(game, profile)) != binding:
                    raise JobError("stale_wrye_bash_run", "The selected profile's paths changed.")

            def checkpoint():
                check_binding()
                game_link(selected, initial["prefix"]["prefix_path"])
                if (identities != [_identity(exe), _identity(runner_path)] or binaries != _application_binaries(exe)
                        or inputs != {p: _identity(Path(p)) for p in inputs}):
                    raise JobError("stale_wrye_bash_run", "Profile, tool or launch settings changed during the Wrye Bash run.")

            def after_prepare():
                nonlocal inputs
                check_binding()
                inputs = {p: _identity(Path(p)) for p in inputs}

            def verify():
                if _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)["revision"] != revision:
                    raise JobError("stale_wrye_bash_run", "Wrye Bash inputs changed; plan the run again.")

            def before_restore():
                check_binding()
                if inputs[str(selected._deploy_state_file)] != _identity(selected._deploy_state_file):
                    raise JobError("stale_wrye_bash_run", "Deployment ownership changed during the Wrye Bash run.")

            settings = initial["settings"]

            def save_settings():
                if not initial["save_settings"]:
                    return False
                launch.save_proton_override(selected, exe.name, settings["proton_name"])
                launch.save_prefix_mode(selected, exe.name, settings["prefix_mode"])
                launch.save_winetricks_style(selected, exe.name, settings["winetricks_style"])
                launch.save_tool_launch_args(exe, settings["launch_args"])
                launch.save_tool_launch_env(exe, settings["launch_env"])
                if saved_options(selected, choice) != settings:
                    raise JobError("wrye_bash_settings_save_failed", "Could not save all Wrye Bash launch settings.", {"state_persisted": True})
                return True

            result = run_wrye_bash(selected, choice, exe, settings, prefix=initial["prefix"], cancel=cancel,
                                   before_write=verify, checkpoint=checkpoint, after_prepare=after_prepare,
                                   after_restore=after_prepare, before_restore=before_restore, prepare_settings=save_settings,
                                   on_started=on_started, deploy_first=initial["deploy_before_run"],
                                   root_folder_enabled=initial["root_folder_enabled"], log_fn=log, progress_fn=progress)
        except ToolRunCancelled as exc:
            raise JobCancelled(str(exc), exc.details) from exc
        except ToolRunError as exc:
            raise _error(exc) from exc
        except (OperationBusy, DeploymentBusy) as exc:
            raise JobError("operation_busy", str(exc), getattr(exc, "details", {})) from exc
        except OperationCancelled as exc:
            raise JobCancelled("Wrye Bash run cancelled.") from exc
        except (JobError, JobCancelled):
            raise
        except (OSError, RuntimeError) as exc:
            raise JobError("wrye_bash_run_unavailable", "Could not read the Wrye Bash run inputs.", {"reason": str(exc)}) from exc
        return {**result, "game": str(game.game_id), "profile": profile.name, "tool_id": tool_id,
                "exe_path": str(exe), "output_path": initial["output_path"], "game_link": initial["game_link"]}
    return run
