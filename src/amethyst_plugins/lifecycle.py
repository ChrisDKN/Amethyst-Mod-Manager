from __future__ import annotations

import importlib.util
import sys
import uuid
from copy import deepcopy
from functools import wraps
from pathlib import Path

from .catalog import discover, load_data, load_settings, plugin_directory


class LifecycleHooks:
    def __init__(self, game, profile_dir, log_fn, cancel=None):
        self.game = game
        self.profile_dir = Path(profile_dir)
        self.log = log_fn
        self.cancel = cancel
        plugins, errors = discover(getattr(game, "game_id", ""))
        for error in errors:
            log_fn(f"Plugin discovery: {error}")
        self.plugins = [(p, load_settings(p)) for p in plugins if p.lifecycle_hooks]
        self.plugins = [(p, settings) for p, settings in self.plugins if settings.get("enabled") is not False]

    def has(self, event):
        return any(event in dict(plugin.lifecycle_hooks) for plugin, _settings in self.plugins)

    def check_cancelled(self):
        if self.cancel is not None and self.cancel.is_set():
            from Utils.operations import OperationCancelled
            raise OperationCancelled("The plugin operation was cancelled.")

    def call(self, event, fields=None, *, strict=True):
        for plugin, settings in self.plugins:
            path = dict(plugin.lifecycle_hooks).get(event)
            if path is None:
                continue
            name = f"_amethyst_lifecycle_{uuid.uuid4().hex}"
            try:
                if strict:
                    self.check_cancelled()
                spec = importlib.util.spec_from_file_location(name, path,
                    submodule_search_locations=[str(path.parent)])
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                callback = getattr(module, event, None)
                if not callable(callback):
                    raise ValueError(f"Missing {event}(context) callback.")
                context = {
                    "api_version": 1, "plugin_id": plugin.id, "event": event,
                    "plugin_dir": str(plugin_directory(plugin)), "settings": deepcopy(settings),
                    "data": load_data(plugin), "game_id": self.game.game_id,
                    "game": getattr(self.game, "name", self.game.game_id),
                    "profile": self.profile_dir.name, "profile_dir": str(self.profile_dir.absolute()),
                    "log": lambda message, p=plugin: self.log(f"{p.name}: {message}"),
                    "check_cancelled": self.check_cancelled,
                    **deepcopy(fields or {}),
                }
                from .metadata import provider_context
                context["metadata"] = provider_context(plugin, {"profile_path": str(self.profile_dir)})["metadata"]
                result = callback(context)
                if result is False:
                    raise ValueError("The plugin rejected this operation.")
                if result is not None and result is not True:
                    raise ValueError("Lifecycle callbacks must return None, True, or False.")
                if strict:
                    self.check_cancelled()
            except Exception as exc:
                from Utils.operations import OperationCancelled
                if strict:
                    if isinstance(exc, OperationCancelled):
                        raise
                    raise ValueError(f"{plugin.name} ({event}): {exc}") from exc
                self.log(f"{plugin.name}: {event} observer failed: {exc}")
            finally:
                for key in list(sys.modules):
                    if key == name or key.startswith(name + "."):
                        sys.modules.pop(key, None)


def prepared_fields(prepared, result=None):
    fields = {"archive_path": str(prepared.archive), "mod_name": prepared.mod_name,
              "extract_dir": str(prepared.extract_dir), "source_root": str(prepared.src_root),
              "installer": ("plugin" if prepared.plugin_install is not None else "fomod" if prepared.is_fomod() else "bain" if prepared.is_bain()
                            else "multi_mod" if prepared.is_multi_mod() else "bundle" if prepared.is_bundle()
                            else "plain")}
    if prepared.plugin_install is not None:
        fields["plugin_installer"] = prepared.plugin_install.summary()
    if prepared.plugin_source is not None:
        fields["download_source"] = deepcopy(prepared.plugin_source)
    if result is not None:
        from Utils.mods.copy import resolve_target_staging
        staging = Path(resolve_target_staging(prepared.game, prepared.profile_dir))
        names = list(getattr(prepared, "installed_mod_names", ()) or [result])
        fields.update(success=True, installed_mods=[{"name": name, "path": str(staging / name)} for name in names])
    return fields


def before_prepared_install(prepared, log_fn):
    from Utils.operations import OperationCancelled
    hooks = getattr(prepared, "_manager_lifecycle", None)
    if hooks is None:
        hooks = LifecycleHooks(prepared.game, prepared.profile_dir, log_fn)
        prepared._manager_lifecycle = hooks
    try:
        if hooks.has("before_install"):
            hooks.call("before_install", prepared_fields(prepared))
    except OperationCancelled as exc:
        prepared.cleanup()
        log_fn(str(exc))
        return False
    except BaseException:
        prepared.cleanup()
        raise
    return True


def after_prepared_install(prepared, result, log_fn):
    if result is None:
        return
    hooks = getattr(prepared, "_manager_lifecycle", None)
    if hooks is not None and hooks.has("after_install"):
        try:
            hooks.call("after_install", prepared_fields(prepared, result), strict=False)
        except Exception as exc:
            log_fn(f"Plugin install observation failed: {exc}")


def archive_hooks(function):
    @wraps(function)
    def prepare(archive_path, game, profile_dir, **kwargs):
        from Utils.operations import OperationCancelled
        hooks = LifecycleHooks(game, profile_dir, kwargs["log_fn"], kwargs.get("cancel"))
        prepared = None
        try:
            hooks.call("before_archive", {"archive_path": str(archive_path),
                                         "mod_name": kwargs.get("preferred_name", "")})
            prepared = function(archive_path, game, profile_dir, **kwargs)
            if prepared is not None:
                prepared._manager_lifecycle = hooks
                if hooks.has("after_extract"):
                    hooks.call("after_extract", prepared_fields(prepared))
            return prepared
        except OperationCancelled as exc:
            if prepared is not None:
                prepared.cleanup()
            kwargs["log_fn"](str(exc))
            return None
        except BaseException:
            if prepared is not None:
                prepared.cleanup()
            raise
    return prepare


def install_hooks(function):
    @wraps(function)
    def install(prepared, fomod_selections, **kwargs):
        if not before_prepared_install(prepared, kwargs["log_fn"]):
            return None
        result = function(prepared, fomod_selections, **kwargs)
        after_prepared_install(prepared, result, kwargs["log_fn"])
        return result
    return install


def with_deploy_hooks(game, profile, log_fn, run):
    hooks = LifecycleHooks(game, Path(game.get_profile_root()) / "profiles" / profile, log_fn)
    success = False
    started = False
    try:
        try:
            hooks.call("before_deploy")
        except ValueError as exc:
            log_fn(f"Deploy rejected: {exc}")
            return False
        started = True
        success = run()
        return success
    finally:
        hooks.call("after_deploy", {"success": bool(success), "started": started}, strict=False)
