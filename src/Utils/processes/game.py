"""Track and stop a game started from the Play bar.

Wine/Proton launches rarely stay in the first ``Popen`` process: launchers,
runtime containers and wineserver all fan out or hand the executable off.  A
plain process handle therefore is not enough for a Stop button.  Direct
Amethyst launches are stamped with a per-launch environment value and their
whole process tree inherits it.  Store-launcher routes use an identity already
present in their game processes (Steam app id, Faugus id, or Wine prefix).

The implementation deliberately scans ``/proc/*/environ`` instead of matching
process names.  Names such as ``wine64-preloader`` are shared by every running
game; killing by name, as old launcher implementations did, can terminate
unrelated games and tools.  In the Flatpak build the scan and signals are
forwarded to the host with ``flatpak-spawn``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Optional


LAUNCH_ID_ENV = "AMETHYST_LAUNCH_ID"
POLL_SECONDS = 0.75
START_TIMEOUT_SECONDS = 90.0
STOP_GRACE_SECONDS = 2.0
_MISSING_POLLS_TO_FINISH = 2

_local = threading.local()


def _noop(_message: str) -> None:
    pass


def _inside_flatpak() -> bool:
    return (Path("/.flatpak-info").exists()
            and shutil.which("flatpak-spawn") is not None)


def _read_environ(pid_dir: Path) -> set[str] | None:
    try:
        raw = (pid_dir / "environ").read_bytes()
    except (OSError, PermissionError):
        return None
    return {item.decode(errors="surrogateescape")
            for item in raw.split(b"\0") if item}


_PROC_SCAN_SCRIPT = r'''
for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    found=
    while IFS= read -r -d '' entry; do
        for marker do
            if [[ "$entry" == "$marker" ]]; then
                found=1
                break 2
            fi
        done
    done < "$d/environ" 2>/dev/null
    [ -n "$found" ] && printf '%s\n' "$pid"
done
exit 0
'''


_PROC_SIGNAL_SCRIPT = r'''
sig=$1
shift
self=$$
parent=$PPID
for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    [ "$pid" = "$self" ] && continue
    [ "$pid" = "$parent" ] && continue
    found=
    while IFS= read -r -d '' entry; do
        for marker do
            if [[ "$entry" == "$marker" ]]; then
                found=1
                break 2
            fi
        done
    done < "$d/environ" 2>/dev/null
    [ -n "$found" ] && kill -"$sig" "$pid" 2>/dev/null
done
exit 0
'''


def matching_pids(markers: Iterable[str]) -> set[int] | None:
    """Return processes whose environment contains any exact *marker*.

    ``None`` means the scan itself failed; callers must not interpret that as
    the game exiting.  An empty set is a successful scan with no matches.
    """
    wanted = tuple(dict.fromkeys(str(m) for m in markers if m))
    if not wanted:
        return set()
    if _inside_flatpak():
        cmd = ["flatpak-spawn", "--host", "bash", "-c",
               _PROC_SCAN_SCRIPT, "bash", *wanted]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return {int(line) for line in result.stdout.splitlines()
                if line.strip().isdigit()}

    found: set[int] = set()
    try:
        proc_dirs = Path("/proc").iterdir()
    except OSError:
        return None
    wanted_set = set(wanted)
    for proc_dir in proc_dirs:
        if not proc_dir.name.isdigit():
            continue
        environ = _read_environ(proc_dir)
        if environ is not None and not wanted_set.isdisjoint(environ):
            found.add(int(proc_dir.name))
    found.discard(os.getpid())
    return found


def _signal_matching(markers: Iterable[str], sig: signal.Signals) -> bool:
    wanted = tuple(dict.fromkeys(str(m) for m in markers if m))
    if not wanted:
        return True
    if _inside_flatpak():
        cmd = ["flatpak-spawn", "--host", "bash", "-c",
               _PROC_SIGNAL_SCRIPT, "bash", str(int(sig)), *wanted]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    pids = matching_pids(wanted)
    if pids is None:
        return False
    own_pid = os.getpid()
    for pid in pids:
        if pid == own_pid:
            continue
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            continue
    return True


def prefix_markers(prefix: "str | Path | None") -> list[str]:
    """Environment identities commonly used by Heroic/Lutris/Proton."""
    if not prefix:
        return []
    path = Path(prefix)
    candidates = {str(path)}
    if path.name == "pfx":
        candidates.add(str(path.parent))
    else:
        candidates.add(str(path / "pfx"))
    return [f"{key}={value}"
            for value in sorted(candidates)
            for key in ("WINEPREFIX", "STEAM_COMPAT_DATA_PATH")]


class LaunchSession:
    """Lifecycle and scoped termination for one Play/Run click."""

    def __init__(self, label: str, log_fn: Callable[[str], None] | None = None):
        self.label = str(label)
        self.token = uuid.uuid4().hex
        self._log = log_fn or _noop
        self._markers: set[str] = {f"{LAUNCH_ID_ENV}={self.token}"}
        self._roots: dict[int, subprocess.Popen] = {}
        self._launch_reports: list[object] = []
        self._lock = threading.Lock()
        self._state_callback: Callable[[bool], None] | None = None
        self._active = False
        self._finished = False
        self._seen_process = False
        self._monitor_started = False
        self._stop_started = False
        self._deadline = time.monotonic() + START_TIMEOUT_SECONDS

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active and not self._finished

    @property
    def stopping(self) -> bool:
        with self._lock:
            return self._stop_started and not self._finished

    @property
    def markers(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._markers)

    def set_state_callback(self, callback: Callable[[bool], None]) -> None:
        self._state_callback = callback

    def _emit_state(self, active: bool) -> None:
        callback = self._state_callback
        if callback is not None:
            try:
                callback(active)
            except Exception:
                pass

    def _activate(self) -> None:
        emit = False
        with self._lock:
            if self._finished:
                return
            if not self._active:
                self._active = True
                emit = True
        if emit:
            self._emit_state(True)
        self._ensure_monitor()

    def _finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self._active = False
        # Emit even when launch failed before a process appeared. The UI may
        # already hold this session as its pending attempt and must discard it.
        self._emit_state(False)

    def cancel(self) -> None:
        """Forget the session without terminating anything (launch failed)."""
        self._finish()

    def attach_process(self, proc: subprocess.Popen) -> None:
        """Register the direct child; descendants are found by launch token."""
        with self._lock:
            if self._finished:
                return
            self._roots[proc.pid] = proc
            self._seen_process = True
        self._activate()

    def attach_launch_report(self, report) -> None:
        """Associate the early-exit reporter with this stoppable session."""
        if report is None:
            return
        with self._lock:
            if all(existing is not report for existing in self._launch_reports):
                self._launch_reports.append(report)

    def arm_external(self, markers: Iterable[str], route: str) -> None:
        """Watch a launcher-owned process tree identified by environment."""
        new_markers = {str(marker) for marker in markers if marker}
        if not new_markers:
            self._log(f"Stop: {route} did not expose a process identity; "
                      "the button will reset if no game process is found.")
        with self._lock:
            if self._finished:
                return
            self._markers.update(new_markers)
        self._activate()

    def _ensure_monitor(self) -> None:
        with self._lock:
            if self._monitor_started or self._finished:
                return
            self._monitor_started = True
        threading.Thread(
            target=self._monitor, daemon=True,
            name=f"game-monitor-{self.token[:8]}").start()

    def _live_roots(self) -> set[int]:
        live: set[int] = set()
        with self._lock:
            roots = list(self._roots.items())
        for pid, proc in roots:
            try:
                if proc.poll() is None:
                    live.add(pid)
            except Exception:
                pass
        return live

    def _monitor(self) -> None:
        missing_polls = 0
        while True:
            with self._lock:
                if self._finished:
                    return
                markers = tuple(self._markers)
                seen = self._seen_process
                deadline = self._deadline
            matched = matching_pids(markers)
            roots = self._live_roots()
            if matched is None:
                time.sleep(POLL_SECONDS)
                continue
            live = matched | roots
            if live:
                missing_polls = 0
                if not seen:
                    with self._lock:
                        self._seen_process = True
            elif seen:
                missing_polls += 1
                if missing_polls >= _MISSING_POLLS_TO_FINISH:
                    self._finish()
                    return
            elif time.monotonic() >= deadline:
                self._log(f"Stop: no process appeared for {self.label}; "
                          "returning the button to Play.")
                self._finish()
                return
            time.sleep(POLL_SECONDS)

    def stop(self) -> bool:
        """Terminate this session asynchronously. Returns False if inactive."""
        with self._lock:
            if self._finished or not self._active or self._stop_started:
                return False
            self._stop_started = True
            reports = list(self._launch_reports)
        # Set this before the worker sends SIGTERM. A fast child can exit and
        # reach its watcher immediately after the first signal.
        for report in reports:
            try:
                report.mark_cancelled()
            except Exception:
                pass
        threading.Thread(
            target=self._stop_worker, daemon=True,
            name=f"game-stop-{self.token[:8]}").start()
        return True

    def _stop_worker(self) -> None:
        self._log(f"Stop: terminating {self.label} …")
        with self._lock:
            roots = list(self._roots.values())
            markers = tuple(self._markers)

        # Direct children lead their own sessions. This catches children that
        # changed/cleared their environment before the marker scan sees them.
        for proc in roots:
            try:
                if proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        signalled = _signal_matching(markers, signal.SIGTERM)
        time.sleep(STOP_GRACE_SECONDS)

        for proc in roots:
            try:
                if proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        _signal_matching(markers, signal.SIGKILL)
        self._log(f"Stop: {self.label} terminated."
                  if signalled else
                  f"Stop: sent termination request for {self.label}; "
                  "the process scan was not fully available.")
        self._finish()


def current() -> Optional[LaunchSession]:
    return getattr(_local, "session", None)


@contextmanager
def bind(session: LaunchSession | None):
    previous = getattr(_local, "session", None)
    _local.session = session
    try:
        yield session
    finally:
        _local.session = previous


def prepare_spawn(cmd: list, env: "dict | None") -> tuple[list, dict | None]:
    """Stamp a direct launch and forward its token across Flatpak boundaries."""
    session = current()
    if session is None:
        return list(cmd), env
    child_env = dict(os.environ if env is None else env)
    child_env[LAUNCH_ID_ENV] = session.token
    command = list(cmd)
    env_arg = f"--env={LAUNCH_ID_ENV}={session.token}"
    if command[:2] == ["flatpak-spawn", "--host"] and env_arg not in command:
        command.insert(2, env_arg)
    elif command[:2] == ["flatpak", "run"] and env_arg not in command:
        command.insert(2, env_arg)
    return command, child_env


def attach_process(proc: subprocess.Popen) -> None:
    session = current()
    if session is not None:
        from Utils.processes import report as launch_report
        session.attach_launch_report(launch_report.current())
        session.attach_process(proc)


def arm_external(markers: Iterable[str], route: str) -> None:
    session = current()
    if session is not None:
        from Utils.processes import report as launch_report
        session.attach_launch_report(launch_report.current())
        session.arm_external(markers, route)
