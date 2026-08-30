"""Decides whether Qt's OpenGL-widget path is safe to use on this machine.

A single QOpenGLWidget anywhere in a top-level window switches that window's
whole backing store to GL composition. When the GL path is broken, the result
is not a failed widget - it is a *black window*, because every non-native
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
  the import fails cleanly, and nothing breaks - which is why this only ever
  reproduced on Arch derivatives.
* **No usable GL driver.** Bare VMs, remote sessions, a container without
  ``/dev/dri`` and without llvmpipe.

The probe itself runs in a **child process**, and that is not optional: when
GLX cannot supply an FBConfig for the requested format, Qt does not return an
error - ``QOpenGLContext::create()`` reaches ``qFatal("Could not initialize
GLX")`` and aborts the process from C, where no Python ``try`` can intercept
it. Probing in-process therefore killed the very app the check exists to
protect (it aborted the AppImage smoke test under Xvfb on the first run after
the check was added). A child that aborts costs us a wait and a return code.

``AMM_DISABLE_GL=1`` forces the answer to "no" as a user-facing escape hatch;
``AMM_FORCE_GL=1`` skips the probe and trusts the machine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

# (ok, reason) - computed once, after QApplication exists. The lock matters: the
# startup warmup asks from a worker thread while the user may be opening a mesh
# on the GUI thread, and two probes would mean two child processes.
_status: tuple[bool, str] | None = None
_details = ""
_lock = threading.Lock()

# QGuiApplication.platformName() as read on the GUI thread by prime_platform().
# The probe needs it to point the child at the same window system, and must not
# reach into Qt from a worker thread to get it.
_platform: str | None = None

# Qt libraries that must come from our own bundle when running as an AppImage:
# these are the ones the GL widget path pulls in on top of QtWidgets/QtGui.
_GL_LIBS = ("libQt6OpenGLWidgets.so", "libQt6OpenGL.so")


def prime_platform() -> None:
    """Record the platform plugin name. Call once from the GUI thread before any
    off-thread ``gl_status()``; a no-op if Qt isn't up yet."""
    global _platform
    if _platform is not None:
        return
    try:
        from PySide6.QtGui import QGuiApplication
        _platform = QGuiApplication.platformName() or ""
    except Exception:                                     # noqa: BLE001
        _platform = ""


def _platform_name() -> str:
    """The primed platform name, falling back to asking Qt directly."""
    if _platform is not None:
        return _platform
    prime_platform()
    return _platform or ""


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_disabled() -> bool:
    return _truthy("AMM_DISABLE_GL")


def _env_forced() -> bool:
    """AMM_FORCE_GL=1 - trust the machine over our probe.

    The checks below are conservative: they can refuse a GL stack that would
    in fact have worked (an odd offscreen-surface config, say). That costs a
    user their 3D preview, so leave them a way to overrule it.
    """
    return _truthy("AMM_FORCE_GL")


