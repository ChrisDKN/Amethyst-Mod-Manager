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
    profile_uses_specific_mods,  # re-exported: backend now imports from Utils
    read_profile_settings,
    write_profile_settings,
)

# Game handlers — populated by _load_games() when first called
_GAMES: dict[str, BaseGame] = {}


def foreign_deployed_plugin_basenames(game) -> set[str]:
    """Return lowercase plugin filenames currently deployed by a *different*
    profile than the one the UI has active.

    When profile A is deployed and the user switches to profile B, the live
    Data/ folder still contains A's mod plugin files. Callers that scan Data/
    (vanilla detection, orphan detection) use this to exclude those files so
    they don't bleed across profiles. Returns an empty set when the active
    profile is the deployed one, or when no deploy is active.
    """
    try:
        if not getattr(game, "get_deploy_active", lambda: False)():
            return set()
        deployed_name = game.get_last_deployed_profile()
        active_dir = getattr(game, "_active_profile_dir", None)
        if active_dir is not None and active_dir.name == deployed_name:
            return set()
        deployed_dir = game.get_profile_root() / "profiles" / deployed_name
        names: set[str] = set()
        for fname in ("plugins.txt", "loadorder.txt"):
            p = deployed_dir / fname
            if not p.is_file():
                continue
            try:
                for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                    n = line.strip()
                    if not n or n.startswith("#"):
                        continue
                    if n.startswith("*"):
                        n = n[1:]
                    if n:
                        names.add(n.lower())
            except OSError:
                continue
        return names
    except (OSError, AttributeError):
        return set()


def game_data_subpath(game) -> str:
    """Relative path of the mod-deploy dir under the game root (posix
    separators), or "" when they are the same directory, unconfigured, or the
    deploy dir is not under the root.

    Root-flagged mods deploy VERBATIM to the game root, so on a subfolder-deploy
    game a root entry '<subpath>/foo.esp' (Morrowind: 'Data Files/foo.esp')
    lands at exactly the same place as a normal top-level filemap entry
    'foo.esp'. Consumers of filemap_root.txt (Data tab merge, plugins panel
    recovery/resolver, plugins.txt sync) use this prefix to recognise those
    entries instead of hiding them as game-root files.
    """
    try:
        gp = game.get_game_path()
        dp = game.get_mod_data_path()
    except Exception:
        return ""
    if not gp or not dp:
        return ""
    try:
        rel = Path(dp).relative_to(Path(gp))
    except ValueError:
        return ""
    return "" if rel == Path(".") else rel.as_posix()


def plugins_routing_ctx(game) -> "tuple | None":
    """(resolve_fn, data_rel_lower) for games that route staged files through
    deploy rules (UE5-style ``_resolve_entry``) AND load plugins from a folder
    BELOW the deploy root — e.g. Oblivion Remastered's
    ``Content/Dev/ObvData/Data``. None for every other game.

    Such games can ship a plugin anywhere inside a mod (nested under a wrapper
    like ``OblivionRemastered/Content/Dev/ObvData/Data/x.esp``) and the deploy
    rules still place it in the plugins dir, so "plugin at the mod root" is the
    wrong test for what belongs in plugins.txt — the routed destination is.
    """
    resolve = getattr(game, "_resolve_entry", None)
    if resolve is None:
        return None
    try:
        gp = game.get_game_path()
        dp = (game.get_vanilla_plugins_path()
              if hasattr(game, "get_vanilla_plugins_path") else None)
    except Exception:
        return None
    if not gp or not dp:
        return None
    try:
        rel = Path(dp).relative_to(Path(gp))
    except ValueError:
        return None
    if rel == Path("."):
        return None
    return resolve, rel.as_posix().lower()


def routed_plugin_name(ctx, staged_rel: str,
                       exts: tuple) -> "str | None":
    """Filename if *staged_rel* (a mod-relative staging path) deploys DIRECTLY
    into the plugins data dir per *ctx* (from plugins_routing_ctx), else None.
    A plugin nested BELOW the data dir (e.g. ``.../Data/optional/x.esp``) is
    not loadable and returns None."""
    resolve, data_rel_low = ctx
    norm = staged_rel.replace("\\", "/")
    name = norm.rsplit("/", 1)[-1]
    if not name.lower().endswith(tuple(exts)):
        return None
    try:
        dest, final_rel = resolve(norm)
    except Exception:
        return None
    full = f"{dest}/{final_rel}" if dest else final_rel
    parent = full.rsplit("/", 1)[0] if "/" in full else ""
    return name if parent.lower() == data_rel_low else None


def routed_mod_plugin_names(game, mod_dir: Path) -> list[str]:
    """Plugin files anywhere inside *mod_dir* whose routed deploy destination
    is the top level of the game's plugins data dir. [] for games without
    routing rules (plugins_routing_ctx is None) or on any error.

    Used by plugins.txt sync (enable/disable/remove) so nested plugins —
    which DO deploy into the data dir — get the same plugins.txt treatment
    as mod-root ones."""
    ctx = plugins_routing_ctx(game)
    if ctx is None:
        return []
    exts = tuple(e.lower() for e in
                 (getattr(game, "plugin_extensions", []) or []))
    if not exts or not mod_dir.is_dir():
        return []
    names: list[str] = []
    try:
        import os
        for root, _dirs, files in os.walk(mod_dir):
            rel_root = Path(root).relative_to(mod_dir).as_posix()
            for fname in files:
                if not fname.lower().endswith(exts):
                    continue
                rel = f"{rel_root}/{fname}" if rel_root != "." else fname
                if routed_plugin_name(ctx, rel, exts):
                    names.append(fname)
    except OSError:
        return names
    return names


