"""Hold a system sleep/idle inhibitor while a long tool run is in progress.

DynDOLOD, PGPatcher, Synthesis and BodySlide batch builds routinely run for
tens of minutes with no input, which is exactly what a Steam Deck's autosuspend
timer waits for - the tool is then frozen mid-build (and Wine often does not
survive the resume). Every blocking wizard-tool launch takes a lock for its
duration so the machine stays awake until the tool exits.

Two backends, and we take *both* when both are available rather than stopping
at the first that works - they cover different things and neither is a superset:

  * ``systemd-inhibit --what=sleep:idle --mode=block`` holding a ``cat`` whose
    stdin is our pipe. This is the one that takes a real logind sleep lock, so
    it is what actually stops autosuspend. Closing the pipe (or dying) gives
    ``cat`` EOF, so the lock is crash-safe.
  * ``org.freedesktop.portal.Inhibit`` over D-Bus (jeepney), for the session's
    idle/screensaver timers and for the Flatpak case. Measured on SteamOS/KDE:
    this call succeeds but registers *no* logind lock, so it must not be
    treated as sufficient on its own. Scoped to the D-Bus connection, so
    closing the connection releases it.

Locks are reference-counted: concurrent wizard runs share one system lock and
it drops when the last one finishes. Set ``AMM_NO_SLEEP_INHIBIT=1`` to disable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from typing import Callable

# org.freedesktop.portal.Inhibit flags: 1 logout, 2 user switch, 4 suspend,
# 8 idle. We want both the suspend and the idle-dim/lock timers held off.
_INHIBIT_SUSPEND = 4
_INHIBIT_IDLE = 8

_PORTAL_BUS = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_INHIBIT_IFACE = "org.freedesktop.portal.Inhibit"

LogFn = Callable[[str], None]

_lock = threading.Lock()
_refcount = 0
_holders: "list[_Holder]" = []


def _noop(_msg: str) -> None:
    pass


def _bounded_detail(value, limit: int = 400) -> str:
    detail = repr(value)
    return detail if len(detail) <= limit else detail[:limit] + "..."


def _safe_log(log_fn: LogFn) -> LogFn:
    def emit(message: str) -> None:
        try:
            log_fn(message)
        except Exception:
            pass
    return emit


def disabled() -> bool:
    """True when the kill switch is set."""
    return os.environ.get("AMM_NO_SLEEP_INHIBIT", "") not in ("", "0", "false", "False")


class _Holder:
    """Something keeping the system awake, with a way to let go."""

    def __init__(self, kind: str, release: Callable[[], None]):
        self.kind = kind
        self._release = release

    def release(self) -> None:
        try:
            self._release()
        except Exception:
            pass


def _portal_hold(reason: str, failures: list[str]) -> "_Holder | None":
    """Take a portal Inhibit lock, or None when the portal can't provide one.

    The inhibition lives as long as the D-Bus connection, so the connection is
    what the holder owns: closing it is the release.
    """
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except ImportError as exc:
        failures.append(f"portal: jeepney unavailable ({exc})")
        return None

    try:
        conn = open_dbus_connection("SESSION")
    except Exception as exc:
        failures.append(f"portal: session D-Bus unavailable ({exc})")
        return None

    try:
        # No Inhibit backend (bare X session, headless CI) answers the version
        # property with an error - fall through to systemd-inhibit rather than
        # blocking on a Request that will never be answered.
        props = DBusAddress(_PORTAL_PATH, bus_name=_PORTAL_BUS,
                            interface="org.freedesktop.DBus.Properties")
        ver = conn.send_and_get_reply(
            new_method_call(props, "Get", "ss", (_INHIBIT_IFACE, "version")))
        if ver.header.message_type.name == "error":
            failures.append(
                "portal: Inhibit interface unavailable "
                f"({_bounded_detail(ver.body)})")
            conn.close()
            return None

        addr = DBusAddress(_PORTAL_PATH, bus_name=_PORTAL_BUS,
                           interface=_INHIBIT_IFACE)
        # The reply carries a Request handle we never use - we release by
        # dropping the connection, not by closing the Request - but it is worth
        # the round-trip to learn the call was refused, so we can still fall
        # back to systemd-inhibit instead of silently holding nothing.
        reply = conn.send_and_get_reply(new_method_call(
            addr, "Inhibit", "sua{sv}",
            ("", _INHIBIT_SUSPEND | _INHIBIT_IDLE,
             {"reason": ("s", reason)}),
        ))
        if reply.header.message_type.name == "error":
            failures.append(
                f"portal: Inhibit request refused ({_bounded_detail(reply.body)})")
            conn.close()
            return None
    except Exception as exc:
        failures.append(f"portal: request failed ({type(exc).__name__}: {exc})")
        try:
            conn.close()
        except Exception:
            pass
        return None

    return _Holder("portal", conn.close)


def _systemd_hold(reason: str, failures: list[str]) -> "_Holder | None":
    """Take a systemd-inhibit lock held open by a ``cat`` reading our pipe."""
    cmd = [
        "systemd-inhibit", "--what=sleep:idle", "--mode=block",
        "--who=Amethyst", f"--why={reason}", "cat",
    ]
    if os.path.exists("/.flatpak-info"):
        # systemd-inhibit isn't in the flatpak runtime; the host has it.
        if not shutil.which("flatpak-spawn"):
            failures.append("systemd-inhibit: flatpak-spawn is unavailable")
            return None
        cmd = ["flatpak-spawn", "--host", *cmd]
    elif not shutil.which("systemd-inhibit"):
        failures.append("systemd-inhibit: command not found")
        return None

    try:
        stderr_capture = tempfile.TemporaryFile()
    except OSError:
        stderr_capture = None
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr_capture if stderr_capture is not None else subprocess.DEVNULL,
        )
    except OSError as exc:
        if stderr_capture is not None:
            stderr_capture.close()
        failures.append(f"systemd-inhibit: could not start ({exc})")
        return None

    try:
        rc = proc.wait(timeout=0.1)
    except subprocess.TimeoutExpired:
        rc = None
    if rc is not None:
        detail = ""
        if stderr_capture is not None:
            try:
                stderr_capture.seek(0)
                detail = stderr_capture.read().decode("utf-8", "replace").strip()
            except Exception:
                pass
        failures.append(f"systemd-inhibit: exited during setup (rc={rc})"
                        + (f": {detail[-600:]}" if detail else ""))
        if stderr_capture is not None:
            stderr_capture.close()
        return None

    def _release() -> None:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if stderr_capture is not None:
            stderr_capture.close()

    return _Holder("systemd-inhibit", _release)


def _acquire(reason: str, log_fn: LogFn) -> tuple[bool, list[str]]:
    """Take (or join) the system locks and return backend failure details."""
    global _refcount
    with _lock:
        if _holders:
            _refcount += 1
            return True, []
        # Both, not first-wins: only systemd-inhibit takes a logind sleep lock,
        # and it is the one missing inside the Flatpak runtime.
        failures: list[str] = []
        taken = [h for h in (
            _systemd_hold(reason, failures), _portal_hold(reason, failures))
                 if h is not None]
        if not taken:
            return False, failures
        _holders.extend(taken)
        _refcount = 1
    log_fn(f"sleep inhibited via {', '.join(h.kind for h in taken)} - {reason}")
    if failures:
        prefix = "WARNING: " if not any(
            holder.kind == "systemd-inhibit" for holder in taken) else ""
        log_fn(prefix + "sleep inhibit fallback diagnostics: "
               + "; ".join(failures))
    return True, failures


def _release(log_fn: LogFn) -> None:
    """Drop this caller's claim; releases the system locks when it was the last."""
    global _refcount
    with _lock:
        if not _holders:
            return
        _refcount -= 1
        if _refcount > 0:
            return
        taken, _holders[:] = list(_holders), []
        _refcount = 0
    for holder in taken:
        holder.release()
    log_fn("sleep inhibit released")


@contextmanager
def inhibit_sleep(reason: str, log_fn: "LogFn | None" = None):
    """Keep the system awake for the duration of the block.

    Never raises and never blocks the work: if no backend is available the
    body just runs unprotected (one log line says so).
    """
    log = _safe_log(log_fn or _noop)
    if disabled():
        log("sleep inhibition disabled by AMM_NO_SLEEP_INHIBIT.")
        yield False
        return
    try:
        held, failures = _acquire(reason, log)
    except Exception as exc:
        held, failures = False, [f"unexpected setup error: {type(exc).__name__}: {exc}"]
    if not held:
        detail = "; ".join(failures) or "no backend reported a reason"
        log("could not inhibit sleep - the system may suspend during this run. "
            f"Backend diagnostics: {detail}")
        yield False
        return
    try:
        yield True
    finally:
        try:
            _release(log)
        except Exception:
            pass
