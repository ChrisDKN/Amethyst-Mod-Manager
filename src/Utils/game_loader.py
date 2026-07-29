"""
game_loader.py
Auto-discovers game handler classes from the Games/ directory.

Any .py file in Games/ (except __init__.py and base_game.py) that contains
a subclass of BaseGame is automatically registered. Bad/incomplete plugin
files are silently skipped so one broken handler doesn't break the rest.

Uses spec_from_file_location so folder names with spaces (e.g. "Stardew Valley")
work without needing a valid dotted module path.

Usage:
    from Utils.game_loader import discover_games
    games = discover_games()          # {game.name: BaseGame instance}
    sse = games["Skyrim Special Edition"]
"""

import importlib.util
import inspect
import os
import re
import sys
import traceback
from pathlib import Path

from Games.base_game import BaseGame

_EXCLUDED_STEMS   = {"__init__", "base_game", "ue5_game", "custom_game"}
_EXCLUDED_FOLDERS = {"Example", "Custom"}

# Cache so we keep using the same path even if cwd changes later (e.g. after os.chdir in install_mod)
_games_dir_cache: Path | None = None

# Records handler files that raised while loading during the last discover_games()
# run: [(relative-path, one-line reason), ...].  A failed load means that game
# silently vanishes from the UI even though the user's mods/config are untouched
# (those live in ~/.config and Profiles/).  The GUI reads this after startup to
# warn the user instead of leaving them to assume the game was deleted.  Also
# printed to stderr immediately, which the AppImage captures to its error log.
_load_failures: list[tuple[str, str]] = []


def get_load_failures() -> list[tuple[str, str]]:
    """Return handler files that failed to load in the last discover_games() run.

    Each entry is ``(relative_path, reason)``.  Empty when everything loaded.
    """
    return list(_load_failures)


def _find_games_dir() -> Path | None:
    """Return the Games directory (containing base_game.py and game subfolders)."""
    global _games_dir_cache

    def _valid_games_dir(cand: Path) -> bool:
        return cand.is_dir() and bool(list(cand.glob("*/*.py")))

    # Use cache if still valid (cwd can change later, e.g. during mod install)
    if _games_dir_cache is not None and _valid_games_dir(_games_dir_cache):
        return _games_dir_cache

    # 0. Environment variable (reliable when launcher changes cwd, e.g. some file managers)
    env_games = os.environ.get("MOD_MANAGER_GAMES")
    if env_games:
        try:
            cand = Path(env_games).resolve()
            if _valid_games_dir(cand):
                _games_dir_cache = cand
                return cand
        except Exception:
            pass

    # 0b. Games.base_game is in Games/ — its __file__'s parent IS the Games dir (works if we imported it)
    try:
        mod = sys.modules.get(BaseGame.__module__)
        if mod is not None:
            base_file = getattr(mod, "__file__", None)
            if base_file:
                cand = Path(base_file).resolve().parent
                if _valid_games_dir(cand):
                    _games_dir_cache = cand
                    return cand
    except Exception:
        pass

    # 1. From this module's loader (cwd-independent; works when launched from file manager etc.)
    try:
        spec = getattr(sys.modules.get(__name__), "__spec__", None)
        if spec is not None and getattr(spec, "origin", None):
            cand = Path(spec.origin).resolve().parent.parent / "Games"
            if _valid_games_dir(cand):
                _games_dir_cache = cand
                return cand
    except Exception:
        pass

    # 2. Relative to this file: Utils/game_loader.py -> parent.parent/Games
    try:
        cand = Path(__file__).resolve().parent.parent / "Games"
        if _valid_games_dir(cand):
            _games_dir_cache = cand
            return cand
    except Exception:
        pass

    # 3. Directory containing the main script (gui.py in src/ or gui/ package; Games may be sibling or parent's sibling)
    try:
        main = sys.modules.get("__main__")
        if main is not None:
            main_file = getattr(main, "__file__", None)
            if main_file:
                base = Path(main_file).resolve().parent
                for cand in (base / "Games", base.parent / "Games"):
                    if _valid_games_dir(cand):
                        _games_dir_cache = cand
                        return cand
    except Exception:
        pass

    # 4. From the already-imported Games.base_game module
    try:
        mod = sys.modules.get(BaseGame.__module__)
        base_file = getattr(mod, "__file__", None) if mod else None
        if base_file:
            cand = Path(base_file).resolve().parent
            if _valid_games_dir(cand):
                _games_dir_cache = cand
                return cand
    except Exception:
        pass

    # 5. sys.path[0] is typically the script's directory when running python script.py
    try:
        if sys.path[0]:
            cand = Path(sys.path[0]).resolve() / "Games"
            if _valid_games_dir(cand):
                _games_dir_cache = cand
                return cand
    except Exception:
        pass

    # 6. Current working directory (run.sh cds to src before running)
    try:
        cand = Path.cwd() / "Games"
        if _valid_games_dir(cand):
            _games_dir_cache = cand
            return cand
    except Exception:
        pass

    # 7. Search sys.path: join entry with "Games" (avoids resolve/cwd; sys.path[0] is script dir)
    for entry in sys.path:
        if not entry:
            continue
        try:
            p = Path(entry)
            if not p.is_dir():
                p = p.resolve()
                if not p.is_dir():
                    continue
            cand = p / "Games"
            if _valid_games_dir(cand):
                _games_dir_cache = cand
                return cand
        except Exception:
            continue

    # 8. Find Utils/game_loader.py in sys.path; Games is sibling of Utils
    try:
        for entry in sys.path:
            if not entry:
                continue
            loader_path = Path(entry) / "Utils" / "game_loader.py"
            if loader_path.is_file():
                cand = Path(entry) / "Games"
                if _valid_games_dir(cand):
                    _games_dir_cache = cand
                    return cand
                break  # found our loader, don't check other entries
    except Exception:
        pass
    return None


