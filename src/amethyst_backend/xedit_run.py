from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .jobs import JobCancelled, JobError
from .tool_install import _selected_game, _token


def _identity(path):
    before = _token(path)
    if before is None:
        return None
    return before, _token(Path(path).resolve())


def _inputs(game, profile, exe):
    from Utils.executables.launch import _launch_mode_path

    paths = (game._paths_file, _launch_mode_path(game), game._deploy_state_file, profile / "profile_state.json",
             profile / "profile_settings.json", profile / "modlist.txt", profile / "plugins.txt",
             profile / "loot.json", exe.parent / "launch_args.json", exe.parent / "launch_env.json")
    return {str(path): _identity(path) for path in paths if path is not None}


def _plugin_sources(game, cancel, *, label="xEdit"):
    from Utils.profiles.groups import staging_walk_roots

    records = {}
    roots = staging_walk_roots(game.get_effective_mod_staging_path())
    for root in roots:
        for current, directories, files in os.walk(root, followlinks=False):
            if cancel.is_set():
                raise JobCancelled(f"{label} planning cancelled.")
            directories[:] = [name for name in directories if name.casefold() not in ("pfx", "drive_c", ".git")]
            for name in files:
                if name.casefold().endswith((".esp", ".esm", ".esl")):
                    path = Path(current) / name
                    records[str(path)] = _identity(path)
    game_root = game.get_game_path()
    for directory in (game.get_mod_data_path(), game_root / "Data_Core"):
        if directory is not None and directory.is_dir():
            for path in directory.iterdir():
                if path.name.casefold().endswith((".esp", ".esm", ".esl")):
                    records[str(path)] = _identity(path)
    return records


def _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel):
    from Utils.bethesda.xedit import collect_dirty_plugins
    from Utils.bethesda.xedit_run import arguments, executable, preview_prefix, saved_options, _check_prefix
    from .tools import catalogue

    if cancel.is_set():
        raise JobCancelled("xEdit planning cancelled.")
    selected = _selected_game(game, profile)
    if not selected.is_configured():
        raise JobError("game_not_configured", "Configure the selected game before running xEdit.")
    if catalogue(game, profile)["revision"] != catalogue_revision:
        raise JobError("stale_tool_catalogue", "Refresh the selected profile's tools.list catalogue.")
    settings = saved_options(selected, choice, request.get("use_64bit"))
    settings.update({key: value for key, value in request.items() if key in settings})
    exe = executable(selected, choice, settings["use_64bit"])
    prefix = preview_prefix(selected, exe, settings["proton_name"], settings["prefix_mode"])
    _check_prefix(prefix, settings["launch_env"])
    dirty = [{"name": name, "detail": detail} for name, detail in collect_dirty_plugins(selected)]
    mode = request.get("mode", "interactive")
    if mode == "qac_all" and not choice["qac"]:
        raise JobError("invalid_xedit_mode", "Choose a QuickAutoClean tool for batch cleaning.")
    available = {row["name"].casefold(): row["name"] for row in dirty}
    plugins = []
    if mode == "qac_all":
        for value in request.get("plugins", list(available.values())):
            if (value.casefold() not in available or value.startswith("-") or "/" in value or "\\" in value
                    or ":" in value or any(ord(char) < 32 for char in value)):
                raise JobError("invalid_xedit_plugins", "Select enabled plugins from the dirty-plugin report.")
            plugins.append(available[value.casefold()])
    elif "plugins" in request:
        raise JobError("invalid_xedit_plugins", "Plugin selections are only used in qac_all mode.")
    deploy = request.get("deploy", True)
    if selected.get_deploy_active():
        last = selected.get_last_deployed_profile()
        if not isinstance(last, str) or not last or Path(last).name != last:
            raise JobError("xedit_restore_target_mismatch", "The deployed profile is unavailable; recover it before running xEdit.")
        recovery = profile.parent / last
        if recovery.parent != profile.parent or not recovery.is_dir():
            raise JobError("xedit_restore_target_mismatch", "The deployed profile is unavailable; recover it before running xEdit.")
        previous = _selected_game(game, recovery)
        if (previous.get_game_path() != selected.get_game_path()
                or previous.get_prefix_path() != selected.get_prefix_path()):
            raise JobError("xedit_restore_target_mismatch", "Restore the deployed profile's different game/prefix first.")
        if not deploy and last != profile.name:
            raise JobError("xedit_deployed_profile_mismatch", "Deploy the selected profile or select the currently deployed profile.")
    inputs = _inputs(selected, profile, exe)
    prefix_files = {str(Path(prefix["prefix_path"]) / name): _identity(Path(prefix["prefix_path"]) / name)
                    for name in ("user.reg", "system.reg", "userdef.reg")}
    snapshot = {"game": str(game.game_id), "profile": str(profile), "tool_id": tool_id,
                "catalogue_revision": catalogue_revision, "choice": choice, "request": request,
                "settings": settings, "exe": [str(exe), _identity(exe)],
                "prefix": prefix, "prefix_files": prefix_files, "runner": _identity(Path(prefix["script"])),
                "inputs": inputs, "deployment": _identity(selected._deploy_state_file),
                "plugin_sources": _plugin_sources(selected, cancel), "dirty": dirty}
    if inputs != _inputs(selected, profile, exe):
        raise JobError("stale_xedit_run", "Profile or launch settings changed while planning.")
    revision = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return {"revision": revision, "game": str(game.game_id), "profile": profile.name,
            "tool_id": tool_id, "mode": mode, "settings": settings, "exe_path": str(exe),
            "prefix": prefix, "arguments": arguments(selected, choice, exe, Path(prefix["compat_data"]), mode,
                                                        settings["launch_args"]),
            "dirty_plugins": dirty, "plugins": plugins, "can_run": mode != "qac_all" or bool(plugins),
            "deploy_before_run": deploy, "root_folder_enabled": request.get("root_folder_enabled", True),
            "save_settings": request.get("save_settings", False), "restore_after_run": True,
            "external_tool_ui": mode == "interactive"}


