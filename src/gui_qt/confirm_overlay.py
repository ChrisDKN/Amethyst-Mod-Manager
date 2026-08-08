"""Generic borderless in-window confirmation overlay.

A dimmed child overlay (see gui_qt/overlay_base.py) with a centered card:
title, body text, and Confirm / Cancel buttons. ``on_done(True)`` on confirm,
``on_done(False)`` on cancel / Esc.

Pass ``cancel_label=None`` for a single-button message card (OK-only) - the
in-app replacement for ``QMessageBox.warning``/``critical``/``information``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QListWidget)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c, contrast_text


class ConfirmOverlay(OverlayBase):
    CARD_W = 480
    CARD_H = 240
    MIN_H = 180
    ESC_RESULT = False

    _NO_CANCEL = object()  # sentinel: distinguish "OK-only" from "use default"

    def __init__(self, host: QWidget, title: str, body: str, on_done,
                 confirm_label: str | None = None,
                 cancel_label=_NO_CANCEL,
                 danger: bool = True,
                 card_h: int | None = None,
                 list_items: list[str] | None = None):
        # When a long ``list_items`` is passed, the card grows and hosts a
        # scrollable list so the item names never overflow the window.
        if list_items and card_h is None:
            card_h = 360
        super().__init__(host, on_done=on_done, card_h=card_h)
        p = active_palette()

        _card, v = self._make_card(
            "ConfirmCard",
            extra_qss=(
                f" #DangerButton {{ background:{_c(p,'BTN_DANGER')};"
                f" color:{contrast_text(_c(p,'BTN_DANGER'))};"
                f" border:none; border-radius:4px; padding:6px 14px;"
                f" font-weight:600; }}"
                f" #DangerButton:hover {{ background:{_c(p,'BTN_DANGER_HOV')}; }}"))

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        body_lbl = QLabel(body)
        body_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        body_lbl.setWordWrap(True)
        v.addWidget(body_lbl)

        if list_items:
            lst = QListWidget()
            lst.setObjectName("ConfirmList")
            lst.addItems(list_items)
            lst.setSelectionMode(QListWidget.NoSelection)
            lst.setFocusPolicy(Qt.NoFocus)
            lst.setStyleSheet(
                f"#ConfirmList {{ background:{_c(p,'BG_LIST')};"
                f" color:{_c(p,'TEXT_DIM')}; border:1px solid {_c(p,'BORDER')};"
                f" border-radius:4px; font-size:12px; padding:2px; }}"
                f" #ConfirmList::item {{ padding:2px 4px; }}")
            v.addWidget(lst, 1)
        else:
            v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        if cancel_label is not None:
            cancel_text = (self.tr("Cancel")
                           if cancel_label is self._NO_CANCEL else cancel_label)
            cancel = QPushButton(cancel_text)
            cancel.setObjectName("FormButton")
            cancel.setCursor(Qt.PointingHandCursor)
            cancel.clicked.connect(lambda: self._finish(False))
            bar.addWidget(cancel)
        confirm = QPushButton(
            confirm_label if confirm_label is not None else self.tr("Remove"))
        confirm.setObjectName("DangerButton" if danger else "PrimaryButton")
        confirm.setCursor(Qt.PointingHandCursor)
        confirm.clicked.connect(lambda: self._finish(True))
        bar.addWidget(confirm)
        v.addLayout(bar)

        self._present()

    @classmethod
    def show_over(cls, host, title, body, on_done, **kw):
        top = host.window() if host is not None else None
        return cls(top or host, title, body, on_done, **kw)

    @classmethod
    def show_message(cls, host, title, body, on_done=None, ok_label=None, **kw):
        """OK-only message card (QMessageBox.warning/critical replacement)."""
        from PySide6.QtCore import QCoreApplication
        if ok_label is None:
            ok_label = QCoreApplication.translate("ConfirmOverlay", "OK")
        return cls.show_over(host, title, body, on_done,
                             confirm_label=ok_label, cancel_label=None,
                             danger=False, **kw)
