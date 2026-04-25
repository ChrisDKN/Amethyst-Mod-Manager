"""
Game and profile helpers: _GAMES registry, load_games, profiles, create_profile, etc.
Used by TopBar, ModListPanel, PluginPanel, and App. No dependency on other gui modules.
"""

import json
import shutil
from pathlib import Path

from Games.base_game import BaseGame
from Utils.config_paths import get_config_dir, get_profiles_dir, get_last_game_path, get_loot_game_dir
from Utils.game_loader import discover_games
from Utils.plugin_loader import discover_plugins
from Utils.profile_state import (
    merge_profile_settings,
    read_profile_settings,
    write_profile_settings,
)

# Game handlers — populated by _load_games() when first called
_GAMES: dict[str, BaseGame] = {}


def _vanilla_plugins_cache_path(game) -> Path | None:
    """Return the path to the vanilla plugins cache file for this game, or None."""
    try:
        config_dir = get_config_dir() / "games" / game.name
        return config_dir / "vanilla_plugins.txt"
    except Exception:
        return None


def _vanilla_plugins_for_game(game) -> dict[str, str]:
    """Return vanilla plugin names from the game's vanilla plugins dir.

    Returns a dict mapping ``lowercase_name -> original_cased_name`` so
    that ``name.lower() in result`` works like the old set, but callers
    can also retrieve the original filename for display.

    For games where plugins_include_vanilla=True (e.g. Oblivion Remastered),
    mod plugins are deployed into the same Data directory as vanilla plugins.
    To avoid misidentifying deployed mod plugins as vanilla, the vanilla list
    is persisted to a cache file on first scan and always read from there.
    """
    use_cache = getattr(game, "plugins_include_vanilla", False)

    if use_cache:
        cache_path = _vanilla_plugins_cache_path(game)
        if cache_path and cache_path.is_file():
            result: dict[str, str] = {}
            for line in cache_path.read_text(encoding="utf-8").splitlines():
                name = line.strip()
                if name:
                    result[name.lower()] = name
            return result

    if hasattr(game, "get_vanilla_plugins_path"):
        data_dir = game.get_vanilla_plugins_path()
    else:
        game_path = game.get_game_path()
        data_dir = (game_path / "Data") if game_path else None
    if not data_dir:
        return {}
    # For standard Bethesda games, prefer Data_Core/ when present — after
    # deployment Data/ contains hard-linked mod files, so Data_Core/ is the
    # reliable source of truth for truly vanilla plugins.
    core_dir = data_dir.parent / (data_dir.name + "_Core")
    scan_dir = core_dir if core_dir.is_dir() else data_dir
    if not scan_dir.is_dir():
        return {}
    exts = {e.lower() for e in game.plugin_extensions}
    all_plugins = {
        entry.name.lower(): entry.name
        for entry in scan_dir.iterdir()
        if entry.is_file() and entry.suffix.lower() in exts
    }

    # For games where mod plugins land in the same dir as vanilla, we need to
    # strip out any mod-provided plugins before caching.  The filemap lists
    # every file provided by a mod, so subtract those to get only vanilla.
    if use_cache and all_plugins:
        # Collect all plugin filenames provided by mods across every profile.
        mod_plugin_names: set[str] = set()
        profiles_root = game.get_profile_root() / "profiles"
        if profiles_root.is_dir():
            for profile_dir in profiles_root.iterdir():
                fm = profile_dir / "filemap.txt"
                if not fm.is_file():
                    continue
                with fm.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.rstrip("\n")
                        if "\t" not in line:
                            continue
                        rel, _ = line.split("\t", 1)
                        fname = rel.replace("\\", "/").rsplit("/", 1)[-1].lower()
                        if Path(fname).suffix.lower() in exts:
                            mod_plugin_names.add(fname)
        vanilla_only = {k: v for k, v in all_plugins.items() if k not in mod_plugin_names}
        # Only write the cache if we ended up with a non-empty vanilla set.
        # If mod_plugin_names is empty (nothing deployed yet) vanilla_only ==
        # all_plugins which is also correct — nothing to subtract.
        plugins_to_cache = vanilla_only if vanilla_only else all_plugins
        cache_path = _vanilla_plugins_cache_path(game)
        if cache_path:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    "\n".join(sorted(plugins_to_cache.values())) + "\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
        return plugins_to_cache

    return all_plugins


