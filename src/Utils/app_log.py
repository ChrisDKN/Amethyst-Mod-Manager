"""
app_log.py
Global app log — forwards messages to the GUI log panel when set.

The main app calls set_app_log(log_fn, after_fn) after building the status bar.
Nexus/Utils code calls app_log(msg) so messages appear in the application log panel.

Thread safety: when app_log is called from a background thread, messages are put
on a queue and drained on the main thread via a periodic after() callback. When
called from the main thread, the message is logged immediately.
"""

from __future__ import annotations

import queue
import threading

_log_fn: callable | None = None
_after_fn: callable | None = None
_main_thread_id: int | None = None
_log_queue: queue.Queue[str] = queue.Queue()


def _drain_log_queue() -> None:
    """Run on main thread: drain queued messages and log them. Reschedule to run again."""
    if _log_fn is None:
        return
    try:
        while True:
            msg = _log_queue.get_nowait()
            try:
                _log_fn(msg)
            except Exception:
                pass
    except queue.Empty:
        pass
    if _after_fn is not None:
        _after_fn(50, _drain_log_queue)


def set_app_log(log_fn: callable[[str], None], after_fn: callable) -> None:
    """Register the GUI log function and a main-thread runner (e.g. app.after(0, cb))."""
    global _log_fn, _after_fn, _main_thread_id
    _log_fn = log_fn
    _after_fn = after_fn
    _main_thread_id = threading.current_thread().ident
    after_fn(0, _drain_log_queue)


def app_log(message: str) -> None:
    """Write a message to the application log panel (thread-safe). No-op if not set."""
    if _log_fn is None:
        return
    try:
        if threading.current_thread().ident == _main_thread_id:
            _log_fn(message)
        else:
            _log_queue.put_nowait(message)
    except Exception:
        pass
