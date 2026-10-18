"""Borderless in-window date-picker overlay.

A dimmed child overlay (see gui_qt/dialogs/base.py) with a centered card
embedding Qt's ``QCalendarWidget`` as a plain child widget, plus our own
Cancel / OK bar. Mirrors ColorPickerOverlay. ``on_done(QDate)`` on confirm,
``on_done(None)`` on cancel.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QDate
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QCalendarWidget,
)

from gui_qt.dialogs.base import OverlayBase
from gui_qt.appearance.theme import active_palette, _c


class DatePickerOverlay(OverlayBase):
    CARD_W = 460
    CARD_H = 420
    MIN_W = 360
    MIN_H = 340
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, title: str, initial: QDate,
                 maximum: QDate, on_done):
        super().__init__(host, on_done=on_done)
        p = active_palette()

        _card, v = self._make_card("DatePickerCard")

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        self._cal = QCalendarWidget(self._card)
        if maximum is not None and maximum.isValid():
            self._cal.setMaximumDate(maximum)      # no future dates
        if initial is not None and initial.isValid():
            self._cal.setSelectedDate(initial)
        v.addWidget(self._cal, 1)

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
        ok.clicked.connect(lambda: self._finish(self._cal.selectedDate()))
        bar.addWidget(ok)
        v.addLayout(bar)

        self._present()

    @classmethod
    def show_over(cls, host, title, initial, maximum, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, title, initial, maximum, on_done)
