"""Per-game LSFG-VK launch settings overlay."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c
from gui_qt.wheel_guard import no_wheel


class LsfgSettingsOverlay(OverlayBase):
    _dll_picked = Signal(object)
    _log_picked = Signal(object)
    _setup_finished = Signal(object)

    CARD_W = 540
    CARD_H = 680
    MIN_H = 360
    STEP_BUTTON_W = 24
    ESC_RESULT = None

    def __init__(self, host: QWidget, settings: dict, on_done,
                 game_name: str = ""):
        super().__init__(host, on_done=on_done)
        p = active_palette()
        self._values = dict(settings or {})
        self._game_name = game_name
        self._dll_picked.connect(self._on_dll_picked)
        self._log_picked.connect(self._on_log_picked)
        self._setup_finished.connect(self._on_setup_finished)

        _card, outer = self._make_card("LsfgSettingsCard")

        title = QLabel(self.tr("LSFG-VK Frame Generation"))
        title.setStyleSheet(
            f"color:{_c(p, 'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        outer.addWidget(title)

        hint = QLabel(self.tr(
            "Applies these settings when Amethyst launches the game. "
            "LSFG-VK and Lossless Scaling must already be installed. "
            "Multiplier, flow scale and performance mode update immediately "
            "in a running game; other changes apply on the next launch."))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')}; font-size:13px;")
        outer.addWidget(hint)

        setup_row = QHBoxLayout()
        setup_row.setContentsMargins(0, 0, 0, 0)
        self._setup_status = QLabel()
        self._setup_status.setStyleSheet(
            f"color:{_c(p, 'TEXT_DIM')}; font-size:12px;")
        setup_row.addWidget(self._setup_status, 1)
        self._setup_button = QPushButton()
        self._setup_button.setObjectName("FormButton")
        self._setup_button.setCursor(Qt.PointingHandCursor)
        self._setup_button.clicked.connect(self._confirm_setup)
        setup_row.addWidget(self._setup_button)
        outer.addLayout(setup_row)
        self._sync_setup_status()

        scroll = QScrollArea()
        scroll.setObjectName("LsfgSettingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            "#LsfgSettingsScroll { background: transparent; border: none; }")
        scroll.viewport().setStyleSheet("background: transparent;")
        body = QWidget()
        body.setObjectName("LsfgSettingsBody")
        body.setStyleSheet(f"""
            #LsfgSettingsBody {{ background: transparent; }}
            QSlider::groove:horizontal {{
                height: 4px; background: {_c(p, 'BG_DEEP')}; border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {_c(p, 'ACCENT')}; width: 14px; margin: -6px 0;
                border-radius: 7px;
            }}
            QSlider::sub-page:horizontal {{
                background: {_c(p, 'ACCENT')}; border-radius: 2px;
            }}
            QPushButton#StepButton {{
                background: {_c(p, 'BG_HEADER')};
                border: 1px solid {_c(p, 'BORDER')};
                border-radius: 4px;
                color: {_c(p, 'TEXT_MAIN')};
                font-weight: 600;
                padding: 0;
            }}
            QPushButton#StepButton:hover {{
                border-color: {_c(p, 'ACCENT')};
                color: {_c(p, 'ACCENT_HOV')};
            }}
            QPushButton#StepButton:pressed {{ background: {_c(p, 'BG_DEEP')}; }}
            QPushButton#StepButton:disabled {{
                background: transparent;
                border-color: {_c(p, 'BORDER')};
                color: {_c(p, 'BORDER')};
            }}
        """)
        body_v = QVBoxLayout(body)
        body_v.setContentsMargins(0, 0, 8, 0)
        body_v.setSpacing(10)

        self._enabled = QCheckBox(self.tr("Enable LSFG-VK for this game"))
        self._enabled.setChecked(bool(self._values.get("enabled", False)))
        body_v.addWidget(self._enabled)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        dll_path = str(self._values.get("dll_path", ""))
        if not dll_path:
            from Utils.executables.launch import detect_lsfg_dll
            dll_path = detect_lsfg_dll()
        self._dll_path = QLineEdit(dll_path)
        self._dll_path.setToolTip(dll_path)
        self._dll_path.textChanged.connect(self._dll_path.setToolTip)
        self._dll_path.setPlaceholderText(self.tr(
            "Optional path to lsfg-vk.dll or Lossless.dll"))
        dll_row = QHBoxLayout()
        dll_row.setContentsMargins(0, 0, 0, 0)
        dll_row.addWidget(self._dll_path, 1)
        dll_browse = QPushButton(self.tr("Browse…"))
        dll_browse.setObjectName("FormButton")
        dll_browse.clicked.connect(self._browse_dll)
        dll_row.addWidget(dll_browse)
        form.addRow(self.tr("DLL location"), dll_row)

        multiplier_label = self.tr("Multiplier")
        multiplier_tip = self.tr(
            "Output-frame multiplier. 1 temporarily disables generation.")
        self._multiplier, multiplier_row, multiplier_controls = \
            self._slider_row(
                multiplier_label, 1, 20,
                int(self._values.get("multiplier", 2)), 1, str,
                multiplier_tip)
        form.addRow(multiplier_label, multiplier_row)

        flow_label = self.tr("Flow scale")
        flow_tip = self.tr(
            "Lower values improve performance at the cost of quality.")
        flow_value = round(float(self._values.get("flow_scale", 1.0)) * 100)
        self._flow_scale, flow_row, flow_controls = self._slider_row(
            flow_label, 25, 100, flow_value, 5,
            lambda value: f"{value / 100:.2f}", flow_tip)
        form.addRow(flow_label, flow_row)
        self._step_pairs = [
            (self._multiplier, multiplier_controls[1], multiplier_controls[2]),
            (self._flow_scale, flow_controls[1], flow_controls[2]),
        ]

        self._pacing = QComboBox()
        self._pacing.addItem(self.tr("VSync"), "vsync")
        pacing = str(self._values.get("pacing_mode", "vsync"))
        index = self._pacing.findData(pacing)
        self._pacing.setCurrentIndex(max(0, index))
        no_wheel(self._pacing)
        form.addRow(self.tr("Pacing mode"), self._pacing)

        self._legacy_present = QComboBox()
        self._legacy_present.addItem(self.tr("VSync/FIFO (Default)"), "fifo")
        self._legacy_present.addItem(self.tr("Mailbox"), "mailbox")
        self._legacy_present.addItem(self.tr("Immediate"), "immediate")
        present = str(self._values.get("legacy_present_mode", "fifo"))
        index = self._legacy_present.findData(present)
        self._legacy_present.setCurrentIndex(max(0, index))
        self._legacy_present.setToolTip(self.tr(
            "Compatibility setting for LSFG-VK 1.x installations."))
        no_wheel(self._legacy_present)
        form.addRow(self.tr("Legacy present mode"), self._legacy_present)

        body_v.addLayout(form)

        self._performance = QCheckBox(self.tr("Performance mode"))
        self._performance.setChecked(bool(
            self._values.get("performance_mode", False)))
        self._performance.setToolTip(self.tr(
            "Uses a faster model with a small quality reduction."))
        body_v.addWidget(self._performance)

        self._live_status = QLabel()
        self._live_status.setWordWrap(True)
        self._live_status.setStyleSheet(
            f"color:{_c(p, 'TEXT_DIM')}; font-size:12px;")
        self._live_status.hide()
        body_v.addWidget(self._live_status)

        self._allow_fp16 = QCheckBox(self.tr("Allow half-precision (FP16)"))
        self._allow_fp16.setChecked(bool(
            self._values.get("allow_fp16", True)))
        self._allow_fp16.setToolTip(self.tr(
            "Recommended for AMD GPUs. Older NVIDIA GPUs may be slower."))
        body_v.addWidget(self._allow_fp16)

        self._override_present = QCheckBox(self.tr(
            "Override present mode for frame pacing"))
        self._override_present.setChecked(bool(
            self._values.get("override_present_mode", True)))
        body_v.addWidget(self._override_present)

        self._preserve_images = QCheckBox(self.tr(
            "Preserve swapchain image count"))
        self._preserve_images.setChecked(bool(
            self._values.get("preserve_swapchain_image_count", False)))
        self._preserve_images.setToolTip(self.tr(
            "May prevent crashes in some Vulkan games, but can cause stutter."))
        body_v.addWidget(self._preserve_images)

        self._legacy_hdr = QCheckBox(self.tr("HDR mode (LSFG-VK 1.x)"))
        self._legacy_hdr.setChecked(bool(
            self._values.get("legacy_hdr_mode", False)))
        body_v.addWidget(self._legacy_hdr)

        advanced = QLabel(self.tr("Logging"))
        advanced.setStyleSheet(
            f"color:{_c(p, 'TEXT_MAIN')}; font-weight:600; margin-top:6px;")
        body_v.addWidget(advanced)

        log_form = QFormLayout()
        log_form.setContentsMargins(0, 0, 0, 0)
        log_form.setHorizontalSpacing(10)
        log_form.setVerticalSpacing(8)

        self._log_level = QComboBox()
        for value, label in (
                ("error", self.tr("Error")),
                ("warning", self.tr("Warning")),
                ("info", self.tr("Info")),
                ("debug", self.tr("Debug"))):
            self._log_level.addItem(label, value)
        level = str(self._values.get("log_level", "info"))
        index = self._log_level.findData(level)
        self._log_level.setCurrentIndex(max(0, index))
        no_wheel(self._log_level)
        log_form.addRow(self.tr("Log level"), self._log_level)

        self._log_file = QLineEdit(str(self._values.get("log_file", "")))
        self._log_file.setPlaceholderText(self.tr("Optional LSFG-VK log file"))
        log_row = QHBoxLayout()
        log_row.setContentsMargins(0, 0, 0, 0)
        log_row.addWidget(self._log_file, 1)
        log_browse = QPushButton(self.tr("Browse…"))
        log_browse.setObjectName("FormButton")
        log_browse.clicked.connect(self._browse_log)
        log_row.addWidget(log_browse)
        log_form.addRow(self.tr("Log file"), log_row)
        body_v.addLayout(log_form)
        body_v.addStretch(1)

        self._controlled = [
            self._dll_path, dll_browse, *multiplier_controls, *flow_controls,
            self._pacing, self._legacy_present, self._performance,
            self._allow_fp16, self._override_present, self._preserve_images,
            self._legacy_hdr, self._log_level, self._log_file, log_browse,
        ]
        self._enabled.toggled.connect(self._sync_enabled)
        self._sync_enabled(self._enabled.isChecked())

        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(80)
        self._live_timer.timeout.connect(self._write_live_config)
        self._multiplier.valueChanged.connect(self._schedule_live_update)
        self._flow_scale.valueChanged.connect(self._schedule_live_update)
        self._performance.toggled.connect(self._schedule_live_update)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        buttons.addWidget(cancel)
        save = QPushButton(self.tr("OK"))
        save.setObjectName("PrimaryButton")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(self._accept)
        buttons.addWidget(save)
        outer.addLayout(buttons)

        self._present()

    @classmethod
    def show_over(cls, host, *, settings, on_done, game_name=""):
        top = host.window() if host is not None else None
        return cls(top or host, settings, on_done, game_name)

    def _sync_enabled(self, enabled: bool):
        for widget in self._controlled:
            widget.setEnabled(enabled)
        for slider, minus, plus in self._step_pairs:
            minus.setEnabled(enabled and slider.value() > slider.minimum())
            plus.setEnabled(enabled and slider.value() < slider.maximum())

    def _sync_setup_status(self):
        from pathlib import Path
        from Utils.executables.lsfg import installed_prefix
        prefix = installed_prefix()
        manually_managed = prefix in (None, Path.home() / ".local")
        if prefix is None:
            self._setup_status.setText(self.tr("LSFG-VK is not installed."))
            self._setup_button.setText(self.tr("Set up LSFG-VK"))
        elif manually_managed:
            self._setup_status.setText(self.tr("LSFG-VK is installed."))
            self._setup_button.setText(self.tr("Update LSFG-VK"))
        else:
            self._setup_status.setText(self.tr(
                "LSFG-VK is managed by the system package manager."))
            self._setup_button.setText(self.tr("System managed"))
        self._setup_button.setEnabled(manually_managed)

    def _confirm_setup(self):
        from gui_qt.confirm_overlay import ConfirmOverlay
        self._card.setEnabled(False)

        def done(confirmed):
            self._card.setEnabled(True)
            self.raise_()
            if confirmed:
                self._start_setup()

        ConfirmOverlay.show_over(
            self._host,
            self.tr("Set up LSFG-VK"),
            self.tr(
                "Download the latest stable LSFG-VK release from the official "
                "build server and install its Vulkan layer into ~/.local? "
                "Lossless Scaling must also use its lsfg-vk Steam branch."),
            done, confirm_label=self.tr("Set up"), danger=False)

    def _start_setup(self):
        from Utils.app_log import app_log
        from Utils.executables.lsfg import install_latest
        from gui_qt.worker import run_in_worker

        self._setup_button.setEnabled(False)
        self._setup_status.setText(self.tr("Setting up LSFG-VK…"))
        run_in_worker(
            lambda: install_latest(app_log), self._setup_finished,
            name="lsfg-vk-setup", error_result=(False, self.tr("Unknown error")))

    def _on_setup_finished(self, result):
        self._setup_button.setEnabled(True)
        success, detail = result or (False, self.tr("Unknown error"))
        if success:
            self._setup_status.setText(
                self.tr("LSFG-VK {0} installed.").format(detail))
            self._setup_button.setText(self.tr("Update LSFG-VK"))
        else:
            self._setup_status.setText(
                self.tr("LSFG-VK setup failed: {0}").format(detail))

    def _slider_row(self, label: str, minimum: int, maximum: int,
                    value: int, step: int, formatter, tooltip: str):
        slider = QSlider(Qt.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setSingleStep(step)
        slider.setValue(max(minimum, min(maximum, value)))
        slider.setMinimumWidth(120)
        slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        slider.setToolTip(tooltip)
        no_wheel(slider)

        readout = QLabel(formatter(slider.value()))
        readout.setFixedWidth(42)
        readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        readout.setToolTip(tooltip)
        slider.valueChanged.connect(
            lambda current: readout.setText(formatter(current)))

        minus = self._step_button(
            slider, -1, self.tr("Decrease {0}").format(label))
        plus = self._step_button(
            slider, 1, self.tr("Increase {0}").format(label))

        def sync_buttons(current: int):
            minus.setEnabled(current > slider.minimum())
            plus.setEnabled(current < slider.maximum())

        slider.valueChanged.connect(sync_buttons)
        sync_buttons(slider.value())

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(minus)
        row.addWidget(slider, 1)
        row.addWidget(plus)
        row.addWidget(readout)
        return slider, row, (slider, minus, plus, readout)

    def _step_button(self, slider: QSlider, direction: int,
                     tooltip: str) -> QPushButton:
        button = QPushButton("−" if direction < 0 else "+")
        button.setObjectName("StepButton")
        button.setFixedSize(self.STEP_BUTTON_W, self.STEP_BUTTON_W)
        button.setToolTip(tooltip)
        button.setAutoRepeat(True)
        button.setAutoRepeatDelay(400)
        button.setAutoRepeatInterval(90)
        button.setFocusPolicy(Qt.NoFocus)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(lambda: slider.setValue(
            slider.value() + direction * slider.singleStep()))
        return button

    def _browse_dll(self):
        from Utils.ui.portal import pick_file
        from gui_qt.safe_emit import safe_emit
        pick_file(
            self.tr("Select the LSFG-VK DLL"),
            lambda path: safe_emit(self._dll_picked, path),
            filters=[(self.tr("DLL files"), ["*.dll"]),
                     (self.tr("All files"), ["*"])])

    def _on_dll_picked(self, path):
        if path is not None:
            self._dll_path.setText(str(path))

    def _browse_log(self):
        from Utils.ui.portal import pick_save_file
        from gui_qt.safe_emit import safe_emit
        pick_save_file(
            self.tr("Select the LSFG-VK log file"),
            lambda path: safe_emit(self._log_picked, path),
            current_name=self._log_file.text().strip() or "lsfg-vk.log",
            filters=[(self.tr("Log files"), ["*.log", "*.txt"]),
                     (self.tr("All files"), ["*"])])

    def _on_log_picked(self, path):
        if path is not None:
            self._log_file.setText(str(path))

    def _current_settings(self) -> dict:
        return {
            "enabled": self._enabled.isChecked(),
            "dll_path": self._dll_path.text().strip(),
            "allow_fp16": self._allow_fp16.isChecked(),
            "multiplier": self._multiplier.value(),
            "flow_scale": self._flow_scale.value() / 100,
            "performance_mode": self._performance.isChecked(),
            "pacing_mode": self._pacing.currentData(),
            "override_present_mode": self._override_present.isChecked(),
            "preserve_swapchain_image_count": self._preserve_images.isChecked(),
            "log_level": self._log_level.currentData(),
            "log_file": self._log_file.text().strip(),
            "legacy_hdr_mode": self._legacy_hdr.isChecked(),
            "legacy_present_mode": self._legacy_present.currentData(),
        }

    def _schedule_live_update(self, *_args):
        if self._game_name:
            self._live_timer.start()

    def _write_live_config(self, settings=None):
        if not self._game_name:
            return
        from Utils.executables.launch import lsfg_config_path, write_lsfg_config
        if not lsfg_config_path(self._game_name).is_file():
            return
        try:
            write_lsfg_config(
                self._game_name,
                settings if settings is not None else self._current_settings())
        except OSError as exc:
            self._live_status.setText(self.tr(
                "Could not update the live LSFG-VK settings: {0}").format(exc))
            self._live_status.show()

    def _accept(self):
        self._finish(self._current_settings())

    def _finish(self, result=None):
        if self._done:
            return
        if hasattr(self, "_live_timer"):
            self._live_timer.stop()
            self._write_live_config(
                self._values if result is None else result)
        super()._finish(result)
