"""safe_emit - emit a Signal that may have lost its C++ owner.

Wizard views hand their bound Signals to daemon workers (and to portal
file-picker callbacks, which fire on a worker thread).  If the user closes
the tab while the worker is still running, the view is deleteLater()'d and a
later ``sig.emit(...)`` raises ``RuntimeError: Signal source has been
deleted`` - which, raised inside a worker's except handler, kills the whole
thread.  The views' ``_closing`` guards only protect the slot side; this
protects the emit side.  Late emits are simply dropped: the object they
would update no longer exists.
"""

from __future__ import annotations


def safe_emit(sig, *args) -> None:
    """Emit *sig* with *args*, dropping the emit if the owning QObject has
    already been deleted. Use for every emit that can fire after the view
    might have closed (worker threads, picker callbacks)."""
    try:
        sig.emit(*args)
    except RuntimeError:
        pass
