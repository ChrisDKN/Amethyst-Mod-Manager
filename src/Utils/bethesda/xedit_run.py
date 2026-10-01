from __future__ import annotations

import threading
from contextlib import ExitStack
from pathlib import Path


class XEditRunError(RuntimeError):
    def __init__(self, name, message, details=None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


class XEditRunCancelled(XEditRunError):
    def __init__(self):
        super().__init__("job_cancelled", "xEdit run cancelled.")


def executable(game, choice, use_64bit=False):
    from Utils.bethesda.xedit_install import installation_target

    relative = choice["optional_64bit_exe"] if use_64bit else choice["exe_name"]
    if not relative:
        raise XEditRunError("xedit_64bit_unavailable", "This build has no separate optional 64-bit executable.")
    path = installation_target(game, choice) / relative
    parent = path.parent
    anchor = installation_target(game, choice).parent.parent
    while parent != anchor:
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise XEditRunError("unsafe_tool_destination", "An xEdit application directory is a link or file.")
        parent = parent.parent
    if path.is_symlink() or not path.is_file():
        raise XEditRunError("xedit_executable_missing", "Install the selected xEdit executable first.",
                            {"path": str(path)})
    return path


def saved_options(game, choice, use_64bit=None):
    from Utils.executables.launch import (
        load_prefix_mode, load_proton_override, load_tool_launch_args,
        load_tool_launch_env, load_tool_use_64bit, load_winetricks_style,
    )

    if use_64bit is None:
        use_64bit = bool(choice["optional_64bit_exe"] and load_tool_use_64bit(game, choice["exe_name"]))
    exe = executable(game, choice, use_64bit)
    return {"use_64bit": use_64bit, "proton_name": load_proton_override(game, exe.name) or "",
            "prefix_mode": load_prefix_mode(game, exe.name), "launch_args": load_tool_launch_args(exe),
            "launch_env": load_tool_launch_env(exe), "winetricks_style": load_winetricks_style(game, exe.name)}


def preview_prefix(game, exe, proton_name, prefix_mode):
    from Utils.executables.launch import PREFIX_MODE_GAME, PREFIX_MODE_SHARED, shared_prefix_dir
    from Utils.wine.registry import normalize_pfx

    if prefix_mode == PREFIX_MODE_GAME:
        configured = game.get_prefix_path()
        if configured is None or not Path(configured).is_dir():
            raise XEditRunError("wine_prefix_unavailable", "The selected game prefix is unavailable.")
        from Utils.executables.launch import get_game_prefix_env
        resolved = get_game_prefix_env(game, allow_runner_fallback=True, read_only=True)
        if resolved is None:
            raise XEditRunError("xedit_runner_unavailable", "The game's prefix has no available Wine/Proton runner.")
        script, compat, _env = resolved
        prefix = normalize_pfx(Path(configured))
    else:
        from Utils.launchers.steam import find_any_installed_proton
        script = find_any_installed_proton(proton_name)
        if script is None:
            raise XEditRunError("xedit_runner_unavailable", "No installed Proton runner is available.")
        compat = (shared_prefix_dir(script.parent.name) if prefix_mode == PREFIX_MODE_SHARED
                  else exe.parent / f"prefix_{script.parent.name}")
        prefix = compat / "pfx"
    if script is None or not Path(script).is_file():
        raise XEditRunError("xedit_runner_unavailable", "The selected prefix has no available Wine/Proton runner.")
    return {"script": str(script), "compat_data": str(compat), "prefix_path": str(prefix)}


def arguments(game, choice, exe, compat_data, mode, launch_args=""):
    from Utils.executables.launch import parse_launch_args
    from Utils.wine.paths import to_wine_path
    from Utils.wine.registry import normalize_pfx

    pfx = normalize_pfx(compat_data)
    args = [f'-d:{to_wine_path(game.get_game_path() / "Data", pfx)}']
    if mode == "qac_all":
        args.extend(("-autoload", "-autoexit"))
    optional = choice["optional_64bit_exe"]
    use_64bit = bool(optional and exe.name == Path(optional).name)
    if use_64bit:
        args.append(f'-s:{to_wine_path(exe.parent.parent / "Edit Scripts", pfx)}\\')
    if choice["qac"] and (choice["discord"] or use_64bit):
        args.insert(0, "-quickautoclean")
    if choice["discord"]:
        args.insert(0, f'-{choice["discord_mode"]}')
    args.extend(parse_launch_args(launch_args))
    return args


def _check_prefix(prefix, launch_env):
    from Utils.executables.launch import parse_env_overrides
    from Utils.wine.repair import prefix_usage

    expected = Path(prefix["prefix_path"]).resolve()
    overrides = parse_env_overrides(launch_env)
    for key, value in (("WINEPREFIX", prefix["prefix_path"]),
                       ("STEAM_COMPAT_DATA_PATH", prefix["compat_data"])):
        if key in overrides and Path(overrides[key]).expanduser().resolve() != Path(value).resolve():
            raise XEditRunError("xedit_prefix_override", "Choose the prefix through prefix_mode instead of redirecting it in launch_env.")
    pids = prefix_usage(expected)
    if pids is None:
        raise XEditRunError("wine_prefix_scan_unavailable", "Could not check whether the selected prefix is in use.")
    if pids:
        raise XEditRunError("wine_prefix_in_use", "Close other processes using the selected prefix first.",
                            {"pids": sorted(pids)})


def run_xedit(game, choice, exe, proton_name, prefix_mode, *, mode="interactive", plugins=(),
              deploy_first=False, root_folder_enabled=True, launch_args=None, launch_env=None,
              winetricks_style=None, cancel=None, prefix=None, before_write=lambda: None,
              checkpoint=lambda: None, after_prepare=lambda: None, on_started=lambda: None,
              prepare_settings=lambda: False, before_restore=lambda: None,
              log_fn=lambda _m: None, progress_fn=lambda *_a: None, owner=None):
    from Utils.bethesda.xedit import (
        begin_xedit_vfs_session, finalize_xedit_saves, persist_xedit_vfs_changes,
        prepare_xedit_prefix, restore_after_xedit,
    )
    from Utils.deployment.locking import game_mutation_lock
    from Utils.deployment.pipeline import _run_deploy_pipeline
    from Utils.executables.launch import (
        load_tool_launch_args, load_tool_launch_env, reap_live_tools,
        resolve_tool_prefix, run_tool_logged, shutdown_prefix_wineserver,
    )
    from Utils.operations import acquire_game_operation, acquire_resource_operation

    if mode not in ("interactive", "qac_all") or (mode == "qac_all" and not choice["qac"]):
        raise XEditRunError("invalid_xedit_mode", "Choose interactive mode or a QuickAutoClean batch tool.")
    if mode == "qac_all" and not plugins:
        raise XEditRunError("xedit_no_dirty_plugins", "No selected enabled plugins need cleaning.")
    if launch_args is None:
        launch_args = load_tool_launch_args(exe)
    if launch_env is None:
        launch_env = load_tool_launch_env(exe)
    cancel = cancel or threading.Event()
    owner = owner if owner is not None else object()
    prefix = prefix or preview_prefix(game, exe, proton_name, prefix_mode)
    result = {"deploy_attempted": False, "prefix_preparation_attempted": False,
              "settings_persisted": False,
              "process_started": False, "restore_attempted": False, "restored": False,
              "completed_plugins": [], "exit_codes": [], "finalized_saves": 0, "saved_vfs_plugins": 0}
    script = compat_data = session = data_dir = None
    monitor_done = threading.Event()
    monitor = None
    pending_error = None

    def check():
        if cancel.is_set():
            raise XEditRunCancelled()
        checkpoint()

    def monitor_cancel():
        while not monitor_done.wait(0.1):
            if cancel.is_set():
                reap_live_tools(owner=owner, log_fn=log_fn, timeout=2)

    def save_changes():
        shutdown_prefix_wineserver(script, compat_data, log_fn=log_fn)
        result["finalized_saves"] += finalize_xedit_saves(data_dir, log_fn=log_fn, strict=True)
        result["saved_vfs_plugins"] += persist_xedit_vfs_changes(game, session, log_fn=log_fn)

    with acquire_game_operation(game, "tools.xedit.run"):
        with game_mutation_lock(game), ExitStack() as stack:
            stack.enter_context(acquire_resource_operation(
                f'wine-prefix:{Path(prefix["prefix_path"]).resolve()}', "tools.xedit.run", cancel=cancel))
            check()
            before_write()
            _check_prefix(prefix, launch_env)
            try:
                result["settings_persisted"] = prepare_settings()
                deployed = True
                if deploy_first:
                    result["deploy_attempted"] = True
                    progress_fn(0, 1, "deploy_xedit")
                    deployed = _run_deploy_pipeline(game, game._active_profile_dir.name,
                                                   log_fn=log_fn, root_folder_enabled=root_folder_enabled)
                after_prepare()
                if not deployed:
                    raise XEditRunError("xedit_deploy_failed", "Deployment failed before the xEdit run.")
                check()
                result["prefix_preparation_attempted"] = True
                progress_fn(0, 1, "prepare_xedit_prefix")
                runner_name = (Path(prefix["script"]).parent.name if prefix_mode != "game" else proton_name)
                resolved = resolve_tool_prefix(exe, game, runner_name, prefix_mode, log_fn=log_fn,
                                                launch_env=launch_env, winetricks_style=winetricks_style)
                if resolved is None:
                    raise XEditRunError("xedit_runner_unavailable", "Could not prepare the selected Wine/Proton prefix.")
                resolved_script, resolved_compat, env = resolved
                if (Path(resolved_script).resolve() != Path(prefix["script"]).resolve()
                        or Path(resolved_compat).resolve() != Path(prefix["compat_data"]).resolve()):
                    raise XEditRunError("xedit_prefix_changed", "The resolved runner or prefix changed after inspection.",
                                        {"resolved_script": str(resolved_script), "resolved_compat_data": str(resolved_compat)})
                script, compat_data = resolved_script, resolved_compat
                env["WINEPREFIX"] = prefix["prefix_path"]
                env["STEAM_COMPAT_DATA_PATH"] = prefix["compat_data"]
                check()
                seed = f'{choice["discord_mode"]}Edit' if choice["discord"] else choice["label"]
                prepare_xedit_prefix(game, compat_data, script, env, xedit_name=seed, exe=exe, log_fn=log_fn)
                check()
                session = begin_xedit_vfs_session(game, log_fn=log_fn)
                data_dir = session.data_dir if session is not None else game.get_game_path() / "Data"
                args = arguments(game, choice, exe, compat_data, mode, launch_args)
                monitor = threading.Thread(target=monitor_cancel, daemon=True, name="xedit-cancel")
                monitor.start()
                queue = list(plugins) if mode == "qac_all" else [None]
                for index, plugin in enumerate(queue):
                    check()
                    progress_fn(index, len(queue), f"clean_xedit:{plugin}" if plugin else "run_xedit")
                    result["process_started"] = True
                    on_started()
                    code = run_tool_logged(script, exe, env, log_fn=log_fn,
                                           extra_args=args + ([plugin] if plugin else []),
                                           label=choice["label"], game=game, owner=owner)
                    result["exit_codes"].append(code)
                    save_changes()
                    check()
                    if code:
                        raise XEditRunError("xedit_process_failed", "xEdit exited with an error.",
                                            {"exit_code": code, "plugin": plugin})
                    if plugin:
                        result["completed_plugins"].append(plugin)
            except Exception as exc:
                pending_error = exc if hasattr(exc, "details") else XEditRunError(
                    "xedit_run_failed", "Could not complete the xEdit run.", {"reason": str(exc)})
                raise pending_error
            finally:
                monitor_done.set()
                if monitor is not None:
                    monitor.join()
                failures = []
                if script is not None and compat_data is not None:
                    try:
                        if data_dir is not None:
                            save_changes()
                        else:
                            shutdown_prefix_wineserver(script, compat_data, log_fn=log_fn)
                    except Exception as exc:
                        failures.append({"stage": "save_plugins", "reason": str(exc)})
                if not failures and (result["deploy_attempted"] or result["process_started"]):
                    result["restore_attempted"] = True
                    progress_fn(0, 1, "restore_xedit")
                    try:
                        before_restore()
                        result["restored"] = restore_after_xedit(game, choice["label"], log_fn=log_fn,
                                                                  operation_locked=True)
                        if not result["restored"]:
                            failures.append({"stage": "restore", "reason": "Restore did not complete."})
                    except Exception as exc:
                        failures.append({"stage": "restore", "reason": str(exc)})
                if failures:
                    raise XEditRunError("xedit_recovery_failed", "xEdit recovery did not finish; deployment and edited files need recovery.",
                                        {**result, "failures": failures, "state_persisted": True})
                if pending_error is not None:
                    pending_error.details.update(result)
                    pending_error.details["state_persisted"] = bool(
                        result["deploy_attempted"] or result["prefix_preparation_attempted"]
                        or result["settings_persisted"] or pending_error.details.get("state_persisted"))
    progress_fn(1, 1, "xedit_finished")
    return {"ok": True, **result}
