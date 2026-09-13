from __future__ import annotations

import re
from pathlib import Path


_DOMAINS = {
    "moddingtools": "site",
    "skyrim": "skyrim", "skyrimlegendaryedition": "skyrim",
    "skyrimspecialedition": "skyrimspecialedition", "skyrimse": "skyrimspecialedition",
    "skyrimvr": "skyrimspecialedition", "fallout4vr": "fallout4",
    "falloutnewvegas": "newvegas", "falloutnv": "newvegas",
    "fallout3goty": "fallout3", "enderalspecialedition": "enderalspecialedition",
    "enderalse": "enderalspecialedition", "baldursgate3": "baldursgate3",
    "oblivionremastered": "oblivionremastered",
    "sevendaystodie": "7daystodie",
}


def token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def nexus_domain(value: str) -> str:
    key = token(value)
    return _DOMAINS.get(key, key)


def matches_game(game, name: str) -> bool:
    key = token(name)
    own = {token(getattr(game, a, "")) for a in ("name", "game_id", "nexus_game_domain")}
    if ("vr" in key) != any("vr" in s for s in own):
        return False
    if key in own:
        return True
    if "vr" in key or any("vr" in s for s in own):
        return False
    return nexus_domain(name) in {nexus_domain(s) for s in own if s}


def configured_games() -> dict:
    from Utils.games.registry import _GAMES
    return {name: game for name, game in _GAMES.items() if game.is_configured()}


def source_roots(package, game) -> dict[str, Path]:
    roots = {}
    games = configured_games()
    names = {package.game, *package.metadata.get("OtherGames", [])}
    names.update(str(a.state.get("Game", a.state.get("GameName", package.game)))
                 for a in package.archives.values() if a.kind == "GameFileSource")
    for name in names:
        found = game if matches_game(game, name) else next(
            (g for g in games.values() if matches_game(g, name)), None)
        if found and found.get_game_path():
            roots[name] = Path(found.get_game_path())
            profile = getattr(found, "_active_profile_dir", None)
            if profile:
                from Utils.profiles.state import read_profile_settings
                from .store import installation_info
                saved = read_profile_settings(Path(profile)).get("wabbajack_directory")
                info = installation_info(Path(saved)) if saved else None
                if info and (roots[name].resolve().is_relative_to(Path(saved).resolve())
                             or not roots[name].is_dir()):
                    configured = getattr(found, "_read_global_paths", lambda: {})().get("game_path")
                    if configured:
                        configured = Path(configured).expanduser()
                        if configured.is_dir() and not configured.resolve().is_relative_to(Path(saved).parent.resolve()):
                            roots[name] = configured
                            continue
                    original = next((p for n, p in info.get("source_roots", {}).items() if matches_game(found, n)), None)
                    if original:
                        roots[name] = Path(original)
    return roots
