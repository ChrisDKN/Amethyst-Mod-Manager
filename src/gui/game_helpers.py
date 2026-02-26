"""
Game and profile helpers: _GAMES registry, load_games, profiles, create_profile, etc.
Used by TopBar, ModListPanel, PluginPanel, and App. No dependency on other gui modules.
"""

import json
import shutil
from pathlib import Path

from Games.base_game import BaseGame
from Utils.config_paths import get_config_dir, get_profiles_dir, get_last_game_path
from Utils.game_loader import discover_games

# Game handlers — populated by _load_games() when first called
_GAMES: dict[str, BaseGame] = {}


def _vanilla_plugins_for_game(game) -> dict[str, str]:
    """Return vanilla plugin names from the game's data dir.

    Returns a dict mapping ``lowercase_name -> original_cased_name`` so
    that ``name.lower() in result`` works like the old set, but callers
    can also retrieve the original filename for display.
    """
    game_path = game.get_game_path()
    if not game_path:
        return {}
    data_dir = game_path / "Data"
    core_dir = game_path / "Data_Core"
    scan_dir = core_dir if core_dir.is_dir() else data_dir
    if not scan_dir.is_dir():
        return {}
    exts = {e.lower() for e in game.plugin_extensions}
    return {
        entry.name.lower(): entry.name
        for entry in scan_dir.iterdir()
        if entry.is_file() and entry.suffix.lower() in exts
    }


def _load_games() -> list[str]:
    """Discover game handlers and return sorted display names (configured games only)."""
    global _GAMES
    new_games = discover_games()
    _GAMES.clear()
    _GAMES.update(new_games)
    names = sorted(name for name, game in _GAMES.items() if game.is_configured())
    return names if names else ["No games configured"]


def _profiles_for_game(game_name: str) -> list[str]:
    """Return sorted profile folder names for the given game, 'default' first."""
    game = _GAMES.get(game_name)
    if game is not None:
        profiles_dir = game.get_profile_root() / "profiles"
    else:
        profiles_dir = get_profiles_dir() / game_name / "profiles"
    if not profiles_dir.is_dir():
        return ["default"]
    names = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir())
    # Ensure 'default' is always first if present
    if "default" in names:
        names.remove("default")
        names.insert(0, "default")
    return names if names else ["default"]


def _create_profile(game_name: str, profile_name: str) -> Path:
    """Create a new profile folder, copying modlist.txt from default."""
    game = _GAMES.get(game_name)
    if game is not None:
        profiles_root = game.get_profile_root()
    else:
        profiles_root = get_profiles_dir() / game_name
    profile_dir = profiles_root / "profiles" / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    plugins = profile_dir / "plugins.txt"
    if not plugins.exists():
        plugins.touch()
    modlist = profile_dir / "modlist.txt"
    if not modlist.exists():
        default_modlist = profiles_root / "profiles" / "default" / "modlist.txt"
        if default_modlist.exists():
            shutil.copy2(default_modlist, modlist)
        else:
            modlist.touch()
    return profile_dir


def _save_last_game(game_name: str) -> None:
    """Persist the last-selected game name to the config directory."""
    try:
        get_last_game_path().write_text(
            json.dumps({"last_game": game_name}), encoding="utf-8"
        )
    except OSError:
        pass


def _load_last_game() -> str | None:
    """Return the previously saved game name, or None if not set / unreadable."""
    try:
        data = json.loads(get_last_game_path().read_text(encoding="utf-8"))
        return data.get("last_game")
    except (OSError, ValueError, KeyError):
        return None


def _clear_game_config(game_name: str) -> None:
    """Remove this game's config from ~/.config/AmethystModManager/games/<game_name>/.
    Causes the game to show as unconfigured on next use."""
    game_config_dir = get_config_dir() / "games" / game_name
    try:
        if game_config_dir.is_dir():
            shutil.rmtree(game_config_dir)
    except OSError:
        pass
    game = _GAMES.get(game_name)
    if game is not None:
        game.load_paths()


def _handle_missing_profile_root(topbar, game_name: str) -> None:
    """Profile/staging folder was deleted: clear game config, refresh list, switch to another game or clear last_game."""
    _clear_game_config(game_name)
    game_names = _load_games()
    topbar._game_menu.configure(values=game_names)
    if game_names and game_names[0] != "No games configured":
        topbar._game_var.set(game_names[0])
        if hasattr(topbar, "_profile_menu") and topbar._profile_menu is not None:
            profiles = _profiles_for_game(game_names[0])
            topbar._profile_menu.configure(values=profiles)
            topbar._profile_var.set(profiles[0])
        topbar._reload_mod_panel()
    else:
        get_last_game_path().unlink(missing_ok=True)
        topbar._game_var.set("No games configured")
        if hasattr(topbar, "_profile_menu") and topbar._profile_menu is not None:
            topbar._profile_menu.configure(values=["default"])
            topbar._profile_var.set("default")
        topbar._reload_mod_panel()
