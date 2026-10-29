"""Run native Wine/Proton commands in NixOS's FHS environment."""

from __future__ import annotations

import platform
from pathlib import Path

from Utils.launchers.host import find_host_program


def wrap_nixos_command(command: list[str], *, env: dict | None = None) -> list[str]:
    try:
        if Path("/.flatpak-info").is_file():
            lines = Path("/run/host/os-release").read_text(encoding="utf-8").splitlines()
            os_id = next((line.partition("=")[2].strip().strip("\"'")
                          for line in lines if line.startswith("ID=")), "")
        else:
            os_id = platform.freedesktop_os_release().get("ID")
        if os_id != "nixos":
            return command
    except OSError:
        return command
    if command and Path(command[0]).name == "steam-run":
        return command
    steam_run = find_host_program("steam-run", env=env)
    if steam_run is None:
        raise RuntimeError(
            "NixOS: steam-run is required to run the selected Wine/Proton runner. "
            "Install the steam-run package on the host and make it available "
            "on the desktop session's PATH, then restart Amethyst.")
    return [steam_run, *command]
