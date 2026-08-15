"""Borderless pre-pack options overlay (Qt).

Qt counterpart of the Tk ``_PackOptionsDialog``. A dimmed child overlay (see
gui_qt/overlay_base.py) with a centered card: title, an optional overwrite
warning, three opt-in checkboxes (delete loose / separate textures (BSA only) /
keep winning files loose) each with a dim hint line, and Cancel / Pack buttons.

``on_done`` receives ``{"delete_loose", "split_textures", "skip_winners"}`` on
Pack, or ``None`` on Cancel / Esc.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QCheckBox,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class BsaPackOverlay(OverlayBase):
    CARD_W = 520
    CARD_H = 460
    MIN_W = 360
    MIN_H = 260

    def __init__(self, host: QWidget, *, archive_name: str, existing: bool,
                 kind: str, on_done):
        super().__init__(host, on_done=on_done)
        p = active_palette()

        _card, v = self._make_card("PackCard", margins=(18, 16, 18, 14),
                                   spacing=6)

        title_lbl = QLabel(self.tr("Pack {0}").format(archive_name))
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        if existing:
            warn = QLabel(
                self.tr("⚠  {0} already exists in this mod and will be overwritten.").format(archive_name))
            warn.setWordWrap(True)
            warn.setStyleSheet(
                f"color:{_c(p, 'TEXT_WARN')}; font-size:12px;")
            v.addWidget(warn)

        # -- delete loose --------------------------------------------------
        self._delete_cb = QCheckBox(self.tr("Delete loose files after packing"))
        v.addWidget(self._delete_cb)
        v.addWidget(self._hint(
            self.tr("Files that get packed will be removed from the mod folder. Files "
            "outside the packable filter (plugins, readmes, .bik videos) and "
            "files you've disabled in the Mod Files tab are left alone."), p))

        # -- split textures (BSA only) -------------------------------------
        self._split_cb: QCheckBox | None = None
        if kind == "bsa":
            self._split_cb = QCheckBox(self.tr("Separate textures archive"))
            v.addWidget(self._split_cb)
            v.addWidget(self._hint(
                self.tr("Writes textures to a sibling “… - Textures.bsa” instead of "
                "bundling them with the main archive. Optional for Skyrim / "
                "FNV / Oblivion; mostly useful for very large texture packs."), p))

        # -- skip winners --------------------------------------------------
        self._skip_cb = QCheckBox(self.tr("Keep winning conflict files loose"))
        v.addWidget(self._skip_cb)
        v.addWidget(self._hint(
            self.tr("Files this mod currently wins as loose are left out of the archive "
            "so deploy still picks them. Files this mod already loses, or that "
            "have no conflict, are packed normally."), p))

        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        pack = QPushButton(self.tr("Pack"))
        pack.setObjectName("PrimaryButton")
        pack.setCursor(Qt.PointingHandCursor)
        pack.clicked.connect(self._confirm)
        bar.addWidget(pack)
        v.addLayout(bar)

        self._present()

    @staticmethod
    def _hint(text: str, p) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_DIM')}; font-size:11px; margin-left:22px;")
        return lbl

    @classmethod
    def show_over(cls, host, *, archive_name, existing, kind, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, archive_name=archive_name, existing=existing,
                   kind=kind, on_done=on_done)

    def _confirm(self):
        self._finish({
            "delete_loose": self._delete_cb.isChecked(),
            "split_textures": bool(self._split_cb and self._split_cb.isChecked()),
            "skip_winners": self._skip_cb.isChecked(),
        })
