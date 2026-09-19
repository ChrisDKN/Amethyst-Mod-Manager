"""Environment-gated performance instrumentation written to stderr.

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

      from Utils.diagnostics import performance as perftrace
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
- Collection and Wabbajack installs also emit a scheduler snapshot every five
  seconds and a final queue/worker/resource summary. Set
  ``MM_PERFTRACE_INSTALL_MS`` to change that snapshot interval.

Output goes to **stderr** so a from-source run needs no extra wiring.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from functools import wraps

# label -> [total_seconds, call_count, max_seconds]
_STATS: dict[str, list] = {}
_STATS_LOCK = threading.Lock()
_OUTPUT_LOCK = threading.Lock()
_DEPTH = 0                       # current nesting depth (for indented live lines)
_ENABLED: bool | None = None     # cached is_enabled()
_THRESHOLD_S: float | None = None


# When MM_PERFTRACE is enabled, the Qt entry point installs this before
# importing gui_qt.app so module-level imports can contribute to the same
# end-to-end startup report.
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
    with _STATS_LOCK:
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


def record(label: str, dt_seconds: float) -> None:
    """Add a duration to the F11 summary without producing a live line."""
    if is_enabled():
        _record(label, max(0.0, float(dt_seconds)))


class InstallerTrace:
    """Low-noise, opt-in scheduler telemetry for collection-style installs."""

    _RESOURCE_KEYS = (
        "network_bytes_per_second", "installation_work_per_second", "cpu_percent",
        "io_some", "io_full", "memory_some", "memory_full",
        "disk_read_bytes_per_second", "disk_write_bytes_per_second",
        "download_write_max_seconds",
    )

    def __init__(self, installer: str):
        self.installer = str(installer)
        self.enabled = is_enabled()
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._config = {}
        self._stages = {}
        self._waits = {}
        self._active = {}
        self._max_active = {}
        self._queues = {}
        self._resources = {}
        self._latest = {}
        self._finished = False
        self._next_report = self.started + self._report_interval()

    @staticmethod
    def _report_interval() -> float:
        try:
            milliseconds = float(os.environ.get("MM_PERFTRACE_INSTALL_MS", "5000"))
        except ValueError:
            milliseconds = 5000.0
        return max(0.25, milliseconds / 1000.0)

    @staticmethod
    def _add(stats: dict, name: str, elapsed: float) -> None:
        row = stats.get(name)
        if row is None:
            stats[name] = [elapsed, 1, elapsed]
        else:
            row[0] += elapsed
            row[1] += 1
            row[2] = max(row[2], elapsed)

    @staticmethod
    def _duration(seconds: float) -> str:
        return f"{seconds:.2f}s" if seconds >= 1 else f"{seconds * 1000:.1f}ms"

    @staticmethod
    def _bytes(value: float | int | None) -> str:
        value = float(value or 0)
        if value >= 1024 ** 3:
            return f"{value / 1024 ** 3:.2f}GiB"
        if value >= 1024 ** 2:
            return f"{value / 1024 ** 2:.1f}MiB"
        if value >= 1024:
            return f"{value / 1024:.1f}KiB"
        return f"{value:.0f}B"

    @staticmethod
    def _rate(value: float | int | None) -> str:
        return InstallerTrace._bytes(value) + "/s"

    @staticmethod
    def _write(line: str) -> None:
        try:
            with _OUTPUT_LOCK:
                sys.stderr.write(line + "\n")
                sys.stderr.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def configure(self, **fields) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._finished:
                return
            self._config.update(fields)

    @contextmanager
    def activity(self, stage: str):
        if not self.enabled:
            yield
            return
        stage = str(stage)
        with self._lock:
            finished = self._finished
            if not finished:
                active = self._active.get(stage, 0) + 1
                self._active[stage] = active
                self._max_active[stage] = max(self._max_active.get(stage, 0), active)
        if finished:
            yield
            return
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - started
            with self._lock:
                self._active[stage] = max(0, self._active.get(stage, 1) - 1)
                self._add(self._stages, stage, elapsed)
            record(f"installer.{self.installer}.{stage}", elapsed)

    def wait(self, name: str, elapsed: float) -> None:
        if not self.enabled:
            return
        elapsed = max(0.0, float(elapsed))
        if elapsed < 0.001:
            return
        name = str(name)
        with self._lock:
            if self._finished:
                return
            self._add(self._waits, name, elapsed)
        record(f"installer.{self.installer}.wait.{name}", elapsed)

    def queue(self, name: str, items: int, *, queued_bytes: int = 0,
              waiters: int = 0, capacity: int = 0) -> None:
        if not self.enabled:
            return
        name = str(name)
        values = {
            "items": max(0, int(items)),
            "queued_bytes": max(0, int(queued_bytes)),
            "waiters": max(0, int(waiters)),
            "capacity": max(0, int(capacity)),
        }
        with self._lock:
            if self._finished:
                return
            row = self._queues.setdefault(name, {
                "max_items": 0, "max_queued_bytes": 0, "max_waiters": 0,
            })
            row.update(values)
            row["max_items"] = max(row["max_items"], values["items"])
            row["max_queued_bytes"] = max(
                row["max_queued_bytes"], values["queued_bytes"])
            row["max_waiters"] = max(row["max_waiters"], values["waiters"])

    def resources(self, **fields) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        line = None
        with self._lock:
            if self._finished:
                return
            self._latest = dict(fields)
            for key in self._RESOURCE_KEYS:
                source = fields.get(key)
                if key in {"io_some", "io_full", "memory_some", "memory_full"}:
                    source = fields.get(f"{key}_recent", source)
                if not isinstance(source, (int, float)):
                    continue
                self._latest[key] = source
                row = self._resources.get(key)
                if row is None:
                    self._resources[key] = [float(source), 1, float(source)]
                else:
                    row[0] += float(source)
                    row[1] += 1
                    row[2] = max(row[2], float(source))
            if now >= self._next_report:
                self._next_report = now + self._report_interval()
                line = self._snapshot_locked(now)
        if line:
            self._write(line)

    def _snapshot_locked(self, now: float) -> str:
        download_active = max(self._active.get("download", 0),
                              self._active.get("acquire", 0))
        download_workers = self._config.get("download_workers", "?")
        install_active = self._latest.get("active", self._active.get("install", 0))
        install_workers = self._latest.get(
            "workers", self._config.get("install_workers", "?"))
        queue = self._queues.get("install_ready", {})
        network = self._latest.get("network_bytes_per_second", 0)
        work = self._latest.get("installation_work_per_second", 0)
        cpu = self._latest.get("cpu_percent", 0)
        io_some = self._latest.get("io_some", 0)
        io_full = self._latest.get("io_full", 0)
        memory_some = self._latest.get("memory_some", 0)
        memory_full = self._latest.get("memory_full", 0)
        cpu_count = self._latest.get("cpu_count", 0)
        return (
            f"[PERF] installer.{self.installer} +{now - self.started:.1f}s "
            f"download={download_active}/{download_workers} "
            f"install={install_active}/{install_workers} "
            f"ready={queue.get('items', 0)} "
            f"({self._bytes(queue.get('queued_bytes', 0))}) "
            f"net={self._rate(network)} "
            f"work={self._rate(work)} "
            f"cpu={cpu:.0f}%/{cpu_count or '?'}c "
            f"io={io_some:.1f}/"
            f"{io_full:.1f}% "
            f"mem={memory_some:.1f}/"
            f"{memory_full:.1f}% "
            f"free={self._bytes(self._latest.get('available_memory', 0))} "
            f"waiting={self._latest.get('waiting', 0)} "
            f"decision={self._latest.get('decision', '')} "
            f"retry={self._latest.get('parallel_retry_seconds', 0):.1f}s "
            f"trial-admissions={self._latest.get('parallel_trial_admissions', 0)} "
            f"trial-seconds={self._latest.get('parallel_trial_seconds', 0):.1f} "
            f"reason={self._latest.get('reason', '')} "
            f"jobs={json.dumps(self._latest.get('active_jobs', []), ensure_ascii=True)}"
        )

    def finish(self, outcome: str = "complete") -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        with self._lock:
            if self._finished:
                return
            self._finished = True
            elapsed = now - self.started
            stages = sorted(self._stages.items(), key=lambda item: item[1][0], reverse=True)
            waits = sorted(self._waits.items(), key=lambda item: item[1][0], reverse=True)
            queues = sorted(self._queues.items())
            resources = dict(self._resources)
            latest = dict(self._latest)
            max_active = dict(self._max_active)
        record(f"installer.{self.installer}.wall", elapsed)
        self._write(
            f"[PERF] ===== installer {self.installer} scheduler: {outcome} "
            f"in {elapsed:.2f}s =====")
        self._write("[PERF] stage times overlap across concurrent lanes and nested phases.")
        for name, (total, calls, maximum) in stages:
            self._write(
                f"[PERF]   {name:<18} worker={self._duration(total):>9} "
                f"calls={calls:<5} avg={self._duration(total / calls):>8} "
                f"max={self._duration(maximum):>8} "
                f"peak-active={max_active.get(name, 0)}")
        for name, (total, calls, maximum) in waits:
            self._write(
                f"[PERF]   wait.{name:<13} total={self._duration(total):>9} "
                f"calls={calls:<5} max={self._duration(maximum):>8}")
        for name, row in queues:
            self._write(
                f"[PERF]   queue.{name:<12} peak={row['max_items']} items "
                f"{self._bytes(row['max_queued_bytes'])} "
                f"waiters={row['max_waiters']} cap={row.get('capacity', 0) or '?'}")
        if resources:
            def average(key):
                total, count, _maximum = resources.get(key, (0, 1, 0))
                return total / max(1, count)
            def peak(key):
                return resources.get(key, (0, 1, 0))[2]
            self._write(
                f"[PERF]   resources avg cpu={average('cpu_percent'):.0f}% "
                f"io={average('io_some'):.1f}/{average('io_full'):.1f}% "
                f"mem={average('memory_some'):.1f}/{average('memory_full'):.1f}% "
                f"net={self._rate(average('network_bytes_per_second'))} "
                f"work={self._rate(average('installation_work_per_second'))} "
                f"disk-r/w={self._rate(average('disk_read_bytes_per_second'))}/"
                f"{self._rate(average('disk_write_bytes_per_second'))} "
                f"peak-net={self._rate(peak('network_bytes_per_second'))} "
                f"peak-work={self._rate(peak('installation_work_per_second'))} "
                f"peak-write-latency={peak('download_write_max_seconds') * 1000:.0f}ms "
                f"cpus={latest.get('cpu_count', 0) or '?'} "
                f"last-free={self._bytes(latest.get('available_memory', 0))} "
                f"last-reason={latest.get('reason', '')}")
        self._write("[PERF] =============================================================")


def reset() -> None:
    """Clear all accumulated stats (Shift+F11)."""
    with _STATS_LOCK:
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
    with _STATS_LOCK:
        rows = sorted(((label, tuple(values)) for label, values in _STATS.items()),
                      key=lambda kv: kv[1][0], reverse=True)
    if not rows:
        print("[PERF] no spans recorded yet.", file=out)
        out.flush()
        return
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
