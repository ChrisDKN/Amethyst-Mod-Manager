"""Per-game frame generation settings."""

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
    _maintenance_finished = Signal(object)

    CARD_W = 540
    CARD_H = 680
    MIN_H = 360
    STEP_BUTTON_W = 24
    ESC_RESULT = None

    def __init__(self, host: QWidget, settings: dict, on_done,
                 game_name: str = ""):
        super().__init__(host, on_done=on_done)
        p = active_palette()
        from Utils.executables.launch import _normalize_lsfg_settings
        self._values = _normalize_lsfg_settings(settings)
        self._live_backend = self._values["backend"]
        self._last_backend = self._values["backend"]
        self._dll_values = {"lsfg": self._values["dll_path"],
                            "mako": self._values["mako_dll_path"]}
        self._game_name = game_name
        self._dll_picked.connect(self._on_dll_picked)
        self._log_picked.connect(self._on_log_picked)
        self._setup_finished.connect(self._on_setup_finished)
        self._maintenance_finished.connect(self._on_maintenance_finished)

        _card, outer = self._make_card("LsfgSettingsCard")

        title = QLabel(self.tr("LSFG / MAKO Frame Generation"))
        title.setStyleSheet(
            f"color:{_c(p, 'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        outer.addWidget(title)

        hint = QLabel(self.tr(
            "Settings apply when Amethyst launches the game. Backend, DLL and "
            "compatibility changes require a restart; generation controls can "
            "update during gameplay."))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')}; font-size:13px;")
        outer.addWidget(hint)
        self._restart_status = QLabel()
        self._restart_status.setWordWrap(True)
        self._restart_status.setStyleSheet(f"color:{_c(p, 'TEXT_DIM')}; font-size:12px;")
        self._restart_status.hide()
        outer.addWidget(self._restart_status)

        self._backend = QComboBox()
        self._backend.addItem("LSFG-VK", "lsfg")
        self._backend.addItem("MAKO", "mako")
        self._backend.setCurrentIndex(1 if self._values["backend"] == "mako" else 0)
        no_wheel(self._backend)
        backend_form = QFormLayout()
        backend_form.addRow(self.tr("Backend (restart)"), self._backend)
        outer.addLayout(backend_form)

        setup_row = QHBoxLayout()
        setup_row.setContentsMargins(0, 0, 0, 0)
        self._setup_status = QLabel()
        self._setup_status.setWordWrap(True)
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

        self._enabled = QCheckBox(self.tr("Use selected backend on next launch"))
        self._enabled.setChecked(bool(self._values.get("enabled", False)))
        body_v.addWidget(self._enabled)

        self._maintenance = QWidget()
        maintenance_layout = QVBoxLayout(self._maintenance)
        maintenance_layout.setContentsMargins(0, 0, 0, 0)
        maintenance_buttons = QHBoxLayout()
        self._check_button = QPushButton(self.tr("Check setup"))
        self._updates_button = QPushButton(self.tr("Check updates"))
        self._rollback_button = QPushButton(self.tr("Roll back"))
        for button, action in ((self._check_button, "check"),
                               (self._updates_button, "updates"),
                               (self._rollback_button, "rollback")):
            button.setObjectName("FormButton")
            button.clicked.connect(lambda _checked=False, action=action: self._start_maintenance(action))
            maintenance_buttons.addWidget(button)
        maintenance_layout.addLayout(maintenance_buttons)
        self._maintenance_status = QLabel()
        self._maintenance_status.setWordWrap(True)
        self._maintenance_status.setTextFormat(Qt.PlainText)
        self._maintenance_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._maintenance_status.hide()
        maintenance_layout.addWidget(self._maintenance_status)
        body_v.addWidget(self._maintenance)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        dll_path = self._dll_values[self._last_backend]
        if not dll_path:
            from Utils.executables.launch import detect_lsfg_dll
            dll_path = detect_lsfg_dll(mako=self._last_backend == "mako")
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
        form.addRow(self.tr("DLL location (restart)"), dll_row)

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
        self._form = form
        self._lsfg_rows = (multiplier_row, self._pacing, self._legacy_present)

        self._mako_box = QWidget()
        mako_form = QFormLayout(self._mako_box)
        mako_form.setContentsMargins(0, 0, 0, 0)
        mako_hint = QLabel(self.tr(
            "Requires Lossless Scaling's default Steam branch. This integration "
            "supports host games in SDR, including the MangoHud controls. Launcher Flatpaks "
            "need MAKO's separate Flatpak setup."))
        mako_hint.setWordWrap(True)
        mako_form.addRow(mako_hint)
        self._mako_generation = QCheckBox(self.tr("Frame generation (live)"))
        self._mako_generation.setChecked(self._values["mako_frame_generation"])
        mako_form.addRow(self._mako_generation)
        self._mako_adaptive = QCheckBox(self.tr("Adaptive frame generation"))
        self._mako_adaptive.setToolTip(self.tr(
            "Aims for the target FPS within the selected multiplier ceiling "
            "and available GPU performance."))
        self._mako_adaptive.setChecked(self._values["mako_adaptive"])
        mako_form.addRow(self._mako_adaptive)
        self._mako_multiplier = QComboBox()
        self._mako_max = QComboBox()
        for value in range(2, 6):
            self._mako_multiplier.addItem(f"{value}×", value)
            self._mako_max.addItem(f"{value}×", value)
        self._mako_multiplier.setCurrentIndex(self._values["mako_multiplier"] - 2)
        self._mako_max.setCurrentIndex(self._values["mako_max_multiplier"] - 2)
        no_wheel(self._mako_multiplier)
        no_wheel(self._mako_max)
        mako_form.addRow(self.tr("Fixed multiplier"), self._mako_multiplier)
        self._mako_target, target_row, target_controls = self._slider_row(
            self.tr("Target FPS"), 30, 240, self._values["mako_target_fps"], 5,
            str, self.tr("Desired output FPS in Adaptive mode."))
        mako_form.addRow(self.tr("Target FPS"), target_row)
        mako_form.addRow(self.tr("Maximum multiplier"), self._mako_max)
        self._mako_steady = QCheckBox(self.tr("Steady base cap"))
        self._mako_steady.setChecked(self._values["mako_steady_base"])
        self._mako_steady.setToolTip(self.tr(
            "Starts with a real-frame cap at half the target for an even cadence. "
            "Turn off to allow fractional adaptive generation."))
        mako_form.addRow(self._mako_steady)
        self._mako_fractional = QCheckBox(self.tr("Fractional Adaptive"))
        self._mako_fractional.setChecked(not self._values["mako_steady_base"])
        self._mako_fractional.setToolTip(self.tr(
            "Keeps a changing mix of real and generated frames. Disables Steady Base Cap."))
        mako_form.addRow(self._mako_fractional)
        self._mako_priority = QComboBox()
        for value, label in (("auto", self.tr("Automatic")), ("low", self.tr("Low")),
                             ("medium", self.tr("Medium")), ("high", self.tr("High")),
                             ("very-high", self.tr("Very high"))):
            self._mako_priority.addItem(label, value)
        self._mako_priority.setCurrentIndex(
            self._mako_priority.findData(self._values["mako_real_frame_priority"]))
        self._mako_priority.setToolTip(self.tr(
            "Higher priority allows more real frames and may improve responsiveness, "
            "at the cost of less even pacing. Explicit priorities replace the manual base cap."))
        no_wheel(self._mako_priority)
        mako_form.addRow(self.tr("Real frame priority"), self._mako_priority)
        self._mako_base_cap, base_row, base_controls = self._slider_row(
            self.tr("Base FPS cap"), 0, 120, self._values["mako_base_fps_cap"], 1,
            lambda value: str(value) if value else self.tr("Off"),
            self.tr("Limits real frames while generation is active. 0 disables the cap."))
        mako_form.addRow(self.tr("Base FPS cap"), base_row)
        self._mako_base_row = base_row
        self._mako_smooth = QCheckBox(self.tr("Smooth cadence"))
        self._mako_smooth.setChecked(self._values["mako_smooth_cadence"])
        mako_form.addRow(self._mako_smooth)
        self._mako_recovery = QCheckBox(self.tr("Dynamic Cadence Recovery"))
        self._mako_recovery.setChecked(self._values["mako_cadence_recovery"])
        self._mako_recovery.setToolTip(self.tr(
            "Rechecks native FPS when gameplay and menus run at different rates. "
            "Clears MAKO's real-frame caps and uses Automatic real frame priority."))
        mako_form.addRow(self._mako_recovery)
        self._mako_probe, probe_row, probe_controls = self._slider_row(
            self.tr("Recovery interval"), 1, 30, round(self._values["mako_probe_interval"] * 10), 1,
            lambda value: self.tr("{0} s").format(f"{value / 10:.1f}"),
            self.tr("Time between native cadence checks, from 0.1 to 3 seconds."))
        mako_form.addRow(self.tr("Recovery interval"), probe_row)
        self._mako_probe_row = probe_row
        self._recovery_hint = QLabel(self.tr(
            "Recovery clears Steady Base Cap, real frame priority and the manual base cap. "
            "Check any separate in-game, MangoHud or Gamescope FPS limit too."))
        self._recovery_hint.setWordWrap(True)
        mako_form.addRow(self._recovery_hint)
        self._mako_form = mako_form
        self._mako_target_row = target_row
        self._step_pairs.append((self._mako_target, target_controls[1], target_controls[2]))
        self._step_pairs.append((self._mako_base_cap, base_controls[1], base_controls[2]))
        self._step_pairs.append((self._mako_probe, probe_controls[1], probe_controls[2]))
        body_v.addWidget(self._mako_box)

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

        self._allow_fp16 = QCheckBox(self.tr("Allow half-precision (FP16, restart)"))
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
            "Preserve swapchain image count (restart)"))
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
        self._log_form = log_form
        self._lsfg_only = (self._override_present, self._legacy_hdr, advanced)
        body_v.addStretch(1)

        self._controlled = [
            self._dll_path, dll_browse, *multiplier_controls, *flow_controls,
            self._pacing, self._legacy_present, self._performance,
            self._allow_fp16, self._override_present, self._preserve_images,
            self._legacy_hdr, self._log_level, self._log_file, log_browse,
            self._mako_box,
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
        for widget in (self._mako_generation, self._mako_adaptive,
                       self._mako_steady, self._mako_smooth, self._mako_recovery):
            widget.toggled.connect(self._schedule_live_update)
        self._mako_target.valueChanged.connect(self._schedule_live_update)
        self._mako_base_cap.valueChanged.connect(self._schedule_live_update)
        self._mako_max.currentIndexChanged.connect(self._schedule_live_update)
        self._mako_multiplier.currentIndexChanged.connect(self._schedule_live_update)
        self._mako_priority.currentIndexChanged.connect(self._schedule_live_update)
        self._mako_probe.valueChanged.connect(self._schedule_live_update)
        self._mako_fractional.toggled.connect(lambda checked: self._mako_steady.setChecked(not checked))
        self._mako_steady.toggled.connect(lambda checked: self._mako_fractional.setChecked(not checked))
        self._mako_adaptive.toggled.connect(self._sync_mako_mode)
        self._mako_steady.toggled.connect(self._sync_mako_mode)
        self._mako_priority.currentIndexChanged.connect(self._sync_mako_mode)
        self._mako_recovery.toggled.connect(self._sync_mako_mode)
        self._backend.currentIndexChanged.connect(self._sync_backend)
        self._sync_backend()
        self._restart_baseline = self._current_settings()
        for widget in (self._enabled, self._allow_fp16, self._preserve_images):
            widget.toggled.connect(self._sync_restart_status)
        self._dll_path.textChanged.connect(self._sync_restart_status)
        self._backend.currentIndexChanged.connect(self._sync_restart_status)

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

    def _sync_mako_mode(self, *_args):
        adaptive = self._mako_adaptive.isChecked()
        recovery = self._mako_recovery.isChecked()
        if recovery:
            self._mako_steady.setChecked(False)
            self._mako_priority.setCurrentIndex(0)
            self._mako_base_cap.setValue(0)
        steady = self._mako_steady.isChecked()
        self._mako_form.setRowVisible(self._mako_multiplier, not adaptive)
        for field in (self._mako_target_row, self._mako_max, self._mako_steady,
                      self._mako_fractional):
            self._mako_form.setRowVisible(field, adaptive)
        self._mako_steady.setEnabled(not recovery)
        self._mako_fractional.setEnabled(not recovery)
        self._mako_form.setRowVisible(self._mako_priority, adaptive and not steady and not recovery)
        self._mako_form.setRowVisible(
            self._mako_base_row, not recovery and not (adaptive and (
                steady or self._mako_priority.currentData() != "auto")))
        self._mako_form.setRowVisible(self._mako_probe_row, recovery)
        self._mako_form.setRowVisible(self._recovery_hint, recovery)

    def _sync_restart_status(self, *_args):
        if not hasattr(self, "_restart_baseline"):
            return
        current = self._current_settings()
        keys = ("enabled", "backend", "allow_fp16", "preserve_swapchain_image_count",
                "mako_dll_path" if current["backend"] == "mako" else "dll_path")
        changed = any(current[key] != self._restart_baseline[key] for key in keys)
        self._restart_status.setText(self.tr(
            "Restart required: launch, DLL or compatibility settings have changed. "
            "They take effect on the next game launch."))
        self._restart_status.setVisible(changed)

    def _sync_backend(self, *_args):
        backend = self._backend.currentData()
        mako = backend == "mako"
        if backend != self._last_backend:
            self._live_timer.stop()
            if backend != self._live_backend:
                self._write_live_config(self._values)
            self._dll_values[self._last_backend] = self._dll_path.text().strip()
            path = self._dll_values[backend]
            if not path:
                from Utils.executables.launch import detect_lsfg_dll
                path = detect_lsfg_dll(mako=mako)
            self._dll_path.setText(path)
            self._last_backend = backend
        self._dll_path.setPlaceholderText(
            self.tr("Optional path to Lossless.dll") if mako else
            self.tr("Optional path to lsfg-vk.dll or Lossless.dll"))
        for row in self._lsfg_rows:
            self._form.setRowVisible(row, not mako)
        for widget in self._lsfg_only:
            widget.setVisible(not mako)
        for row in range(self._log_form.rowCount()):
            self._log_form.setRowVisible(row, not mako)
        self._mako_box.setVisible(mako)
        self._maintenance.setVisible(mako)
        self._sync_mako_mode()
        self._sync_setup_status()

    def _sync_setup_status(self):
        from pathlib import Path
        if self._backend.currentData() == "mako":
            from Utils.executables import mako
            version = mako.installed_version()
            self._setup_status.setText(
                self.tr("MAKO {0} installed (Amethyst).").format(version)
                if version else self.tr("MAKO is not installed for Amethyst."))
            current = version == mako.VERSION
            self._setup_button.setText(
                self.tr("Reinstall MAKO") if current else
                self.tr("Install MAKO {0}").format(mako.VERSION))
            self._setup_button.setEnabled(True)
            self._setup_button.setToolTip(self.tr(
                "Install the verified release for future launches. The previous installation is kept for rollback."))
            if hasattr(self, "_rollback_button"):
                previous = mako.rollback_prefix()
                self._rollback_button.setEnabled(previous is not None)
                self._rollback_button.setToolTip(
                    self.tr("Use the previous installation on the next launch: {0}").format(previous.name)
                    if previous else self.tr("No previous installation is available yet."))
            return
        self._setup_button.setToolTip("")
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
        if self._backend.currentData() == "mako":
            self._start_setup()
            return
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
        from Utils.executables import lsfg, mako
        from gui_qt.worker import run_in_worker

        self._set_maintenance_busy(True)
        self._setup_backend = self._backend.currentData()
        installer = mako if self._setup_backend == "mako" else lsfg
        self._setup_status.setText(self.tr("Installing renderer…"))
        run_in_worker(
            lambda: installer.install_latest(app_log, force=True)
            if installer is mako else installer.install_latest(app_log), self._setup_finished,
            name="lsfg-vk-setup", error_result=(False, self.tr("Unknown error")))

    def _on_setup_finished(self, result):
        self._set_maintenance_busy(False)
        success, detail = result or (False, self.tr("Unknown error"))
        if success:
            self._sync_setup_status()
            if self._setup_backend == "mako":
                self._maintenance_status.setText(self.tr(
                    "MAKO {0} is ready for the next launch. Installation changes are kept even if you cancel this dialog.").format(detail))
                self._maintenance_status.show()
        else:
            self._setup_status.setText(
                self.tr("Renderer setup failed: {0}").format(detail))

    def _set_maintenance_busy(self, busy):
        self._setup_button.setEnabled(not busy)
        self._backend.setEnabled(not busy)
        self._maintenance.setEnabled(not busy)
        if not busy:
            self._sync_setup_status()

    def _start_maintenance(self, action):
        from Utils.executables import mako
        from gui_qt.worker import run_in_worker
        settings = self._current_settings()
        self._set_maintenance_busy(True)
        self._maintenance_status.setText(self.tr("Checking…"))
        self._maintenance_status.show()

        def work():
            try:
                if action == "check":
                    result = mako.check_setup(settings)
                elif action == "updates":
                    result = mako.check_updates()
                else:
                    success, result = mako.rollback()
                    if not success:
                        raise RuntimeError(result)
                return action, result, ""
            except Exception as exc:
                return action, None, str(exc)

        run_in_worker(work, self._maintenance_finished, name="mako-maintenance")

    def _on_maintenance_finished(self, payload):
        self._set_maintenance_busy(False)
        action, result, error = payload or ("", None, self.tr("Unknown error"))
        if error:
            message = self.tr("MAKO check or installation change failed: {0}").format(error)
        elif action == "check":
            labels = {"renderer": self.tr("Renderer installation"),
                      "dll": self.tr("Lossless Scaling DLL"),
                      "config": self.tr("Selected configuration"),
                      "models": self.tr("Frame generation models"),
                      "mangohud": self.tr("MangoHud (optional)")}
            lines = [self.tr("Setup check for the selected settings:")]
            for key, success, detail in result:
                status = self.tr("OK") if success else self.tr("Needs attention")
                if key == "mangohud":
                    status = self.tr("Available architectures: {0}-bit").format(
                        detail.replace(", ", " / ")) if success else self.tr("No usable host layer found")
                lines.append(self.tr("{0}: {1}").format(labels[key], status))
                if detail and (not success or key == "renderer"):
                    lines.append(detail)
            lines.append(self.tr("This checks files and models; GPU operation still needs an in-game check."))
            message = "\n".join(lines)
        elif action == "updates":
            message = self.tr("Latest upstream: {0}. Supported by Amethyst: {1}.").format(
                result["latest"], result["supported"])
            if result["installed"] != result["supported"]:
                message += " " + self.tr("Use Install MAKO to install the supported release.")
            else:
                message += " " + self.tr("The supported release is already installed.")
            if tuple(map(int, result["latest"].split("."))) > tuple(map(int, result["supported"].split("."))):
                message += " " + self.tr("The newer upstream release needs an Amethyst update before it can be installed here.")
        else:
            message = self.tr(
                "Rolled back to MAKO {0} for the next launch. Installation changes are kept even if you cancel this dialog.").format(result)
        self._maintenance_status.setText(message)
        self._maintenance_status.show()

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
        button.setFocusPolicy(Qt.StrongFocus)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(lambda: slider.setValue(
            slider.value() + direction * slider.singleStep()))
        return button

    def _browse_dll(self):
        from Utils.ui.portal import pick_file
        from gui_qt.safe_emit import safe_emit
        pick_file(
            self.tr("Select the frame generation DLL"),
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
        dlls = {**self._dll_values,
                self._backend.currentData(): self._dll_path.text().strip()}
        return {
            "enabled": self._enabled.isChecked(),
            "backend": self._backend.currentData(),
            "dll_path": dlls["lsfg"],
            "mako_dll_path": dlls["mako"],
            "mako_frame_generation": self._mako_generation.isChecked(),
            "mako_multiplier": self._mako_multiplier.currentData(),
            "mako_adaptive": self._mako_adaptive.isChecked(),
            "mako_target_fps": self._mako_target.value(),
            "mako_base_fps_cap": self._mako_base_cap.value(),
            "mako_max_multiplier": self._mako_max.currentData(),
            "mako_steady_base": self._mako_steady.isChecked(),
            "mako_smooth_cadence": self._mako_smooth.isChecked(),
            "mako_real_frame_priority": self._mako_priority.currentData(),
            "mako_cadence_recovery": self._mako_recovery.isChecked(),
            "mako_probe_interval": self._mako_probe.value() / 10,
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
        settings = settings if settings is not None else self._current_settings()
        if settings.get("backend", "lsfg") != self._live_backend:
            return
        if self._live_backend == "mako":
            from Utils.executables.mako import config_path as lsfg_config_path
            from Utils.executables.mako import write_config as write_lsfg_config
        if not lsfg_config_path(self._game_name).is_file():
            return
        try:
            write_lsfg_config(
                self._game_name,
                settings)
        except OSError as exc:
            self._live_status.setText(self.tr(
                "Could not update the live frame generation settings: {0}").format(exc))
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
