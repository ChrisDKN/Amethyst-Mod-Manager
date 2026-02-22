"""
heroic_finder.py
Utilities for locating game installations managed by Heroic Games Launcher.

Heroic supports Epic Games (via Legendary) and GOG (via heroic-gogdl).
It can be installed as a Flatpak (most common on Steam Deck) or natively.

No UI, no game-specific knowledge.
"""

from __future__ import annotations

import json
from pathlib import Path

_HOME = Path.home()

# ---------------------------------------------------------------------------
# Heroic config root candidates (Flatpak first — most common on Steam Deck)
# ---------------------------------------------------------------------------
_HEROIC_CONFIG_CANDIDATES: list[Path] = [
    _HOME / ".var" / "app" / "com.heroicgameslauncher.hgl" / "config" / "heroic",  # Flatpak
    _HOME / ".config" / "heroic",  # Native / AppImage
]


def _find_heroic_config_roots() -> list[Path]:
    """Return all Heroic config directories that exist on disk."""
    return [p for p in _HEROIC_CONFIG_CANDIDATES if p.is_dir()]


# ---------------------------------------------------------------------------
# Epic Games (Legendary backend)
# ---------------------------------------------------------------------------

def _load_epic_installed(heroic_root: Path) -> dict:
    """
    Parse legendaryConfig/legendary/installed.json from a Heroic config root.
    Returns a dict keyed by appName, each value containing at least:
      install_path, title
    Returns an empty dict on any error.
    """
    installed_json = heroic_root / "legendaryConfig" / "legendary" / "installed.json"
    if not installed_json.is_file():
        return {}
    try:
        data = json.loads(installed_json.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _find_epic_game(heroic_root: Path, app_names: list[str]) -> Path | None:
    """
    Search Epic installed.json for any of the given appNames.
    Returns the install_path as a Path if found and the directory exists.
    """
    installed = _load_epic_installed(heroic_root)
    for app_name in app_names:
        entry = installed.get(app_name)
        if not entry:
            continue
        install_path = entry.get("install_path", "")
        if install_path:
            p = Path(install_path)
            if p.is_dir():
                return p
    return None


# ---------------------------------------------------------------------------
# GOG (heroic-gogdl backend)
# ---------------------------------------------------------------------------

def _load_gog_library(heroic_root: Path) -> list[dict]:
    """
    Parse store_cache/gog_library.json from a Heroic config root.
    Returns the list of game entries, or an empty list on any error.

    Note: the is_installed field in this file is unreliable; we check
    install_path exists on disk instead.
    """
    library_json = heroic_root / "store_cache" / "gog_library.json"
    if not library_json.is_file():
        return []
    try:
        data = json.loads(library_json.read_text(encoding="utf-8", errors="replace"))
        # The file is either a list of games or {"games": [...]}
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            games = data.get("games", [])
            if isinstance(games, list):
                return games
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _find_gog_game(heroic_root: Path, app_names: list[str]) -> Path | None:
    """
    Search GOG library cache for any of the given app_names (GOG product IDs
    as strings, or title substrings).  Returns the install_path as a Path if
    found and the directory exists on disk.
    """
    library = _load_gog_library(heroic_root)
    app_names_lower = [n.lower() for n in app_names]
    for entry in library:
        if not isinstance(entry, dict):
            continue
        # Match on app_name / appName / title
        entry_id = str(entry.get("app_name") or entry.get("appName") or "")
        entry_title = str(entry.get("title") or "")
        if (
            entry_id in app_names
            or entry_id.lower() in app_names_lower
            or entry_title.lower() in app_names_lower
        ):
            install_path = entry.get("install_path", "")
            if install_path:
                p = Path(install_path)
                if p.is_dir():
                    return p
    return None


# ---------------------------------------------------------------------------
# Wine prefix lookup
# ---------------------------------------------------------------------------

def _find_heroic_prefix_for_app(heroic_root: Path, app_name: str) -> Path | None:
    """
    Look up the Wine prefix for a game in Heroic's GamesConfig/<appName>.json.

    If the per-game config doesn't specify a winePrefix, fall back to the
    global default from config.json (defaultWinePrefix), and if that is also
    absent, try ~/Games/Heroic/Prefixes/<appName>/.

    Returns the prefix Path if it exists on disk, otherwise None.
    """
    # 1. Per-game override
    game_cfg_file = heroic_root / "GamesConfig" / f"{app_name}.json"
    if game_cfg_file.is_file():
        try:
            cfg = json.loads(game_cfg_file.read_text(encoding="utf-8", errors="replace"))
            wine_prefix = cfg.get("winePrefix", "")
            if wine_prefix:
                p = Path(wine_prefix)
                if p.is_dir():
                    return p
        except (OSError, json.JSONDecodeError):
            pass

    # 2. Global default from config.json
    global_cfg_file = heroic_root / "config.json"
    if global_cfg_file.is_file():
        try:
            cfg = json.loads(global_cfg_file.read_text(encoding="utf-8", errors="replace"))
            # Heroic nests settings inside a "defaultSettings" key
            settings = cfg.get("defaultSettings", cfg)
            default_prefix_folder = settings.get("defaultWinePrefix", "")
            if default_prefix_folder:
                p = Path(default_prefix_folder) / app_name
                if p.is_dir():
                    return p
        except (OSError, json.JSONDecodeError):
            pass

    # 3. Hard-coded conventional fallback
    fallback = _HOME / "Games" / "Heroic" / "Prefixes" / app_name
    if fallback.is_dir():
        return fallback

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_heroic_game(app_names: list[str]) -> Path | None:
    """
    Search all Heroic config roots for a game matching any of the given
    app_names.  Checks Epic (Legendary) installs first, then GOG.

    app_names should contain the Heroic/Epic appName identifiers and/or GOG
    product IDs declared by the game handler.  Matching is case-insensitive
    for GOG titles.

    Returns the game install directory Path, or None if not found.
    """
    for heroic_root in _find_heroic_config_roots():
        result = _find_epic_game(heroic_root, app_names)
        if result:
            return result
        result = _find_gog_game(heroic_root, app_names)
        if result:
            return result
    return None


def find_heroic_prefix(app_names: list[str]) -> Path | None:
    """
    Search all Heroic config roots for the Wine prefix of a game matching any
    of the given app_names.

    Returns the prefix Path (the pfx-equivalent root that Heroic manages),
    or None if not found.
    """
    for heroic_root in _find_heroic_config_roots():
        for app_name in app_names:
            result = _find_heroic_prefix_for_app(heroic_root, app_name)
            if result:
                return result
    return None
