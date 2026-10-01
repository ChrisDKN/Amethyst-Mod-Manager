"""Mirror ``sys.stderr`` and uncaught exceptions into the application log.

Everything written to ``sys.stderr`` - plus any uncaught exception on
the main thread or a worker thread - into the application log panel via
``Utils.app_log.app_log``.

Rationale: Python tracebacks, ``warnings``, and stderr writes from third-party
libraries never reach the GUI log otherwise. Users who hit an error can then
see it (and copy/share it) straight from the log panel instead of having to
launch from a terminal.

Design:
 - ``_Tee`` wraps the real stderr: it forwards writes to the terminal unchanged
   AND buffers them into complete lines that are handed to ``app_log``. The tee
   never raises into its caller - a logging failure must not break the write it
   was mirroring.
 - ``sys.excepthook`` / ``threading.excepthook`` format uncaught tracebacks and
   send them to ``app_log`` as well. The default hooks already write to stderr
   (which the tee would catch), but explicit hooks tag them clearly and keep
   working even if stderr is later re-wrapped.

``install()`` is idempotent and best-effort; a failure here must never stop the
app from starting.
"""

from __future__ import annotations

import sys
import threading
import traceback

_installed = False
_fault_installed = False
_fault_file = None  # kept open for the process lifetime - faulthandler needs it
_file_installed = False
_stderr_file = None  # retained pipe to the redacting reader


def install_stderr_file(log_dir=None) -> bool:
    """Capture native and Python stderr through an independent redacting reader."""
    global _file_installed, _stderr_file
    if _file_installed:
        return True

    import os
    from pathlib import Path
    from Utils.diagnostics.privacy import open_redacted_log

    if os.environ.get("AMM_STDERR_TEED"):
        _file_installed = True
        return True

    try:
        if log_dir is None:
            from Utils.config_paths import get_config_dir
            log_dir = get_config_dir()
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "run-qt-stderr.log"
        try:
            if path.exists():
                os.replace(path, str(path) + ".old")
        except OSError:
            pass
        stream = open_redacted_log(path, echo=True)
        try:
            os.dup2(stream.fileno(), 2)
        except OSError:
            stream.close()
            raise
        _stderr_file = stream
    except Exception:
        return False

    _file_installed = True
    return True


class _Tee:
    """A stderr proxy that forwards to the real stream and to a line sink."""

    def __init__(self, real, sink):
        self._real = real
        self._sink = sink
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, data):
        # Forward to the terminal first - never let logging break real stderr.
        try:
            if self._real is not None:
                self._real.write(data)
        except Exception:
            pass
        try:
            self._emit_lines(data)
        except Exception:
            pass
        # Match the io contract: return the number of characters written.
        try:
            return len(data)
        except Exception:
            return 0

    def _emit_lines(self, data):
        if not isinstance(data, str):
            try:
                data = data.decode("utf-8", "replace")
            except Exception:
                data = str(data)
        with self._lock:
            self._buf += data
            lines = self._buf.split("\n")
            # Keep the last (possibly incomplete) fragment buffered.
            self._buf = lines.pop()
            complete = lines
        for line in complete:
            if line.strip():
                self._sink(line.rstrip("\r"))

    def flush(self):
        try:
            if self._real is not None:
                self._real.flush()
        except Exception:
            pass
        # Emit any buffered partial line so nothing is lost on flush/exit.
        try:
            with self._lock:
                pending = self._buf
                self._buf = ""
            if pending.strip():
                self._sink(pending.rstrip("\r"))
        except Exception:
            pass

    # Delegate everything else (isatty, fileno, encoding, …) to the real stream
    # so callers that introspect stderr keep working.
    def __getattr__(self, name):
        return getattr(self._real, name)


def install() -> bool:
    """Route stderr + uncaught tracebacks to the app log. Idempotent.

    Returns True if the hooks were installed (or already were), False on error.
    """
    global _installed
    if _installed:
        return True

    try:
        from Utils.app_log import app_log
    except Exception:
        return False

    def _sink(line: str) -> None:
        # app_log is thread-safe (queues from worker threads) and a no-op until
        # the GUI wires set_app_log, so this is safe to call from anywhere.
        try:
            app_log(line)
        except Exception:
            pass

    try:
        sys.stderr = _Tee(sys.stderr, _sink)
    except Exception:
        pass

    # Uncaught exceptions on the main thread.
    _prev_excepthook = sys.excepthook

    def _excepthook(exc_type, exc, tb):
        try:
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            for line in text.rstrip("\n").split("\n"):
                _sink(line)
        except Exception:
            pass
        # Preserve default behaviour (prints to real stderr, sets exit status).
        try:
            _prev_excepthook(exc_type, exc, tb)
        except Exception:
            pass

    try:
        sys.excepthook = _excepthook
    except Exception:
        pass

    # Uncaught exceptions on worker threads (Python 3.8+).
    _prev_threadhook = getattr(threading, "excepthook", None)

    def _threadhook(args):
        try:
            if args.exc_type is SystemExit:
                return
            text = "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback))
            name = getattr(args.thread, "name", "?")
            _sink(f"[thread:{name}] uncaught exception:")
            for line in text.rstrip("\n").split("\n"):
                _sink(line)
        except Exception:
            pass
        if _prev_threadhook is not None:
            try:
                _prev_threadhook(args)
            except Exception:
                pass

    try:
        if hasattr(threading, "excepthook"):
            threading.excepthook = _threadhook
    except Exception:
        pass

    _installed = True
    return True


def install_faulthandler(log_dir=None) -> bool:
    """Enable ``faulthandler`` so native crashes (segfaults, aborts) leave a
    C-level traceback on disk. Idempotent, best-effort.

    A segfault from Qt/other C code cannot be caught in Python - by the time the
    interpreter would run our excepthook the process is already gone. faulthandler
    installs an OS signal handler that dumps every thread's Python stack straight
    to a pipe at crash time. An independent reader redacts paths before writing
    the dump to disk, even after the application has crashed.

    The dump is written to ``<log_dir>/amethyst-fault-<pid>.log`` when *log_dir*
    is given (defaults to ``Utils.config_paths.get_logs_dir()``), so a crashed
    user can find and share it. If no writable file can be opened we fall back to
    dumping to the real stderr (still better than a silent crash).

    Returns True if faulthandler was enabled.
    """
    global _fault_installed, _fault_file
    if _fault_installed:
        return True

    import faulthandler

    stream = None
    try:
        if log_dir is None:
            from Utils.config_paths import get_logs_dir
            log_dir = get_logs_dir()
        import os
        from pathlib import Path
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"amethyst-fault-{os.getpid()}.log"
        from Utils.diagnostics.privacy import open_redacted_log
        stream = open_redacted_log(path)
        _fault_file = stream  # keep a reference so the fd stays open
    except Exception:
        stream = None

    try:
        # all_threads=True dumps every thread's stack - the crashing one may not
        # be the main thread. file defaults to sys.stderr when stream is None.
        if stream is not None:
            faulthandler.enable(file=stream, all_threads=True)
        else:
            faulthandler.enable(all_threads=True)
    except Exception:
        return False

    _fault_installed = True
    return True
