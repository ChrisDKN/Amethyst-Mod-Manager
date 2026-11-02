"""Run native Wine/Proton commands in NixOS's FHS environment."""

from __future__ import annotations

import platform
from pathlib import Path

from Utils.launchers.host import find_host_program


def wrap_nixos_command(command: list[str], *, env: dict | None = None,
                       forward_host: bool = False,
                       cwd: str | Path | None = None) -> list[str]:
    try:
        in_flatpak = Path("/.flatpak-info").is_file()
        if in_flatpak:
            lines = Path("/run/host/os-release").read_text(encoding="utf-8").splitlines()
            os_id = next((line.partition("=")[2].strip().strip("\"'")
                          for line in lines if line.startswith("ID=")), "")
        else:
            os_id = platform.freedesktop_os_release().get("ID")
        if os_id not in ("nixos", "glfos"):
            return command
    except OSError:
        return command
    if not command or Path(command[0]).name != "steam-run":
        steam_run = find_host_program("steam-run", env=env)
        if steam_run is None:
            raise RuntimeError(
                "NixOS: steam-run is required to run the selected Wine/Proton runner. "
                "Install the steam-run package on the host and make it available "
                "on the desktop session's PATH, then restart Amethyst.")
        command = [steam_run, *command]
    if forward_host and in_flatpak:
        from Utils.flatpak.env import flatpak_forward_env_args
        command = ["flatpak-spawn", "--host", f"--directory={cwd or '/'}",
                   *flatpak_forward_env_args(env), *command]
    return command
