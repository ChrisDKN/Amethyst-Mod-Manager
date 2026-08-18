"""Tracks whether a Play click actually got a process running.

A launch can fail in a dozen places - no Proton found, no
Steam/Heroic/Lutris/Faugus route matched, the xdg-open handler chain
exhausted, the spawned process dying
instantly - and several of them only find out from a watcher thread long after
the launch call returned. Rather than thread a callback through every one of
those paths, the Play handler opens a report for the duration of the launch:
the spawn helpers record into the report bound to the calling thread, and late
failures reach the same object through the reference their watcher captured.

Nothing here is Play-specific except who opens the report - with no report
bound (tool launches, wizards, CLI) every call is a no-op.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

# A non-zero exit this soon after spawning means the launch never got off the
# ground. Later exits are the user quitting (or a mid-session crash), which is
# not something to raise a "couldn't launch" popup for.
EARLY_EXIT_SECONDS = 30.0

_local = threading.local()


class LaunchReport:
    """Outcome of one launch attempt. Fires ``on_fail(detail)`` at most once."""

    def __init__(self, on_fail: Callable[[str], None]):
        self._on_fail = on_fail
        self._lock = threading.Lock()
        self._spawned = False
        self._fired = False
        self._last_note = ""

    @property
    def spawned(self) -> bool:
        return self._spawned

    def note(self, message: str) -> None:
        """Remember the newest log line: launch paths that give up do so with a
        log message and a bare ``return``, so the last line is the reason."""
        if message:
            self._last_note = str(message).strip()

    def mark_spawned(self) -> None:
        self._spawned = True

    def mark_failed(self, detail: str = "") -> None:
        with self._lock:
            if self._fired:
                return
            self._fired = True
            cb, note = self._on_fail, self._last_note
        try:
            cb(detail or note)
        except Exception:
            pass

    def finish(self) -> None:
        """Call once the launch call returns: reaching no spawn at all means
        the launch failed, and the last log line says why."""
        if not self._spawned:
            self.mark_failed()


def current() -> Optional[LaunchReport]:
    """The report bound to this thread, or None outside a launch."""
    return getattr(_local, "report", None)


@contextmanager
def bind(rep: Optional[LaunchReport]):
    """Bind *rep* to the calling thread for the duration of the block.

    Watcher threads use this to re-enter the report before running a fallback
    chain, so the chain's own spawns are still recorded.
    """
    prev = getattr(_local, "report", None)
    _local.report = rep
    try:
        yield rep
    finally:
        _local.report = prev


@contextmanager
def report(on_fail: Callable[[str], None]):
    """Open a report for a launch attempt; yields it so the caller can
    ``finish()`` once the (synchronous part of the) launch has returned."""
    rep = LaunchReport(on_fail)
    with bind(rep):
        yield rep


def note(message: str) -> None:
    rep = current()
    if rep is not None:
        rep.note(message)


def mark_spawned() -> None:
    rep = current()
    if rep is not None:
        rep.mark_spawned()


def mark_failed(detail: str = "") -> None:
    rep = current()
    if rep is not None:
        rep.mark_failed(detail)


# Prefix stamped on a diagnosed failure so the popup can tell "here is the fix"
# apart from "it exited with code 1" without re-parsing the text.
_ACTIONABLE = "✓ "


def actionable(detail: str) -> str:
    """Mark *detail* as a diagnosed failure that already states its own fix."""
    return detail if is_actionable(detail) else _ACTIONABLE + detail


def is_actionable(detail: str) -> bool:
    """Whether *detail* is a diagnosed failure carrying its own fix."""
    return detail.startswith(_ACTIONABLE)


def strip_marker(detail: str) -> str:
    """Return *detail* without the actionable marker."""
    return detail[len(_ACTIONABLE):] if is_actionable(detail) else detail


def explain_stderr(tail: str) -> str:
    """Turn a known launcher error in *tail* into advice, or return ''.

    A launcher that refuses to start prints a precise reason and exits, but the
    raw text is developer-facing ("unable to find installation of Proton
    runtime proton_8"). Where the fix is a specific user action, say that
    instead - a popup that only shows an exit code sends people to the logs.
    """
    if not tail:
        return ""
    low = tail.lower()

    # me3 (FROMSOFTWARE games) picks the Proton build Steam has mapped for the
    # app, falling back to a hardcoded "verified on Deck" version that is often
    # not installed. Setting the app's compatibility tool in Steam wins over it.
    marker = "unable to find installation of proton runtime"
    if marker in low:
        wanted = tail[low.index(marker) + len(marker):].strip().split()
        version = wanted[0].strip("\"'.,") if wanted else ""
        named = f" ({version})" if version else ""
        return (
            _ACTIONABLE
            + "The mod loader asked for a Proton version that is not installed"
            f"{named}.\n\n"
            "In Steam, right-click the game → Properties → Compatibility → "
            "tick \"Force the use of a specific Steam Play compatibility "
            "tool\" and pick an installed Proton (Proton 10 or a GE-Proton "
            "build). That setting overrides the loader's default."
        )
    return ""


def mark_exit(rep: Optional[LaunchReport], started: float, rc: int,
              label: str, stderr_tail: str = "") -> None:
    """Report a watched process's exit: only an immediate non-zero one counts
    as a failed launch (see EARLY_EXIT_SECONDS)."""
    if rep is None or rc == 0:
        return
    if time.monotonic() - started > EARLY_EXIT_SECONDS:
        return
    advice = explain_stderr(stderr_tail)
    if advice:
        rep.mark_failed(advice)
        return
    rep.mark_failed(f"{label} exited immediately with code {rc}.")
