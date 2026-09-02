"""Start and wait for Steam without asking it to launch a game."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from Utils.launchers.steam import steam_client_running


# Amethyst can itself be launched as a Steam shortcut or from a game's Steam
# Linux Runtime.  Those variables describe that *parent game*, not the Steam
# client we may need to start.  Passing them to ``steam -silent`` can make the
# new client inherit a stale compat prefix/runtime or advertise the shortcut's
# app identity.  Keep the list deliberately scoped to Steam's game/compat
# context; ordinary desktop/session variables still come from ``host_env``.
_STEAM_CLIENT_ENV_EXACT = frozenset({
    "SteamAppId",
    "SteamGameId",
    "SteamOverlayGameId",
    "STEAM_COMPAT_APP_ID",
    "SteamAppUser",
    "SteamUser",
    "SteamClientLaunch",
    "SteamEnv",
})
_STEAM_CLIENT_ENV_PREFIXES = (
    "STEAM_COMPAT_",
    "STEAM_RUNTIME_",
    "PRESSURE_VESSEL_",
)


def _steam_client_env() -> dict[str, str]:
    """Host environment with inherited per-game Steam context removed."""
    from Utils.environment.xdg import host_env

    env = host_env()
    for key in tuple(env):
        if (key in _STEAM_CLIENT_ENV_EXACT
                or key == "STEAM_RUNTIME"
                or key.startswith(_STEAM_CLIENT_ENV_PREFIXES)):
            env.pop(key, None)
    return env


def ensure_steam_client_running(log_fn=None, timeout: float = 30.0) -> bool:
    """Start Steam when necessary and wait for a stable client process.

    Some native Linux games are launched directly even though they still use
    Steamworks. Supplying ``SteamAppId`` identifies the game, but SteamAPI also
    needs a live client process to provide its IPC session. Starting the game
    through ``steam://rungameid`` is not suitable for a profile VFS because
    Steam would launch the physical install outside the private view. This
    helper opens only the client and leaves the caller to launch the selected
    physical or virtual executable.

    ``xdg-open`` follows the desktop's registered native, Flatpak, or Snap
    Steam installation. A native ``steam -silent`` call remains as fallback.
    The function is synchronous and is called only from the Play worker.
    """
    _log = log_fn or (lambda _message: None)
    if steam_client_running(strict=True):
        _log("Play: Steam client is already running.")
        return True

    timeout = max(1.0, float(timeout))
    home = Path.home()
    in_flatpak = Path("/.flatpak-info").exists()
    have_spawn = shutil.which("flatpak-spawn") is not None
    candidates: list[list[str]] = []
    if in_flatpak and have_spawn:
        candidates.extend((
            ["flatpak-spawn", "--host", "xdg-open", "steam://open/main"],
            ["flatpak-spawn", "--host", "steam", "-silent"],
            ["flatpak-spawn", "--host", "flatpak", "run",
             "com.valvesoftware.Steam", "-silent"],
        ))
    else:
        if shutil.which("xdg-open"):
            candidates.append(["xdg-open", "steam://open/main"])
        if shutil.which("steam"):
            candidates.append(["steam", "-silent"])
        # A host with only Flatpak Steam normally has no `steam` executable.
        # xdg-open remains the preferred route because it follows the user's
        # registered client; this explicit command is the reliable fallback
        # when the steam:// MIME association is missing or stale.
        if shutil.which("flatpak"):
            candidates.append([
                "flatpak", "run", "com.valvesoftware.Steam", "-silent",
            ])

    if not candidates:
        _log("Play: Steam is not running and no Steam client launcher was found.")
        return False

    _log("Play: Steam is required by this native game; starting the client ...")
    deadline = time.monotonic() + timeout
    # ``xdg-open`` follows the user's registered Steam distribution. Once a
    # launcher accepts the request (stays alive or exits successfully), give it
    # the whole remaining timeout: starting a native/Flatpak fallback merely
    # because a cold client is slow could start two different Steam clients.
    # Fall through only when a candidate cannot spawn or explicitly fails.
    for command in candidates:
        try:
            proc = subprocess.Popen(
                command,
                env=_steam_client_env(),
                cwd=str(home) if home.is_dir() else "/",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            _log(f"Play: could not start Steam via {command[0]}: {exc}")
            continue

        while time.monotonic() < deadline:
            if steam_client_running(strict=True):
                # steam.pid can appear while the client bootstrap is still
                # replacing processes. Require the recorded Steam process to
                # survive a short settle window; this proves process stability,
                # not Steamworks IPC/login readiness.
                time.sleep(2.0)
                if steam_client_running(strict=True):
                    _log("Play: Steam client process is running.")
                    return True
            rc = proc.poll()
            if rc is not None and rc != 0:
                _log(
                    f"Play: Steam launcher {command[0]} exited with code "
                    f"{rc}; trying the next client route."
                )
                break
            time.sleep(0.25)
        if time.monotonic() >= deadline:
            break

    if steam_client_running(strict=True):
        _log("Play: Steam client process is running.")
        return True
    _log(
        "Play: Steam did not become ready in time. Finish starting/signing "
        "in to Steam, then press Play again."
    )
    return False
