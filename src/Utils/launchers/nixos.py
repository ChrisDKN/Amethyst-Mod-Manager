"""Run native Wine/Proton commands in NixOS's FHS environment."""

from __future__ import annotations

import platform
import shutil
from pathlib import Path


def wrap_nixos_command(command: list[str], *, env: dict | None = None) -> list[str]:
    if Path("/.flatpak-info").is_file():
        return command
    try:
        if platform.freedesktop_os_release().get("ID") != "nixos":
            return command
    except OSError:
        return command
    if command and Path(command[0]).name == "steam-run":
        return command
    steam_run = shutil.which("steam-run", path=env.get("PATH") if env is not None else None)
    if steam_run is None:
        raise RuntimeError(
            "NixOS: steam-run is required to run the selected Wine/Proton runner. "
            "Install the steam-run package and make it available on PATH.")
    return [steam_run, *command]
