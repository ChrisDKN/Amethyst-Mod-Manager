"""Borderless in-window overlay for the game-launch settings.

Qt port of the game-exe branch of Tk's ExeConfigPanel: a "Launch via"
selector (Auto / Steam / Heroic / Lutris / Faugus / None), launch arguments,
Steam-style launch options, Wayland, LSFG-VK, MangoHud and deploy controls,
plus any game-specific checkboxes the handler declares (BaseGame.launch_toggles
- e.g. OpenMW's "skip the launcher").
``on_done(mode, deploy, args, options, wayland, lsfg, mangohud, toggles)`` fires on Save,
all-None on Cancel/Esc; *toggles* is {key: bool} and empty for a game that
declares none.

Dimmed child backdrop + centered card via gui_qt/overlay_base.py.
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QComboBox, QCheckBox, QLineEdit,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c
from gui_qt.wheel_guard import no_wheel

_MODES = ["Auto", "Steam", "Heroic", "Lutris", "Faugus", "None"]


class LauncherSettingsOverlay(OverlayBase):
    CARD_W = 520
    CARD_H = 460
    MIN_H = 340
    ESC_RESULT = False

    def __init__(self, host: QWidget, game_name: str, mode: str, deploy: bool,
                 args: str, options: str, wayland: bool, lsfg: dict,
                 mangohud: dict, on_done,
                 toggles=None, toggle_values=None):
        # Each extra checkbox (plus its optional hint) needs room, or the card
        # clips them - OverlayBase gives the card a FIXED size.
        toggles = list(toggles or [])
        extra_h = sum(30 + (34 if t.hint else 0) for t in toggles)
        super().__init__(host, on_done=on_done,
                         card_h=self.CARD_H + extra_h if toggles else None)
        p = active_palette()
        self._game_name = game_name

        _card, v = self._make_card("ConfirmCard")

        title_lbl = QLabel(self.tr("Launch settings - {0}").format(game_name))
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

        args_lbl = QLabel(self.tr("Launch arguments"))
        args_lbl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        v.addWidget(args_lbl)
        self._args_edit = QLineEdit(args or "")
        self._args_edit.setPlaceholderText(self.tr("Arguments passed to the game exe"))
        v.addWidget(self._args_edit)

        opts_lbl = QLabel(self.tr("Launch Options"))
        opts_lbl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        v.addWidget(opts_lbl)
        self._options_edit = QLineEdit(options or "")
        self._options_edit.setPlaceholderText(
            self.tr("e.g. SteamDeck=0 gamemoderun %command%"))
        v.addWidget(self._options_edit)
        opts_hint = QLabel(self.tr(
            "Steam syntax. When set, the manager launches the game directly "
            "so these options apply. Empty: the game's own launcher options "
            "are used."))
        opts_hint.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        opts_hint.setWordWrap(True)
        v.addWidget(opts_hint)

        launch_row = QHBoxLayout()
        launch_row.setContentsMargins(0, 0, 0, 0)
        self._wayland_check = QCheckBox(self.tr("Launch with wayland"))
        self._wayland_check.setChecked(bool(wayland))
        launch_row.addWidget(self._wayland_check)
        launch_row.addStretch(1)
        self._lsfg_settings = dict(lsfg or {})
        self._original_lsfg_settings = dict(self._lsfg_settings)
        self._lsfg_preview_changed = False
        self._lsfg_button = QPushButton()
        self._lsfg_button.setObjectName("FormButton")
        self._lsfg_button.setCursor(Qt.PointingHandCursor)
        self._lsfg_button.clicked.connect(self._open_lsfg_settings)
        launch_row.addWidget(self._lsfg_button)
        v.addLayout(launch_row)
        self._sync_lsfg_button()

        mangohud_row = QHBoxLayout()
        mangohud_row.setContentsMargins(0, 0, 0, 0)
        self._deploy_check = QCheckBox(self.tr("Deploy mods before launching"))
        self._deploy_check.setChecked(bool(deploy))
        mangohud_row.addWidget(self._deploy_check)
        mangohud_row.addStretch(1)
        self._mangohud_settings = dict(mangohud or {})
        self._mangohud_button = QPushButton()
        self._mangohud_button.setObjectName("FormButton")
        self._mangohud_button.setCursor(Qt.PointingHandCursor)
        self._mangohud_button.clicked.connect(self._open_mangohud_settings)
        mangohud_row.addWidget(self._mangohud_button)
        v.addLayout(mangohud_row)
        self._sync_mangohud_button()

        # Handler-declared checkboxes. Labels/hints come from the game module,
        # which has no Qt to translate with, so they stay canonical English
        # there and are translated here, at display. The literals are extracted
        # from gui_qt/wizard_tr_markers.py (LAUNCH_TOGGLES) under the shared
        # "WizardTools" context, so the lookup must name that context too.
        values = dict(toggle_values or {})
        self._toggle_checks: dict = {}
        for t in toggles:
            box = QCheckBox(QCoreApplication.translate("WizardTools", t.label))
            box.setChecked(bool(values.get(t.key, t.default)))
            v.addWidget(box)
            if t.hint:
                t_hint = QLabel(
                    QCoreApplication.translate("WizardTools", t.hint))
                t_hint.setStyleSheet(
                    f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
                t_hint.setWordWrap(True)
                t_hint.setIndent(22)
                v.addWidget(t_hint)
            self._toggle_checks[t.key] = box
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
    def show_over(cls, host, *, game_name, mode, deploy, args, options,
                  wayland, lsfg, mangohud, on_done, toggles=None,
                  toggle_values=None):
        top = host.window() if host is not None else None
        return cls(top or host, game_name, mode, deploy, args, options,
                   wayland, lsfg, mangohud, on_done, toggles=toggles,
                   toggle_values=toggle_values)

    # -- internals ----------------------------------------------------------
    def _sync_lsfg_button(self):
        state = self.tr("Enabled") if self._lsfg_settings.get("enabled") \
            else self.tr("Disabled")
        self._lsfg_button.setText(self.tr("LSFG-VK: {0}").format(state))

    def _open_lsfg_settings(self):
        from gui_qt.lsfg_settings_overlay import LsfgSettingsOverlay
        self._card.setEnabled(False)

        def _done(settings):
            self._card.setEnabled(True)
            self.raise_()
            if settings is not None:
                self._lsfg_preview_changed = (
                    self._lsfg_preview_changed
                    or settings != self._lsfg_settings)
                self._lsfg_settings = settings
                self._sync_lsfg_button()

        LsfgSettingsOverlay.show_over(
            self._host, settings=self._lsfg_settings, on_done=_done,
            game_name=self._game_name)

    def _sync_mangohud_button(self):
        state = self.tr("Enabled") if self._mangohud_settings.get("enabled") \
            else self.tr("Disabled")
        self._mangohud_button.setText(self.tr("MangoHud: {0}").format(state))

    def _open_mangohud_settings(self):
        from gui_qt.mangohud_settings_overlay import MangohudSettingsOverlay
        self._card.setEnabled(False)

        def _done(settings):
            self._card.setEnabled(True)
            self.raise_()
            if settings is not None:
                self._mangohud_settings = settings
                self._sync_mangohud_button()

        MangohudSettingsOverlay.show_over(
            self._host, settings=self._mangohud_settings, on_done=_done)

    def _finish(self, saved: bool = False):
        """Return every launch setting on Save, or all None on cancellation."""
        if self._done:
            return
        if not saved and self._lsfg_preview_changed:
            try:
                from Utils.executables.launch import (
                    lsfg_config_path, write_lsfg_config)
                if lsfg_config_path(self._game_name).is_file():
                    write_lsfg_config(
                        self._game_name, self._original_lsfg_settings)
            except OSError:
                pass
        self._done = True
        self._host.removeEventFilter(self)
        cb = self._on_done
        mode = self._mode_combo.currentText().lower()
        deploy = self._deploy_check.isChecked()
        args = self._args_edit.text().strip()
        options = self._options_edit.text().strip()
        wayland = self._wayland_check.isChecked()
        lsfg = dict(self._lsfg_settings)
        mangohud = dict(self._mangohud_settings)
        toggles = {k: b.isChecked() for k, b in self._toggle_checks.items()}
        self.hide()
        self.deleteLater()
        if cb is not None:
            if saved:
                cb(mode, deploy, args, options, wayland, lsfg, mangohud,
                   toggles)
            else:
                cb(None, None, None, None, None, None, None, None)