def _own_appdir() -> str:
    """Our AppImage's mount root, or "" when we are not running from one.

    APPDIR leaks into the environment of anything launched from another
    AppImage (an AppImage code editor's terminal, say), so its mere presence
    proves nothing - only trust it when this very file lives inside it.
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
# the viewport will ask for (3.3 core - the shaders are GLSL 330) and reports
# through the exit code, because anything it prints may be drowned out by Qt's
# own warnings. A qFatal in here takes the child down and nothing else.
_PROBE_SRC = r'''
import json, os, sys
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
functions = ctx.functions()
def gl_string(token):
    value = functions.glGetString(token)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return str(value or "")
actual = ctx.format()
print("__AMM_GL_INFO__" + json.dumps({
    "renderer": gl_string(0x1F01),
    "vendor": gl_string(0x1F00),
    "version": gl_string(0x1F02),
    "glsl": gl_string(0x8B8C),
    "surface": f"{actual.majorVersion()}.{actual.minorVersion()}",
    "gles": ctx.isOpenGLES(),
}), flush=True)
ctx.doneCurrent()
raise SystemExit(0)
'''

_PROBE_CODES = {
    2: "OpenGL context creation failed",
    3: "offscreen GL surface unavailable",
    4: "OpenGL context could not be made current",
    5: "the OpenGL probe could not load PySide6",
}


def _probe_output(raw, limit: int = 600) -> str:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = str(raw or "")
    detail = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    return detail if len(detail) <= limit else "..." + detail[-(limit - 3):]


def _probe_context() -> tuple[bool, str]:
    """Create a throwaway offscreen GL context - in a child process - to see if
    the driver works.

    Anything other than a clean exit 0 means "no": a non-zero code from the
    checks above, a signal (Qt's qFatal aborts), a timeout, or a child that
    would not start at all. Assuming "yes" when we cannot tell would put the
    abort back in this process, so an unusable probe disables the GL path and
    ``AMM_FORCE_GL=1`` remains the way to overrule it.
    """
    global _details
    _details = ""
    if not sys.executable:
        return False, "no interpreter available to run the OpenGL probe"
    env = dict(os.environ)
    # The child must talk to the same window system we do, and must be able to
    # import PySide6 from wherever this process found it (the AppImage's bundled
    # site-packages, a venv, /app in the flatpak). Passed as our own variable
    # rather than PYTHONPATH so it cannot leak into anything else we launch.
    env["_AMM_GL_PROBE_SYSPATH"] = os.pathsep.join(p for p in sys.path if p)
    platform = _platform_name()
    if platform:
        env["QT_QPA_PLATFORM"] = platform
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_SRC], env=env, timeout=30,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired as exc:
        detail = _probe_output(exc.stderr)
        reason = "the OpenGL probe timed out"
        return False, f"{reason}: {detail}" if detail else reason
    except Exception as exc:                              # noqa: BLE001
        return False, f"the OpenGL probe could not run ({exc})"
    if proc.returncode == 0:
        output = (proc.stdout or b"").decode("utf-8", "replace")
        for line in output.splitlines():
            if not line.startswith("__AMM_GL_INFO__"):
                continue
            try:
                info = json.loads(line.removeprefix("__AMM_GL_INFO__"))
                renderer = " ".join(
                    " ".join(str(part).split())
                    for part in (info.get("renderer"), info.get("vendor"))
                    if part)
                version = " ".join(str(info.get("version") or "unknown").split())
                glsl = " ".join(str(info.get("glsl") or "unknown").split())
                api = "OpenGL ES" if info.get("gles") else "OpenGL"
                _details = (f"{renderer or 'unknown renderer'}; {api} {version}; "
                            f"GLSL {glsl}; surface {info.get('surface') or '?'}")
            except Exception:
                _details = "probe succeeded; renderer details could not be parsed"
            break
        if not _details:
            _details = "probe succeeded; no renderer details were returned"
        return True, ""
    if proc.returncode < 0:
        # Killed by a signal - qFatal("Could not initialize GLX") and friends.
        detail = _probe_output(proc.stderr)
        why = f"the OpenGL driver aborted the probe (signal {-proc.returncode})"
        return False, f"{why}: {detail}" if detail else why
    reason = _PROBE_CODES.get(
        proc.returncode, f"the OpenGL probe failed (exit {proc.returncode})")
    detail = _probe_output(proc.stderr)
    return False, f"{reason}: {detail}" if detail else reason


def gl_status() -> tuple[bool, str]:
    """(usable, reason-if-not) for the OpenGL widget path. Cached.

    Blocks for as long as the child probe takes, so the GUI thread should only
    ask when it is already committed to building a viewport (nif_preview does).
    Everything speculative - the startup warmup - asks from a worker thread
    after calling prime_platform(), and takes whatever is cached afterwards.
    """
    global _status
    if _status is not None:
        return _status
    with _lock:
        if _status is not None:
            return _status
        status = _compute_status()
        _status = status
    detail = gl_details()
    try:
        if status[0]:
            print("OpenGL status: available" + (f" ({detail})" if detail else ""),
                  file=sys.stderr)
        else:
            print(f"OpenGL status: unavailable ({status[1]})", file=sys.stderr)
    except (OSError, ValueError):
        pass
    return status


def gl_details() -> str:
    return _details


# Platform plugins with no window system behind them: a GL context can still
# be created (EGL talks to the driver directly), but QOpenGLWidget cannot.
_NO_GL_PLATFORMS = {"offscreen", "minimal", "minimalegl", "vnc"}


def _compute_status() -> tuple[bool, str]:
    global _details
    if _env_disabled():
        _details = "disabled by AMM_DISABLE_GL"
        return False, "disabled by AMM_DISABLE_GL"
    if _env_forced():
        _details = "forced by AMM_FORCE_GL; driver was not probed"
        return True, ""
    platform = _platform_name().lower()
    if platform in _NO_GL_PLATFORMS:
        return False, f"the {platform} platform plugin has no OpenGL support"
    try:
        import PySide6.QtOpenGLWidgets  # noqa: F401
    except Exception as exc:                              # noqa: BLE001
        return False, f"QtOpenGLWidgets unavailable ({exc})"
    foreign = _foreign_gl_libs()
    if foreign:
        return False, ("Qt OpenGL libraries loaded from outside the AppImage "
                       f"({', '.join(foreign)}) - mixing them with the bundled "
                       "Qt renders the window black")
    try:
        return _probe_context()
    except Exception as exc:                              # noqa: BLE001
        return False, f"OpenGL probe failed ({exc})"


def gl_available() -> bool:
    """True when a QOpenGLWidget can safely be created."""
    return gl_status()[0]