def plan_runner(game, profile, tool_id, catalogue_revision, choice, request, cancel):
    def run(log, progress):
        from Utils.bethesda.xedit_run import XEditRunError

        progress(0, 1, "plan_xedit_run")
        try:
            result = _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)
        except XEditRunError as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except OSError as exc:
            raise JobError("xedit_plan_unavailable", "Could not read the xEdit run inputs.", {"reason": str(exc)}) from exc
        progress(1, 1, "plan_xedit_run")
        return result
    return run


def run_runner(game, profile, tool_id, catalogue_revision, choice, request, revision, cancel):
    def run(log, progress):
        from Utils.bethesda.xedit_run import XEditRunCancelled, XEditRunError, run_xedit
        from Utils.deployment.locking import DeploymentBusy
        from Utils.executables import launch
        from Utils.operations import OperationBusy, OperationCancelled

        selected = _selected_game(game, profile)
        try:
            initial = _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)
        except XEditRunError as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except OSError as exc:
            raise JobError("xedit_plan_unavailable", "Could not read the xEdit run inputs.", {"reason": str(exc)}) from exc
        if initial["revision"] != revision:
            raise JobError("stale_xedit_run", "The xEdit executable, prefix, profile, or plugin sources changed; plan again.")
        exe = Path(initial["exe_path"])
        runner_path = Path(initial["prefix"]["script"])
        executable_identity, runner_identity = _identity(exe), _identity(runner_path)
        inputs = _inputs(selected, profile, exe)

        def paths(value):
            return [value.get_game_path(), value.get_prefix_path(), value.get_effective_mod_staging_path(),
                    value.get_effective_overwrite_path(), value.get_effective_root_folder_path(),
                    value.get_effective_filemap_path()]

        binding = paths(selected)

        def check_binding():
            latest = _selected_game(game, profile)
            if paths(latest) != binding:
                raise JobError("stale_xedit_run", "The selected profile's paths changed.")

        def before_restore():
            check_binding()
            state_path = str(selected._deploy_state_file)
            if inputs[state_path] != _identity(selected._deploy_state_file):
                raise JobError("stale_xedit_run", "Deployment ownership changed during the xEdit run.")
            if selected.get_deploy_active():
                last = selected.get_last_deployed_profile()
                if not isinstance(last, str) or not last or Path(last).name != last:
                    raise JobError("xedit_restore_target_mismatch", "The deployed profile is unavailable.")
                previous = _selected_game(game, profile.parent / last)
                if paths(previous)[:2] != binding[:2]:
                    raise JobError("xedit_restore_target_mismatch", "The deployed profile's game/prefix paths changed.")

        def checkpoint():
            check_binding()
            if executable_identity != _identity(exe) or runner_identity != _identity(runner_path):
                raise JobError("stale_xedit_run", "The xEdit executable or runner changed during preparation.")
            if inputs != _inputs(selected, profile, exe):
                raise JobError("stale_xedit_run", "Profile or launch settings changed during the xEdit run.")

        def after_prepare():
            nonlocal inputs
            check_binding()
            inputs = _inputs(selected, profile, exe)

        def verify():
            current = _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)
            if current["revision"] != revision:
                raise JobError("stale_xedit_run", "The xEdit executable, prefix, profile, or plugin sources changed; plan again.")

        settings = initial["settings"]

        def save_settings():
            if not initial["save_settings"]:
                return False
            launch.save_tool_use_64bit(selected, choice["exe_name"], settings["use_64bit"])
            launch.save_proton_override(selected, exe.name, settings["proton_name"])
            launch.save_prefix_mode(selected, exe.name, settings["prefix_mode"])
            launch.save_winetricks_style(selected, exe.name, settings["winetricks_style"])
            launch.save_tool_launch_args(exe, settings["launch_args"])
            launch.save_tool_launch_env(exe, settings["launch_env"])
            from Utils.bethesda.xedit_run import saved_options
            if saved_options(selected, choice) != settings:
                raise JobError("xedit_settings_save_failed", "Could not save all xEdit launch settings.",
                               {"state_persisted": True})
            return True

        try:
            result = run_xedit(selected, choice, exe, settings["proton_name"], settings["prefix_mode"],
                               mode=initial["mode"], plugins=initial["plugins"],
                               deploy_first=initial["deploy_before_run"], root_folder_enabled=initial["root_folder_enabled"],
                               launch_args=settings["launch_args"], launch_env=settings["launch_env"],
                               winetricks_style=settings["winetricks_style"], prefix=initial["prefix"], cancel=cancel,
                               before_write=verify, checkpoint=checkpoint, after_prepare=after_prepare,
                               prepare_settings=save_settings, before_restore=before_restore,
                               log_fn=log, progress_fn=progress)
        except XEditRunCancelled as exc:
            raise JobCancelled(str(exc), exc.details) from exc
        except XEditRunError as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except (OperationBusy, DeploymentBusy) as exc:
            raise JobError("operation_busy", str(exc), getattr(exc, "details", {})) from exc
        except OperationCancelled as exc:
            raise JobCancelled("xEdit run cancelled.") from exc
        return {**result, "game": str(game.game_id), "profile": profile.name, "tool_id": tool_id,
                "mode": initial["mode"], "exe_path": str(exe)}
    return run
