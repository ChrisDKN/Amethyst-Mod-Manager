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

The probe itself runs in a **child process**, and that is not optional: when
GLX cannot supply an FBConfig for the requested format, Qt does not return an
error — ``QOpenGLContext::create()`` reaches ``qFatal("Could not initialize
GLX")`` and aborts the process from C, where no Python ``try`` can intercept
it. Probing in-process therefore killed the very app the check exists to
protect (it aborted the AppImage smoke test under Xvfb on the first run after
the check was added). A child that aborts costs us a wait and a return code.

``AMM_DISABLE_GL=1`` forces the answer to "no" as a user-facing escape hatch;
``AMM_FORCE_GL=1`` skips the probe and trusts the machine.
"""

from __future__ import annotations

import os
import subprocess
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


# Run in a child interpreter by _probe_context(). Creates exactly the context
# the viewport will ask for (3.3 core — the shaders are GLSL 330) and reports
# through the exit code, because anything it prints may be drowned out by Qt's
# own warnings. A qFatal in here takes the child down and nothing else.
_PROBE_SRC = r'''
import os, sys
_p = os.environ.get("_AMM_GL_PROBE_SYSPATH", "")
if _p:
    sys.path[:0] = [x for x in _p.split(os.pathsep) if x]
try:
    from PySide6.QtGui import (QGuiApplication, QOffscreenSurface,
                               QOpenGLContext, QSurfaceFormat)
except Exception as exc:
    print(exc, file=sys.stderr)
    raise SystemExit(5)
app = QGuiApplication(sys.argv)
fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.CoreProfile)
ctx = QOpenGLContext()
ctx.setFormat(fmt)
if not ctx.create():
    raise SystemExit(2)
surface = QOffscreenSurface()
surface.setFormat(ctx.format())
surface.create()
if not surface.isValid():
    raise SystemExit(3)
if not ctx.makeCurrent(surface):
    raise SystemExit(4)
ctx.doneCurrent()
raise SystemExit(0)
'''

_PROBE_CODES = {
    2: "OpenGL context creation failed",
    3: "offscreen GL surface unavailable",
    4: "OpenGL context could not be made current",
    5: "the OpenGL probe could not load PySide6",
}


def _probe_context() -> tuple[bool, str]:
    """Create a throwaway offscreen GL context — in a child process — to see if
    the driver works.

    Anything other than a clean exit 0 means "no": a non-zero code from the
    checks above, a signal (Qt's qFatal aborts), a timeout, or a child that
    would not start at all. Assuming "yes" when we cannot tell would put the
    abort back in this process, so an unusable probe disables the GL path and
    ``AMM_FORCE_GL=1`` remains the way to overrule it.
    """
    if not sys.executable:
        return False, "no interpreter available to run the OpenGL probe"
    env = dict(os.environ)
    # The child must talk to the same window system we do, and must be able to
    # import PySide6 from wherever this process found it (the AppImage's bundled
    # site-packages, a venv, /app in the flatpak). Passed as our own variable
    # rather than PYTHONPATH so it cannot leak into anything else we launch.
    env["_AMM_GL_PROBE_SYSPATH"] = os.pathsep.join(p for p in sys.path if p)
    try:
        from PySide6.QtGui import QGuiApplication
        platform = QGuiApplication.platformName()
        if platform:
            env["QT_QPA_PLATFORM"] = platform
    except Exception:                                     # noqa: BLE001
        pass
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_SRC], env=env, timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        return False, "the OpenGL probe timed out"
    except Exception as exc:                              # noqa: BLE001
        return False, f"the OpenGL probe could not run ({exc})"
    if proc.returncode == 0:
        return True, ""
    if proc.returncode < 0:
        # Killed by a signal — qFatal("Could not initialize GLX") and friends.
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        last = detail.splitlines()[-1] if detail else ""
        why = f"the OpenGL driver aborted the probe (signal {-proc.returncode})"
        return False, f"{why}: {last}" if last else why
    return False, _PROBE_CODES.get(
        proc.returncode, f"the OpenGL probe failed (exit {proc.returncode})")


def gl_status() -> tuple[bool, str]:
    """(usable, reason-if-not) for the OpenGL widget path. Cached.

    Call this lazily, right before a QOpenGLWidget would be built — never at
    startup. The answer costs a child process, and a machine that never opens a
    mesh should never pay for it or switch its window to GL composition.
    """
    global _status
    if _status is not None:
        return _status
    _status = _compute_status()
    if not _status[0]:
        # stderr_capture tees this into the log panel and run-qt-stderr.log, so
        # "the 3D preview is a grey box" comes with its reason attached.
        print(f"3D preview disabled: {_status[1]}", file=sys.stderr)
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
