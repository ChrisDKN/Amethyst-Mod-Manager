"""Per-game MangoHud launch settings overlay."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QWidget,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c
from gui_qt.wheel_guard import no_wheel


class MangohudSettingsOverlay(OverlayBase):
    CARD_W = 520
    CARD_H = 390
    MIN_H = 350

    def __init__(self, host: QWidget, settings: dict, on_done):
        super().__init__(host, on_done=on_done)
        p = active_palette()
        values = dict(settings or {})
        _card, layout = self._make_card("MangohudSettingsCard")

        title = QLabel(self.tr("MangoHud controls"))
        title.setStyleSheet(
            f"color:{_c(p, 'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        layout.addWidget(title)

        hint = QLabel(self.tr(
            "MangoHud must be installed. These Vulkan overlay settings apply "
            "the next time Amethyst launches this game. For OpenGL, add "
            "mangohud %command% to Launch Options."))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')}; font-size:13px;")
        layout.addWidget(hint)

        self._enabled = QCheckBox(self.tr("Enable MangoHud for this game"))
        self._enabled.setChecked(bool(values.get("enabled", False)))
        layout.addWidget(self._enabled)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self._display = QComboBox()
        for label, key in (
                (self.tr("Use MangoHud config"), "default"),
                (self.tr("FPS only"), "fps_only"),
                (self.tr("Full"), "full")):
            self._display.addItem(label, key)
        self._display.setCurrentIndex(max(
            0, self._display.findData(values.get("display", "default"))))
        no_wheel(self._display)
        form.addRow(self.tr("Display"), self._display)

        self._position = QComboBox()
        for label, key in (
                (self.tr("Use MangoHud config"), "default"),
                (self.tr("Top left"), "top-left"),
                (self.tr("Top right"), "top-right"),
                (self.tr("Middle left"), "middle-left"),
                (self.tr("Middle right"), "middle-right"),
                (self.tr("Bottom left"), "bottom-left"),
                (self.tr("Bottom right"), "bottom-right"),
                (self.tr("Top center"), "top-center"),
                (self.tr("Bottom center"), "bottom-center")):
            self._position.addItem(label, key)
        self._position.setCurrentIndex(max(
            0, self._position.findData(values.get("position", "default"))))
        no_wheel(self._position)
        form.addRow(self.tr("Position"), self._position)

        self._fps_limit = QSpinBox()
        self._fps_limit.setRange(0, 1000)
        self._fps_limit.setSpecialValueText(self.tr("Use MangoHud config"))
        self._fps_limit.setSuffix(" FPS")
        self._fps_limit.setValue(int(values.get("fps_limit", 0)))
        no_wheel(self._fps_limit)
        form.addRow(self.tr("FPS limit"), self._fps_limit)

        self._extra_options = QLineEdit(
            str(values.get("extra_options", "") or ""))
        self._extra_options.setPlaceholderText(
            self.tr("e.g. gpu_temp,cpu_temp,font_size=24"))
        form.addRow(self.tr("Extra options"), self._extra_options)
        layout.addLayout(form)

        options_hint = QLabel(self.tr(
            "Options use MANGOHUD_CONFIG syntax and take priority over the "
            "controls above. Unchanged controls use your MangoHud config."))
        options_hint.setWordWrap(True)
        options_hint.setStyleSheet(
            f"color:{_c(p, 'TEXT_DIM')}; font-size:13px;")
        layout.addWidget(options_hint)

        self._enabled.toggled.connect(self._sync_controls)
        self._sync_controls(self._enabled.isChecked())

        layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        buttons.addWidget(cancel)
        save = QPushButton(self.tr("Save"))
        save.setObjectName("PrimaryButton")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(self._accept)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self._present()

    @classmethod
    def show_over(cls, host, *, settings, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, settings, on_done)

    def _sync_controls(self, enabled: bool):
        for widget in (self._display, self._position, self._fps_limit,
                       self._extra_options):
            widget.setEnabled(enabled)

    def _accept(self):
        self._finish({
            "enabled": self._enabled.isChecked(),
            "display": self._display.currentData(),
            "position": self._position.currentData(),
            "fps_limit": self._fps_limit.value(),
            "extra_options": self._extra_options.text().strip(),
        })
