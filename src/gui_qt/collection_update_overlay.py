"""Collection update-confirmation overlay (Qt port of the update half of
gui/collection_install_dialogs.py :: CollectionUpdateDialog).

Shown when the user clicks "Update Collection": presents the reconciliation diff
between the installed revision and the revision being viewed (mods to remove /
update / add / orphan) and asks the user to confirm before the install runs.

Borderless in-window overlay via gui_qt/overlay_base.py. All widgets are built
ONCE with real parents (no per-item unparented widgets that could flash as
blank top-level windows - see the collection install-overlay fix).

``on_done(True)`` on Apply Update, ``on_done(False)`` on Cancel / Escape.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class UpdateOverlay(OverlayBase):
    CARD_W = 560
    CARD_H = 460
    MIN_W = 400
    MIN_H = 300
    ESC_RESULT = False

    def __init__(self, host: QWidget, *, profile_name: str,
                 from_rev, to_rev, to_remove: "list[str]",
                 to_update: "list[str]", to_add: "list[str]",
                 orphans: "list[str]", on_done):
        super().__init__(host, on_done=on_done)
        self._p = active_palette()
        self._profile_name = profile_name or ""
        self._from_rev = from_rev
        self._to_rev = to_rev
        self._to_remove = list(to_remove or [])
        self._to_update = list(to_update or [])
        self._to_add = list(to_add or [])
        self._orphans = list(orphans or [])

        self._build()
        self._present()
        self.setFocus()

    @classmethod
    def show_over(cls, host, **kwargs):
        top = host.window() if host is not None else None
        return cls(top or host, **kwargs)

    def _c(self, k):
        return _c(self._p, k)

    # -- build --------------------------------------------------------------
    def _build(self):
        _card, v = self._make_card("_UpdateCard", margins=(20, 16, 20, 16))

        title = QLabel(self.tr("Update Collection"), self._card)
        title.setStyleSheet(
            f"color:{self._c('TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        def _rev(r):
            return self.tr("Rev {0}").format(r) if r is not None else self.tr("?")
        summary = QLabel(
            self.tr("Profile '{0}' - {1} → {2}").format(
                self._profile_name, _rev(self._from_rev), _rev(self._to_rev)),
            self._card)
        summary.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:13px;")
        v.addWidget(summary)

        counts = QLabel(
            self.tr("{0} to remove · {1} to update · {2} to add · {3} orphan(s)")
            .format(len(self._to_remove), len(self._to_update),
                    len(self._to_add), len(self._orphans)),
            self._card)
        counts.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:12px;")
        v.addWidget(counts)

        warn = QLabel(
            self.tr("Removed and updated mods will be reinstalled. Your existing load "
            "order is preserved where possible."), self._card)
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:11px;")
        v.addWidget(warn)

        # Scrollable body: one labelled section per bucket (built once).
        scroll = QScrollArea(self._card)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }")
        body = QFrame()
        body.setObjectName("_UpdBody")
        body.setStyleSheet(
            f"#_UpdBody {{ background:{self._c('BG_LIST')};"
            f" border:1px solid {self._c('BORDER')}; border-radius:6px; }}")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(10, 8, 10, 8)
        blay.setSpacing(8)
        self._add_section(blay, self.tr("Remove"), self._to_remove)
        self._add_section(blay, self.tr("Update"), self._to_update)
        self._add_section(blay, self.tr("Add"), self._to_add)
        self._add_section(blay, self.tr("Orphans"), self._orphans)
        blay.addStretch(1)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"), self._card)
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(False))
        bar.addWidget(cancel)
        apply_btn = QPushButton(self.tr("Apply Update"), self._card)
        apply_btn.setObjectName("PrimaryButton")
        apply_btn.setCursor(Qt.PointingHandCursor)
        apply_btn.clicked.connect(lambda: self._finish(True))
        bar.addWidget(apply_btn)
        v.addLayout(bar)

    def _add_section(self, layout, title: str, items: "list[str]"):
        hdr = QLabel(self.tr("{0} ({1})").format(title, len(items)))
        hdr.setStyleSheet(
            f"color:{self._c('TEXT_MAIN')}; font-weight:600; font-size:12px;")
        layout.addWidget(hdr)
        if items:
            lbl = QLabel("\n".join(f"  • {name}" for name in items))
            lbl.setWordWrap(True)
            lbl.setStyleSheet(
                f"color:{self._c('TEXT_DIM')}; font-size:12px;"
                " background:transparent;")
            layout.addWidget(lbl)
        else:
            none_lbl = QLabel(self.tr("  (none)"))
            none_lbl.setStyleSheet(
                f"color:{self._c('TEXT_DIM')}; font-size:11px;"
                " background:transparent;")
            layout.addWidget(none_lbl)
