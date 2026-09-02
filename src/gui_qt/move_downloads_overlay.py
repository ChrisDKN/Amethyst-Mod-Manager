"""Borderless in-window overlay to move selected download archives between the
*configured* download locations (default Downloads, Mod Manager cache, and any
extra locations) - NOT a native folder browser.

The list of targets comes from ``Utils.downloads.core.get_scan_dirs`` /
``section_label_for_dir`` so it matches the folders the Downloads tab already
scans. Picking a target invokes ``on_pick(Path)`` with the chosen destination.

Dimmed child overlay + centered card via gui_qt/overlay_base.py.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c
import Utils.downloads.core as dc


class MoveDownloadsOverlay(OverlayBase):
    CARD_W = 520
    CARD_H = 440
    MIN_W = 360
    MIN_H = 240

    def __init__(self, host: QWidget, count: int, game_name, on_pick):
        super().__init__(host, on_done=on_pick)
        p = active_palette()

        _card, v = self._make_card(
            "MoveDownloadsCard",
            extra_qss=(
                f" #LocRow {{ background:{_c(p,'BG_ROW')};"
                f" border:1px solid {_c(p,'BORDER')}; border-radius:6px; }}"
                f" #LocRow:hover {{ border:1px solid {_c(p,'BTN_INFO')}; }}"))

        title_lbl = QLabel(self.tr("Move {0} archive(s) to…").format(count))
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        intro = QLabel(self.tr("Choose a configured download location."))
        intro.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        intro.setWordWrap(True)
        v.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        rows = QVBoxLayout(inner)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(6)

        any_row = False
        for d in dc.get_scan_dirs(game_name):
            if not d.is_dir():
                continue
            any_row = True
            rows.addWidget(self._loc_row(d, game_name, p))
        rows.addStretch(1)
        if not any_row:
            empty = QLabel(self.tr("No configured download locations."))
            empty.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
            rows.insertWidget(0, empty)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        v.addLayout(bar)

        self._present()

    @classmethod
    def show_over(cls, host, count, game_name, on_pick):
        top = host.window() if host is not None else None
        return cls(top or host, count, game_name, on_pick)

    # -- rows ---------------------------------------------------------------
    def _loc_row(self, d: Path, game_name, p) -> QWidget:
        label = dc.section_label_for_dir(d, game_name)
        row = QFrame()
        row.setObjectName("LocRow")
        row.setCursor(Qt.PointingHandCursor)
        rv = QVBoxLayout(row)
        rv.setContentsMargins(12, 8, 12, 8)
        rv.setSpacing(2)
        name = QLabel(label)
        name.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:13px;")
        rv.addWidget(name)
        sub = QLabel(str(d))
        sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        sub.setWordWrap(True)
        rv.addWidget(sub)
        row.mouseReleaseEvent = lambda _e, path=d: self._finish(path)
        return row
