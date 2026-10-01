from __future__ import annotations

import threading
from contextlib import ExitStack
from pathlib import Path


class ToolRunError(RuntimeError):
    def __init__(self, name, message, details=None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


class ToolRunCancelled(ToolRunError):
    def __init__(self, label="xEdit"):
        super().__init__("job_cancelled", f"{label} run cancelled.")


def run_bethesda_tool(game, choice, exe, proton_name, prefix_mode, *, mode="interactive", plugins=(),
              deploy_first=False, root_folder_enabled=True, launch_args=None, launch_env=None,
              winetricks_style=None, cancel=None, prefix=None, before_write=lambda: None,
              checkpoint=lambda: None, after_prepare=lambda: None, on_started=lambda: None,
              prepare_settings=lambda: False, before_restore=lambda: None,
              log_fn=lambda _m: None, progress_fn=lambda *_a: None, owner=None, family="xedit", build_arguments=None,
              prepare_environment=lambda _env: None, complete=lambda _result: {},
              after_restore=lambda: None, resources=(), capture_changes=None):
    from Utils.bethesda.xedit_run import arguments, preview_prefix, _check_prefix
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
    from Utils.profiles.state import read_only_profile_state

    if mode not in ("interactive", "qac_all") or (mode == "qac_all" and not choice.get("qac", False)):
        raise ToolRunError("invalid_xedit_mode", "Choose interactive mode or a QuickAutoClean batch tool.")
    if mode == "qac_all" and not plugins:
        raise ToolRunError("xedit_no_dirty_plugins", "No selected enabled plugins need cleaning.")
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
            raise ToolRunCancelled(choice["label"])
        checkpoint()

    def monitor_cancel():
        while not monitor_done.wait(0.1):
            if cancel.is_set():
                reap_live_tools(owner=owner, log_fn=log_fn, timeout=2)

    def save_changes():
        shutdown_prefix_wineserver(script, compat_data, log_fn=log_fn, wait=True)
        result["finalized_saves"] += finalize_xedit_saves(data_dir, log_fn=log_fn, strict=True)
        result["saved_vfs_plugins"] += persist_xedit_vfs_changes(game, session, log_fn=log_fn)
        if capture_changes is not None:
            result["captured_plugins"] = result.get("captured_plugins", 0) + capture_changes(session)

    with read_only_profile_state(), acquire_game_operation(game, f"tools.{family}.run"):
        with game_mutation_lock(game), ExitStack() as stack:
            stack.enter_context(acquire_resource_operation(
                f'wine-prefix:{Path(prefix["prefix_path"]).resolve()}', f"tools.{family}.run", cancel=cancel))
            for resource in resources:
                stack.enter_context(acquire_resource_operation(resource, f"tools.{family}.run", cancel=cancel))
            check()
            before_write()
            _check_prefix(prefix, launch_env)
            try:
                result["settings_persisted"] = prepare_settings()
                deployed = True
                if deploy_first:
                    result["deploy_attempted"] = True
                    progress_fn(0, 1, f"deploy_{family}")
                    deployed = _run_deploy_pipeline(game, game._active_profile_dir.name,
                                                   log_fn=log_fn, root_folder_enabled=root_folder_enabled,
                                                   path_load_options={"read_only": True, "prefix_fallback": False})
                after_prepare()
                if not deployed:
                    raise ToolRunError(f"{family}_deploy_failed", f"Deployment failed before the {choice['label']} run.")
                check()
                result["prefix_preparation_attempted"] = True
                progress_fn(0, 1, f"prepare_{family}_prefix")
                runner_name = (Path(prefix["script"]).parent.name if prefix_mode != "game" else proton_name)
                resolved = resolve_tool_prefix(exe, game, runner_name, prefix_mode, log_fn=log_fn,
                                                launch_env=launch_env, winetricks_style=winetricks_style)
                if resolved is None:
                    raise ToolRunError(f"{family}_runner_unavailable", "Could not prepare the selected Wine/Proton prefix.")
                resolved_script, resolved_compat, env = resolved
                if (Path(resolved_script).resolve() != Path(prefix["script"]).resolve()
                        or Path(resolved_compat).resolve() != Path(prefix["compat_data"]).resolve()):
                    raise ToolRunError(f"{family}_prefix_changed", "The resolved runner or prefix changed after inspection.",
                                        {"resolved_script": str(resolved_script), "resolved_compat_data": str(resolved_compat)})
                script, compat_data = resolved_script, resolved_compat
                prepare_environment(env)
                env["WINEPREFIX"] = prefix["prefix_path"]
                env["STEAM_COMPAT_DATA_PATH"] = prefix["compat_data"]
                check()
                seed = (f'{choice["discord_mode"]}Edit' if choice["discord"] else choice["label"]) if family == "xedit" else None
                prepare_xedit_prefix(game, compat_data, script, env, xedit_name=seed,
                                     exe=exe if seed else None, log_fn=log_fn)
                check()
                session = begin_xedit_vfs_session(game, log_fn=log_fn)
                data_dir = session.data_dir if session is not None else game.get_mod_data_path()
                args = (build_arguments(compat_data) if build_arguments is not None
                        else arguments(game, choice, exe, compat_data, mode, launch_args))
                monitor = threading.Thread(target=monitor_cancel, daemon=True, name=f"{family}-cancel")
                monitor.start()
                queue = list(plugins) if mode == "qac_all" else [None]
                for index, plugin in enumerate(queue):
                    check()
                    progress_fn(index, len(queue), f"clean_xedit:{plugin}" if plugin else f"run_{family}")
                    result["process_started"] = True
                    on_started()
                    code = run_tool_logged(script, exe, env, log_fn=log_fn,
                                           extra_args=args + ([plugin] if plugin else []),
                                           label=choice["label"], game=game, owner=owner)
                    result["exit_codes"].append(code)
                    save_changes()
                    check()
                    if code:
                        raise ToolRunError(f"{family}_process_failed", f"{choice['label']} exited with an error.",
                                            {"exit_code": code, "plugin": plugin})
                    if plugin:
                        result["completed_plugins"].append(plugin)
            except Exception as exc:
                pending_error = exc if hasattr(exc, "details") else ToolRunError(
                    f"{family}_run_failed", f"Could not complete the {choice['label']} run.", {"reason": str(exc)})
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
                    progress_fn(0, 1, f"restore_{family}")
                    try:
                        before_restore()
                        result["restored"] = restore_after_xedit(game, choice["label"], log_fn=log_fn,
                                                                  operation_locked=True,
                                                                  path_load_options={"read_only": True, "prefix_fallback": False})
                        if not result["restored"]:
                            failures.append({"stage": "restore", "reason": "Restore did not complete."})
                    except Exception as exc:
                        failures.append({"stage": "restore", "reason": str(exc)})
                if failures:
                    raise ToolRunError(f"{family}_recovery_failed", f"{choice['label']} recovery did not finish; deployment and edited files need recovery.",
                                        {**result, "failures": failures, "state_persisted": True})
                if pending_error is not None:
                    pending_error.details.update(result)
                    pending_error.details["state_persisted"] = bool(
                        result["deploy_attempted"] or result["prefix_preparation_attempted"]
                        or result["settings_persisted"] or pending_error.details.get("state_persisted"))
            try:
                after_restore()
                result.update(complete(result))
            except Exception as exc:
                if hasattr(exc, "details"):
                    exc.details.update(result, state_persisted=True)
                    raise
                raise ToolRunError(f"{family}_completion_failed", "Could not complete the tool output registration.",
                                   {**result, "reason": str(exc), "state_persisted": True}) from exc
    progress_fn(1, 1, f"{family}_finished")
    return {"ok": True, **result}
