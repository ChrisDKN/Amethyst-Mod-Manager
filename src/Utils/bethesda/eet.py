from __future__ import annotations

import os
import re
from html import escape, unescape
from pathlib import Path
from typing import TYPE_CHECKING

from Utils.atomic_write import write_atomic

if TYPE_CHECKING:
    from Games.base_game import BaseGame


APP_DIR = "ESP-ESM Translator"
EXE_NAME = "EET4.exe"

_GAME_KEYS = {
    "morrowind": "Morrowind",
    "Oblivion": "Oblivion",
    "skyrim": "Skyrim",
    "skyrim_se": "SkyrimSE",
    "Fallout3": "Fallout3",
    "Fallout3GOTY": "Fallout3",
    "FalloutNV": "FalloutNV",
    "Fallout4": "Fallout4",
    "Starfield": "Starfield",
}
_PLUGIN_EXTS = (".esp", ".esm", ".esl")
_ENTRY_SEP = "|||"
_VALUE_SEP = "~~~"
_INITIAL_SETTINGS = (
    b"\xef\xbb\xbf<?xml version=\"1.0\" encoding=\"utf-8\"?>\r\n"
    b"<Settings>\r\n</Settings>\r\n"
)


def applications_root(game: "BaseGame") -> Path:
    return Path(game.get_mod_staging_path()).parent / "Applications"


def find_eet_exe(game: "BaseGame") -> Path | None:
    root = applications_root(game)
    canonical = root / APP_DIR / EXE_NAME
    if canonical.is_file():
        return canonical
    try:
        candidates = [
            entry / EXE_NAME
            for entry in root.iterdir()
            if entry.is_dir()
            and entry.name.casefold().startswith(APP_DIR.casefold())
            and (entry / EXE_NAME).is_file()
        ]
    except OSError:
        return None
    try:
        return max(candidates, key=lambda path: path.stat().st_mtime,
                   default=None)
    except OSError:
        return candidates[0] if candidates else None


def game_key(game: "BaseGame") -> str:
    try:
        return _GAME_KEYS[game.game_id]
    except KeyError as exc:
        raise RuntimeError(
            f"ESP-ESM Translator is not configured for {game.name}.") from exc


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


def _dictionary_value(current: str, key: str, value: str) -> str:
    entries: list[tuple[str, str]] = []
    for raw in current.split(_ENTRY_SEP):
        if _VALUE_SEP not in raw:
            continue
        entry_key, entry_value = raw.split(_VALUE_SEP, 1)
        if entry_key:
            entries.append((entry_key, entry_value))
    for index, (entry_key, _entry_value) in enumerate(entries):
        if entry_key == key:
            entries[index] = (key, value)
            break
    else:
        entries.append((key, value))
    return _ENTRY_SEP.join(
        f"{entry_key}{_VALUE_SEP}{entry_value}"
        for entry_key, entry_value in entries)


def _set_xml_value(text: str, name: str, key: str, value: str) -> str:
    pattern = re.compile(
        rf"(<{re.escape(name)}>)(.*?)(</{re.escape(name)}>)",
        re.DOTALL,
    )
    match = pattern.search(text)
    current = unescape(match.group(2).strip()) if match else ""
    encoded = escape(_dictionary_value(current, key, value), quote=False)
    if match:
        return text[:match.start()] + match.group(1) + encoded + \
            match.group(3) + text[match.end():]
    empty_pattern = re.compile(rf"<{re.escape(name)}\s*/>")
    empty_match = empty_pattern.search(text)
    if empty_match:
        replacement = f"<{name}>{encoded}</{name}>"
        return (text[:empty_match.start()] + replacement
                + text[empty_match.end():])
    closing = "</Settings>"
    if closing not in text:
        raise RuntimeError("EET4.settings does not contain a Settings root.")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace(
        closing, f"  <{name}>{encoded}</{name}>{newline}{closing}", 1)


def configure_game_data(exe: Path, game: "BaseGame",
                        wine_data_path: str) -> Path:
    settings = Path(exe).with_suffix(".settings")
    try:
        raw = settings.read_bytes()
    except FileNotFoundError:
        raw = _INITIAL_SETTINGS
    except OSError as exc:
        raise RuntimeError(f"EET settings file could not be read: {settings}") from exc
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"EET settings are not valid UTF-8: {settings}") from exc
    key = game_key(game)
    text = _set_xml_value(text, "GameDataPath", key, wine_data_path)
    text = _set_xml_value(text, "UseGameDataPath", key, "True")
    payload = text.encode("utf-8")
    if had_bom:
        payload = b"\xef\xbb\xbf" + payload
    write_atomic(settings, payload)
    return settings