def _vanilla_plugins_for_game(game) -> dict[str, str]:
    """Return {lowercase_name: original_name} for vanilla plugins."""
    result: dict[str, str] = {}
    base_plugins = list(getattr(game, "vanilla_plugins", []) or [])
    dlc_plugins = getattr(game, "vanilla_dlc_plugins", []) or []
    ccc_name = getattr(game, "vanilla_ccc_filename", None)

    # Base, DLC, and CC entries are only treated as vanilla if the file is
    # present in the Data folder — users may not own every DLC/expansion, and
    # vanilla lists include plugins from future expansions (e.g. Starfield's
    # SFBGS* placeholders). We scan the live folder without subtracting another
    # profile's deployment: vanilla names are a fixed list and CC names come
    # from the .ccc manifest, so cross-profile mod plugins can't accidentally
    # match here.
    data_dir = game.get_vanilla_plugins_path() if hasattr(game, "get_vanilla_plugins_path") else None
    present: set[str] = set()
    if data_dir and data_dir.is_dir():
        try:
            present = {entry.name.lower() for entry in data_dir.iterdir() if entry.is_file()}
        except OSError:
            pass

    for name in base_plugins:
        if name.lower() in present:
            result[name.lower()] = name

    if not dlc_plugins and not ccc_name:
        return result

    for name in dlc_plugins:
        if name.lower() in present:
            result.setdefault(name.lower(), name)

    for low, orig in _read_ccc_manifest(game, ccc_name, present).items():
        result.setdefault(low, orig)
    return result


def _read_ccc_manifest(game, ccc_name: str, present: "set[str]") -> dict[str, str]:
    """Return {lower: original} for CC plugins listed in the .ccc manifest that
    are present on disk. *present* is the set of lowercased filenames in Data/."""
    result: dict[str, str] = {}
    if not ccc_name:
        return result
    candidates: list = []
    # Starfield reads its .ccc from My Games first, then the game root
    # (libloadorder 17.0.0 behavior); other games only ship it in the root.
    if getattr(game, "ccc_in_my_games", False) and hasattr(game, "_mygames_paths"):
        try:
            candidates.extend(d / ccc_name for d in game._mygames_paths())
        except Exception:
            pass
    game_path = game.get_game_path()
    if game_path:
        candidates.append(game_path / ccc_name)
    ccc = next((c for c in candidates if c.is_file()), None)
    if ccc is None:
        return result
    try:
        for line in ccc.read_text(encoding="utf-8", errors="ignore").splitlines():
            n = line.strip()
            if n and not n.startswith("#") and n.lower() in present:
                result.setdefault(n.lower(), n)
    except OSError:
        pass
    return result


def _load_games() -> list[str]:
    """Discover game handlers and return sorted display names (configured games only)."""
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
    default_dir = profiles_root / "profiles" / "default"

    if not profile_specific_mods and default_dir.is_dir():
        # Non-specific profiles share the mods folder, so a brand-new one should
        # be a 1:1 clone of default — modlist, plugins, loadorder, loot.json,
        # userlist.yaml, fomod selections, ini files, profile_state.json all
        # carry over so it deploys identically out of the gate. Exclude backups/
        # (default's deploy/restore snapshots — profile-specific and large;
        # a fresh profile has nothing to restore). If profile_dir already exists
        # (re-create), fall through to the touch()-based init below.
        if not profile_dir.exists():
            def _skip_backups(src, names):
                # Only skip the top-level backups/ dir, not any nested match.
                if Path(src) == default_dir:
                    return {"backups"} & set(names)
                return set()
            shutil.copytree(default_dir, profile_dir, copy_function=shutil.copy2,
                            ignore=_skip_backups, dirs_exist_ok=False)
            # A clone is never the (locked, un-removable) original default, and
            # must not inherit its lock — scrub those flags from the copied
            # profile_settings while keeping the rest (e.g. collection_url).
            settings = read_profile_settings(profile_dir, None)
            for k in ("original_default", "profile_locked", "profile_specific_mods"):
                settings.pop(k, None)
            write_profile_settings(profile_dir, settings)
            return profile_dir

    profile_dir.mkdir(parents=True, exist_ok=True)
    plugins = profile_dir / "plugins.txt"
    if not plugins.exists():
        if profile_specific_mods:
            # Profile-specific mods start empty, so its plugin list must too —
            # the shared default plugins reference mods this profile won't have.
            plugins.touch()
        else:
            # Fallback (no default folder to clone): inherit at least the plugin
            # list so the profile isn't blank.
            default_plugins = default_dir / "plugins.txt"
            if default_plugins.exists():
                shutil.copy2(default_plugins, plugins)
            else:
                plugins.touch()
    modlist = profile_dir / "modlist.txt"
    if not modlist.exists():
        if profile_specific_mods:
            # Profile-specific mods folder starts empty — don't inherit the
            # default modlist which references the shared mods directory.
            modlist.touch()
        else:
            default_modlist = default_dir / "modlist.txt"
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
