"""Generic in-window list-picker overlay.

A dimmed borderless child overlay (see gui_qt/overlay_base.py) with a centered
card: title, a scrollable list of choices, and a Cancel button. Double-click or
Select → ``on_pick(value)``; Cancel / Esc / backdrop click → ``on_pick(None)``.

Used for choosing a target profile or a target separator from the modlist menu.
Items are ``(display_label, value)`` pairs.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class ListPickerOverlay(OverlayBase):
    CARD_W = 420
    CARD_H = 420
    MIN_W = 300
    MIN_H = 220
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, title: str, items, on_pick,
                 select_label: str = "Select", *, search_placeholder: str = ""):
        super().__init__(host, on_done=on_pick)
        p = active_palette()

        _card, v = self._make_card("_PickerCard", margins=(16, 14, 16, 14))

        hdr = QLabel(title)
        hdr.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:15px;")
        hdr.setWordWrap(True)
        v.addWidget(hdr)

        self._search = None
        if search_placeholder:
            self._search = QLineEdit()
            self._search.setPlaceholderText(search_placeholder)
            self._search.setClearButtonEnabled(True)
            self._search.textChanged.connect(self._apply_filter)
            self._search.returnPressed.connect(self._pick)
            v.addWidget(self._search)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setStyleSheet(
            f"QListWidget {{ font-size:14px; }}"
            f"QListWidget::item {{ padding:7px 6px;"
            f" border-bottom:1px solid {_c(p,'BORDER')}; }}")
        for label, value in items:
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, value)
            self._list.addItem(it)
        if self._list.count():
            self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(lambda _i: self._pick())
        v.addWidget(self._list, 1)

        self._empty = None
        if self._search is not None:
            self._empty = QLabel(self.tr("No matching items."))
            self._empty.setAlignment(Qt.AlignCenter)
            self._empty.hide()
            v.addWidget(self._empty)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        self._select = QPushButton(select_label)
        self._select.setObjectName("PrimaryButton")
        self._select.setCursor(Qt.PointingHandCursor)
        self._select.clicked.connect(self._pick)
        self._select.setEnabled(self._list.currentItem() is not None)
        bar.addWidget(self._select)
        v.addLayout(bar)

        self._present()
        (self._search if self._search is not None else self._list).setFocus()

    @classmethod
    def show_over(cls, host, title, items, on_pick, select_label="Select", **kw):
        top = host.window() if host is not None else None
        return cls(top or host, title, items, on_pick,
                   select_label=select_label, **kw)

    # -- internals ----------------------------------------------------------
    def _apply_filter(self, text: str):
        query = text.strip().casefold()
        first = None
        for row in range(self._list.count()):
            item = self._list.item(row)
            item.setHidden(query not in item.text().casefold())
            if first is None and not item.isHidden():
                first = item
        current = self._list.currentItem()
        if current is None or current.isHidden():
            self._list.setCurrentItem(first)
        self._select.setEnabled(first is not None)
        self._empty.setVisible(first is None)
        if first is not None:
            self._list.scrollToItem(self._list.currentItem())

    def _pick(self):
        item = self._list.currentItem()
        if item is not None and not item.isHidden():
            self._finish(item.data(Qt.UserRole))
