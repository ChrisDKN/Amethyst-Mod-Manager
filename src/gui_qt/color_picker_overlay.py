"""Borderless in-window colour-picker overlay.

A dimmed child overlay (see gui_qt/overlay_base.py) with a centered card
embedding Qt's non-native ``QColorDialog`` as a plain child widget
(``Qt.Widget`` flags + ``NoButtons``), plus our own Cancel / OK bar.
``on_done(QColor)`` on confirm, ``on_done(None)`` on cancel.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QColorDialog,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class ColorPickerOverlay(OverlayBase):
    CARD_W = 620
    CARD_H = 480
    MIN_W = 420
    MIN_H = 360
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, title: str, initial: QColor, on_done):
        super().__init__(host, on_done=on_done)
        p = active_palette()

        _card, v = self._make_card("ColorPickerCard")

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        self._picker = QColorDialog(self._card)
        self._picker.setWindowFlags(Qt.Widget)
        self._picker.setOptions(QColorDialog.DontUseNativeDialog
                                | QColorDialog.NoButtons)
        if initial is not None and initial.isValid():
            self._picker.setCurrentColor(initial)
        v.addWidget(self._picker, 1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        ok = QPushButton(self.tr("OK"))
        ok.setObjectName("PrimaryButton")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(lambda: self._finish(self._picker.currentColor()))
        bar.addWidget(ok)
        v.addLayout(bar)

        self._present()

    @classmethod
    def show_over(cls, host, title, initial, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, title, initial, on_done)

    def _finish(self, result=None):
        """Dismiss the heavyweight QColorDialog before applying the preview.

        QApplication stylesheet changes visit every live widget. Queuing the
        result callback for the next event-loop turn lets deleteLater remove
        the colour dialog and all of its internal controls first, shortening
        the subsequent theme pass and making the OK button respond at once.
        """
        if self._done:
            return
        callback = self._on_done
        self._on_done = None
        super()._finish(None)
        if callback is not None:
            QTimer.singleShot(
                0, lambda value=result, done=callback: done(value))
