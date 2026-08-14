"""Backdrop picker for the NPC viewer's "Save as image" export.

A dimmed child overlay (see gui_qt/overlay_base.py, NOT a QDialog - gaming
mode opens top-levels behind the app) offering every viewer backdrop plus a
transparent cut-out, so the exported PNG's background is chosen at save time
rather than by whatever the viewport happens to be showing.

``on_done`` receives a BACKGROUNDS key, ``"transparent"``, or ``None`` when
the user cancels.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QButtonGroup, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QRadioButton,
)

from gui_qt.nif_preview import BACKGROUNDS, BACKGROUND_ORDER
from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c

TRANSPARENT = "transparent"

# Swatch size; a colour chip reads the choice faster than its name does.
# Deliberately a BAR, not a square: a square sits next to the radio's own dot
# and the two read as one confused control.
_CHIP_W, _CHIP_H = 24, 14


class ImageExportOverlay(OverlayBase):
    """Pick the exported image's backdrop. ``on_done(key | None)``."""

    # Six rows + title + hint + a button bar. OverlayBase fixes the card size
    # and gives it no scroll area, so this has to be tall enough for all of
    # them or the bottom row is clipped - measured against a rendered card,
    # not guessed, because too MUCH height leaves an obvious dead band above
    # the buttons.
    CARD_W = 400
    CARD_H = 326
    MIN_H = 300
    ESC_RESULT = None
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, on_done, current: str = "light",
                 labels: dict | None = None):
        # *current* preselects the viewer's own backdrop, so the default
        # answer is always "what I am looking at".
        super().__init__(host, on_done=on_done)
        p = active_palette()
        _card, v = self._make_card("ImageExportCard")

        title = QLabel(self.tr("Save as image"))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        body = QLabel(self.tr("Choose the background for the exported PNG."))
        body.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        body.setWordWrap(True)
        v.addWidget(body)

        names = labels or {}
        self._group = QButtonGroup(self)
        grid = QGridLayout()
        grid.setContentsMargins(0, 6, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        row = 0
        for key in BACKGROUND_ORDER:
            grid.addWidget(self._swatch(BACKGROUNDS[key]), row, 0)
            grid.addWidget(self._option(key, names.get(key, key.title())),
                           row, 1)
            row += 1
        grid.addWidget(self._swatch(None), row, 0)
        grid.addWidget(
            self._option(TRANSPARENT, self.tr("Transparent")), row, 1)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)

        hint = QLabel(self.tr(
            "Transparent cuts the actor out with no backdrop at all. Green "
            "screen does the same for editors that key by colour."))
        hint.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        hint.setWordWrap(True)
        v.addWidget(hint)
        v.addStretch(1)

        chosen = self._group.button(self._index_of(current))
        if chosen is None:
            chosen = self._group.button(0)
        if chosen is not None:
            chosen.setChecked(True)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        save = QPushButton(self.tr("Save…"))
        save.setObjectName("FormButton")
        save.setCursor(Qt.PointingHandCursor)
        save.setDefault(True)
        save.clicked.connect(self._accept)
        bar.addWidget(save)
        v.addLayout(bar)

        save.setFocus()
        self._present()

    # -- building -----------------------------------------------------------
    @staticmethod
    def _keys() -> list[str]:
        return [*BACKGROUND_ORDER, TRANSPARENT]

    def _index_of(self, key: str) -> int:
        keys = self._keys()
        return keys.index(key) if key in keys else 0

    def _option(self, key: str, label: str) -> QRadioButton:
        btn = QRadioButton(label)
        btn.setCursor(Qt.PointingHandCursor)
        self._group.addButton(btn, self._index_of(key))
        return btn

    def _swatch(self, colour: str | None) -> QLabel:
        """A colour chip; *colour* None draws the checkerboard for no backdrop."""
        p = active_palette()
        chip = QLabel()
        chip.setFixedSize(_CHIP_W, _CHIP_H)
        if colour is None:
            # Two-tone tile - the universal "nothing here" of image editors.
            light, dark = "#ffffff", "#c0c0c0"
            chip.setStyleSheet(
                f"border:1px solid {_c(p,'BORDER')}; border-radius:3px;"
                f" background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
                f" stop:0 {light}, stop:0.49 {light},"
                f" stop:0.5 {dark}, stop:1 {dark});")
        else:
            chip.setStyleSheet(
                f"border:1px solid {_c(p,'BORDER')}; border-radius:3px;"
                f" background:{QColor(colour).name()};")
        return chip

    # -- outcome ------------------------------------------------------------
    def _accept(self):
        checked = self._group.checkedId()
        keys = self._keys()
        if not 0 <= checked < len(keys):
            self._finish(None)
            return
        self._finish(keys[checked])