def _load_games() -> list[str]:
    """Discover game handlers and return sorted display names (configured games only)."""
    global _GAMES
    new_games = discover_games()
    _GAMES.clear()
    _GAMES.update(new_games)
    discover_plugins()
    for game in _GAMES.values():
        if getattr(game, "loot_sort_enabled", False) and getattr(game, "game_id", None):
            get_loot_game_dir(game.game_id)
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


def profile_uses_specific_mods(profile_dir: Path) -> bool:
    """Return True if this profile stores its own mods folder inside itself."""
    return bool(read_profile_settings(profile_dir, None).get("profile_specific_mods", False))


def get_collection_url_from_profile(profile_dir: Path) -> str | None:
    """Return the collection URL from profile_state profile_settings, or None if not set."""
    url = read_profile_settings(profile_dir, None).get("collection_url")
    return url if url else None


def save_collection_url_to_profile(profile_dir: Path, collection_url: str) -> None:
    """Save collection_url into profile_settings, merging with existing keys."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    merge_profile_settings(profile_dir, {"collection_url": collection_url})


def find_profile_with_collection_url(game_name: str, collection_url: str) -> str | None:
    """Return the profile name whose profile_state contains *collection_url*, or None."""
    game = _GAMES.get(game_name)
    if game is not None:
        profiles_dir = game.get_profile_root() / "profiles"
    else:
        profiles_dir = get_profiles_dir() / game_name / "profiles"
    if not profiles_dir.is_dir():
        return None
    for p in profiles_dir.iterdir():
        if p.is_dir():
            url = get_collection_url_from_profile(p)
            if url and url == collection_url:
                return p.name
    return None


def find_profile_with_collection_slug(game_name: str, slug: str) -> str | None:
    """Return the profile name whose stored collection_url matches *slug*, regardless
    of which /revisions/{N} suffix (if any) is attached. Use this to find a profile
    eligible for a revision swap (update) rather than an exact-URL reinstall."""
    if not slug:
        return None
    game = _GAMES.get(game_name)
    if game is not None:
        profiles_dir = game.get_profile_root() / "profiles"
    else:
        profiles_dir = get_profiles_dir() / game_name / "profiles"
    if not profiles_dir.is_dir():
        return None
    needle = f"/collections/{slug}"
    for p in profiles_dir.iterdir():
        if not p.is_dir():
            continue
        url = get_collection_url_from_profile(p)
        if not url:
            continue
        if needle in url and (url.endswith(needle) or f"{needle}/" in url):
            return p.name
    return None


def _create_profile(
    game_name: str,
    profile_name: str,
    profile_specific_mods: bool = False,
) -> Path:
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
        if profile_specific_mods:
            # Profile-specific mods folder starts empty — don't inherit the
            # default modlist which references the shared mods directory.
            modlist.touch()
        else:
            default_modlist = profiles_root / "profiles" / "default" / "modlist.txt"
            if default_modlist.exists():
                shutil.copy2(default_modlist, modlist)
            else:
                modlist.touch()
    if profile_specific_mods:
        write_profile_settings(profile_dir, {"profile_specific_mods": True})
        # Create the profile-specific mods, overwrite, and Root_Folder directories
        # up front so they exist as soon as the profile is selected.
        (profile_dir / "mods").mkdir(exist_ok=True)
        (profile_dir / "overwrite").mkdir(exist_ok=True)
        (profile_dir / "Root_Folder").mkdir(exist_ok=True)
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
