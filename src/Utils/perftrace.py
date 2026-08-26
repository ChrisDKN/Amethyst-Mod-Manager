"""Lightweight timing instrumentation - env-gated, prints to stderr.

Purpose
-------
When the UI "feels slow" on a big modlist (many mods, many conflicts, many
files/plugins), this module pins *where* the time goes. Wrap a hot block in
``perftrace.span("label")`` and any call that exceeds a small threshold prints
one line to stderr with its duration. Cumulative per-label stats accumulate so
you can press a key to see the worst offenders across a whole session.

This is the timing twin of ``memtrace.py`` (which tracks memory). Same
conventions: opt-in via env var, stderr output (teed live to the terminal and
run-stderr.log by run.sh).

Usage
-----
- Opt-in: off by default. Enable with ``MM_PERFTRACE=1`` (disable again with
  ``MM_PERFTRACE=0`` or by unsetting it).
- Wrap a block::

      from Utils import perftrace
      with perftrace.span("modlist._redraw"):
          ...                       # the work being timed

  or decorate a method::

      @perftrace.timed("filemap.build_filemap")
      def build_filemap(...):
          ...

- Any span slower than the threshold (default 8 ms, set ``MM_PERFTRACE_MS``)
  prints immediately, e.g.::

      [PERF] modlist._redraw            42.7 ms   (n=1)

  Sub-threshold spans are silent but still counted toward the summary.
- Press **F11** in the app to dump a summary table sorted by total time spent -
  this is the "where did the session's time go" view. **Shift+F11** resets the
  counters so you can profile a single action in isolation:
      1. Shift+F11   -> zero the stats
      2. Do the slow thing (toggle a mod, scroll, open a tab)
      3. F11         -> table of every span, worst total-time first
- Nested spans are tracked; the summary marks call counts so a cheap span
  called thousands of times (death by a thousand cuts) stands out from one
  genuinely slow call.

Output goes to **stderr** so a from-source run needs no extra wiring.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from functools import wraps

# label -> [total_seconds, call_count, max_seconds]
_STATS: dict[str, list] = {}
_DEPTH = 0                       # current nesting depth (for indented live lines)
_ENABLED: bool | None = None     # cached is_enabled()
_THRESHOLD_S: float | None = None


# The Qt entry point installs this before importing gui_qt.app so module-level
# imports can contribute to the same end-to-end startup report.  It is separate
# from MM_PERFTRACE: startup emits one compact report per launch by default,
# while the span tracer remains opt-in for noisy, action-level profiling.
_STARTUP_TIMELINE = None


class StartupTimeline:
    """Thread-safe, low-noise timing for one application launch.

    Events retain both their own duration and their wall-clock completion time.
    The latter matters because metadata, plugin, and conflict workers overlap;
    summing worker durations would overstate the launch time.  Filegraph events
    are tagged and rendered in their own section so unrelated startup costs are
    immediately visible.
    """

    __slots__ = (
        "started", "_events", "_lane_last", "_lock", "_finished",
        "_sequence",
    )

    def __init__(self, started: float | None = None):
        self.started = started if started is not None else time.perf_counter()
        self._events: list[tuple[int, float, float, str, str, str]] = []
        self._lane_last: dict[str, float] = {"main": self.started}
        self._lock = threading.Lock()
        self._finished = False
        self._sequence = 0

    @property
    def active(self) -> bool:
        with self._lock:
            return not self._finished

    @staticmethod
    def now() -> float:
        return time.perf_counter()

    def record(
        self,
        label: str,
        *,
        phase_started: float | None = None,
        phase_finished: float | None = None,
        lane: str = "main",
        category: str = "application",
    ) -> float:
        """Record a completed phase and return its finish timestamp.

        When *phase_started* is omitted the phase starts at the previous event
        on the same lane, which makes sequential main-thread checkpoints terse.
        Worker callers should pass an explicit start because their work is
        intentionally concurrent with other lanes.
        """
        finished = (phase_finished if phase_finished is not None
                    else time.perf_counter())
        lane = str(lane or "main")
        category = str(category or "application")
        label = str(label)
        with self._lock:
            if self._finished:
                return finished
            started = (phase_started if phase_started is not None
                       else self._lane_last.get(lane, self.started))
            duration = max(0.0, finished - started)
            self._lane_last[lane] = finished
            self._sequence += 1
            self._events.append((
                self._sequence, finished, duration, lane,
                category, label,
            ))
        # The final table remains the easiest way to read the whole launch,
        # but it is necessarily emitted only once startup completes.  When the
        # action-level tracer is enabled, echo slow startup checkpoints as they
        # happen as well.  This is especially useful before QApplication/the
        # splash exists, where otherwise a multi-second stall is completely
        # silent.  One write keeps worker-thread lines from interleaving.
        if is_enabled() and duration >= _threshold_s():
            at = max(0.0, finished - self.started)
            try:
                sys.stderr.write(
                    f"[STARTUP-LIVE] +{at:7.3f}s  "
                    f"{self._duration(duration)}  "
                    f"{lane[:16]:<16} {category[:13]:<13} {label}\n"
                )
                sys.stderr.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
        return finished

    @contextmanager
    def span(self, label: str, *, lane: str = "main",
             category: str = "application"):
        """Record one block without producing live per-span log noise."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.record(label, phase_started=started, lane=lane,
                        category=category)

    @staticmethod
    def _duration(value: float) -> str:
        if value >= 1.0:
            return f"{value:7.3f} s"
        return f"{value * 1000:7.1f} ms"

    def finish(self, ready_label: str = "Application ready") -> list[str]:
        """Freeze the timeline and return a log-ready report.

        Calling this more than once is harmless; subsequent calls return an
        empty list so watchdog and normal completion paths cannot duplicate the
        report.
        """
        finished = time.perf_counter()
        with self._lock:
            if self._finished:
                return []
            self._finished = True
            self._sequence += 1
            self._events.append((
                self._sequence, finished, 0.0, "main", "ready",
                str(ready_label),
            ))
            events = sorted(self._events, key=lambda event: (event[1], event[0]))

        total = max(0.0, finished - self.started)
        normal = [event for event in events if event[4] != "filegraph"]
        filegraph = [event for event in events if event[4] == "filegraph"]
        lines = [
            f"[STARTUP] ===== ready in {total:.3f} s =====",
            "[STARTUP] Worker lanes and aggregate rows overlap; use the 'at' "
            "column for the wall-clock critical path rather than adding rows.",
            "[STARTUP]  duration       at  lane             area          phase",
        ]

        def append_rows(rows):
            for _seq, ended, duration, lane, category, label in rows:
                at = max(0.0, ended - self.started)
                lines.append(
                    f"[STARTUP] {self._duration(duration)}  "
                    f"{at:7.3f}s  {lane[:16]:<16} "
                    f"{category[:13]:<13} {label}"
                )

        append_rows(normal)
        if filegraph:
            lines.append(
                "[STARTUP] ----- filegraph/conflict work (separated) -----")
            append_rows(filegraph)
        lines.append("[STARTUP] =================================")
        return lines


