"""Decides whether Qt's OpenGL-widget path is safe to use on this machine.

A single QOpenGLWidget anywhere in a top-level window switches that window's
whole backing store to GL composition. When the GL path is broken, the result
is not a failed widget — it is a *black window*, because every non-native
child is composited through a surface that never presents (GH#350). Nothing
downstream can recover from that, so the check has to happen before the first
QOpenGLWidget is ever created.

Two things can break it:

* **Mixed Qt builds.** The AppImage bundles Arch's system Qt. Until the fix
  for GH#350 it did not bundle ``libQt6OpenGLWidgets``, so on Arch-family
  hosts (which have their own copy in ``/usr/lib``) the loader satisfied the
  import from the *host* while ``libQt6Widgets``/``libQt6Core`` stayed
  bundled. Both export ``Qt_6_PRIVATE_API``, so it loaded without complaint
  and then disagreed about widget internals. Elsewhere the host has no copy,
  the import fails cleanly, and nothing breaks — which is why this only ever
  reproduced on Arch derivatives.
* **No usable GL driver.** Bare VMs, remote sessions, a container without
  ``/dev/dri`` and without llvmpipe.

``AMM_DISABLE_GL=1`` forces the answer to "no" as a user-facing escape hatch.
"""

from __future__ import annotations

import os
import sys

# (ok, reason) — computed once, on the GUI thread, after QApplication exists.
_status: tuple[bool, str] | None = None

# Qt libraries that must come from our own bundle when running as an AppImage:
# these are the ones the GL widget path pulls in on top of QtWidgets/QtGui.
_GL_LIBS = ("libQt6OpenGLWidgets.so", "libQt6OpenGL.so")


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_disabled() -> bool:
    return _truthy("AMM_DISABLE_GL")


def _env_forced() -> bool:
    """AMM_FORCE_GL=1 — trust the machine over our probe.

    The checks below are conservative: they can refuse a GL stack that would
    in fact have worked (an odd offscreen-surface config, say). That costs a
    user their 3D preview, so leave them a way to overrule it.
    """
    return _truthy("AMM_FORCE_GL")


def _own_appdir() -> str:
    """Our AppImage's mount root, or "" when we are not running from one.

    APPDIR leaks into the environment of anything launched from another
    AppImage (an AppImage code editor's terminal, say), so its mere presence
    proves nothing — only trust it when this very file lives inside it.
    """
    appdir = os.environ.get("APPDIR", "")
    if not appdir:
        return ""
    root = os.path.realpath(appdir)
    if not os.path.realpath(__file__).startswith(root + os.sep):
        return ""
    return root


def _foreign_gl_libs() -> list[str]:
    """Loaded Qt GL libs that came from the host instead of our AppImage."""
    appdir = _own_appdir()
    if not appdir or not sys.platform.startswith("linux"):
        return []
    foreign = []
    try:
        with open("/proc/self/maps", "r", encoding="utf-8",
                  errors="replace") as fh:
            seen = set()
            for line in fh:
                path = line.rstrip("\n").partition(" /")[2]
                if not path or path in seen:
                    continue
                seen.add(path)
                path = "/" + path
                if not any(lib in path for lib in _GL_LIBS):
                    continue
                if not os.path.realpath(path).startswith(appdir + os.sep):
                    foreign.append(path)
    except OSError:
        return []
    return foreign


def _probe_context() -> tuple[bool, str]:
    """Create a throwaway offscreen GL context to see if the driver works."""
    from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    ctx = QOpenGLContext()
    ctx.setFormat(fmt)
    if not ctx.create():
        return False, "OpenGL context creation failed"
    surface = QOffscreenSurface()
    surface.setFormat(ctx.format())
    surface.create()
    if not surface.isValid():
        return False, "offscreen GL surface unavailable"
    if not ctx.makeCurrent(surface):
        return False, "OpenGL context could not be made current"
    ctx.doneCurrent()
    return True, ""


def gl_status() -> tuple[bool, str]:
    """(usable, reason-if-not) for the OpenGL widget path. Cached."""
    global _status
    if _status is not None:
        return _status
    _status = _compute_status()
    return _status


# Platform plugins with no window system behind them: a GL context can still
# be created (EGL talks to the driver directly), but QOpenGLWidget cannot.
_NO_GL_PLATFORMS = {"offscreen", "minimal", "minimalegl", "vnc"}


def _compute_status() -> tuple[bool, str]:
    if _env_disabled():
        return False, "disabled by AMM_DISABLE_GL"
    if _env_forced():
        return True, ""
    try:
        from PySide6.QtGui import QGuiApplication
        platform = (QGuiApplication.platformName() or "").lower()
    except Exception:                                     # noqa: BLE001
        platform = ""
    if platform in _NO_GL_PLATFORMS:
        return False, f"the {platform} platform plugin has no OpenGL support"
    try:
        import PySide6.QtOpenGLWidgets  # noqa: F401
    except Exception as exc:                              # noqa: BLE001
        return False, f"QtOpenGLWidgets unavailable ({exc})"
    foreign = _foreign_gl_libs()
    if foreign:
        return False, ("Qt OpenGL libraries loaded from outside the AppImage "
                       f"({', '.join(foreign)}) — mixing them with the bundled "
                       "Qt renders the window black")
    try:
        return _probe_context()
    except Exception as exc:                              # noqa: BLE001
        return False, f"OpenGL probe failed ({exc})"


def gl_available() -> bool:
    """True when a QOpenGLWidget can safely be created."""
    return gl_status()[0]
