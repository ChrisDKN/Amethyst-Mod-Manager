"""OverlayBase - shared skeleton for the borderless in-window overlays.

Every overlay is a dimmed CHILD widget over the main window (NOT a top-level
window - gaming-mode opens top-levels behind the app) with a centred card.
Before this base class each ``*_overlay.py`` re-implemented the same backdrop,
card frame, Esc handling, host-resize tracking and finish-once lifecycle;
subclasses now only build the card's content.

Subclass contract:
  * call ``super().__init__(host, on_done=...)`` first,
  * build the card with ``self._make_card("MyCard")`` (returns (frame, vbox)),
  * call ``self._present()`` at the end of ``__init__``,
  * funnel every outcome through ``self._finish(result)``.

Class attributes tune behaviour: CARD_W/CARD_H (preferred card size, per-
instance override via ``card_w=``/``card_h=``), MIN_W/MIN_H (floor when the
host is small), ESC_RESULT (what Esc / a backdrop click reports) and
CLICK_OUTSIDE_CANCELS. Overlays whose geometry or finish semantics genuinely
differ override ``_reposition()`` / ``_finish()``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import QWidget, QFrame, QVBoxLayout

from gui_qt.theme_qt import active_palette, _c


class OverlayBase(QWidget):
    CARD_W = 480
    CARD_H = 240
    MIN_W = 340
    MIN_H = 160
    ESC_RESULT = None               # _finish() arg for Esc / backdrop click
    CLICK_OUTSIDE_CANCELS = False

    def __init__(self, host: QWidget, on_done=None,
                 card_w: int | None = None, card_h: int | None = None):
        super().__init__(host)
        self._host = host
        self._on_done = on_done
        self._done = False
        self._card: QFrame | None = None
        self._card_w = card_w if card_w is not None else self.CARD_W
        self._card_h = card_h if card_h is not None else self.CARD_H
        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

    # -- card ---------------------------------------------------------------
    def _make_card(self, obj_name: str, extra_qss: str = "",
                   margins: tuple = (18, 16, 18, 16), spacing: int = 8,
                   bg_key: str = "BG_PANEL"):
        """The standard panel card + its QVBoxLayout. *extra_qss* is appended
        to the card stylesheet (e.g. a #DangerButton rule)."""
        p = active_palette()
        card = QFrame(self)
        card.setObjectName(obj_name)
        card.setStyleSheet(
            f"#{obj_name} {{ background:{_c(p, bg_key)};"
            f" border:1px solid {_c(p, 'BORDER')}; border-radius:8px; }}"
            + extra_qss)
        v = QVBoxLayout(card)
        v.setContentsMargins(*margins)
        v.setSpacing(spacing)
        self._card = card
        return card, v

    def _present(self):
        """Show the overlay - call at the end of the subclass __init__."""
        self._host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    # -- lifecycle ------------------------------------------------------------
    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self._card_w, self._host.width() - 40)
        h = min(self._card_h, self._host.height() - 40)
        self._card.setFixedSize(max(self.MIN_W, w), max(self.MIN_H, h))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def _finish(self, result=None):
        if self._done:
            return
        self._done = True
        self._host.removeEventFilter(self)
        cb = self._on_done
        self.hide()
        self.deleteLater()
        if cb is not None:
            cb(result)

    # -- events ---------------------------------------------------------------
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._finish(self.ESC_RESULT)
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if (self.CLICK_OUTSIDE_CANCELS and self._card is not None
                and not self._card.geometry().contains(
                    event.position().toPoint())):
            self._finish(self.ESC_RESULT)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)