def set_startup_timeline(timeline: StartupTimeline | None) -> None:
    """Expose the active launch timeline to modules imported during startup."""
    global _STARTUP_TIMELINE
    _STARTUP_TIMELINE = timeline


def startup_timeline() -> StartupTimeline | None:
    """Return the active launch timeline, if the Qt entry point installed one."""
    return _STARTUP_TIMELINE


def is_enabled() -> bool:
    global _ENABLED
    if _ENABLED is None:
        val = os.environ.get("MM_PERFTRACE")
        # Opt-in only: off unless MM_PERFTRACE is explicitly set to a truthy
        # value. Previously auto-on from source, which cluttered the log.
        _ENABLED = val is not None and val not in ("0", "", "false", "False")
    return _ENABLED


def _threshold_s() -> float:
    """Live-print threshold in seconds. Spans faster than this are silent
    (but still counted). Default 8 ms; override with MM_PERFTRACE_MS."""
    global _THRESHOLD_S
    if _THRESHOLD_S is None:
        try:
            _THRESHOLD_S = max(0.0, float(os.environ.get("MM_PERFTRACE_MS", "8"))) / 1000.0
        except ValueError:
            _THRESHOLD_S = 0.008
    return _THRESHOLD_S


def _record(label: str, dt: float) -> None:
    s = _STATS.get(label)
    if s is None:
        _STATS[label] = [dt, 1, dt]
    else:
        s[0] += dt
        s[1] += 1
        if dt > s[2]:
            s[2] = dt


