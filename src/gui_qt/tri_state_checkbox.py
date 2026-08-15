"""Tri-state checkbox for Qt filter panels.

States: 0 = off, 1 = include (blue check), 2 = exclude (red minus). Clicking
cycles 0 -> 1 -> 2 -> 0, mirroring the Tk TriStateCheckBox so the same filter
engine (off / include / exclude) drives both toolkits.

Painted by hand (QAbstractButton) so all three states render consistently
regardless of the platform style - the box is the same 16px blue indicator as
the rest of the app, the exclude state swaps to a red box with a white minus.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath
from PySide6.QtWidgets import QAbstractButton, QSizePolicy

from gui_qt.theme_qt import bind_theme, _c, contrast_text


STATE_OFF = 0
STATE_INCLUDE = 1
STATE_EXCLUDE = 2

_BOX = 16            # indicator box size (px)
_GAP = 8             # gap between box and label


class TriStateCheckBox(QAbstractButton):
    """A three-state checkbox. `state` is 0/1/2; `stateChanged(int)` fires on
    every change (click or set_state with a different value)."""

    stateChanged = Signal(int)

    def __init__(self, text: str = "", parent=None, *,
                 include_color: str | None = None, two_state: bool = False):
        super().__init__(parent)
        self._state = STATE_OFF
        self._include_override = (str(include_color)
                                  if include_color is not None
                                  and not getattr(include_color, "theme_key", None)
                                  else None)
        self._include_role = getattr(include_color, "theme_key", None)
        roles = {
            "BTN_DANGER", "BG_ROW", "BORDER_FAINT", "ACCENT",
            "TEXT_MAIN", "TEXT_DIM",
        }
        if self._include_role:
            roles.add(self._include_role)
        elif self._include_override is None:
            roles.add("CHECK_FILL")
        bind_theme(self, roles=roles)
        # two_state: cycle off <-> include only (no exclude). Used where a plain
        # on/off check is wanted but the row must look identical to the tri-state
        # filter rows (e.g. the Nexus categories panel).
        self._modulo = 2 if two_state else 3
        self.setText(text)
        self.setCheckable(False)          # we manage our own tri-state
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.clicked.connect(self._cycle)

    def refresh_theme(self, pal: dict) -> None:
        self._include = (self._include_override
                         or _c(pal, self._include_role or "CHECK_FILL"))
        self._exclude = _c(pal, "BTN_DANGER")
        self._box_bg = _c(pal, "BG_ROW")
        self._box_border = _c(pal, "BORDER_FAINT")
        self._box_border_hover = _c(pal, "ACCENT")
        self._text_color = _c(pal, "TEXT_MAIN")
        self._text_disabled = _c(pal, "TEXT_DIM")
        self._glyph = contrast_text(self._include)
        self._exclude_glyph = contrast_text(self._exclude)
        self.update()

    # -- state ----------------------------------------------------------------
    def state(self) -> int:
        return self._state

    def set_state(self, state: int, *, emit: bool = False) -> None:
        state = int(state) % self._modulo
        if state == self._state:
            return
        self._state = state
        self.update()
        if emit:
            self.stateChanged.emit(state)

    def _cycle(self) -> None:
        self._state = (self._state + 1) % self._modulo
        self.update()
        self.stateChanged.emit(self._state)

    # -- sizing ---------------------------------------------------------------
    def sizeHint(self):
        fm = self.fontMetrics()
        w = _BOX + _GAP + fm.horizontalAdvance(self.text()) + 4
        h = max(_BOX, fm.height()) + 6
        from PySide6.QtCore import QSize
        return QSize(w, h)

    # -- painting -------------------------------------------------------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        h = self.height()
        box_y = (h - _BOX) / 2
        box = QRectF(0.5, box_y + 0.5, _BOX - 1, _BOX - 1)

        enabled = self.isEnabled()
        hover = self.underMouse() and enabled

        if self._state == STATE_INCLUDE:
            fill = QColor(self._include)
            border = QColor(self._include)
        elif self._state == STATE_EXCLUDE:
            fill = QColor(self._exclude)
            border = QColor(self._exclude)
        else:
            fill = QColor(self._box_bg)
            border = QColor(self._box_border_hover if hover else self._box_border)

        if not enabled:
            fill.setAlpha(90)
            border.setAlpha(120)

        p.setPen(QPen(border, 1))
        p.setBrush(fill)
        p.drawRoundedRect(box, 3, 3)

        if self._state == STATE_INCLUDE:
            self._draw_check(p, box)
        elif self._state == STATE_EXCLUDE:
            self._draw_minus(p, box)

        # Label - elide with "…" if it's wider than the available room (longer
        # translated filter labels can exceed the fixed panel width) and show
        # the full text in a tooltip so nothing is lost.
        p.setPen(QColor(self._text_color if enabled else self._text_disabled))
        text_rect = self.rect().adjusted(_BOX + _GAP, 0, -2, 0)
        fm = self.fontMetrics()
        elided = fm.elidedText(self.text(), Qt.ElideRight, text_rect.width())
        if elided != self.text():
            if self.toolTip() != self.text():
                self.setToolTip(self.text())
        elif self.toolTip():
            self.setToolTip("")
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        p.end()

    def _draw_check(self, p: QPainter, box: QRectF) -> None:
        glyph = QColor(self._glyph)
        if not self.isEnabled():
            glyph.setAlpha(150)
        pen = QPen(glyph, 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        x, y, w, hh = box.x(), box.y(), box.width(), box.height()
        path = QPainterPath()
        path.moveTo(x + w * 0.26, y + hh * 0.52)
        path.lineTo(x + w * 0.43, y + hh * 0.70)
        path.lineTo(x + w * 0.76, y + hh * 0.30)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    def _draw_minus(self, p: QPainter, box: QRectF) -> None:
        glyph = QColor(self._exclude_glyph)
        if not self.isEnabled():
            glyph.setAlpha(150)
        pen = QPen(glyph, 2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        x, y, w, hh = box.x(), box.y(), box.width(), box.height()
        cy = y + hh / 2
        p.drawLine(int(x + w * 0.24), int(cy), int(x + w * 0.76), int(cy))
