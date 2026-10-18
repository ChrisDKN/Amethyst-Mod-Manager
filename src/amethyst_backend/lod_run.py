from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .jobs import JobCancelled, JobError
from .tool_install import _selected_game
from .xedit_run import _identity, _inputs


def _map_error(exc):
    return JobError(exc.name.replace("xedit_", "lod_"), str(exc), exc.details)


def _source_tree(root, cancel, *, family="lod", label="LOD"):
    records, visited = {}, set()
    before = _identity(root)
    for current, directories, files in os.walk(root, followlinks=True):
        if cancel.is_set():
            raise JobCancelled(f"{label} planning cancelled.")
        parent = Path(current)
        value = parent.stat()
        key = value.st_dev, value.st_ino
        if key in visited:
            directories[:] = []
            continue
        visited.add(key)
        for path in (parent, *(parent / name for name in directories + files)):
            if cancel.is_set():
                raise JobCancelled(f"{label} planning cancelled.")
            records[str(path.relative_to(root))] = _identity(path)
    if _identity(root) != before:
        raise JobError(f"stale_{family}_run", f"{label} sources changed during planning.")
    return before, hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(), len(records)


def _application_binaries(exe):
    records = {}
    for current, directories, files in os.walk(exe.parent, followlinks=False):
        directories[:] = [name for name in directories if not name.startswith("prefix_") and name not in ("pfx", "drive_c")]
        for name in files:
            if name.casefold().endswith((".exe", ".dll")):
                path = Path(current) / name
                records[str(path)] = _identity(path)
    return records


def _binding(game):
    from Utils.profiles.state import read_only_profile_state

    with read_only_profile_state():
        return [game.get_game_path(), game.get_prefix_path(), game.get_effective_mod_staging_path(),
                game.get_effective_overwrite_path(), game.get_effective_root_folder_path(),
                game.get_effective_filemap_path()]


def _mod_target(game, profile, choice, request, cancel):
    from Utils.mods.modlist import read_modlist
    from Utils.mods.names import sanitize_mod_folder_name
    from Utils.wizards.payload import tree_identity

    name = request.get("mod_name", choice["output_dir"])
    if (sanitize_mod_folder_name(name) != name or name.casefold() in {"root_folder", "overwrite", "applications"}
            or name.casefold().endswith("_separator") or name.casefold().startswith("@group:") or name.startswith(".")):
        raise JobError("invalid_lod_mod_name", "Choose a valid output mod folder name.")
    staging = Path(game.get_effective_mod_staging_path()).resolve()
    target = staging / name
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise JobError("unsafe_tool_destination", "The output mod destination is a link or file.")
    if staging.exists() and any(p.name.casefold() == name.casefold() and p.name != name for p in staging.iterdir()):
        raise JobError("tool_mod_name_conflict", "An output folder differs only in letter case.")
    rows = read_modlist(profile / "modlist.txt")
    candidates = {name.casefold(), choice["output_dir"].casefold()}
    if any(row.name.casefold() in candidates and row.enabled for row in rows):
        raise JobError("lod_previous_output_enabled", "Disable or remove the previous output mod before generating LOD.")
    if any(row.name.casefold() == name.casefold() and row.locked for row in rows):
        raise JobError("lod_output_locked", "Unlock the output mod before replacing it.")
    if target.exists() and not request.get("replace_output", False):
        raise JobError("lod_output_exists", "Set replace_output:true to replace the disabled output after a successful run.")

    def check():
        if cancel.is_set():
            raise JobCancelled("LOD planning cancelled.")

    return name, target, tree_identity(target, check)


def _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel):
    from Utils.profiles.state import read_only_profile_state

    with read_only_profile_state():
        return _build_plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)


