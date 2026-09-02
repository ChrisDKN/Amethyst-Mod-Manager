"""End-to-end diagnostics for mod toggles and priority moves."""

from __future__ import annotations

import itertools
import threading
import time

from Utils.app_log import safe_print


_SEQUENCE = itertools.count(1)


class ConflictTimeline:
    """A small thread-safe timeline shared by model, worker, and Qt callbacks."""

    __slots__ = (
        "operation_id", "kind", "started", "_lane_last", "_lock",
        "_finished", "_enabled",
    )

    def __init__(self, kind: str, mods=()):
        self.operation_id = next(_SEQUENCE)
        self.kind = str(kind or "edit")
        self.started = time.perf_counter()
        self._lane_last = {"UI": self.started, "worker": self.started}
        self._lock = threading.Lock()
        self._finished = False
        from Utils.diagnostics import performance as perftrace
        self._enabled = perftrace.is_enabled()
        names = [str(name) for name in mods if name]
        detail = ", ".join(names[:3])
        if len(names) > 3:
            detail += f", +{len(names) - 3} more"
        suffix = f" ({detail})" if detail else ""
        if self._enabled:
            self.mark(f"edit accepted{suffix}", phase_started=self.started)

    @staticmethod
    def now() -> float:
        return time.perf_counter()

    def mark(
        self,
        label: str,
        *,
        phase_started: float | None = None,
        lane: str = "UI",
    ) -> None:
        if not self._enabled:
            return
        now = time.perf_counter()
        with self._lock:
            if self._finished:
                return
            previous = self._lane_last.get(lane, self.started)
            duration = now - (
                phase_started if phase_started is not None else previous)
            self._lane_last[lane] = now
            elapsed = now - self.started
        safe_print(
            f"[CONFLICT-TIMING] op={self.operation_id} "
            f"kind={self.kind} + {elapsed:7.3f}s "
            f"({lane} step {duration:7.3f}s) {label}",
            flush=True,
        )

    def finish(self, label: str, *, lane: str = "UI") -> None:
        if not self._enabled:
            return
        now = time.perf_counter()
        with self._lock:
            if self._finished:
                return
            self._finished = True
            previous = self._lane_last.get(lane, self.started)
            duration = now - previous
            self._lane_last[lane] = now
            elapsed = now - self.started
        safe_print(
            f"[CONFLICT-TIMING] op={self.operation_id} "
            f"kind={self.kind} + {elapsed:7.3f}s "
            f"({lane} step {duration:7.3f}s) {label}",
            flush=True,
        )


def timeline_from_edit_ctx(edit_ctx) -> ConflictTimeline | None:
    if not isinstance(edit_ctx, (tuple, list)):
        return None
    for value in reversed(edit_ctx):
        if isinstance(value, ConflictTimeline):
            return value
    return None


def edit_mod_names(edit_ctx) -> list[str]:
    if not edit_ctx:
        return []
    try:
        if edit_ctx[0] == "toggle":
            return [str(name) for name, _enabled in edit_ctx[1]]
        if edit_ctx[0] == "move":
            return [str(name) for name in edit_ctx[1]]
    except (IndexError, TypeError, ValueError):
        return []
    return []


def ensure_timeline(edit_ctx):
    """Return ``(possibly augmented edit_ctx, timeline)`` for toggle/move."""
    from Utils.diagnostics import performance as perftrace
    if not perftrace.is_enabled():
        return edit_ctx, None
    timeline = timeline_from_edit_ctx(edit_ctx)
    if timeline is not None:
        return edit_ctx, timeline
    if not edit_ctx or edit_ctx[0] not in ("toggle", "move"):
        return edit_ctx, None
    timeline = ConflictTimeline(str(edit_ctx[0]), edit_mod_names(edit_ctx))
    edit_ctx = tuple(edit_ctx) + (timeline,)
    return edit_ctx, timeline
