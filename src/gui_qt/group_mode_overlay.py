"""Profile Group export/import mode-choice overlays.

Shown BEFORE the export/import pipeline, only when the active profile (export)
or the picked file/code (import) is a Profile Group, to choose between
treating it as a real group (members preserved) or as one flattened combined
profile. Same borderless in-window overlay pattern as
``gui_qt/collection_mode_overlay.py``.

``on_done(mode)`` is called with ``"group"``, ``"combined"``, or ``None``
(cancelled).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QRadioButton, QButtonGroup

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class _BaseGroupModeOverlay(OverlayBase):
    CARD_W = 460
    CARD_H = 260
    MIN_W = 340
    MIN_H = 200

    def __init__(self, host, title, subtitle, group_label, group_desc,
                combined_label, combined_desc, action_label, on_done):
        super().__init__(host, on_done=on_done)
        self._p = active_palette()
        _card, self._v = self._make_card("_GroupModeCard", margins=(20, 16, 20, 16))
        self._build(title, subtitle, group_label, group_desc,
                   combined_label, combined_desc, action_label)
        self._present()

    def _c(self, k):
        return _c(self._p, k)

    def _build(self, title, subtitle, group_label, group_desc,
              combined_label, combined_desc, action_label):
        v = self._v

        t = QLabel(title, self._card)
        t.setStyleSheet(f"color:{self._c('TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(t)

        sub = QLabel(subtitle, self._card)
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:13px;")
        v.addWidget(sub)

        self._group_btns = QButtonGroup(self._card)

        self._group_radio = QRadioButton(group_label, self._card)
        self._group_radio.setChecked(True)
        self._group_btns.addButton(self._group_radio)
        v.addWidget(self._group_radio)
        group_hint = QLabel(group_desc, self._card)
        group_hint.setWordWrap(True)
        group_hint.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:11px; margin-left:20px;")
        v.addWidget(group_hint)

        self._combined_radio = QRadioButton(combined_label, self._card)
        self._group_btns.addButton(self._combined_radio)
        v.addWidget(self._combined_radio)
        combined_hint = QLabel(combined_desc, self._card)
        combined_hint.setWordWrap(True)
        combined_hint.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:11px; margin-left:20px;")
        v.addWidget(combined_hint)

        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"), self._card)
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        go = QPushButton(action_label, self._card)
        go.setObjectName("PrimaryButton")
        go.setCursor(Qt.PointingHandCursor)
        go.clicked.connect(
            lambda: self._finish("group" if self._group_radio.isChecked() else "combined"))
        bar.addWidget(go)
        v.addLayout(bar)


class GroupExportModeOverlay(_BaseGroupModeOverlay):
    def __init__(self, host, on_done):
        super().__init__(
            host,
            self.tr("Export Profile Group"),
            self.tr("This profile is a Profile Group. How should it be exported?"),
            self.tr("Export as Profile Group"),
            self.tr("Keeps the group and every member profile as separate, "
                    "re-importable entities."),
            self.tr("Export as single combined profile"),
            self.tr("Flattens the group's current merged modlist into one "
                    "ordinary profile export (previous behavior)."),
            self.tr("Export…"),
            on_done)

    @classmethod
    def show_over(cls, host, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, on_done)


class GroupImportModeOverlay(_BaseGroupModeOverlay):
    def __init__(self, host, on_done):
        super().__init__(
            host,
            self.tr("Import Profile Group"),
            self.tr("This is a Profile Group export. How should it be imported?"),
            self.tr("Import as Group + separate profiles"),
            self.tr("Recreates the group and every member as its own profile-specific profile."),
            self.tr("Import as single combined profile"),
            self.tr("Merges every member into one new profile, with no group entity."),
            self.tr("Import…"),
            on_done)

    @classmethod
    def show_over(cls, host, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, on_done)
