"""Stop an exception in a Qt callback from poisoning the whole interpreter.

PySide6 can only re-raise an exception from a Python override when the call
came from Python. When Qt itself makes the call - an event filter run from
inside QWidget::setParent, a layout virtual run from a layout pass - there is
no Python frame to raise into, so shiboken leaves CPython's error indicator SET
and returns. Nothing is printed. The error then surfaces at the next unrelated
PySide6 constructor as

    SystemError: <class 'PySide6.QtWidgets.QToolButton'> returned NULL
                 without setting an exception

pointing at innocent code, on a line that moves between runs. Wrapping the
override so no exception can escape it keeps the real failure visible (logged,
with its traceback) and local.
"""

from __future__ import annotations

import functools
import sys
import threading
import traceback
import types

from PySide6.QtCore import QObject

_lock = threading.Lock()
_seen: set[type] = set()
_installed = False
_reported: dict[str, int] = {}
_REPORT_LIMIT = 3       # a filter on a hot object can fire hundreds of times/s


def _report(where: str) -> None:
    """Log the escaped exception with its traceback; never raise doing it."""
    with _lock:
        n = _reported[where] = _reported.get(where, 0) + 1
    if n > _REPORT_LIMIT:
        return
    tb = traceback.format_exc()
    if n == _REPORT_LIMIT:
        tb += "[further reports from this site suppressed]\n"
    try:
        from Utils.app_log import app_log
        app_log(f"[qt-guard] exception escaped {where} (suppressed):\n{tb}")
    except Exception:
        pass
    try:
        sys.stderr.write(f"[qt-guard] exception escaped {where}:\n{tb}")
    except Exception:
        pass


def guard_virtuals(cls: type, **fallbacks) -> None:
    """Wrap *cls*'s Qt virtuals as name=fallback (a value, or a callable
    returning one) - the fallback is what Qt gets if the override raises.
    It must match the virtual's C++ return type: handing shiboken a None it
    can't convert raises inside the virtual again, which is the very thing
    being guarded against."""
    for name, fallback in fallbacks.items():
        fn = cls.__dict__.get(name)
        if not isinstance(fn, types.FunctionType) or getattr(fn, "_guarded", False):
            continue
        where = f"{cls.__module__}.{cls.__qualname__}.{name}"

        @functools.wraps(fn)
        def wrapper(*a, _fn=fn, _where=where, _fb=fallback, **kw):
            try:
                return _fn(*a, **kw)
            except Exception:
                _report(_where)
                return _fb() if callable(_fb) else _fb
        wrapper._guarded = True
        setattr(cls, name, wrapper)


def _guard_event_filter(cls: type) -> None:
    """Wrap the most-derived Python eventFilter in *cls*'s MRO."""
    for klass in cls.__mro__:
        if "eventFilter" not in klass.__dict__:
            continue
        fn = klass.__dict__["eventFilter"]
        if not isinstance(fn, types.FunctionType):
            return          # shiboken's own - nothing to guard
        if getattr(fn, "_guarded", False):
            return
        where = f"{klass.__module__}.{klass.__qualname__}.eventFilter"

        @functools.wraps(fn)
        def wrapper(self, obj, event, _fn=fn, _where=where):
            try:
                return _fn(self, obj, event)
            except Exception:
                _report(_where)
                return False        # don't eat the event
        wrapper._guarded = True
        klass.eventFilter = wrapper
        return


def install() -> None:
    """Guard every event filter installed from now on. Call once, at startup."""
    global _installed
    if _installed:
        return
    _installed = True
    original = QObject.installEventFilter

    def installEventFilter(self, obj):      # noqa: N802 (Qt name)
        try:
            cls = type(obj)
            with _lock:
                fresh = cls not in _seen
                _seen.add(cls)
            if fresh:
                _guard_event_filter(cls)
        except Exception:
            pass
        return original(self, obj)

    QObject.installEventFilter = installEventFilter
