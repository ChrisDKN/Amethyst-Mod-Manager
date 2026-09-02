"""Helpers for safely launching host-system programs such as xdg-open from
a polluted shell environment.

Inside an AppImage, anylinux.so (LD_PRELOAD-injected by quick-sharun) hooks
execve and scrubs AppDir-pointing env vars from child processes - so we
don't need to do anything special there. sharun also doesn't use
LD_LIBRARY_PATH; it invokes the dynamic linker with --library-path.

host_env() therefore only protects against pollution from *outside* the
AppImage: conda/pyenv/Steam-runtime can leave LD_LIBRARY_PATH pointing at
incompatible libraries, which would break xdg-open or Dolphin.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from Utils.app_log import app_log


from Utils.environment.appimage import strip_appimage_vars


# Env that configures OUR Qt UI and must not follow a launched program.
# Amethyst forces QT_QPA_PLATFORM=xcb on itself (XWayland gives Qt the global
# coordinates tooltips and exact scaling need) - but a child can be *unable*
# to honour it: the OpenMW flatpak ships `fallback-x11`, so on a Wayland
# session its sandbox gets no X socket at all, and its Qt launcher aborted
# with "no Qt platform plugin could be initialized" (SIGABRT, rc 137) before
# the game ever started. Same for a scale factor: ours would silently resize
# somebody else's UI.
#
# Each name is dropped only when the value is OURS - a wrapper marker set
# where we defaulted the var, or a name Settings ▸ Advanced applied this
# launch - so a value the user exported in their own shell still reaches the
# child, exactly as it would had they started it from that shell.
_UI_ENV_MARKERS: dict[str, str] = {
    "QT_QPA_PLATFORM": "_AMM_OWNS_QT_PLATFORM",
    "QT_SCALE_FACTOR": "_AMM_OWNS_SCALE",
}

# Only ever set through Settings ▸ Advanced (app_env.KNOWN_VARS), never by a
# wrapper - so being listed in _AMM_ENV_KEYS is the whole ownership test.
_UI_ENV_APP_ONLY: tuple[str, ...] = ("QT_XCB_GL_INTEGRATION",)


def strip_ui_env(env: dict) -> dict:
    """Return *env* without the Qt display vars this app set for itself.

    Also drops every ``_AMM_`` internal marker: they describe our own process
    state (which vars we own across a re-exec) and mean nothing to a child.
    """
    app_set = {n for n in (env.get("_AMM_ENV_KEYS") or "").split(",") if n}
    for name, marker in _UI_ENV_MARKERS.items():
        if env.get(marker) == "1" or name in app_set:
            env.pop(name, None)
    for name in _UI_ENV_APP_ONLY:
        if name in app_set:
            env.pop(name, None)
    for name in [n for n in env if n.startswith("_AMM_")]:
        env.pop(name, None)
    return env


# Which display sockets a Flatpak app gets is decided by ITS manifest, not by
# our session: org.openmw.OpenMW ships `fallback-x11`, which on a Wayland
# session grants no X socket at all. Naming a platform plugin for a sandboxed
# child therefore points it at something it may have no way to reach, and Qt
# aborts (SIGABRT) rather than falling back. Inside the sandbox Qt's own
# auto-detection is always the better-informed choice, so these are dropped for
# a `flatpak run` child even when the value is the USER's - unlike
# strip_ui_env, which only scrubs what we set ourselves. The value still has to
# reach the sandbox to matter: `flatpak run` forwards the caller's environment,
# and QT_QPA_PLATFORM is not on flatpak's own dont-export list.
_SANDBOX_DISPLAY_VARS: tuple[str, ...] = (
    "QT_QPA_PLATFORM", "QT_XCB_GL_INTEGRATION",
)


def is_flatpak_run(cmd) -> bool:
    """True when *cmd* starts a Flatpak app (directly or via flatpak-spawn)."""
    toks = [str(c) for c in cmd]
    return any(os.path.basename(tok) == "flatpak" and toks[i + 1] == "run"
               for i, tok in enumerate(toks[:-1]))


def strip_sandbox_display_env(cmd, env: "dict | None") -> "dict | None":
    """Drop display env a Flatpak child may be unable to honour.

    ``env=None`` means "inherit ours", which for a Flatpak child is exactly the
    leak we're closing - so it materialises the environment to scrub it.
    """
    if not is_flatpak_run(cmd):
        return env
    source = os.environ if env is None else env
    return {k: v for k, v in source.items() if k not in _SANDBOX_DISPLAY_VARS}


def host_env() -> dict[str, str]:
    """Return os.environ scrubbed of AppImage-injected pollution.

    Inside an AppImage, anylinux.so (LD_PRELOAD'd by quick-sharun) already
    drops some AppDir-pointing vars on execve, but it doesn't know about our
    custom ones (MOD_MANAGER_GAMES, FONTCONFIG_FILE) or about /tmp/.mount_*
    fragments inside PATH / XDG_DATA_DIRS - and it isn't built at all on some
    build hosts (ANYLINUX_LIB=0). So we strip in Python too.

    Deliberately unconditional (unlike ``protontricks.strip_appimage_env``):
    outside an AppImage this also defends against stale env in shells the
    user opened *from* a previous AppImage launch - `$PATH` still has
    `/tmp/.mount_<dead>/bin` in it, etc. The var list lives in
    :mod:`Utils.environment.appimage` (single source of truth).

    Also drops the Qt display env we set for our OWN window - see
    :func:`strip_ui_env`.
    """
    return strip_ui_env(strip_appimage_vars(os.environ.copy()))


def _in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def xdg_download_dir() -> Path:
    """Return the user's Downloads directory.

    Desktops record localised user dirs ("Téléchargements", "Descargas", …)
    in ~/.config/user-dirs.dirs but rarely export XDG_DOWNLOAD_DIR into the
    environment, so check the env var first, then parse the file, then fall
    back to ~/Downloads.
    """
    env = os.environ.get("XDG_DOWNLOAD_DIR")
    if env:
        return Path(env)
    home = Path.home()
    config_dirs = [Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")]
    # Flatpak redirects XDG_CONFIG_HOME to the app-specific config directory,
    # while the desktop's localised user-dirs file remains in ~/.config.  The
    # app has home access, so try that host location as a secondary source.
    host_config = home / ".config"
    if host_config != config_dirs[0]:
        config_dirs.append(host_config)
    for cfg_base in config_dirs:
        try:
            lines = (cfg_base / "user-dirs.dirs").read_text(
                encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            line = line.strip()
            if not line.startswith("XDG_DOWNLOAD_DIR="):
                continue
            raw = line.split("=", 1)[1].strip().strip('"')
            raw = raw.replace("$HOME", str(home))
            # A value of just "$HOME/" means the dir is disabled per the
            # xdg-user-dirs spec - use the fallback instead.
            if raw and Path(raw) != home:
                return Path(raw)
            break
    return home / "Downloads"


def spawn_watched(
    cmd: list[str],
    label: str,
    log_fn: Callable[[str], None] | None,
    on_fail: Callable[[], None] | None = None,
    log_success: bool = False,
) -> None:
    """Run *cmd* in the background, log non-zero exits, optionally chain a fallback.

    Public so other launchers (Utils/executables/launch.launch_via_steam) can reuse the
    Flatpak-safe CWD handling and exit-code watching instead of calling
    ``subprocess.Popen`` directly - a bare Popen of ``flatpak-spawn --host …``
    succeeds even when the *host* command it forwards to is missing or fails,
    which silently swallows launch errors.

    *log_success* also logs the rc=0 hand-off. A clean exit of e.g.
    ``xdg-open steam://…`` only proves the URL was handed to a handler - the
    launcher can still silently drop it - so game-launch chains record the
    hand-off to make "our side worked, launcher ignored it" diagnosable from
    a user's session log.
    """
    # Use a CWD the host definitely has. Inside Flatpak the sandbox CWD
    # (e.g. /app/share/amethyst-mod-manager) doesn't exist on the host, so
    # `flatpak-spawn --host` inherits it and the spawned host process fails
    # to start with "Failed to change to directory".
    cwd = os.path.expanduser("~") if os.path.isdir(os.path.expanduser("~")) else "/"
    # A fallback chain hops threads (the next candidate is tried from _watch),
    # so carry the launch report across explicitly - see Utils.processes.report.
    from Utils.processes import report as launch_report
    rep = launch_report.current()
    try:
        proc = subprocess.Popen(
            cmd,
            env=strip_sandbox_display_env(cmd, host_env()),
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        msg = f"{label}: {cmd[0]} not found ({exc})"
        app_log(msg)
        if log_fn:
            log_fn(msg)
        if on_fail:
            with launch_report.bind(rep):
                on_fail()
        return
    if rep is not None:
        rep.mark_spawned()

    def _watch() -> None:
        _, err = proc.communicate()
        rc = proc.returncode
        if rc != 0:
            text = err.decode(errors="replace").strip() or "(no output)"
            msg = f"{label}: rc={rc} {text}"
            app_log(msg)
            if log_fn:
                log_fn(msg)
            if on_fail:
                with launch_report.bind(rep):
                    on_fail()
        elif log_success:
            msg = f"{label}: handed off via {cmd[0]} (rc=0)"
            app_log(msg)
            if log_fn:
                log_fn(msg)

    threading.Thread(target=_watch, daemon=True).start()


def xdg_open(path: str | Path, log_fn: Callable[[str], None] | None = None) -> None:
    """Open *path* with the user's default application via xdg-open.

    Uses host_env() so that the launched application (e.g. Dolphin) loads
    its own system libraries. Failures are logged to app_log (always) and
    log_fn (if provided), so they don't disappear silently.

    Inside a Flatpak sandbox, mirror open_url's chain rather than betting
    everything on one command - a single `flatpak-spawn --host xdg-open`
    silently does nothing when the *host* has no inode/directory handler
    (a Deck that never booted to Desktop Mode), when its mimeapps.list
    points at a removed .desktop, or when a user revoked
    org.freedesktop.Flatpak in Flatseal (flatpak-spawn is still on PATH
    inside the sandbox, so which() can't detect that). Try, in order:
      1. `flatpak-spawn --host xdg-open <path>` - host handler, opens the
         user's real file manager outside the sandbox.
      2. `gio open <path>` - OpenURI portal from *inside* the sandbox;
         works with no host MIME association and no host-talk permission.
      3. bare `xdg-open <path>` - last resort (runtime handler).
    Each step's failure is logged and triggers the next.
    """
    target = str(path)
    if not _in_flatpak():
        spawn_watched(["xdg-open", target], f"xdg-open {target!r}", log_fn)
        return

    def try_gio() -> None:
        if shutil.which("gio"):
            spawn_watched(["gio", "open", target], f"gio open {target!r}",
                          log_fn, on_fail=try_xdg)
        else:
            try_xdg()

    def try_xdg() -> None:
        if shutil.which("xdg-open"):
            spawn_watched(["xdg-open", target], f"xdg-open {target!r}", log_fn,
                          on_fail=_exhausted)
        else:
            _exhausted()

    def _exhausted() -> None:
        msg = (f"xdg_open: no working handler for {target!r} - check the host "
               f"file-manager association (xdg-open) and that "
               f"org.freedesktop.Flatpak talk is permitted")
        app_log(msg)
        if log_fn:
            log_fn(msg)

    if shutil.which("flatpak-spawn"):
        spawn_watched(
            ["flatpak-spawn", "--host", "xdg-open", target],
            f"flatpak-spawn xdg-open {target!r}",
            log_fn,
            on_fail=try_gio,
        )
    else:
        try_gio()


def open_url(url: str, log_fn: Callable[[str], None] | None = None) -> None:
    """Open *url* in the user's default browser.

    Inside a Flatpak sandbox `xdg-open` from the runtime usually can't reach
    the host's browser. Try, in order:
      1. `flatpak-spawn --host xdg-open <url>` - runs xdg-open on the host.
      2. `gio open <url>` - uses the OpenURI portal from inside the sandbox.
      3. bare `xdg-open <url>` - last resort.
    Each step's failure is logged and triggers the next.
    """
    if not _in_flatpak():
        spawn_watched(["xdg-open", url], f"xdg-open {url!r}", log_fn)
        return

    def try_gio() -> None:
        if shutil.which("gio"):
            spawn_watched(["gio", "open", url], f"gio open {url!r}", log_fn,
                           on_fail=try_xdg)
        else:
            try_xdg()

    def try_xdg() -> None:
        if shutil.which("xdg-open"):
            spawn_watched(["xdg-open", url], f"xdg-open {url!r}", log_fn)
        else:
            msg = f"open_url: no working launcher for {url!r}"
            app_log(msg)
            if log_fn:
                log_fn(msg)

    if shutil.which("flatpak-spawn"):
        spawn_watched(
            ["flatpak-spawn", "--host", "xdg-open", url],
            f"flatpak-spawn xdg-open {url!r}",
            log_fn,
            on_fail=try_gio,
        )
    else:
        try_gio()