def _build_plan(game, profile, tool_id, catalogue_revision, choice, request, cancel):
    from Utils.bethesda.lod_run import arguments, executable, saved_options, validate_output_path
    from Utils.bethesda.xedit_run import preview_prefix, _check_prefix
    from Utils.profiles.groups import is_group
    from .tools import catalogue

    def check():
        if cancel.is_set():
            raise JobCancelled("LOD planning cancelled.")

    check()
    selected = _selected_game(game, profile)
    if not selected.is_configured():
        raise JobError("game_not_configured", "Configure the selected game before generating LOD.")
    if is_group(profile):
        raise JobError("profile_group_unsupported", "Generate LOD from a concrete profile so its output has an owner.")
    if catalogue(game, profile)["revision"] != catalogue_revision:
        raise JobError("stale_tool_catalogue", "Refresh the selected profile's tools.list catalogue.")
    settings = saved_options(selected, choice, tool_id)
    settings.update({key: value for key, value in request.items() if key in settings})
    exe = executable(selected, choice)
    prefix = preview_prefix(selected, exe, settings["proton_name"], settings["prefix_mode"])
    _check_prefix(prefix, settings["launch_env"])
    output = Path(request["output_path"]).expanduser().absolute()
    validate_output_path(selected, choice, output, prefix)
    name, target, target_identity = _mod_target(selected, profile, choice, request, cancel)
    deploy = request.get("deploy", True)
    if selected.get_deploy_active():
        last = selected.get_last_deployed_profile()
        if not isinstance(last, str) or not last or Path(last).name != last:
            raise JobError("lod_restore_target_mismatch", "Recover the unavailable deployed profile first.")
        recovery = profile.parent / last
        if not recovery.is_dir():
            raise JobError("lod_restore_target_mismatch", "Recover the unavailable deployed profile first.")
        previous = _selected_game(game, recovery)
        if _binding(previous)[:2] != _binding(selected)[:2]:
            raise JobError("lod_restore_target_mismatch", "Restore the deployed profile's different game/prefix first.")
        if not deploy and last != profile.name:
            raise JobError("lod_deployed_profile_mismatch", "Deploy this profile or select the currently deployed profile.")
    inputs = _inputs(selected, profile, exe)
    inputs[str(profile / "loadorder.txt")] = _identity(profile / "loadorder.txt")
    sources = {str(root): _source_tree(root, cancel) for root in
               (Path(selected.get_effective_mod_staging_path()), selected.get_game_path() / "Data",
                selected.get_game_path() / "Data_Core")}
    parent_identity = _identity(output.parent)
    snapshot = {"game": str(game.game_id), "profile": str(profile), "tool_id": tool_id,
                "catalogue_revision": catalogue_revision, "choice": choice, "request": request,
                "settings": settings, "exe": [str(exe), _identity(exe)], "prefix": prefix,
                "prefix_files": {n: _identity(Path(prefix["prefix_path"]) / n) for n in ("user.reg", "system.reg", "userdef.reg")},
                "runner": _identity(Path(prefix["script"])), "inputs": inputs, "sources": sources,
                "application_binaries": _application_binaries(exe),
                "output_mod": [str(target), target_identity],
                "output_parent": [token[:3] for token in parent_identity] if parent_identity else None}
    if inputs != {path: _identity(Path(path)) for path in inputs}:
        raise JobError("stale_lod_run", "Profile or launch settings changed while planning.")
    return {"revision": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            "game": str(game.game_id), "profile": profile.name, "tool_id": tool_id,
            "settings": settings, "exe_path": str(exe), "prefix": prefix,
            "arguments": arguments(selected, choice, output, Path(prefix["compat_data"]), settings["launch_args"]),
            "output_path": str(output), "output_mod": {"name": name, "path": str(target),
                                                       "replacing": target_identity is not None},
            "deploy_before_run": deploy, "root_folder_enabled": request.get("root_folder_enabled", True),
            "save_settings": request.get("save_settings", False), "restore_after_run": True,
            "external_tool_ui": True}


def plan_runner(game, profile, tool_id, catalogue_revision, choice, request, cancel):
    def run(log, progress):
        from Utils.bethesda.tool_run import ToolRunError
        from Utils.wizards.payload import PayloadInstallError

        progress(0, 1, "plan_lod_run")
        try:
            result = _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)
        except (ToolRunError, PayloadInstallError) as exc:
            raise _map_error(exc) from exc
        except OSError as exc:
            raise JobError("lod_plan_unavailable", "Could not read the LOD run inputs.", {"reason": str(exc)}) from exc
        progress(1, 1, "plan_lod_run")
        return result
    return run


