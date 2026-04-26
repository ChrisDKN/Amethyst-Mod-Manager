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


def get_game_config_dir(game_name: str) -> Path:
    """Return the config directory for a given game, creating it if needed.

    Result: ~/.config/AmethystModManager/games/<game_name>/
    """
    d = get_config_dir() / "games" / game_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_loot_data_dir() -> Path:
    """Return the LOOT masterlist data directory, creating it if needed.

    Result: ~/.config/AmethystModManager/LOOT/data/
    """
    d = get_config_dir() / "LOOT" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_loot_game_dir(game_id: str) -> Path:
    """Return the per-game LOOT directory, creating it if needed.

    Result: ~/.config/AmethystModManager/LOOT/<game_id>/
    A global userlist.yaml placed here applies to all profiles of this game.
    """
    d = get_config_dir() / "LOOT" / game_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_profiles_dir() -> Path:
    """Return the root Profiles directory.

    Inside an AppImage, $MOD_MANAGER_PROFILES_DIR is set by AppRun to a
    writable location (~/.config/AmethystModManager/Profiles).  Outside an AppImage
    the default is get_config_dir()/Profiles so config and Profiles stay consistent
    regardless of launch method (run.sh, AppImage, etc.).
    """
    env = os.environ.get("MOD_MANAGER_PROFILES_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    p = get_config_dir() / "Profiles"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_exe_args_path() -> Path:
    """Return the path to exe_args.json in the config directory.

    Result: ~/.config/AmethystModManager/exe_args.json
    """
    return get_config_dir() / "exe_args.json"


def get_profile_exe_args_path(profile_dir: Path) -> Path:
    """Return the per-profile exe_args.json path inside a profile directory.

    Result: <profile_dir>/exe_args.json
    """
    return profile_dir / "exe_args.json"


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


def get_logs_dir() -> Path:
    """Return the logs directory, creating it if it doesn't exist.

    Result: ~/.config/AmethystModManager/logs/
    """
    d = get_config_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_requirement_external_tool_mod_ids_path() -> Path:
    """Return the path to the cached requirement filter (external tool mod IDs).

    Fetched from GitHub and merged with user additions. Users can edit this file
    to add mod IDs; new IDs from the remote are appended on the next fetch.

    Result: ~/.config/AmethystModManager/requirement_external_tool_mod_ids.txt
    """
    return get_config_dir() / "requirement_external_tool_mod_ids.txt"


def get_custom_games_dir() -> Path:
    """Return the directory where user-defined custom game JSON files are stored.

    Users drop one JSON file per game here to add support for games not built
    into the application.

    Result: ~/.config/AmethystModManager/custom_games/
    """
    d = get_config_dir() / "custom_games"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_vcredist_cache_path() -> Path:
    """Return the path where the VC++ Redistributable installer is cached.

    Result: ~/.config/AmethystModManager/vcredist/vc_redist.x64.exe
    """
    path = get_config_dir() / "vcredist" / "vc_redist.x64.exe"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_dotnet_cache_dir() -> Path:
    """Return the directory where .NET runtime installers are cached.

    Result: ~/.config/AmethystModManager/dotnet/
    """
    path = get_config_dir() / "dotnet"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_custom_game_images_dir() -> Path:
    """Return the directory where downloaded custom game banner images are cached.

    When a user provides an image URL in the custom game definition, the image
    is downloaded once and stored here so the game picker can display it offline.

    Result: ~/.config/AmethystModManager/custom_game_images/
    """
    d = get_config_dir() / "custom_game_images"
    d.mkdir(parents=True, exist_ok=True)
    return d


_CACHE_ROOT_RESERVED = {"wine_prefixes"}


def get_download_cache_dir() -> Path:
    """Return the download cache root directory, creating it if it doesn't exist.

    Honours the user-configured path from ``[paths] download_cache_path`` in
    amethyst.ini.  When unset (or unwritable) falls back to
    ``~/.config/AmethystModManager/download_cache/``.

    The setting is read on every call so a path change in the Settings panel
    takes effect without restarting.
    """
    try:
        from Utils.ui_config import load_download_cache_path  # lazy: avoid cycles
        custom = load_download_cache_path().strip()
    except Exception:
        custom = ""
    if custom:
        d = Path(custom).expanduser()
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            pass  # fall through to default
    d = get_config_dir() / "download_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_download_cache_dir_for_game(game_name: str | None) -> Path:
    """Per-game cache subfolder under :func:`get_download_cache_dir`.

    Falls back to the cache root when *game_name* is empty.  Game name is
    used as a directory component verbatim, matching the convention used
    elsewhere (e.g. :func:`get_game_config_path`).
    """
    root = get_download_cache_dir()
    if not game_name:
        return root
    d = root / game_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_all_cache_dirs(active_game_name: str | None = None) -> list[Path]:
    """Cache directories to scan for already-downloaded archives.

    Returns ``[active-game folder, cache root]``, de-duplicated by resolved
    path.  Other games' subfolders are intentionally NOT included: file_id
    is not unique across mods/games, so cross-game scans risk matching an
    unrelated archive whose ``.fileid`` sidecar happens to share a value
    with the requested file (e.g. Darktide mod 373 file 2964 colliding with
    Palworld mod 678 file 2964).  The cache root is still included so
    legacy archives placed there before per-game subdirs existed remain
    discoverable.
    """
    root = get_download_cache_dir()
    out: list[Path] = []
    seen: set[Path] = set()
    if active_game_name:
        active = root / active_game_name
        if active.is_dir():
            out.append(active)
            seen.add(active.resolve())
    root_resolved = root.resolve()
    if root_resolved not in seen:
        out.append(root)
        seen.add(root_resolved)
    return out


def get_plugins_dir() -> Path:
    """Return the directory where external wizard plugin scripts are stored.

    Users drop Python scripts here to add custom wizard tools without modifying
    the application source.

    Result: ~/.config/AmethystModManager/Plugins/
    """
    d = get_config_dir() / "Plugins"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_download_locations_path() -> Path:
    """Return the path to the extra download scan locations config file.

    Users can add custom folders to scan for archives in addition to ~/Downloads.
    Stored as JSON array of path strings.

    Result: ~/.config/AmethystModManager/download_locations.json
    """
    return get_config_dir() / "download_locations.json"
