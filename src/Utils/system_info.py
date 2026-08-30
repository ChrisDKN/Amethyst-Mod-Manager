"""Collect the environment facts a bug report needs (no toolkit imports).

Shown read-only at the bottom of the Settings panel and written to the top of
the application log at startup, so a pasted log carries the same facts the user
would otherwise have to be walked through collecting.

Every probe is best-effort: a missing /etc/os-release or an import failure
yields "Unknown" rather than raising, because this runs during startup.
"""

from __future__ import annotations

import locale
import os
import platform
import struct
import sys
from pathlib import Path

_UNKNOWN = "Unknown"


def app_version() -> str:
    """The Amethyst version string, or "Unknown"."""
    try:
        from version import __version__
        return str(__version__)
    except Exception:
        return _UNKNOWN


def run_mode() -> str:
    """How this process was packaged: Flatpak / AppImage / AUR / Source."""
    try:
        if Path("/.flatpak-info").exists() or os.environ.get("FLATPAK_ID"):
            return "Flatpak"
        # APPDIR/APPIMAGE are inherited by every child of an AppImage-packaged
        # process (e.g. a terminal inside an AppImage VS Code), so the vars
        # alone would misreport a source run. Confirm OUR code lives under the
        # mounted bundle before believing them.
        appdir = os.environ.get("APPDIR")
        if appdir and Path(__file__).resolve().is_relative_to(
                Path(appdir).resolve()):
            return "AppImage"
        # An AUR/system install lands under /usr; a git checkout does not.
        exe = Path(sys.argv[0]).resolve()
        if str(exe).startswith(("/usr/bin/", "/usr/lib/", "/usr/share/")):
            return "System package"
    except Exception:
        pass
    return "Source"


def _flatpak_info() -> dict[str, str]:
    """Parsed /.flatpak-info key=value pairs (last section wins). {} if absent."""
    out: dict[str, str] = {}
    try:
        for line in Path("/.flatpak-info").read_text(
                encoding="utf-8", errors="replace").splitlines():
            key, sep, val = line.partition("=")
            if sep:
                out[key.strip()] = val.strip()
    except Exception:
        pass
    return out


def package_details() -> str:
    """Build/runtime detail behind `run_mode` - "Flatpak" alone can't tell a
    stale install from a current one.

    Flatpak: runtime ref + whether the 32-bit extensions are present (their
    absence is what breaks Proton launches). AppImage: the bundle's filename,
    which carries the version/build. "-" when there is nothing to add.
    """
    mode = run_mode()
    if mode == "Flatpak":
        info = _flatpak_info()
        runtime = info.get("runtime") or _UNKNOWN
        bits = [f"runtime={runtime}"]
        try:
            from Utils.flatpak_i386 import i386_support_missing
            bits.append(f"i386={'missing' if i386_support_missing() else 'ok'}")
        except Exception:
            pass
        return ", ".join(bits)
    if mode == "AppImage":
        # $APPIMAGE is the bundle path; its name carries the version.
        img = os.environ.get("APPIMAGE") or ""
        return Path(img).name or _UNKNOWN
    return "-"


# GL is a Qt concept and Utils must not import gui_qt, so the Qt side injects
# a provider at startup (gui_qt.glue). Unset → "Not probed yet".
_gl_status_provider: "callable | None" = None
_qt_runtime_provider: "callable | None" = None


def set_gl_status_provider(fn: "callable") -> None:
    """Register a callable returning ``(ok, reason)`` or None if not yet probed."""
    global _gl_status_provider
    _gl_status_provider = fn


def set_qt_runtime_provider(fn: "callable") -> None:
    global _qt_runtime_provider
    _qt_runtime_provider = fn


def gl_status_text() -> str:
    """The cached OpenGL verdict, without ever triggering the probe.

    ``gl_support.gl_status()`` spawns a child process and BLOCKS, so asking it
    here would cost a subprocess on every Settings open (and at startup, where
    the whole point is to stay cheap). The registered provider reads the
    memoised result only and reports "Not probed yet" when nothing has asked
    for it - which is itself accurate: no 3D preview has been opened yet.
    """
    if _gl_status_provider is None:
        return "Not probed yet"
    try:
        status = _gl_status_provider()
        if status is None:
            return "Not probed yet"
        ok, reason, *details = status
        if ok:
            return "Available" + (f" ({details[0]})" if details and details[0] else "")
        return f"Unavailable ({reason})"
    except Exception:
        return _UNKNOWN


def active_env_overrides() -> str:
    """AMM_*/Qt overrides in effect - kill switches change behaviour a lot.

    Reads the live environment rather than the saved settings, so a variable
    exported in the user's real shell shows up too. A forgotten
    ``AMM_DISABLE_GL=1`` is invisible in a bug report otherwise.
    """
    try:
        from Utils.app_env import KNOWN_VARS
        names = [v["name"] for v in KNOWN_VARS if v.get("name")]
    except Exception:
        names = []
    known = set(names)
    # Catch any other AMM_ var the user set that isn't in the catalogue,
    # but skip the internal _AMM_ markers the app sets for itself.
    names += [k for k in os.environ
              if k.startswith("AMM_") and k not in names]

    def display_value(name: str) -> str:
        value = os.environ[name].replace("\r", r"\r").replace("\n", r"\n")
        return value if len(value) <= 200 else value[:197] + "..."

    active = [
        (f"{n}={display_value(n)}" if n in known else f"{n}=<set>")
        for n in sorted(set(names)) if os.environ.get(n)
    ]
    if not active:
        return "None"
    result = ", ".join(active)
    return result if len(result) <= 2000 else result[:1997] + "..."


