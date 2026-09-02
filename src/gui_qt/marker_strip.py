"""Marker scrollbar - a QScrollBar that paints coloured conflict-highlight ticks
directly into its own track, mirroring the Tk app's combined scrollbar+marker
canvas. Used by both the modlist and the plugins panel.

We subclass QScrollBar (rather than overlay a separate widget) because a sibling
overlay parented to the view doesn't composite reliably over the scrollbar - by
owning the scrollbar's paintEvent the ticks are guaranteed to render, in the real
scrollbar track, behind the handle (exactly MO2/Tk behaviour).

Ticks: orange = anchor (the mod/plugin selected in the other panel), green = rows
the selection beats, red = rows that beat the selection, purple/blue = the
requirement highlights while the View Requirements tab is open (mods the
selection requires / mods that require it). Positions are proportional to row
index so they line up with the visible scroll position.
"""

from __future__ import annotations

from time import perf_counter

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QRegion
from PySide6.QtWidgets import QScrollBar, QStyle, QStyleOptionSlider

from Utils.diagnostics import performance as perftrace
from gui_qt.theme_qt import bind_theme, qc


class MarkerScrollBar(QScrollBar):
    def __init__(self, view, highlight_role: int, code_map: dict | None = None):
        super().__init__(Qt.Vertical, view)
        self._view = view
        self._role = highlight_role
        self._code_roles = code_map if code_map is not None else {
            -1: "CONFLICT_HL_LOSE", 1: "CONFLICT_HL_WIN",
            -3: "REQ_HL_REQUIRED_BY", 3: "REQ_HL_REQUIRES",
            2: "CONFLICT_HL_ANCHOR",
        }
        # Persistent, selection-independent overlays (plugins panel). Rows are
        # model row indices. Painted on top of the role-driven conflict ticks,
        # so they mirror the Tk marker-strip priority: missing (red) beats the
        # cross-panel highlight, which beats master (green). See paintEvent.
        self._missing_rows: set[int] = set()   # plugins with missing masters
        self._master_rows: set[int] = set()    # masters of the selected plugin
        self._cycle_rows: set[int] = set()     # plugins with a broken cycle
        self._marks: list[tuple[int, int]] | None = None
        self._offsets: list[int] | None = None
        self._content_height = 1
        bind_theme(self, roles=(
            set(self._code_roles.values()) | {"TONE_RED", "TONE_GREEN"}))

    def refresh_theme(self, palette):
        self._code_cols = {code: qc(palette, role)
                           for code, role in self._code_roles.items()}
        self._c_missing = qc(palette, "TONE_RED")
        self._c_master = qc(palette, "TONE_GREEN")
        self._c_cycle = qc(palette, "TONE_RED")
        self.update()

    def set_persistent_rows(self, missing=None, master=None, cycle=None) -> None:
        """Set the persistent overlay row sets (missing masters / selected
        plugin's masters / broken userlist cycle) and repaint. Pass a set to
        replace, None to leave a given overlay unchanged."""
        if missing is not None:
            self._missing_rows = set(missing)
        if master is not None:
            self._master_rows = set(master)
        if cycle is not None:
            self._cycle_rows = set(cycle)
        self.update()

    def invalidate_marks(self, *_args) -> None:
        self._marks = None
        self.update()

    def invalidate_geometry(self, *_args) -> None:
        self._offsets = None
        self.update()

    def invalidate_cache(self, *_args) -> None:
        self._marks = None
        self._offsets = None
        self.update()

    def _highlight_marks(self, model, n: int) -> list[tuple[int, int]]:
        if self._marks is None:
            self._marks = [
                (r, code)
                for r in range(n)
                if (code := model.data(model.index(r, 0), self._role) or 0)
            ]
        return self._marks

    def _row_offsets(self, model):
        """Return (offsets, total) where offsets[row] is the row's content-space
        Y centre (px from the top of the full content) and *total* is the full
        content height. Hidden rows (under a collapsed separator) take 0 height -
        so ticks line up with where the row actually sits on the scroll track,
        accounting for the row heights supplied by the view."""
        n = model.rowCount()
        if self._offsets is not None and len(self._offsets) == n:
            return self._offsets, self._content_height
        view = self._view
        cum = 0
        offsets = [0] * n
        root = view.rootIndex()
        for r in range(n):
            if view.isRowHidden(r, root):
                offsets[r] = cum
                continue
            idx = model.index(r, 0)
            rh = view.rowHeight(idx)
            if rh <= 0:
                rh = 0
            offsets[r] = cum + rh // 2
            cum += rh
        self._offsets = offsets
        self._content_height = max(1, cum)
        return offsets, self._content_height

    def paintEvent(self, event):
        tracing = perftrace.is_enabled()
        trace_started = perf_counter() if tracing else 0.0
        model = self._view.model()
        n = model.rowCount() if model is not None else 0

        # Paint the themed scrollbar first so it clears stale ticks, then keep
        # new ticks out of the handle's rectangle so they still appear beneath it.
        super().paintEvent(event)

        native_finished = perf_counter() if tracing else 0.0
        marks = self._highlight_marks(model, n) if n > 0 else []
        marks_finished = perf_counter() if tracing else 0.0
        if n > 0 and (marks or self._missing_rows or self._master_rows
                      or self._cycle_rows):
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            groove = self.style().subControlRect(
                QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarGroove, self)
            handle = self.style().subControlRect(
                QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarSlider, self)
            top = groove.top()
            h = max(1, groove.height())
            w = self.width()
            offsets, total = self._row_offsets(model)
            p = QPainter(self)
            p.setClipRegion(QRegion(groove).subtracted(QRegion(handle)))

            def tick(r, col):
                if 0 <= r < n:
                    y = top + int(offsets[r] / total * h)
                    p.fillRect(0, max(top, y - 1), w, 3, col)

            # Draw low→high priority so higher-priority ticks overpaint on
            # coincidence, mirroring the Tk marker-strip order:
            # master(green) < conflict_lower(red) < conflict_higher(green)
            #   < highlighted/anchor(orange) < missing(red).
            for r in self._master_rows:
                tick(r, self._c_master)
            # lower → higher → required-by → requires → anchor so the anchor
            # wins on coincidence (the requirement codes never coexist with the
            # conflict ones - set_highlights swaps all sets at once). The map is
            # panel-specific (see __init__) so code 3 reads correctly per panel.
            for wanted, col in self._code_cols.items():
                for r, code in marks:
                    if code == wanted:
                        tick(r, col)
            for r in self._cycle_rows:
                tick(r, self._c_cycle)
            for r in self._missing_rows:
                tick(r, self._c_missing)
            p.end()
        if tracing:
            finished = perf_counter()
            perftrace.mark("ui.marker.native_paint", native_finished - trace_started)
            perftrace.mark("ui.marker.collect_marks", marks_finished - native_finished)
            perftrace.mark("ui.marker.paint_total", finished - trace_started)


def install_marker_strip(view, highlight_role: int,
                         code_map: dict | None = None) -> MarkerScrollBar:
    """Replace *view*'s vertical scrollbar with a MarkerScrollBar that paints
    conflict ticks. Refreshes on scroll + any highlight-role change. Returns the
    scrollbar (also stored on the view as ``_marker_strip``). Pass *code_map*
    (highlight code → palette role) to override the default modlist tick colours - the
    plugins panel does this because it reuses code 3 for masters, not requires."""
    sb = MarkerScrollBar(view, highlight_role, code_map)
    view.setVerticalScrollBar(sb)
    view._marker_strip = sb

    model = view.model()
    if model is not None:
        model.dataChanged.connect(sb.invalidate_marks)
        for signal in (model.modelReset, model.rowsInserted, model.rowsRemoved,
                       model.rowsMoved, model.layoutChanged):
            signal.connect(sb.invalidate_cache)
    return sb


def reposition_marker_strip(view) -> None:
    """No-op - the MarkerScrollBar IS the scrollbar, so it positions itself. Kept
    so callers (resizeEvent/showEvent) don't need to special-case."""
    sb = getattr(view, "_marker_strip", None)
    if sb is not None:
        sb.update()
