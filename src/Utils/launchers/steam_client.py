"""Start and wait for Steam without asking it to launch a game."""

from __future__ import annotations

import re
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

_LOGIN_STATE_RE = re.compile(
    rb"SetLoginState:\s*([A-Za-z]+)\s*-\s*OK"
)
_READY_LOGIN_STATES = frozenset((b"Success", b"Offline"))
_UNKNOWN_LOGIN_SETTLE = 15.0
_MAX_LOGIN_LOG_READ = 128 * 1024


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


def _steam_login_log_paths(home: Path) -> tuple[Path, ...]:
    roots = (
        home / ".local" / "share" / "Steam",
        home / ".steam" / "root",
        home / ".steam" / "debian-installation",
        home / ".var" / "app" / "com.valvesoftware.Steam"
        / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam"
        / "data" / "Steam",
        home / "snap" / "steam" / "common"
        / ".local" / "share" / "Steam",
        home / "snap" / "steam" / "common" / ".steam" / "root",
    )
    return tuple(root / "logs" / "steamui_login.txt" for root in roots)


def _steam_login_log_cursors(
        home: Path) -> dict[Path, tuple[int, int, int] | None]:
    cursors: dict[Path, tuple[int, int, int] | None] = {}
    for path in _steam_login_log_paths(home):
        try:
            st = path.stat()
            cursors[path] = (st.st_dev, st.st_ino, st.st_size)
        except OSError:
            cursors[path] = None
    return cursors


def _steam_login_ready_since(
        cursors: dict[Path, tuple[int, int, int] | None],
) -> tuple[bool, bool]:
    saw_state = False
    for path, cursor in cursors.items():
        try:
            st = path.stat()
            offset = 0
            if (cursor is not None
                    and cursor[:2] == (st.st_dev, st.st_ino)
                    and st.st_size >= cursor[2]):
                offset = cursor[2]
            if st.st_size <= offset:
                continue
            offset = max(offset, st.st_size - _MAX_LOGIN_LOG_READ)
            with path.open("rb") as stream:
                stream.seek(offset)
                states = _LOGIN_STATE_RE.findall(stream.read())
        except OSError:
            continue
        if not states:
            continue
        saw_state = True
        if any(state in _READY_LOGIN_STATES for state in states):
            return True, True
    return False, saw_state


def ensure_steam_client_running(log_fn=None, timeout: float = 30.0) -> bool:
    """Start Steam when necessary and wait for a usable client session.

    A direct or profile-VFS launch cannot use ``steam://rungameid`` because
    Steam would launch the physical install outside the private view. This
    helper opens only the client and leaves the caller to launch the selected
    physical or virtual executable.

    ``xdg-open`` follows the desktop's registered native, Flatpak, or Snap
    Steam installation. Distribution-specific commands remain as fallbacks.
    The function is synchronous and is called only from the Play worker.
    """
    _log = log_fn or (lambda _message: None)
    if steam_client_running(strict=True):
        _log("Play: Steam client is already running.")
        return True

    timeout = max(1.0, float(timeout))
    home = Path.home()
    login_cursors = _steam_login_log_cursors(home)
    in_flatpak = Path("/.flatpak-info").exists()
    have_spawn = shutil.which("flatpak-spawn") is not None
    candidates: list[list[str]] = []
    if in_flatpak and have_spawn:
        candidates.extend((
            ["flatpak-spawn", "--host", "xdg-open", "steam://open/main"],
            ["flatpak-spawn", "--host", "steam", "-silent"],
            ["flatpak-spawn", "--host", "flatpak", "run",
             "com.valvesoftware.Steam", "-silent"],
            ["flatpak-spawn", "--host", "snap", "run", "steam", "-silent"],
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
        if shutil.which("snap"):
            candidates.append(["snap", "run", "steam", "-silent"])

    if not candidates:
        _log("Play: Steam is not running and no Steam client launcher was found.")
        return False

    _log("Play: Steam is required by this direct launch; starting the client ...")
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    saw_login_state = False
    # ``xdg-open`` follows the user's registered Steam distribution. Once a
    # launcher accepts the request (stays alive or exits successfully), give it
    # the whole remaining timeout: starting another distribution fallback
    # because a cold client is slow could start two different Steam clients.
    # Fall through only when a candidate cannot spawn or explicitly fails.
    for command in candidates:
        try:
            proc = subprocess.Popen(
                command,
                env=_steam_client_env(),
                cwd=str(home) if home.is_dir() else "/",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            _log(f"Play: could not start Steam via {command[0]}: {exc}")
            continue

        while time.monotonic() < deadline:
            if steam_client_running(strict=True):
                now = time.monotonic()
                if stable_since is None:
                    stable_since = now
                login_ready, saw_state = _steam_login_ready_since(login_cursors)
                saw_login_state = saw_login_state or saw_state
                if login_ready and steam_client_running(strict=True):
                    _log("Play: Steam client is signed in and ready.")
                    return True
                if (not saw_login_state
                        and now - stable_since >= _UNKNOWN_LOGIN_SETTLE):
                    _log(
                        "Play: Steam client IPC is ready; its login state "
                        "was not available."
                    )
                    return True
            else:
                stable_since = None
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

    login_ready, _saw_state = _steam_login_ready_since(login_cursors)
    if login_ready and steam_client_running(strict=True):
        _log("Play: Steam client is signed in and ready.")
        return True
    _log(
        "Play: Steam did not become ready in time. Finish starting/signing "
        "in to Steam, then press Play again."
    )
    return False
