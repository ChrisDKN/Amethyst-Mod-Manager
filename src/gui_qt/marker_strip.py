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

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QScrollBar, QStyle, QStyleOptionSlider

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
        bind_theme(self, roles=(
            set(self._code_roles.values())
            | {"TONE_RED", "TONE_GREEN", "SCROLL_TROUGH", "BG_MAIN"}))

    def refresh_theme(self, palette):
        self._code_cols = {code: qc(palette, role)
                           for code, role in self._code_roles.items()}
        self._c_missing = qc(palette, "TONE_RED")
        self._c_master = qc(palette, "TONE_GREEN")
        self._c_cycle = qc(palette, "TONE_RED")
        self._c_trough = qc(
            palette, "SCROLL_TROUGH" if "SCROLL_TROUGH" in palette else "BG_MAIN")
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

    def _row_offsets(self, model):
        """Return (offsets, total) where offsets[row] is the row's content-space
        Y centre (px from the top of the full content) and *total* is the full
        content height. Hidden rows (under a collapsed separator) take 0 height -
        so ticks line up with where the row actually sits on the scroll track,
        accounting for variable row heights (separators are taller). Returns
        (None, 0) if geometry isn't available yet."""
        view = self._view
        n = model.rowCount()
        cum = 0
        offsets: dict[int, int] = {}
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
        return offsets, max(1, cum)

    def paintEvent(self, event):
        model = self._view.model()
        n = model.rowCount() if model is not None else 0

        # Without explicitly clearing its backing-store pixels, ticks removed by
        # a conflict delta remain visible until another widget happens to expose
        # that area. Clear to the themed trough colour (NOT transparency - a
        # Source-composited transparent fill punches a hole through the opaque
        # window and renders black); the styled groove paints over it below.
        clear = QPainter(self)
        clear.setCompositionMode(QPainter.CompositionMode_Source)
        clear.fillRect(event.rect(), getattr(self, "_c_trough", Qt.transparent))
        clear.end()

        # Ticks paint UNDER the scrollbar handle: draw them first, then let the
        # styled groove + handle paint on top (the handle hides ticks only where
        # it currently sits; the rest of the track shows every tick).
        marks = []
        if n > 0:
            for r in range(n):
                code = model.data(model.index(r, 0), self._role) or 0
                if code:
                    marks.append((r, code))
        if n > 0 and (marks or self._missing_rows or self._master_rows
                      or self._cycle_rows):
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            groove = self.style().subControlRect(
                QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarGroove, self)
            top = groove.top()
            h = max(1, groove.height())
            w = self.width()
            offsets, total = self._row_offsets(model)
            p = QPainter(self)

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

        # Groove + handle on top → ticks read as being "under" the scrollbar.
        super().paintEvent(event)


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

    def _refresh(*_):
        s = getattr(view, "_marker_strip", None)
        if s is not None:
            s.update()
    if view.model() is not None:
        view.model().dataChanged.connect(_refresh)
    return sb


def reposition_marker_strip(view) -> None:
    """No-op - the MarkerScrollBar IS the scrollbar, so it positions itself. Kept
    so callers (resizeEvent/showEvent) don't need to special-case."""
    sb = getattr(view, "_marker_strip", None)
    if sb is not None:
        sb.update()
