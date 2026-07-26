"""Generic borderless "new profile" name + kind prompt.

Like ``gui_qt/text_input_overlay.py``'s ``TextInputOverlay`` but with the
extra "Use Profile Specific Mods" checkbox from ``gui_qt/new_profile_bar.py``
— used where a new profile needs to be created as a destination from
somewhere other than the profile selector's inline bar (e.g. the modlist's
"Copy/Move to profile" submenu's "New profile…" entry).

``on_done((name, profile_specific_mods))`` on confirm, ``on_done(None)`` on
cancel / Esc / backdrop click.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QCheckBox, QPushButton

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class NewProfilePromptOverlay(OverlayBase):
    CARD_W = 440
    CARD_H = 220
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host, title: str, on_done, *, default_specific: bool = False):
        super().__init__(host, on_done=on_done)
        p = active_palette()
        _card, v = self._make_card("_NewProfilePromptCard")

        title_lbl = QLabel(title, _card)
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        self._name = QLineEdit(_card)
        self._name.setPlaceholderText(self.tr("Profile name"))
        self._name.returnPressed.connect(self._confirm)
        v.addWidget(self._name)

        self._specific = QCheckBox(self.tr("Use Profile Specific Mods"), _card)
        self._specific.setToolTip(
            self.tr("Profiles with this setting use their own mods folders"))
        self._specific.setChecked(default_specific)
        v.addWidget(self._specific)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"), _card)
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish((None, None)))
        bar.addWidget(cancel)
        ok = QPushButton(self.tr("Create"), _card)
        ok.setObjectName("PrimaryButton")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self._confirm)
        bar.addWidget(ok)
        v.addLayout(bar)

        self._present()
        self._name.setFocus()

    @classmethod
    def show_over(cls, host, title, on_done, *, default_specific: bool = False):
        top = host.window() if host is not None else None
        return cls(top or host, title, on_done, default_specific=default_specific)

    def _confirm(self):
        name = self._name.text().strip()
        if not name:
            self._name.setFocus()
            return
        self._finish((name, self._specific.isChecked()))
