from __future__ import annotations

import importlib.util
import sys
import threading
import uuid
from copy import deepcopy
from difflib import SequenceMatcher
from pathlib import Path

from Utils.mods.state import ModStateError


OPERATIONS = frozenset({"mods.set_enabled", "mods.move", "mods.rename", "mods.remove"})
_local = threading.local()


class OperationRejected(ModStateError, ValueError):
    def __init__(self, message):
        super().__init__("plugin_operation_rejected", message)


def validate_operation(game, profile, operation, **fields):
    from .catalog import discover, load_data, load_settings, plugin_directory
    from .metadata import provider_context
    if game is None:
        return
    if getattr(_local, "active", False):
        raise OperationRejected("Plugin operation guards cannot start another mod mutation.")
    plugins, _ = discover(game.game_id)
    for plugin in plugins:
        path = dict(plugin.operation_guards).get(operation)
        if path is None:
            continue
        settings = load_settings(plugin)
        if settings.get("enabled") is False:
            continue
        name = f"_amethyst_guard_{uuid.uuid4().hex}"
        try:
            _local.active = True
            spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            context = {"api_version": 1, "plugin_id": plugin.id, "plugin_dir": str(plugin_directory(plugin)),
                       "operation": operation, "game_id": game.game_id, "game": game.name,
                       "profile": Path(profile).name, "profile_dir": str(Path(profile).absolute()),
                       "settings": settings, "data": load_data(plugin), **deepcopy(fields)}
            context.update(provider_context(plugin, {"profile_path": str(profile)}))
            result = module.validate_operation(context)
            if result is False:
                raise ValueError("The plugin rejected this operation.")
            if result is not None and result is not True:
                raise ValueError("Operation guards must return None, True, or False.")
        except Exception as exc:
            raise OperationRejected(f"{plugin.name} ({operation}): {exc}") from exc
        finally:
            _local.active = False
            for key in list(sys.modules):
                if key == name or key.startswith(name + "."):
                    sys.modules.pop(key, None)


def moved_names(before, after):
    stable = {name for block in SequenceMatcher(None, before, after, autojunk=False).get_matching_blocks()
              for name in before[block.a:block.a + block.size]}
    return [name for name in before if name not in stable]


def validate_modlist(game, profile, previous, entries, edit_ctx=None, moving=None):
    old = {e.name: e for e in previous if not e.is_separator and not e.is_group_header}
    new = {e.name: e for e in entries if not e.is_separator and not e.is_group_header}
    changes = [{"name": name, "enabled": e.enabled} for name, e in new.items()
               if name in old and old[name].enabled != e.enabled]
    if changes:
        validate_operation(game, profile, "mods.set_enabled", mods=[c["name"] for c in changes], changes=changes)
    before = [name for name in old if name in new]
    after = [name for name in new if name in old]
    if before != after:
        if moving is not None:
            moved = [name for name in moving if name in old and name in new]
        elif edit_ctx and edit_ctx[0] == "move":
            moved = list(edit_ctx[1])
        else:
            moved = moved_names(before, after)
        validate_operation(game, profile, "mods.move", mods=moved, old_order=before, new_order=after)