def run_runner(game, profile, tool_id, catalogue_revision, choice, request, revision, cancel, *, on_started=lambda: None):
    def run(log, progress):
        from Utils.bethesda.lod_run import run_lod, saved_options
        from Utils.bethesda.tool_run import ToolRunCancelled, ToolRunError
        from Utils.deployment.locking import DeploymentBusy
        from Utils.executables import launch
        from Utils.operations import OperationBusy, OperationCancelled
        from Utils.wizards.payload import PayloadInstallError, tree_identity

        try:
            initial = _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)
            if initial["revision"] != revision:
                raise JobError("stale_lod_run", "LOD inputs changed; plan the run again.")
            selected = _selected_game(game, profile)
            binding = _binding(selected)
            exe = Path(initial["exe_path"])
            runner_path = Path(initial["prefix"]["script"])
            identities = [_identity(exe), _identity(runner_path)]
            binaries = _application_binaries(exe)
            inputs = _inputs(selected, profile, exe)
            inputs[str(profile / "loadorder.txt")] = _identity(profile / "loadorder.txt")
            mod_target = Path(initial["output_mod"]["path"])
            mod_identity = tree_identity(mod_target)

            def check_binding():
                if _binding(_selected_game(game, profile)) != binding:
                    raise JobError("stale_lod_run", "The selected profile's paths changed.")

            def checkpoint():
                check_binding()
                if (identities != [_identity(exe), _identity(runner_path)] or binaries != _application_binaries(exe)
                        or inputs != {p: _identity(Path(p)) for p in inputs}):
                    raise JobError("stale_lod_run", "Profile, tool or launch settings changed during the LOD run.")

            def after_prepare():
                nonlocal inputs
                check_binding()
                inputs = {p: _identity(Path(p)) for p in inputs}

            def verify():
                if _plan(game, profile, tool_id, catalogue_revision, choice, request, cancel)["revision"] != revision:
                    raise JobError("stale_lod_run", "LOD inputs changed; plan the run again.")

            def before_restore():
                check_binding()
                if inputs[str(selected._deploy_state_file)] != _identity(selected._deploy_state_file):
                    raise JobError("stale_lod_run", "Deployment ownership changed during the LOD run.")

            def before_import():
                checkpoint()
                if tree_identity(mod_target) != mod_identity:
                    raise JobError("stale_lod_run", "The previous output mod changed during generation.")
                _mod_target(selected, profile, choice, request, cancel)

            def import_checkpoint():
                check_binding()
                if cancel.is_set():
                    raise ToolRunCancelled(choice["label"])

            settings = initial["settings"]

            def save_settings():
                if not initial["save_settings"]:
                    return False
                launch.save_proton_override(selected, exe.name, settings["proton_name"])
                launch.save_prefix_mode(selected, exe.name, settings["prefix_mode"])
                launch.save_winetricks_style(selected, exe.name, settings["winetricks_style"])
                launch.save_tool_launch_args(exe, settings["launch_args"])
                launch.save_tool_launch_env(exe, settings["launch_env"])
                launch.save_wizard_prefer_discrete_gpu(selected, tool_id, settings["prefer_discrete_gpu"])
                if saved_options(selected, choice, tool_id) != settings:
                    raise JobError("lod_settings_save_failed", "Could not save all LOD launch settings.", {"state_persisted": True})
                return True

            result = run_lod(selected, choice, exe, settings, Path(initial["output_path"]), initial["output_mod"]["name"],
                             prefix=initial["prefix"], cancel=cancel, before_write=verify, before_import=before_import,
                             import_checkpoint=import_checkpoint,
                             checkpoint=checkpoint, after_prepare=after_prepare, after_restore=after_prepare,
                             before_restore=before_restore, prepare_settings=save_settings,
                             on_started=on_started,
                             deploy_first=initial["deploy_before_run"], root_folder_enabled=initial["root_folder_enabled"],
                             log_fn=log, progress_fn=progress)
        except ToolRunCancelled as exc:
            raise JobCancelled(str(exc), exc.details) from exc
        except (ToolRunError, PayloadInstallError) as exc:
            raise _map_error(exc) from exc
        except (OperationBusy, DeploymentBusy) as exc:
            raise JobError("operation_busy", str(exc), getattr(exc, "details", {})) from exc
        except OperationCancelled as exc:
            raise JobCancelled("LOD run cancelled.") from exc
        except OSError as exc:
            raise JobError("lod_run_unavailable", "Could not read the LOD run inputs.", {"reason": str(exc)}) from exc
        return {**result, "game": str(game.game_id), "profile": profile.name, "tool_id": tool_id, "exe_path": str(exe)}
    return run
