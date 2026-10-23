from __future__ import annotations

import hashlib
import importlib.util
import inspect
import re
import sys

from .catalog import discover, load_settings


def load(games):
    from Games.base_game import BaseGame
    errors = []
    plugins, discovery_errors = discover(None)
    errors.extend(("manager plugins", error) for error in discovery_errors)
    ids = {game.game_id.casefold() for game in games.values()}
    names = {name.casefold() for name in games}
    for plugin in plugins:
        if load_settings(plugin).get("enabled") is False:
            continue
        for path in plugin.game_handlers:
            name = "_amethyst_game_" + hashlib.sha256(str(path).encode()).hexdigest()[:24]
            try:
                if path.resolve() != path or not path.is_relative_to(plugin.directory):
                    raise ValueError("The handler path changed; rediscover the plugin.")
                spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)])
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                candidates = [value() for value in vars(module).values() if inspect.isclass(value)
                              and value.__module__ == name and issubclass(value, BaseGame) and value is not BaseGame]
                pending, pending_ids = {}, set()
                for game in candidates:
                    if (not isinstance(game.game_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", game.game_id)
                            or not isinstance(game.name, str) or not 1 <= len(game.name) <= 200
                            or game.name.casefold() in names or game.name.casefold() in {name.casefold() for name in pending}
                            or game.game_id.casefold() in ids or game.game_id.casefold() in pending_ids):
                        raise ValueError("Plugin game names and IDs must be unique and cannot replace existing games.")
                    game.manager_plugin_id = plugin.id
                    pending[game.name] = game
                    pending_ids.add(game.game_id.casefold())
                if not candidates:
                    raise ValueError("The handler file declares no BaseGame subclasses.")
                games.update(pending)
                names.update(name.casefold() for name in pending)
                ids.update(pending_ids)
            except Exception as exc:
                errors.append((f"{plugin.id}/{path.name}", str(exc)))
                for key in list(sys.modules):
                    if key == name or key.startswith(name + "."):
                        sys.modules.pop(key, None)
    return errors
