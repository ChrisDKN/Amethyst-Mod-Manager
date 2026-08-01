"""Borderless in-window overlay for the game-launch settings.

Qt port of the game-exe branch of Tk's ExeConfigPanel: a "Launch via"
selector (Auto / Steam / Heroic / Lutris / Faugus / None) plus the "Deploy mods before
launching" checkbox. ``on_done(mode, deploy)`` fires with the lowercase mode
string on Save, or ``on_done(None, None)`` on Cancel / Esc.

Dimmed child backdrop + centered card via gui_qt/overlay_base.py.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QComboBox, QCheckBox,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c
from gui_qt.wheel_guard import no_wheel

_MODES = ["Auto", "Steam", "Heroic", "Lutris", "Faugus", "None"]


class LauncherSettingsOverlay(OverlayBase):
    CARD_W = 480
    CARD_H = 260
    MIN_H = 200
    ESC_RESULT = False

    def __init__(self, host: QWidget, game_name: str, mode: str, deploy: bool,
                 on_done):
        super().__init__(host, on_done=on_done)
        p = active_palette()

        _card, v = self._make_card("ConfirmCard")

        title_lbl = QLabel(self.tr("Launch settings — {0}").format(game_name))
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        row = QHBoxLayout()
        row.setSpacing(8)
        via_lbl = QLabel(self.tr("Launch via"))
        via_lbl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        row.addWidget(via_lbl)
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(_MODES)
        cap = (mode or "auto").capitalize()
        self._mode_combo.setCurrentText(cap if cap in _MODES else "Auto")
        no_wheel(self._mode_combo)
        row.addWidget(self._mode_combo)
        row.addStretch(1)
        v.addLayout(row)

        hint = QLabel(self.tr("Auto detects Steam/Heroic/Lutris/Faugus ownership. "
                      "Force a specific launcher, or None to always launch the "
                      "exe directly via Proton."))
        hint.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self._deploy_check = QCheckBox(self.tr("Deploy mods before launching"))
        self._deploy_check.setChecked(bool(deploy))
        v.addWidget(self._deploy_check)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(False))
        bar.addWidget(cancel)
        save = QPushButton(self.tr("Save"))
        save.setObjectName("PrimaryButton")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(lambda: self._finish(True))
        bar.addWidget(save)
        v.addLayout(bar)

        self._present()

    @classmethod
    def show_over(cls, host, *, game_name, mode, deploy, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, game_name, mode, deploy, on_done)

    # -- internals ----------------------------------------------------------
    def _finish(self, saved: bool = False):
        """Override: on_done takes (mode, deploy) — (None, None) on cancel."""
        if self._done:
            return
        self._done = True
        self._host.removeEventFilter(self)
        cb = self._on_done
        mode = self._mode_combo.currentText().lower()
        deploy = self._deploy_check.isChecked()
        self.hide()
        self.deleteLater()
        if cb is not None:
            if saved:
                cb(mode, deploy)
            else:
                cb(None, None)
