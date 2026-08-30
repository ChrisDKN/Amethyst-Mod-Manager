"""
app_log.py
Global app log - forwards messages to the GUI log panel when set.

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
_log_queue: queue.Queue[str] = queue.Queue(maxsize=512)
_drop_lock = threading.Lock()
_dropped_messages = 0


def _enqueue(message: str) -> None:
    global _dropped_messages
    try:
        _log_queue.put_nowait(message)
        return
    except queue.Full:
        pass
    try:
        _log_queue.get_nowait()
        _log_queue.put_nowait(message)
    except (queue.Empty, queue.Full):
        pass
    with _drop_lock:
        _dropped_messages += 1


def _drain_log_queue() -> None:
    """Run on main thread: drain queued messages and log them. Reschedule to run again."""
    global _dropped_messages
    if _log_fn is None:
        return
    with _drop_lock:
        dropped, _dropped_messages = _dropped_messages, 0
    if dropped:
        try:
            _log_fn(f"WARNING: {dropped} queued log message(s) were dropped.")
        except Exception:
            pass
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


_NOOP_LOG: callable = lambda _: None


def safe_log(log_fn: callable | None) -> callable:
    """Return *log_fn* if truthy, otherwise a no-op callable.

    Replaces the common pattern ``_log = log_fn or (lambda _: None)``
    with a single reusable sentinel to avoid creating a new lambda each time.
    """
    return log_fn if log_fn is not None else _NOOP_LOG


def safe_print(*args, **kwargs) -> None:
    """``print`` that never crashes the caller.

    Under Flatpak / AppImage the process is frequently launched with no reader
    on stdout. Once the OS pipe buffer fills, the next ``print(..., flush=True)``
    raises ``BrokenPipeError``. Since many of these prints run on worker threads,
    that exception propagated out of the thread target and killed the worker
    (e.g. the play/deploy conflict build), aborting the operation. These prints
    are diagnostics only, so swallowing any stream error here is safe.

    Import as ``from Utils.app_log import safe_print as print`` at module top to
    make every ``print(...)`` in that module crash-proof.
    """
    import builtins
    try:
        builtins.print(*args, **kwargs)
    except (BrokenPipeError, OSError, ValueError):
        pass


def app_log(message: str) -> None:
    """Write a message to the application log panel (thread-safe).

    Messages produced before the GUI sink exists are retained in a bounded
    queue and replayed when :func:`set_app_log` wires the panel.
    """
    if _log_fn is None:
        _enqueue(str(message))
        return
    try:
        if threading.current_thread().ident == _main_thread_id:
            _log_fn(message)
        else:
            _enqueue(str(message))
    except Exception:
        pass
