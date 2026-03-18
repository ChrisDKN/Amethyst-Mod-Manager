"""
heroic_finder.py
Utilities for locating game installations managed by Heroic Games Launcher.

Heroic supports Epic Games (via Legendary) and GOG (via heroic-gogdl).
It can be installed as a Flatpak (most common on Steam Deck) or natively.

No UI, no game-specific knowledge.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_HOME = Path.home()
_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", _HOME / ".config"))

# ---------------------------------------------------------------------------
# Heroic config root candidates
# GamesConfig/<Appname>.json lives under each root; path varies by install type.
# ---------------------------------------------------------------------------
def _heroic_config_candidates() -> list[Path]:
    """All possible Heroic config roots, ordered by likelihood."""
    return [
        # Flatpak (most common on Steam Deck)
        _HOME / ".var" / "app" / "com.heroicgameslauncher.hgl" / "config" / "heroic",
        # Native / AppImage — respects XDG_CONFIG_HOME
        _XDG_CONFIG / "heroic",
        _HOME / ".config" / "heroic",  # Fallback if XDG_CONFIG was overridden
    ]


def _find_heroic_config_roots() -> list[Path]:
    """Return all Heroic config directories that exist on disk."""
    seen: set[Path] = set()
    out: list[Path] = []
    for p in _heroic_config_candidates():
        if p not in seen and p.is_dir():
            seen.add(p)
            out.append(p)
    return out


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
    # 1. Per-game: heroic_root/GamesConfig/<app_name>.json
    #    Path varies: Flatpak ~/.var/app/.../config/heroic, native ~/.config/heroic (or XDG_CONFIG_HOME)
    games_config = heroic_root / "GamesConfig"
    game_cfg_file = games_config / f"{app_name}.json"
    if game_cfg_file.is_file():
        try:
            cfg = json.loads(game_cfg_file.read_text(encoding="utf-8", errors="replace"))
            # Settings nested under appName key (Heroic format)
            inner = cfg.get(app_name, cfg)
            wine_prefix = (
                inner.get("winePrefix", "")
                or inner.get("wine_prefix", "")
                or cfg.get("winePrefix", "")
                or cfg.get("wine_prefix", "")
            )
            if wine_prefix:
                p = Path(wine_prefix)
                if (p / "pfx").is_dir():
                    return p / "pfx"
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
                if (p / "pfx").is_dir():
                    return p / "pfx"
                if p.is_dir():
                    return p
        except (OSError, json.JSONDecodeError):
            pass

    # 3. Hard-coded conventional fallback
    fallback = _HOME / "Games" / "Heroic" / "Prefixes" / app_name
    if (fallback / "pfx").is_dir():
        return fallback / "pfx"
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


def find_heroic_launch_info(app_names: list[str]) -> "tuple[str, str] | None":
    """
    Search Heroic config for a game matching any of the given app_names.
    Returns (store, matched_app_name) where store is 'legendary' (Epic) or 'gog',
    or None if not found.

    The returned values can be used to build a heroic:// launch URL:
        heroic://launch/<store>/<app_name>
    """
    for heroic_root in _find_heroic_config_roots():
        installed = _load_epic_installed(heroic_root)
        for app_name in app_names:
            if app_name in installed:
                install_path = installed[app_name].get("install_path", "")
                if install_path and Path(install_path).is_dir():
                    return ("legendary", app_name)
        library = _load_gog_library(heroic_root)
        app_names_lower = [n.lower() for n in app_names]
        for entry in library:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("app_name") or entry.get("appName") or "")
            entry_title = str(entry.get("title") or "")
            for app_name in app_names:
                if (
                    entry_id == app_name
                    or entry_id.lower() == app_name.lower()
                    or entry_title.lower() == app_name.lower()
                ):
                    install_path = entry.get("install_path", "")
                    if install_path and Path(install_path).is_dir():
                        return ("gog", entry_id or app_name)
    return None


def find_heroic_app_name_by_exe(exe_name: str) -> str | None:
    """
    Search Heroic's installed.json for a game whose executable matches exe_name.
    Returns the app_name string if found, otherwise None.

    exe_name should be the bare filename, e.g. 'SubnauticaZero.exe'.
    Matching is case-insensitive.
    """
    info = find_heroic_game_info_by_exe(exe_name)
    return info[2] if info else None


def find_heroic_game_info_by_exe(exe_name: str) -> "tuple[Path, Path | None, str] | None":
    """
    Full Heroic detection workflow keyed by executable name from the handler:

    1. Look in Heroic's installed.json (legendaryConfig/legendary/installed.json)
       for a game whose executable matches exe_name.
    2. Get app_name and install_path from that entry.
    3. Look in GamesConfig/<appname>.json for the winePrefix.
    4. Return (install_path, prefix_path, app_name) if all found.

    Used for games like Subnautica Below Zero where the handler provides
    SubnauticaZero.exe; we resolve appname (Foxglove), install path, and prefix.
    """
    exe_lower = exe_name.lower()

    for heroic_root in _find_heroic_config_roots():
        # 1. Epic (Legendary) installed.json
        installed = _load_epic_installed(heroic_root)
        for app_name, entry in installed.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("executable", "").lower() != exe_lower:
                continue
            install_path_raw = entry.get("install_path", "")
            if not install_path_raw:
                continue
            install_path = Path(install_path_raw)
            if not install_path.is_dir():
                continue
            # 2. GamesConfig/<appname>.json for prefix
            prefix_path = _find_heroic_prefix_for_app(heroic_root, app_name)
            if prefix_path:
                return (install_path, prefix_path, app_name)
            # Still return install_path + app_name if prefix lookup fails;
            # caller can retry prefix later
            return (install_path, None, app_name)

        # 3. GOG: check gog_store/installed.json for executable match
        gog_installed = heroic_root / "gog_store" / "installed.json"
        if gog_installed.is_file():
            try:
                gog_data = json.loads(gog_installed.read_text(encoding="utf-8", errors="replace"))
                if isinstance(gog_data, dict):
                    for app_id, entry in gog_data.items():
                        if not isinstance(entry, dict):
                            continue
                        # GOG may store executable in different keys
                        exe = (
                            entry.get("executable", "")
                            or entry.get("exe", "")
                            or ""
                        )
                        if not exe:
                            # Fallback: check install_path + exe
                            inst = entry.get("install_path", "")
                            if inst and exe_name:
                                inst_path = Path(inst)
                                if (inst_path / exe_name).exists():
                                    exe = exe_name
                        if exe.lower() != exe_lower:
                            continue
                        install_path_raw = entry.get("install_path", entry.get("path", ""))
                        if not install_path_raw:
                            continue
                        install_path = Path(install_path_raw)
                        if not install_path.is_dir():
                            continue
                        prefix_path = _find_heroic_prefix_for_app(heroic_root, app_id)
                        if prefix_path:
                            return (install_path, prefix_path, app_id)
                        return (install_path, None, app_id)
            except (OSError, json.JSONDecodeError):
                pass

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
