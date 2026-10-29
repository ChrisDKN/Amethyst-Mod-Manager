"""Resolve programs in the environment used for host launches."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def find_host_program(name: str, *, env: dict | None = None) -> str | None:
    if not Path("/.flatpak-info").is_file():
        return shutil.which(name, path=env.get("PATH") if env is not None else None)

    if shutil.which("flatpak-spawn") is None:
        raise RuntimeError("flatpak-spawn is required to check the host runtime.")
    from Utils.flatpak.env import flatpak_forward_env_args
    command = [
        "flatpak-spawn", "--host", "--directory=/",
        *flatpak_forward_env_args(env),
        "sh", "-c", 'command -v "$1" || exit 127', "amethyst-host-check", name,
    ]
    try:
        result = subprocess.run(
            command, env=env, cwd="/", stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not check the host runtime: {exc}") from exc
    if result.returncode == 127:
        return None
    if result.returncode != 0:
        raise RuntimeError(
            "Could not check the host runtime through flatpak-spawn: "
            + (result.stderr.strip() or f"exit {result.returncode}"))
    return result.stdout.strip() or None
