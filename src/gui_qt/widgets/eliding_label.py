"""
eliding_label.py
A QLabel that elides its text instead of forcing its container wider.

A plain QLabel reports the full text width as its minimum, so one long string
sets a floor under the whole panel; this one reports a zero minimum and
re-elides to whatever width it is given.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QLabel, QSizePolicy

__all__ = ["ElidingLabel"]


class ElidingLabel(QLabel):
    """Draws its text elided to the current width; never widens its parent."""

    def __init__(self, text: str = "", mode=Qt.ElideRight,
                 max_width: int = 520, parent=None):
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self._max_width = max_width
        self._eliding = False
        # Preferred, NOT Ignored: Ignored makes QWidgetItem::sizeHint() report
        # ZERO width, so a FlowLayout would give the label no room at all. The
        # zero-width minimumSizeHint below is what avoids flooring the panel.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text):                             # noqa: N802
        self._full = text or ""
        self.setToolTip(self._full)
        self._apply_elide()
        self.updateGeometry()

    def text(self) -> str:
        return self._full

    def _apply_elide(self) -> None:
        # Let QLabel paint the elided string itself: a custom paintEvent does
        # not see the QSS `color:` rule and drew the title invisibly.
        if self._eliding:
            return
        self._eliding = True
        try:
            fm = self.fontMetrics()
            super().setText(fm.elidedText(self._full, self._mode,
                                          max(0, self.width())))
        finally:
            self._eliding = False

    def resizeEvent(self, event):                        # noqa: N802
        super().resizeEvent(event)
        self._apply_elide()

    def sizeHint(self) -> QSize:                         # noqa: N802
        # What the text wants, up to max_width; FlowLayout clamps to the row
        # and resizeEvent re-elides to the width actually granted.
        fm = self.fontMetrics()
        want = fm.horizontalAdvance(self._full) + 2
        return QSize(min(want, self._max_width), fm.height() + 4)

    def minimumSizeHint(self) -> QSize:                  # noqa: N802
        return QSize(0, self.fontMetrics().height() + 4)
