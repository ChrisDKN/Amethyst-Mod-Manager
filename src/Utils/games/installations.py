from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any, Callable


def heroic_app_names(game) -> list[str]:
    names = list(getattr(game, "heroic_app_names", []) or [])
    if not names and getattr(game, "name", None):
        try:
            from Utils.config_paths import get_game_config_path

            path = get_game_config_path(game.name)
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                saved = (data.get("heroic_app_name", "") or "").strip()
                if saved:
                    names = [saved]
        except Exception:
            pass
    return names


def is_native_exe_name(exe_name: str | None) -> bool:
    if not exe_name:
        return False
    return Path(str(exe_name)).suffix.casefold() not in {
        ".exe", ".bat", ".cmd", ".com", ".msi",
    }


def discover_installations(
    game: Any, log_fn: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    log = log_fn or (lambda message: None)
    candidates: list[dict[str, Any]] = []

    def add(source, path, prefix, launcher_id, matched_exe=None):
        prefix_mode = (
            "native" if is_native_exe_name(matched_exe)
            else "resolved" if prefix is not None else "detect"
        )
        candidates.append({"source": source, "path": path,
                           "prefix": prefix, "id": launcher_id,
                           "prefix_mode": prefix_mode,
                           "executable": matched_exe})

    game_name = getattr(game, "name", repr(game))
    log(f"[Configure Game] Auto-detecting: {game_name}")
    try:
        from Utils.launchers.steam import (
            find_steam_libraries, find_game_by_steam_id, find_game_in_libraries)
        from Utils.launchers.heroic import (
            find_heroic_game_info_by_app_names, find_heroic_game_info_by_exe)

        exe_names = list(getattr(game, "configure_exe_names", []) or [])
        if not exe_names:
            exe_names = [getattr(game, "exe_name", None)] + list(
                getattr(game, "exe_name_alts", []) or [])
            exe_names = [exe for exe in exe_names if exe]
        heroic_names = heroic_app_names(game)
        if heroic_names:
            log(f"[Configure Game] Checking Heroic app names: {heroic_names}")
            info = find_heroic_game_info_by_app_names(heroic_names)
            if info:
                add("heroic", info[0], info[1], info[2])
                log(f"[Configure Game] Found via Heroic app name "
                    f"({info[2]}): {info[0]}")
        else:
            log(f"[Configure Game] Checking Heroic (exe names: {exe_names})")
            for exe in exe_names:
                info = find_heroic_game_info_by_exe(exe)
                if info:
                    add("heroic", info[0], info[1], info[2], exe)
                    log(f"[Configure Game] Found via Heroic exe scan ({exe}): {info[0]}")
                    break
        from Utils.launchers.lutris import find_lutris_game_info_by_exe

        log(f"[Configure Game] Checking Lutris (exe names: {exe_names})")
        for exe in exe_names:
            info = find_lutris_game_info_by_exe(exe)
            if info:
                add("lutris", info[0], info[1], info[2], exe)
                log(f"[Configure Game] Found via Lutris ({exe}): {info[0]}")
                break
        from Utils.launchers.faugus import find_faugus_game_info_by_exe

        log(f"[Configure Game] Checking Faugus (exe names: {exe_names})")
        for exe in exe_names:
            info = find_faugus_game_info_by_exe(exe)
            if info:
                add("faugus", info[0], info[1], info[2], exe)
                log(f"[Configure Game] Found via Faugus ({exe}): {info[0]}")
                break
        from Utils.launchers.bottles import find_bottles_game_info_by_exes
        log(f"[Configure Game] Checking Bottles (exe names: {exe_names})")
        info = find_bottles_game_info_by_exes(exe_names)
        if info:
            add("bottles", *info)
            log(f"[Configure Game] Found via Bottles ({info[3]}): {info[0]}")
        from Utils.launchers.steam_shortcuts import find_shortcut_game_info_by_exe

        log(f"[Configure Game] Checking non-Steam shortcuts "
            f"(exe names: {exe_names})")
        for exe in exe_names:
            info = find_shortcut_game_info_by_exe(exe)
            if info:
                add("shortcut", info[0], info[1], info[2], exe)
                log(f"[Configure Game] Found via non-Steam shortcut "
                    f"({exe}, app ID {info[2]}): {info[0]}")
                break
        found = None
        matched_steam_exe = None
        sid = str(getattr(game, "steam_id", "") or "")
        if sid:
            libs = find_steam_libraries()
            log(f"[Configure Game] Steam libraries found: "
                f"{libs if libs else 'none'}")
            log(f"[Configure Game] Checking Steam manifest "
                f"(app ID: {sid}, exes: {exe_names})")
            for exe in exe_names:
                found = find_game_by_steam_id(libs, sid, exe)
                if found:
                    matched_steam_exe = exe
                    log(f"[Configure Game] Found via Steam manifest "
                        f"({exe}): {found}")
                    break
            if not found:
                log("[Configure Game] Falling back to exe scan across "
                    "Steam libraries")
                for exe in exe_names:
                    found = find_game_in_libraries(libs, exe)
                    if found:
                        matched_steam_exe = exe
                        log(f"[Configure Game] Found via Steam exe scan "
                            f"({exe}): {found}")
                        break
                else:
                    log(f"[Configure Game] Not found via Steam exe scan "
                        f"(tried: {exe_names})")
        else:
            log("[Configure Game] No Steam app ID configured for this game")
        if found:
            add("steam", found, None, None, matched_steam_exe)
    except Exception as exc:
        log(f"[Configure Game] Scan failed: {exc}\n{traceback.format_exc()}")
    from amethyst_plugins.launchers import list_entries, matches
    for entry in list_entries(log=log)["entries"]:
        if matches(game, entry):
            add("plugin", Path(entry["game_path"]),
                Path(entry["prefix_path"]) if entry["prefix_path"] else None,
                entry["id"], entry["executable"])
            candidates[-1].update(provider_id=entry["provider_id"],
                                  provider_label=entry["provider_label"],
                                  provider_icon=entry["provider_icon"], name=entry["name"])
    if not candidates:
        try:
            detect = getattr(game, "detect_standalone_install", None)
            found = detect() if callable(detect) else None
            if found is not None:
                add("standalone", found, None, None, game.exe_name)
                log(f"[Configure Game] Found standalone install: {found}")
        except Exception as exc:
            log(f"[Configure Game] Standalone detection failed: {exc}")
    if not candidates:
        log(f"[Configure Game] Game location not auto-detected for: {game_name}")
        try:
            from Utils.launchers.steam import steam_discovery_report
            for line in steam_discovery_report():
                log(f"[Configure Game] {line}")
        except Exception as exc:
            log(f"[Configure Game] Steam discovery report failed: {exc}")
    return candidates


def discover_prefix(
    game: Any, game_path: Path | None, preferred_source: str | None = None,
    launcher_id: str | None = None,
    log_fn: Callable[[str], None] | None = None,
    provider_id: str | None = None,
) -> dict[str, Any]:
    if preferred_source == "plugin":
        from amethyst_plugins.launchers import get_entry, matches
        entry = get_entry(provider_id, launcher_id)
        if not matches(game, entry) or Path(entry["game_path"]).resolve() != Path(game_path).resolve():
            raise ValueError("The launcher entry no longer matches this installation.")
        return {"prefix": Path(entry["prefix_path"]) if entry["prefix_path"] else None,
                "source": "plugin", "provider_id": provider_id}
    found = None
    found_source = None
    lutris_slug = None
    faugus_gameid = None
    bottles_program = None
    try:
        from Utils.launchers.steam import find_prefix
        from Utils.launchers.heroic import find_heroic_prefix

        ids = [getattr(game, "steam_id", None)] + [
            str(s) for s in getattr(game, "alt_steam_ids", []) or [] if s]
        try:
            saved_appid = game.get_shortcut_appid()
        except AttributeError:
            saved_appid = ""
        if preferred_source == "shortcut" and launcher_id:
            ids = [launcher_id]
            saved_appid = str(launcher_id)
        elif preferred_source == "steam":
            saved_appid = ""
        elif saved_appid:
            ids.insert(0, saved_appid)
        if preferred_source in (None, "steam", "shortcut"):
            for steam_id in [value for value in ids if value]:
                found = find_prefix(steam_id, game_path)
                if found:
                    found_source = ("shortcut" if saved_appid
                                    and str(steam_id) == str(saved_appid)
                                    else "steam")
                    break
        if not found and preferred_source in (None, "heroic"):
            names = ([str(launcher_id)] if launcher_id
                     else heroic_app_names(game))
            found = find_heroic_prefix(names)
            if found:
                found_source = "heroic"
        exe_names = [getattr(game, "exe_name", None)] + list(
            getattr(game, "exe_name_alts", []) or [])
        exe_names = [name for name in exe_names if name]
        if not found and preferred_source in (None, "lutris"):
            from Utils.launchers.lutris import find_lutris_game_info_by_exe
            for exe in exe_names:
                info = find_lutris_game_info_by_exe(exe)
                if (info and info[1] is not None
                        and (not launcher_id or str(info[2]) == str(launcher_id))):
                    found = info[1]
                    found_source = "lutris"
                    lutris_slug = info[2]
                    break
        if not found and preferred_source in (None, "faugus"):
            from Utils.launchers.faugus import find_faugus_game_info_by_exe
            for exe in exe_names:
                info = find_faugus_game_info_by_exe(exe)
                if (info and info[1] is not None
                        and (not launcher_id or str(info[2]) == str(launcher_id))):
                    found = info[1]
                    found_source = "faugus"
                    faugus_gameid = info[2]
                    break
        if not found and preferred_source in (None, "bottles"):
            from Utils.launchers.bottles import find_bottles_game_info_by_exes
            info = find_bottles_game_info_by_exes(
                exe_names, game_path=game_path, program_id=launcher_id)
            if info:
                found, found_source = info[1], "bottles"
                bottles_program = info[2]
        if not found and preferred_source in (None, "shortcut"):
            from Utils.launchers.steam_shortcuts import find_shortcut_game_info_by_exe
            for exe in exe_names:
                info = find_shortcut_game_info_by_exe(exe)
                if (info and info[1] is not None
                        and (not launcher_id or str(info[2]) == str(launcher_id))):
                    found = info[1]
                    found_source = "shortcut"
                    break
    except Exception as exc:
        if log_fn is not None:
            log_fn(f"[Configure Game] Prefix scan failed: {exc}\n"
                   f"{traceback.format_exc()}")
        found = None
        found_source = None
    return {"prefix": found, "source": found_source,
            "lutris_slug": lutris_slug, "faugus_gameid": faugus_gameid,
            "bottles_program": bottles_program}