def _record_load_failure(py_file: Path, games_dir: Path, exc: Exception) -> None:
    """Note a handler file that raised while loading and echo it to stderr."""
    try:
        rel = str(py_file.relative_to(games_dir))
    except ValueError:
        rel = py_file.name
    reason = f"{type(exc).__name__}: {exc}"
    _load_failures.append((rel, reason))
    try:
        print(
            f"[game_loader] failed to load handler {rel}: {reason}\n"
            + traceback.format_exc(),
            file=sys.stderr,
        )
    except Exception:
        pass


def discover_games() -> dict[str, BaseGame]:
    """
    Scan Games/<GameFolder>/*.py, load each module from its file path, find
    BaseGame subclasses, instantiate them, and return {game.name: instance}.
    Also loads user-defined custom games from the config directory.
    """
    games: dict[str, BaseGame] = {}
    _load_failures.clear()
    games_dir = _find_games_dir()
    if games_dir is None:
        return games

    for py_file in sorted(games_dir.glob("*/*.py")):
        if py_file.stem in _EXCLUDED_STEMS or py_file.parent.name in _EXCLUDED_FOLDERS:
            continue

        # Key on folder AND stem — a bare stem collides in sys.modules when two
        # game folders ship a same-named handler file, silently dropping one.
        _folder = re.sub(r"\W", "_", py_file.parent.name)
        module_name = f"Games._loaded_{_folder}_{py_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(py_file))
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            # The module itself failed to import (bad/moved import, syntax error,
            # missing dependency). This is the case that made a game silently
            # vanish from the UI — users read that as "my game (and mods) got
            # deleted" though mods live untouched in ~/.config and Profiles/.
            # Record it so the GUI can warn, and echo to stderr (captured by the
            # AppImage error log) for diagnosis.
            _record_load_failure(py_file, games_dir, exc)
            continue

        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls is BaseGame:
                continue
            if not issubclass(cls, BaseGame):
                continue
            # Abstract bases (e.g. UE5Game) are imported by concrete handlers and
            # show up here too — they can't and shouldn't be instantiated.
            if inspect.isabstract(cls):
                continue
            # Strict __module__ match: classes IMPORTED into this file (e.g.
            # Fallout_3 in every Bethesda subclass file) carry their defining
            # module's name and are instantiated when THEIR file loads — the
            # old membership fallback constructed each base once per sibling
            # file (~13 Fallout_3 load_paths() runs per discovery).
            if cls.__module__ != module_name:
                continue
            try:
                instance = cls()
                games[instance.name] = instance
            except Exception as exc:
                # One un-instantiable class must not drop the file's other games,
                # nor should an imported concrete sibling (re-registered from many
                # files) be reported per-file. Log to stderr only.
                try:
                    print(
                        f"[game_loader] failed to instantiate {cls.__name__} "
                        f"from {py_file.name}: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                except Exception:
                    pass

    # Merge user-defined custom games (JSON files in ~/.config/.../custom_games/)
    try:
        from Games.Custom.custom_game import load_all_custom_games
        for name, instance in load_all_custom_games().items():
            if name not in games:          # built-in games take priority
                games[name] = instance
    except Exception:
        pass

    return games
