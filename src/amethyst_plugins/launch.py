from __future__ import annotations

import functools
import importlib.util
import sys
import threading
import uuid
from pathlib import Path

from .catalog import (data_path, discover, load_data, load_settings,
                      plugin_directory, save_data, settings_path)


_local = threading.local()


class LaunchHooks:
    def __init__(self, log):
        self.log = log
        self.instances = []
        self.finished = False
        self.lock = threading.Lock()

    def finish(self, completion):
        with self.lock:
            if self.finished:
                return
            self.finished = True
        for name, plugin, module, context, token in reversed(self.instances):
            try:
                callback = getattr(module, "after_launch", None)
                if callable(callback):
                    callback({**context, "completion": dict(completion)}, token)
            except Exception as exc:
                self.log(f"{plugin.name}: launch cleanup failed: {exc}")
            finally:
                self._unload(name)
        self.instances.clear()

    @staticmethod
    def _unload(name):
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)

    def detach(self):
        for name, *_ in self.instances:
            self._unload(name)
        self.instances.clear()


def prepare_launch(game, executable=None, *, log_fn=lambda message: None):
    hooks = LaunchHooks(log_fn)
    if game is None:
        return hooks
    plugins, errors = discover(game.game_id)
    for error in errors:
        log_fn(f"Plugin discovery: {error}")
    plugins = [(p, load_settings(p)) for p in plugins if p.launch_hooks is not None]
    plugins = [(p, settings) for p, settings in plugins if settings.get("enabled") is not False]
    if not plugins or not game.get_deploy_active():
        return hooks
    profile = getattr(game, "_active_profile_dir", None)
    if profile is None:
        profile = game.get_profile_root() / "profiles" / (game.get_last_deployed_profile() or "default")
    profile = Path(profile)
    if (not profile.is_dir()
            or (game.get_last_deployed_profile() or "default") != profile.name):
        raise RuntimeError("Deploy the selected profile before running plugin launch hooks.")
    from Utils.vfs import effective_tool_data_root, effective_tool_game_root
    from Utils.executables.launch import is_game_launch_exe
    from Utils.processes import game as processes
    from Utils.processes import report
    session = processes.current()
    if session is not None:
        session.attach_launch_report(report.current())
        session.begin_preparation()

    def check_cancelled():
        if session is not None and (session.stopping or session.finished):
            raise RuntimeError("The launch was cancelled.")

    def resolve_data_files(paths):
        import sqlite3
        from Utils.filegraph.service import resolved_data_sources
        check_cancelled()
        try:
            return {relative: str(path) for relative, path
                    in resolved_data_sources(game, profile, paths).items()}
        except (OSError, sqlite3.Error) as exc:
            log_fn(f"Plugin Filegraph lookup unavailable; checking deployed files: {exc}")
            return {}

    root = effective_tool_game_root(game)
    data = effective_tool_data_root(game)
    prefix = game.get_prefix_path()
    base = {
        "api_version": 1, "game_id": game.game_id, "game": game.name,
        "profile": profile.name, "profile_dir": str(profile.absolute()),
        "game_path": str(root), "data_root": str(data),
        "configured_game_path": str(game.get_game_path()),
        "overwrite_path": str(game.get_effective_overwrite_path()),
        "wine_prefix": str(prefix) if prefix is not None else None,
        "executable": str(executable) if executable is not None else None,
        "is_game": executable is None or is_game_launch_exe(game, Path(executable)),
        "log": log_fn,
        "check_cancelled": check_cancelled,
        "resolve_data_files": resolve_data_files,
    }
    try:
        for plugin, settings in plugins:
            check_cancelled()
            name = f"_amethyst_launch_{uuid.uuid4().hex}"
            spec = importlib.util.spec_from_file_location(
                name, plugin.launch_hooks,
                submodule_search_locations=[str(plugin.launch_hooks.parent)])
            module = importlib.util.module_from_spec(spec)
            context = {
                **base, "plugin_id": plugin.id,
                "plugin_dir": str(plugin_directory(plugin)),
                "settings_path": str(settings_path(plugin)),
                "data_path": str(data_path(plugin)), "settings": settings,
                "load_data": lambda p=plugin: load_data(p),
                "save_data": lambda value, p=plugin: save_data(p, value),
            }
            hooks.instances.append((name, plugin, module, context, None))
            sys.modules[name] = module
            spec.loader.exec_module(module)
            callback = getattr(module, "before_launch", None)
            if not callable(callback):
                raise ValueError(f"{plugin.name}: launch hooks must export before_launch(context).")
            token = callback(context)
            hooks.instances[-1] = (name, plugin, module, context, token)
    except Exception:
        hooks.finish({"started": False, "cancelled": True, "stopped": False, "returncodes": []})
        raise
    return hooks


def launch_hook(game_index):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            if getattr(_local, "running", False):
                return function(*args, **kwargs)
            game = args[game_index] if len(args) > game_index else kwargs["game"]
            if game is None:
                return function(*args, **kwargs)
            executable = None if game_index == 0 else (args[0] if args else next(
                kwargs[key] for key in ("exe_path", "sh_path", "jar_path") if key in kwargs))
            log = kwargs.get("log_fn", args[game_index + 1] if len(args) > game_index + 1 else lambda message: None)
            from Utils.processes import game as processes
            _local.running = True
            hooks = None
            had_entry = hasattr(game, "_plugin_launcher_entry")
            previous_entry = getattr(game, "_plugin_launcher_entry", None)
            try:
                from .launchers import matches_game_path, selected_entry
                game._plugin_launcher_entry = selected_entry(game, log=log)
                entry = game._plugin_launcher_entry
                if entry is not None:
                    if (not matches_game_path(game, entry["game_path"])
                            or str(game.get_prefix_path() or "") != str(entry["prefix_path"] or "")):
                        raise ValueError("The launcher installation changed. Restore and reconfigure it before launching.")
                hooks = prepare_launch(game, executable, log_fn=log)
                session = processes.current()
                if hooks.instances and session is not None:
                    session.add_finish_callback(hooks.finish)
                    if session.preparing:
                        if not session.end_preparation():
                            return
                    elif not session.active:
                        return
                result = function(*args, **kwargs)
                if hooks.instances and session is not None:
                    session.finish_dispatch()
                return result
            except Exception:
                if hooks is not None:
                    hooks.finish({"started": False, "cancelled": True, "stopped": False, "returncodes": []})
                session = processes.current()
                if session is not None and session.preparing:
                    session.end_preparation()
                    session.cancel()
                raise
            finally:
                if hooks is not None and processes.current() is None:
                    hooks.detach()
                _local.running = False
                if had_entry:
                    game._plugin_launcher_entry = previous_entry
                else:
                    game.__dict__.pop("_plugin_launcher_entry", None)
        return wrapped
    return decorate
