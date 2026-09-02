"""Log subprocess lifecycles for Wine/Proton helper applications."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

_CAPTURE_LIMIT = 1024 * 1024
_CAPTURE_RETAIN = 64 * 1024

_SECRET = re.compile(
    r"(?i)(access[_-]?token|auth(?:orization)?|api[_-]?key|password|passwd|"
    r"secret|\btoken\b)"
)
_QUERY_SECRET = re.compile(
    r"(?i)((?:access[_-]?token|token|api[_-]?key|password|secret)=)[^&;|\s]+"
)
_LINE_SECRET = re.compile(
    r"(?i)\b(access[_-]?token|token|authorization|api[_-]?key|password|passwd|secret)"
    r"(\s*[:=]\s*)(?:bearer\s+)?[^;|\s]+"
)
_URL_USERINFO = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@"
)
_KEY_ASSIGNMENT = re.compile(r"^(?:--?)?[A-Za-z_][A-Za-z0-9_.-]*$")
_BEARER_SECRET = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def redact_text(text: str) -> str:
    text = _ANSI_ESCAPE.sub("", str(text)).replace("\0", r"\0")
    text = _URL_USERINFO.sub(r"\1<redacted>@", text)
    text = _QUERY_SECRET.sub(r"\1<redacted>", text)
    text = _LINE_SECRET.sub(r"\1\2<redacted>", text)
    return _BEARER_SECRET.sub("Bearer <redacted>", text)


def _log(log_fn: LogFn, message: str) -> None:
    try:
        log_fn(message)
    except Exception:
        pass


def _one_line(value, limit: int = 200) -> str:
    text = str(value).replace("\r", r"\r").replace("\n", r"\n")
    return text if len(text) <= limit else text[:limit - 3] + "..."


def format_command(cmd: list) -> str:
    """Shell-readable command with likely credentials redacted."""
    rendered: list[str] = []
    redact_next = False
    for raw in cmd:
        token = _one_line(raw, 1600)
        if redact_next:
            rendered.append("<redacted>")
            redact_next = False
            continue
        key, sep, _value = token.partition("=")
        if sep and _KEY_ASSIGNMENT.match(key) and _SECRET.search(key):
            rendered.append(f"{key}=<redacted>")
            continue
        if token.startswith("-") and _SECRET.search(token):
            rendered.append(token)
            redact_next = "=" not in token
            continue
        rendered.append(redact_text(token))
    text = shlex.join(rendered)
    return text if len(text) <= 1600 else text[:1597] + "..."


def _capture_file():
    try:
        cache_base = Path(
            os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        capture_dir = cache_base / "AmethystModManager"
        capture_dir.mkdir(parents=True, exist_ok=True)
        capture = tempfile.TemporaryFile(
            prefix="amm-helper-output-", dir=capture_dir)
        return capture, ""
    except Exception as cache_exc:
        try:
            return (tempfile.TemporaryFile(),
                    "cache output capture unavailable "
                    f"({type(cache_exc).__name__}: {cache_exc}); using system temp")
        except Exception as temp_exc:
            return (None, "output capture unavailable "
                    f"(cache: {type(cache_exc).__name__}: {cache_exc}; "
                    f"temp: {type(temp_exc).__name__}: {temp_exc})")


def _start(cmd: list, *, env, cwd, label: str, log_fn: LogFn):
    capture, capture_note = _capture_file()
    _log(log_fn, f"{label}: command: {format_command(cmd)}")
    if capture_note:
        _log(log_fn, f"{label}: {_one_line(capture_note, 800)}.")
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=capture if capture is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if capture is not None else subprocess.DEVNULL,
        )
    except Exception as exc:
        if capture is not None:
            capture.close()
        detail = _one_line(redact_text(str(exc)), 800)
        _log(log_fn, f"{label} error: could not start: {type(exc).__name__}: {detail}")
        raise
    _log(log_fn, f"{label}: started (pid {proc.pid}).")
    return proc, capture, time.monotonic()


def _trim_capture(capture) -> None:
    if capture is None:
        return
    try:
        capture.seek(0, os.SEEK_END)
        size = capture.tell()
        if size <= _CAPTURE_LIMIT:
            return
        capture.seek(max(0, size - _CAPTURE_RETAIN))
        tail = capture.read()
        capture.seek(0)
        capture.write(b"[... earlier helper output truncated ...]\n" + tail)
        capture.truncate()
        capture.flush()
    except Exception:
        pass


def _finish(proc, capture, started: float, label: str, log_fn: LogFn) -> int:
    while True:
        try:
            rc = proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            _trim_capture(capture)
    tail = ""
    if capture is not None:
        try:
            capture.seek(0, os.SEEK_END)
            size = capture.tell()
            capture.seek(max(0, size - 8192))
            tail = capture.read().decode("utf-8", "replace").strip()
        except Exception:
            tail = ""
        finally:
            try:
                capture.close()
            except Exception:
                pass
    elapsed = time.monotonic() - started
    if rc == 0:
        _log(log_fn, f"{label}: exited cleanly after {elapsed:.1f}s (rc=0).")
    else:
        _log(log_fn, f"{label} error: exited after {elapsed:.1f}s with code {rc}.")
    for line in tail.splitlines()[-12:]:
        if line.strip():
            detail = redact_text(line.strip())
            if len(detail) > 1000:
                detail = "..." + detail[-997:]
            _log(log_fn, f"{label}:   {detail}")
    return rc


def spawn_process_logged(cmd: list, *, env=None, cwd=None, label: str,
                         log_fn: LogFn) -> bool:
    """Start a process and report its eventual exit on a daemon thread."""
    label = _one_line(label)
    try:
        proc, capture, started = _start(
            cmd, env=env, cwd=cwd, label=label, log_fn=log_fn)
    except Exception:
        return False
    watcher = threading.Thread(
        target=_finish, args=(proc, capture, started, label, log_fn),
        daemon=True, name=f"watch-{label.lower().replace(' ', '-')}",
    )
    try:
        watcher.start()
    except RuntimeError as exc:
        _log(log_fn, f"{label}: lifecycle watcher unavailable: {_one_line(exc)}.")
    return True


def run_process_logged(cmd: list, *, env=None, cwd=None, label: str,
                       log_fn: LogFn, started_fn=None) -> int:
    """Run a process synchronously with the same lifecycle and tail logging."""
    label = _one_line(label)
    proc, capture, started = _start(
        cmd, env=env, cwd=cwd, label=label, log_fn=log_fn)
    if started_fn is not None:
        try:
            started_fn()
        except Exception:
            pass
    return _finish(proc, capture, started, label, log_fn)
