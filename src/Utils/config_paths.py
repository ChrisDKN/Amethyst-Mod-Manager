"""
config_paths.py
Central helpers for resolving user-writable config directories.

Follows the XDG Base Directory Specification:
  Config lives in $XDG_CONFIG_HOME/AmethystModManager  (default: ~/.config/AmethystModManager)

This is required for AppImage packaging — the AppImage mount is read-only,
so all user config must be written outside the app bundle.
"""

import os
from pathlib import Path

APP_NAME = "AmethystModManager"


def get_config_dir() -> Path:
    """Return the app config directory, creating it if it doesn't exist.

    Respects $XDG_CONFIG_HOME; falls back to ~/.config/AmethystModManager.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    config_dir = base / APP_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_game_config_path(game_name: str) -> Path:
    """Return the paths.json path for a given game, creating parent dirs as needed.

    Result: ~/.config/AmethystModManager/games/<game_name>/paths.json
    """
    path = get_config_dir() / "games" / game_name / "paths.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_loot_data_dir() -> Path:
    """Return the LOOT masterlist data directory, creating it if needed.

    Result: ~/.config/AmethystModManager/LOOT/data/
    """
    d = get_config_dir() / "LOOT" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_profiles_dir() -> Path:
    """Return the root Profiles directory.

    Inside an AppImage, $MOD_MANAGER_PROFILES_DIR is set by AppRun to a
    writable location (~/.config/AmethystModManager/Profiles).  Outside an AppImage
    the default is <project_root>/Profiles.
    """
    env = os.environ.get("MOD_MANAGER_PROFILES_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    return Path(__file__).parent.parent / "Profiles"


def get_exe_args_path() -> Path:
    """Return the path to exe_args.json in the config directory.

    Result: ~/.config/AmethystModManager/exe_args.json
    """
    return get_config_dir() / "exe_args.json"


def get_fomod_selections_path(game_name: str, mod_name: str) -> Path:
    """Return the path to a saved FOMOD selection file for a given game and mod.

    Result: ~/.config/AmethystModManager/games/<game_name>/fomod_selections/<mod_name>.json
    """
    path = get_config_dir() / "games" / game_name / "fomod_selections" / f"{mod_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_nexus_config_dir() -> Path:
    """Return the Nexus Mods config directory, creating it if needed.

    Result: ~/.config/AmethystModManager/Nexus/
    """
    d = get_config_dir() / "Nexus"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_last_game_path() -> Path:
    """Return the path to the last-opened game state file.

    Result: ~/.config/AmethystModManager/last_game.json
    """
    return get_config_dir() / "last_game.json"
