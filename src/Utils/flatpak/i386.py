"""
Utils/flatpak/i386.py
Detect and repair missing 32-bit support inside our own Flatpak sandbox.

The Flatpak manifest declares the org.freedesktop.Platform.Compat.i386 (+GL32)
extensions, which provide /lib/ld-linux.so.2 - the 32-bit ELF interpreter
Proton's wine needs when it boots a prefix (syswow64 processes are 32-bit even
for 64-bit games/tools). Flathub installs pull those related refs in
automatically, but installs from a .flatpak *bundle* (our release zip,
Warehouse, `flatpak install file.flatpak`) do NOT - the extension point stays
empty and the runtime's /lib/ld-linux.so.2 symlink dangles. Every in-sandbox
Proton/wine run then dies with "/lib/ld-linux.so.2: could not open".

Two repair surfaces:
  * ``preflight_i386_error`` - called before an in-sandbox Proton run so the
    user gets an actionable message instead of the cryptic loader error.
  * ``install_i386_extensions`` - installs the refs on the host via
    flatpak-spawn (the app startup self-heal in gui_qt.app uses this).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable

from Utils.app_log import safe_log as _safe_log

# Extension refs the sandbox needs for 32-bit support. Branches MUST match the
# manifest's add-extensions block in flatpak/io.github.Amethyst.ModManager.yml.
#
# IMPORTANT: the two extensions are branched on DIFFERENT axes:
#   * Compat.i386 tracks the freedesktop base (KDE 6.9 → 24.08). This ref
#     provides /lib/ld-linux.so.2 - the 32-bit ELF interpreter wine needs. It
#     is the ONE that actually matters; without it wine cannot boot at all.
#   * GL32.default tracks the GL driver version (1.4), NOT the freedesktop base.
#     There is no "GL32.default//24.08" ref on Flathub - asking for it fails
#     with "…not installed" / "file doesn't exist". GL32 only supplies 32-bit
#     OpenGL, which most tools (dtkit-patch, vcredist, wine setup) never touch,
#     so a GL32 failure must NOT sink the whole repair.
REQUIRED_I386_REF = "org.freedesktop.Platform.Compat.i386//24.08"
OPTIONAL_I386_REF = "org.freedesktop.Platform.GL32.default//1.4"

I386_EXTENSION_REFS = (REQUIRED_I386_REF, OPTIONAL_I386_REF)

# One-line manual fix, used in error messages and docs. Only the required ref
# is offered - it is what unblocks wine, and it avoids handing the user a GL32
# ref that may not resolve on every remote.
MANUAL_INSTALL_CMD = "flatpak install flathub " + REQUIRED_I386_REF


def _in_flatpak_sandbox() -> bool:
    return os.path.exists("/.flatpak-info")


def i386_support_missing() -> bool:
    """True when we run inside our Flatpak sandbox and the 32-bit loader is
    absent (the Compat.i386 extension isn't installed/mounted).

    /lib/ld-linux.so.2 is a runtime symlink into the extension mount point
    (/app/lib/i386-linux-gnu); ``os.path.exists`` follows it, so a dangling
    link correctly reads as missing. Outside Flatpak this always returns
    False - native/AppImage hosts manage their own 32-bit userland.
    """
    return _in_flatpak_sandbox() and not os.path.exists("/lib/ld-linux.so.2")


def i386_error_message() -> str:
    """Actionable message for a missing-32-bit-support failure."""
    return (
        "32-bit support is missing from the Flatpak sandbox, so wine cannot "
        "start (/lib/ld-linux.so.2 is not available). Install it with: "
        + MANUAL_INSTALL_CMD + " - then restart the app."
    )


def preflight_i386_error(proton_script) -> "str | None":
    """Return an actionable error when running *proton_script* would exec wine
    inside our Flatpak sandbox without 32-bit support, else None.

    Mirrors proton_run_command's dispatch: bare wine binaries (Lutris) and
    Steam-Flatpak Protons are forwarded to the host / Steam's own sandbox,
    which carry their own 32-bit runtimes - only the in-sandbox exec path
    needs the Compat.i386 extension.
    """
    if not i386_support_missing():
        return None
    from pathlib import Path
    from Utils.launchers.steam import (
        _proton_script_in_steam_flatpak, _own_process_in_steam_flatpak,
    )
    script = Path(proton_script)
    if script.name in ("wine", "wine64"):
        return None  # runs on the host via flatpak-spawn
    if _proton_script_in_steam_flatpak(script) and not _own_process_in_steam_flatpak():
        return None  # runs inside Steam's own sandbox
    return i386_error_message()


def _install_ref_on_host(ref: str, log_fn) -> "tuple[bool, str]":
    """Install a single ref on the host, trying the --user then the system
    ``flathub`` remote. Returns (ok, last_error_detail).

    Bazzite and other image-based distros ship ``flathub`` as a SYSTEM remote,
    so ``flatpak install --user flathub …`` fails with "remote not found".
    We try --user first (works on Steam Deck / most desktops and needs no
    polkit auth), then fall back to the default/system scope.
    """
    _log = _safe_log(log_fn)
    last_detail = ""
    for scope in ("--user", "--system"):
        cmd = [
            "flatpak-spawn", "--host", "--directory=/",
            "flatpak", "install", scope, "--noninteractive", "flathub", ref,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except Exception as exc:
            last_detail = str(exc)
            _log(f"i386 extensions: {ref} ({scope}) failed to run: {exc}")
            continue
        if result.returncode == 0:
            _log(f"i386 extensions: installed {ref} ({scope}).")
            return True, ""
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        last_detail = detail[-1] if detail else f"exit {result.returncode}"
        _log(f"i386 extensions: {ref} ({scope}) failed - {last_detail}")
    return False, last_detail


def install_i386_extensions(log_fn: "Callable[[str], None] | None" = None) -> bool:
    """Install the 32-bit compat extension(s) from Flathub.

    Runs ``flatpak install`` on the host via flatpak-spawn (the sandbox has no
    flatpak CLI; the manifest grants --talk-name=org.freedesktop.Flatpak).
    Returns True when the REQUIRED ref (Compat.i386, which provides
    ld-linux.so.2) is installed - that alone unblocks wine. The optional GL32
    ref is attempted best-effort and never fails the operation. The extensions
    only MOUNT on the next app launch, so callers should tell the user to
    restart.
    """
    _log = _safe_log(log_fn)
    if not _in_flatpak_sandbox():
        _log("i386 extensions: not running inside a Flatpak sandbox - nothing to do.")
        return False
    if shutil.which("flatpak-spawn") is None:
        _log("i386 extensions: flatpak-spawn is unavailable - install manually: "
             + MANUAL_INSTALL_CMD)
        return False

    _log(f"i386 extensions: installing {REQUIRED_I386_REF} …")
    ok, detail = _install_ref_on_host(REQUIRED_I386_REF, log_fn)
    if not ok:
        _log("i386 extensions: install failed"
             + (f" - {detail}" if detail else "")
             + f". Install manually: {MANUAL_INSTALL_CMD}")
        return False

    # Best-effort: 32-bit OpenGL. A failure here (e.g. the GL32 ref not being
    # present on the user's remote) must not report the whole repair as failed -
    # wine already works once Compat.i386 is in place.
    _log(f"i386 extensions: installing optional {OPTIONAL_I386_REF} (best-effort) …")
    gl_ok, _ = _install_ref_on_host(OPTIONAL_I386_REF, log_fn)
    if not gl_ok:
        _log("i386 extensions: optional GL32 not installed - 32-bit OpenGL "
             "unavailable, but wine will still run.")

    _log("i386 extensions: 32-bit support installed - mounts on the next app launch.")
    return True
