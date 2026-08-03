"""Tk-style column header: a boundary drag grows one column and shrinks the
other side (cascading past minimums), total width constant, no overflow. We own
the drag because QHeaderView's native resize maps a boundary to the wrong column
and allows overflow.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHeaderView
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QColor, QPolygonF


_GRAB_PX = 8   # pixels either side of a boundary that count as "on the line"

# Sort-triangle metrics (painted per section when the view opts in).
_TRI_W = 12
_TRI_H = 7
_TRI_PAD = 4   # gap from the section's right edge


class TkStyleHeader(QHeaderView):
    def __init__(self, view, col_mins: dict, col_defaults: dict | None = None,
                 parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._view = view
        self._col_mins = col_mins
        self._col_defaults = col_defaults or {}
        # Section-move OFF: it conflicts with boundary-drag resizing and isn't
        # needed for the Tk feel. (Column reordering can return later via a
        # dedicated affordance.)
        # Section MOVE is enabled (Tk parity — drag a header to reorder). It
        # coexists with our boundary-drag resize: a press ON a boundary line is
        # consumed for resizing (see mousePressEvent), anything else falls
        # through to Qt's native move. The view pins COL 0 (Mod Name) in place.
        self.setSectionsMovable(True)
        self.setSectionsClickable(True)
        # We own resizing; Qt must not auto-resize or interactively resize.
        self.setSectionResizeMode(QHeaderView.Fixed)
        self.setMouseTracking(True)
        self._drag_boundary = None   # (left_logical, right_logical)
        self._drag_x = 0
        self._cursor_on = False      # whether SplitHCursor is currently set
        self._tri_active = None      # lazily-resolved triangle colours
        self._tri_idle = None
        self._tri_text = None

    # -- per-section sort triangles ------------------------------------------
    # Painted when the owning view provides sort_triangle_spec(logical) →
    # None (no triangle) or (active: bool, ascending: bool). Every sortable
    # column always shows a triangle; the active sort's is accent-blue and
    # points ▲/▼ with the direction. Views without the hook (plugins list,
    # Mod Files tree) are untouched.
    def paintSection(self, painter, rect, logicalIndex):
        # Views that pin a widget over the header's left edge (the tabs' eye
        # filter button) set `header_left_pad` on the view: the FIRST visual
        # section is then painted that many px in, so its label isn't hidden.
        # The bare strip that leaves is exactly what the opaque button covers.
        pad = getattr(self._view, "header_left_pad", 0)
        if pad and self.visualIndex(logicalIndex) == 0:
            rect = rect.adjusted(pad, 0, 0, 0)
        spec_fn = getattr(self._view, "sort_triangle_spec", None)
        spec = spec_fn(logicalIndex) if callable(spec_fn) else None
        if spec is None:
            # Icon-only sections (no text label, just a DecorationRole icon —
            # e.g. the plugins lock column) are painted with the icon CENTERED.
            # QHeaderView otherwise left-aligns the decoration alongside the
            # (empty) label.
            model = self.model()
            deco = model.headerData(logicalIndex, Qt.Horizontal,
                                    Qt.DecorationRole)
            text = model.headerData(logicalIndex, Qt.Horizontal,
                                    Qt.DisplayRole)
            if deco is not None and not text:
                # Draw the section chrome with the decoration suppressed so the
                # native left-aligned icon doesn't show under our centered one.
                painter.save()
                setattr(model, "_suppress_header_deco", True)
                try:
                    super().paintSection(painter, rect, logicalIndex)
                finally:
                    setattr(model, "_suppress_header_deco", False)
                painter.restore()
                sz = 14
                pm = deco.pixmap(sz, sz)
                if not pm.isNull():
                    x = rect.center().x() - pm.width() // 2
                    y = rect.center().y() - pm.height() // 2
                    painter.drawPixmap(x, y, pm)
                return
            painter.save()
            super().paintSection(painter, rect, logicalIndex)
            painter.restore()
            return
        active, ascending = spec
        if self._tri_active is None:
            from gui_qt.theme_qt import active_palette, _c
            p = active_palette()
            self._tri_active = QColor(_c(p, "ACCENT"))
            self._tri_idle = QColor(_c(p, "TEXT_DIM"))
        # Paint the section chrome (QSS background/borders/hover) with the
        # label suppressed, then draw the text ourselves elided into the space
        # LEFT of the triangle strip so the two can never overlap.
        model = self.model()
        painter.save()
        try:
            setattr(model, "_suppress_header_text", True)
            super().paintSection(painter, rect, logicalIndex)
        finally:
            setattr(model, "_suppress_header_text", False)
            painter.restore()
        text = model.headerData(logicalIndex, Qt.Horizontal, Qt.DisplayRole)
        text = "" if text is None else str(text)
        strip = _TRI_W + _TRI_PAD + 2        # triangle zone + breathing room
        avail = rect.adjusted(4, 0, -strip, 0)
        if text and avail.width() > 4:
            fm = painter.fontMetrics()
            elided = fm.elidedText(text, Qt.ElideRight, avail.width())
            painter.save()
            # ButtonText is what QHeaderView paints labels with — QSS's
            # `color:` lands there, so this matches the native look exactly.
            painter.setPen(self.palette().buttonText().color())
            painter.drawText(avail, int(self.defaultAlignment()), elided)
            painter.restore()
        x = rect.right() - _TRI_PAD - _TRI_W
        if x < rect.left() + 2:
            return   # section too narrow
        cy = rect.center().y()
        top, bot = cy - _TRI_H // 2, cy - _TRI_H // 2 + _TRI_H
        if ascending:
            pts = [QPointF(x, bot), QPointF(x + _TRI_W, bot),
                   QPointF(x + _TRI_W / 2, top)]
        else:
            pts = [QPointF(x, top), QPointF(x + _TRI_W, top),
                   QPointF(x + _TRI_W / 2, bot)]
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._tri_active if active else self._tri_idle)
        painter.drawPolygon(QPolygonF(pts))
        painter.restore()

    # -- boundary detection -------------------------------------------------
    def _boundary_at(self, x: int):
        """Return (left_logical, right_logical) if x is on a boundary between
        two visible sections, else None. Uses visual order."""
        count = self.count()
        # Build visible sections in visual order with their right-edge x.
        edges = []
        for vis in range(count):
            logical = self.logicalIndex(vis)
            if self.isSectionHidden(logical):
                continue
            pos = self.sectionViewportPosition(logical)
            size = self.sectionSize(logical)
            edges.append((logical, pos, pos + size))
        for i in range(len(edges) - 1):   # skip the very last right edge
            left_logical, _, right_edge = edges[i]
            right_logical = edges[i + 1][0]
            if abs(x - right_edge) <= _GRAB_PX:
                return (left_logical, right_logical)
        return None

    # -- mouse handling -----------------------------------------------------
    def mouseMoveEvent(self, event):
        x = event.position().toPoint().x()
        if self._drag_boundary is not None:
            self._do_drag(x)
            return   # don't let the base class interfere mid-drag
        # Cursor feedback. Only toggle when it actually changes, and DON'T call
        # super() while on a boundary — the base QHeaderView resets the cursor
        # every move, which caused the flicker.
        # Toggle cursor only on change; skip super() while on a boundary (it
        # resets the cursor every move → flicker).
        on = self._boundary_at(x) is not None
        if on != self._cursor_on:
            self.setCursor(Qt.SplitHCursor) if on else self.unsetCursor()
            self._cursor_on = on
        if not on:
            super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            b = self._boundary_at(event.position().toPoint().x())
            if b is not None:
                self._drag_boundary = b
                self._drag_x = event.position().toPoint().x()
                # Snapshot widths at press; each move recomputes from snapshot +
                # cumulative delta so back-and-forth drags return exactly to
                # start (per-event mutation drifts over many 1px moves).
                self._drag_order = self._visible_order()
                self._drag_start_w = {c: self.sectionSize(c)
                                      for c in self._drag_order}
                return   # consume — don't start a section move/sort
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_boundary is not None:
            self._drag_boundary = None
            # The modlist/plugins views persist column widths; the Mod Files tree
            # doesn't need to, so only call the hook when the view provides it.
            save = getattr(self._view, "_schedule_save", None)
            if callable(save):
                save()
            return
        super().mouseReleaseEvent(event)

    def _visible_order(self):
        order = []
        for vis in range(self.count()):
            lg = self.logicalIndex(vis)
            if not self.isSectionHidden(lg):
                order.append(lg)
        return order

    def _do_drag(self, x: int):
        """Recompute widths from the press snapshot + cumulative delta. Cursor
        right of start grows LEFT and shrinks the RIGHT chain (cascade past
        mins); cursor left does the inverse. Growth is capped to what the shrink
        side frees, so the table never overflows."""
        left, right = self._drag_boundary
        order = self._drag_order
        start = self._drag_start_w
        try:
            li = order.index(left)
        except ValueError:
            return
        total = x - self._drag_x        # cumulative delta from press point

        # Start from the snapshot; we'll overwrite the affected columns.
        widths = dict(start)
        right_chain = order[li + 1:]                 # nearest-first
        left_chain = list(reversed(order[:li + 1]))  # nearest-first (incl. left)

        if total > 0:
            moved = self._take(widths, right_chain, total)  # shrink right side
            widths[left] = start[left] + moved              # grow left by freed
        elif total < 0:
            moved = self._take(widths, left_chain, -total)  # shrink left side
            widths[right] = start[right] + moved            # grow right by freed
        # total == 0 → widths == snapshot (restores start exactly)

        for c, w in widths.items():
            if w != self.sectionSize(c):
                self.resizeSection(c, w)

    def _take(self, widths, donors, amount):
        """Reduce donor columns in *widths* (in order, toward their mins) to
        free up to *amount* px. Mutates *widths*; returns total actually freed."""
        freed = 0
        for d in donors:
            if amount <= 0:
                break
            room = widths[d] - self._col_mins.get(d, 60)
            if room <= 0:
                continue
            take = min(room, amount)
            widths[d] -= take
            amount -= take
            freed += take
        return freed
