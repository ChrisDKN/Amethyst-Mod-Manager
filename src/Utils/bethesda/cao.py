from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from Utils.atomic_write import write_atomic_text

if TYPE_CHECKING:
    from Games.base_game import BaseGame


APP_DIR = "Cathedral Assets Optimizer"
EXE_NAME = "Cathedral_Assets_Optimizer.exe"


def find_cao_exe(game: "BaseGame") -> Path | None:
    from Utils.bethesda.xedit import tool_exe_path
    return tool_exe_path(game, EXE_NAME, APP_DIR)


def profile_for_game(game: "BaseGame") -> str:
    if game.game_id == "skyrim_se":
        return "SSE"
    if game.game_id == "Fallout4":
        return "FO4"
    raise RuntimeError(f"Cathedral Assets Optimizer does not support {game.name} here.")


def _set_ini_value(path: Path, section: str, key: str, value: str) -> None:
    try:
        text = path.read_bytes().decode("utf-8", errors="surrogateescape")
    except OSError:
        text = ""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    section_re = re.compile(r"^\s*\[([^]]+)]\s*$")
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=", re.IGNORECASE)
    start = end = None
    for index, line in enumerate(lines):
        match = section_re.match(line)
        if match is None:
            continue
        if start is not None:
            end = index
            break
        if match.group(1).casefold() == section.casefold():
            start = index
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend((f"[{section}]", f"{key}={value}"))
    else:
        end = len(lines) if end is None else end
        for index in range(start + 1, end):
            if key_re.match(lines[index]):
                lines[index] = f"{key}={value}"
                break
        else:
            lines.insert(end, f"{key}={value}")
    write_atomic_text(
        path, newline.join(lines) + newline,
        errors="surrogateescape",
    )


def configure_profile(exe: Path, profile: str, user_path: str) -> Path:
    profiles = exe.parent / "profiles"
    profile_dir = profiles / profile
    if not profile_dir.is_dir():
        raise RuntimeError(f"CAO profile '{profile}' was not found in {profiles}.")
    settings = profile_dir / "settings.ini"
    if not settings.is_file():
        template = profiles / "SSE" / "settings.ini"
        if template.is_file() and template != settings:
            shutil.copy2(template, settings)
    _set_ini_value(settings, "General", "userPath", user_path)
    _set_ini_value(profiles / "common.ini", "General", "profile", profile)
    return settings


def staged_mod_paths(game: "BaseGame") -> list[Path]:
    staging = Path(game.get_effective_mod_staging_path())
    try:
        mods = [
            entry for entry in staging.iterdir()
            if entry.is_dir()
            and not entry.name.startswith(".")
            and not entry.name.casefold().endswith("_separator")
        ]
    except OSError:
        return []
    return sorted(mods, key=lambda path: path.name.casefold())
