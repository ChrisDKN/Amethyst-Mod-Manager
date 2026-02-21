"""
game_loader.py
Auto-discovers game handler classes from the Games/ directory.

Any .py file in Games/ (except __init__.py and base_game.py) that contains
a subclass of BaseGame is automatically registered. Bad/incomplete plugin
files are silently skipped so one broken handler doesn't break the rest.

Usage:
    from Utils.game_loader import discover_games
    games = discover_games()          # {game.name: BaseGame instance}
    sse = games["Skyrim Special Edition"]
"""

import importlib
import inspect
from pathlib import Path

from Games.base_game import BaseGame

_GAMES_DIR = Path(__file__).parent.parent / "Games"

_EXCLUDED_STEMS   = {"__init__", "base_game"}
_EXCLUDED_FOLDERS = {"Example"}


def discover_games() -> dict[str, BaseGame]:
    """
    Scan Games/<GameFolder>/*.py, import each module, find BaseGame subclasses,
    instantiate them, and return {game.name: instance}.

    Each game handler lives inside its own named subfolder, e.g.:
        Games/Skyrim Special Edition/skyrim_se.py
    """
    games: dict[str, BaseGame] = {}

    for py_file in sorted(_GAMES_DIR.glob("*/*.py")):
        if py_file.stem in _EXCLUDED_STEMS or py_file.parent.name in _EXCLUDED_FOLDERS:
            continue

        # Build dotted module path: Games.<FolderName>.<stem>
        # Replace spaces in folder names with underscores for importlib
        folder = py_file.parent.name
        module_name = f"Games.{folder}.{py_file.stem}"
        try:
            module = importlib.import_module(module_name)
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(cls, BaseGame)
                    and cls is not BaseGame
                    and cls.__module__ == module_name
                ):
                    instance = cls()
                    games[instance.name] = instance
        except Exception:
            # Malformed handler — skip without crashing
            pass

    return games
