"""
config_paths.py
Central helpers for resolving user-writable config directories.

Follows the XDG Base Directory Specification:
  Config lives in $XDG_CONFIG_HOME/ModManager  (default: ~/.config/ModManager)

This is required for AppImage packaging — the AppImage mount is read-only,
so all user config must be written outside the app bundle.
"""

import os
from pathlib import Path

APP_NAME = "ModManager"


def get_config_dir() -> Path:
    """Return the app config directory, creating it if it doesn't exist.

    Respects $XDG_CONFIG_HOME; falls back to ~/.config/ModManager.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    config_dir = base / APP_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_game_config_path(game_name: str) -> Path:
    """Return the paths.json path for a given game, creating parent dirs as needed.

    Result: ~/.config/ModManager/games/<game_name>/paths.json
    """
    path = get_config_dir() / "games" / game_name / "paths.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_loot_data_dir() -> Path:
    """Return the LOOT masterlist data directory, creating it if needed.

    Result: ~/.config/ModManager/LOOT/data/
    """
    d = get_config_dir() / "LOOT" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_exe_args_path() -> Path:
    """Return the path to exe_args.json in the config directory.

    Result: ~/.config/ModManager/exe_args.json
    """
    return get_config_dir() / "exe_args.json"
