from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from Utils.atomic_write import write_atomic

if TYPE_CHECKING:
    from Games.base_game import BaseGame


APP_DIR = "_xTranslator"
EXE_NAME = "xTranslator.exe"

_GAME_CONFIG = {
    "skyrim": ("Skyrim", "Skyrim"),
    "enderal": ("Skyrim", "Skyrim"),
    "skyrim_se": ("SkyrimSE", "SkyrimSE"),
    "skyrimvr": ("SkyrimSE", "SkyrimSE"),
    "enderalse": ("SkyrimSE", "SkyrimSE"),
    "FalloutNV": ("FalloutNV", "FalloutNV"),
    "Fallout4": ("Fallout4", "Fallout4"),
    "Fallout4VR": ("Fallout4", "Fallout4"),
    "Fallout76": ("Fallout76", "Fallout76"),
    "Starfield": ("Starfield", "Starfield"),
}
_PLUGIN_EXTS = (".esp", ".esm", ".esl")


def find_xtranslator_exe(game: "BaseGame") -> Path | None:
    from Utils.bethesda.xedit import tool_exe_path
    return tool_exe_path(game, EXE_NAME, APP_DIR)


def workspace_for_game(game: "BaseGame") -> str:
    try:
        return _GAME_CONFIG[game.game_id][0]
    except KeyError as exc:
        raise RuntimeError(
            f"xTranslator is not configured for {game.name}.") from exc


def prefs_folder_for_game(game: "BaseGame") -> str:
    try:
        return _GAME_CONFIG[game.game_id][1]
    except KeyError as exc:
        raise RuntimeError(
            f"xTranslator is not configured for {game.name}.") from exc


def staged_plugins(game: "BaseGame") -> list[tuple[str, Path]]:
    staging = Path(game.get_effective_mod_staging_path())
    found: list[tuple[str, Path]] = []
    try:
        mod_dirs = sorted(
            (entry for entry in staging.iterdir()
             if entry.is_dir()
             and not entry.name.startswith(".")
             and not entry.name.casefold().endswith("_separator")),
            key=lambda path: path.name.casefold(),
        )
    except OSError:
        return found

    for mod_dir in mod_dirs:
        try:
            for dirpath, dirnames, filenames in os.walk(mod_dir):
                dirnames[:] = [name for name in dirnames
                               if not name.startswith(".")]
                for filename in filenames:
                    if filename.casefold().endswith(_PLUGIN_EXTS):
                        found.append((mod_dir.name, Path(dirpath) / filename))
        except OSError:
            continue
    found.sort(key=lambda item: (
        item[1].name.casefold(), item[0].casefold(), str(item[1]).casefold()))
    return found


def _with_trailing_backslash(path: str) -> str:
    return path.rstrip("\\/") + "\\"


def _set_ini_values(path: Path, values: dict[str, str]) -> None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        raise RuntimeError(
            f"xTranslator preferences could not be read: {path}") from exc

    had_bom = raw.startswith(b"\xef\xbb\xbf") or not raw
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"xTranslator preferences are not valid UTF-8: {path}") from exc

    newline = "\r\n" if "\r\n" in text or not text else "\n"
    lines = text.splitlines()
    positions: dict[str, int] = {}
    for index, line in enumerate(lines):
        key, separator, _value = line.partition("=")
        if separator:
            positions.setdefault(key.strip().casefold(), index)

    for key, value in values.items():
        index = positions.get(key.casefold())
        line = f"{key}={value}"
        if index is None:
            positions[key.casefold()] = len(lines)
            lines.append(line)
        else:
            lines[index] = line

    payload = (newline.join(lines) + newline).encode("utf-8")
    if had_bom:
        payload = b"\xef\xbb\xbf" + payload
    write_atomic(path, payload)


def configure_paths(exe: Path, game: "BaseGame", wine_data_path: str,
                    wine_plugin_folder: str) -> Path:
    data = _with_trailing_backslash(wine_data_path)
    plugin_folder = _with_trailing_backslash(wine_plugin_folder)
    prefs = (Path(exe).parent / "UserPrefs" / prefs_folder_for_game(game)
             / "prefs.ini")
    _set_ini_values(prefs, {
        "isFirstLaunch2": "0",
        "Game_CacheStringFolder": data + "strings\\",
        "Game_CacheDataFolder": data,
        "Game_FuzDataFolder": data,
        "StringAddonFolder": data + "strings\\",
        "StringsAddonFolder": data + "strings\\",
        "Game_StringCompareFolder": plugin_folder,
        "papyrusPexFolder": data + "scripts\\",
        "EspFolder": plugin_folder,
        "NpcMapFolder": data,
        "BSAFolder": data,
        "TXTFolder": data + "interface\\",
        "EspFolderSaveAs": plugin_folder,
        "EspFolderFixedExport": plugin_folder,
        "EspCompareFolder": plugin_folder,
        "EspBackup": plugin_folder,
    })
    return prefs