@contextmanager
def span(label: str):
    """Time the wrapped block. No-op (near-zero overhead) when disabled.

    Prints one stderr line if the block exceeds the threshold; always feeds the
    cumulative summary shown by F11.
    """
    if not is_enabled():
        yield
        return
    global _DEPTH
    depth = _DEPTH
    _DEPTH += 1
    t0 = time.perf_counter()
    try:
        yield
    finally:
        dt = time.perf_counter() - t0
        _DEPTH = depth
        _record(label, dt)
        if dt >= _threshold_s():
            indent = "  " * depth
            print(f"[PERF] {indent}{label:<34} {dt * 1000:8.1f} ms", file=sys.stderr)
            sys.stderr.flush()


def timed(label: str):
    """Decorator form of :func:`span`."""
    def deco(fn):
        if not is_enabled():
            return fn

        @wraps(fn)
        def wrapper(*args, **kwargs):
            with span(label):
                return fn(*args, **kwargs)
        return wrapper
    return deco


def mark(label: str, dt_seconds: float) -> None:
    """Manually record an already-measured duration (e.g. across thread / after
    boundaries where a context manager can't span the gap). Prints if slow."""
    if not is_enabled():
        return
    _record(label, dt_seconds)
    if dt_seconds >= _threshold_s():
        print(f"[PERF] {label:<34} {dt_seconds * 1000:8.1f} ms", file=sys.stderr)
        sys.stderr.flush()


def reset() -> None:
    """Clear all accumulated stats (Shift+F11)."""
    _STATS.clear()
    if is_enabled():
        print("[PERF] stats reset - do the slow action, then press F11 for the table.",
              file=sys.stderr)
        sys.stderr.flush()


def dump(_event=None) -> None:
    """Print a summary table sorted by total time spent (F11)."""
    if not is_enabled():
        return
    out = sys.stderr
    if not _STATS:
        print("[PERF] no spans recorded yet.", file=out)
        out.flush()
        return
    rows = sorted(_STATS.items(), key=lambda kv: kv[1][0], reverse=True)
    print("\n[PERF] ===== timing summary (by total time) =====", file=out)
    print(f"[PERF] {'label':<36} {'total':>9} {'calls':>7} {'avg':>9} {'max':>9}",
          file=out)
    for label, (total, n, mx) in rows:
        avg = total / n if n else 0.0
        print(f"[PERF] {label:<36} {total * 1000:7.1f}ms {n:>7} "
              f"{avg * 1000:7.1f}ms {mx * 1000:7.1f}ms", file=out)
    print("[PERF] ============================================\n", file=out)
    out.flush()


def install(root) -> None:
    """Wire F11 -> summary, Shift+F11 -> reset. No-op when disabled."""
    if not is_enabled():
        return
    try:
        root.bind_all("<F11>", dump, add="+")
        root.bind_all("<Shift-F11>", lambda _e: reset(), add="+")
    except Exception:
        pass
    print(f"[PERF] perftrace enabled - live-prints spans >{_threshold_s() * 1000:.0f}ms; "
          "F11 = summary table, Shift+F11 = reset counters.", file=sys.stderr)
    print("[PERF] work tags: [CPU] in-memory computation; [FS I/O] filesystem "
          "calls (which may be cache-backed); [DB I/O] SQLite/catalog access; "
          "[BACKGROUND] outside the button's critical path.", file=sys.stderr)
    sys.stderr.flush()