def distribution() -> str:
    """Pretty distro name from /etc/os-release (SteamOS, Arch Linux, …)."""
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            for line in Path(path).read_text(encoding="utf-8",
                                             errors="replace").splitlines():
                key, _, val = line.partition("=")
                if key == "PRETTY_NAME" or key == "NAME":
                    name = val.strip().strip('"').strip("'")
                    if name and key == "PRETTY_NAME":
                        return name
        except Exception:
            continue
    return _UNKNOWN


def qt_version() -> str:
    """"PySide6 x.y.z / Qt x.y.z", or "Unknown" when PySide6 is absent."""
    try:
        import PySide6
        from PySide6.QtCore import qVersion
        return f"PySide6 {PySide6.__version__} / Qt {qVersion()}"
    except Exception:
        return _UNKNOWN


def desktop() -> str:
    """Desktop environment name (KDE, GNOME, …) from XDG_CURRENT_DESKTOP."""
    val = (os.environ.get("XDG_CURRENT_DESKTOP")
           or os.environ.get("DESKTOP_SESSION") or "")
    # "KDE", but also "ubuntu:GNOME" / "X-Cinnamon" - take the last component.
    val = val.split(":")[-1].strip()
    return val or _UNKNOWN


def session_type() -> str:
    """Display server: wayland, x11, or Unknown."""
    val = os.environ.get("XDG_SESSION_TYPE", "").strip()
    if not val:
        if os.environ.get("WAYLAND_DISPLAY"):
            val = "wayland"
        elif os.environ.get("DISPLAY"):
            val = "x11"
    return val or _UNKNOWN


def architecture_details() -> str:
    machine = platform.machine() or _UNKNOWN
    return f"{machine}, {struct.calcsize('P') * 8}-bit"


def libc_details() -> str:
    name, version = platform.libc_ver()
    return " ".join(part for part in (name, version) if part) or _UNKNOWN


def locale_details() -> str:
    current = locale.setlocale(locale.LC_CTYPE, None) or _UNKNOWN
    preferred = locale.getpreferredencoding(False) or _UNKNOWN
    filesystem = sys.getfilesystemencoding() or _UNKNOWN
    return f"{current}; preferred={preferred}; filesystem={filesystem}"


def display_environment() -> str:
    return (f"DISPLAY={'set' if os.environ.get('DISPLAY') else 'unset'}, "
            f"WAYLAND_DISPLAY={'set' if os.environ.get('WAYLAND_DISPLAY') else 'unset'}")


def qpa_request() -> str:
    value = os.environ.get("QT_QPA_PLATFORM")
    if not value:
        return "automatic (Qt)"
    app_set = set((os.environ.get("_AMM_ENV_KEYS") or "").split(","))
    if value and "QT_QPA_PLATFORM" in app_set:
        source = "saved Amethyst setting"
    elif os.environ.get("_AMM_OWNS_QT_PLATFORM") == "1":
        source = "Amethyst launcher default"
    else:
        source = "inherited environment"
    return f"{value} ({source})"


def qt_runtime_details() -> dict[str, str]:
    if _qt_runtime_provider is None:
        return {}
    try:
        details = _qt_runtime_provider()
        return details if isinstance(details, dict) else {}
    except Exception:
        return {}


def collect() -> list[tuple[str, str]]:
    """Ordered (label, value) pairs - the display and log order are the same."""
    def _safe(fn, fallback: str = _UNKNOWN) -> str:
        try:
            return fn() or fallback
        except Exception:
            return fallback

    runtime = list(qt_runtime_details().items())
    return [
        ("App version", app_version()),
        ("OS", _safe(platform.platform)),
        ("Distribution", distribution()),
        ("Kernel", _safe(platform.release)),
        ("Architecture", _safe(architecture_details)),
        ("libc", _safe(libc_details)),
        ("Python", _safe(platform.python_version)),
        ("Locale", _safe(locale_details)),
        ("Qt", qt_version()),
        ("Run mode", run_mode()),
        ("Package", _safe(package_details, "-")),
        ("Desktop", desktop()),
        ("Session", session_type()),
        ("Display sockets", display_environment()),
        *runtime,
        ("QPA request", qpa_request()),
        ("OpenGL", _safe(gl_status_text)),
        ("Env overrides", _safe(active_env_overrides, "None")),
    ]


def log_lines() -> list[str]:
    """The same facts as one aligned block for the top of the app log."""
    pairs = collect()
    width = max((len(k) for k, _v in pairs), default=0)
    return [f"{k.ljust(width)} : {v}" for k, v in pairs]
