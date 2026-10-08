from __future__ import annotations

import shutil
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton

from gui_qt.i18n import is_reserved_profile_name, profile_display
from gui_qt.overlay_base import OverlayBase
from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from gui_qt.worker import run_in_worker
from Utils.downloads.core import fmt_size
from Utils.profiles.copy import (
    InsufficientCopySpace, copy_profile, estimate_profile_copy, validate_copy_name,
)


class CopyProfileOverlay(OverlayBase):
    CARD_W = 540
    CARD_H = 370
    MIN_H = 300
    _size_ready = Signal(object)
    _progress = Signal(object, object)
    _copy_finished = Signal(object)

    def __init__(self, host, window, game, source, on_done):
        super().__init__(host, on_done=on_done)
        self._window = window
        self._game = game
        self._source = source
        self._stop = threading.Event()
        self._size = None
        self._copying = False
        self._size_ready.connect(self._on_size_ready)
        self._progress.connect(self._on_progress)
        self._copy_finished.connect(self._on_copy_finished)
        p = active_palette()
        _card, layout = self._make_card("CopyProfileCard")
        title = QLabel(self.tr("Copy Profile"))
        title.setStyleSheet(
            f"color:{_c(p, 'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        layout.addWidget(title)
        warning = QLabel(self.tr(
            "Copying '{0}' creates a separate copy of its mods and profile files. "
            "This will use additional disk space.").format(profile_display(source.name)))
        warning.setWordWrap(True)
        warning.setTextFormat(Qt.PlainText)
        layout.addWidget(warning)
        layout.addWidget(QLabel(self.tr("New profile name:")))
        self._name = QLineEdit(self.tr("{0} (copy)").format(source.name))
        self._name.selectAll()
        self._name.textChanged.connect(self._validate)
        self._name.returnPressed.connect(self._start_copy)
        layout.addWidget(self._name)
        self._space = QLabel(self.tr("Calculating disk space…"))
        self._space.setWordWrap(True)
        layout.addWidget(self._space)
        self._error = QLabel()
        self._error.setStyleSheet(f"color:{_c(p, 'TEXT_ERR')};")
        self._error.setWordWrap(True)
        self._error.setTextFormat(Qt.PlainText)
        layout.addWidget(self._error)
        self._bar = QProgressBar()
        self._bar.setRange(0, 0)
        self._bar.hide()
        layout.addWidget(self._bar)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._cancel = QPushButton(self.tr("Cancel"))
        self._cancel.setObjectName("FormButton")
        self._cancel.setCursor(Qt.PointingHandCursor)
        self._cancel.clicked.connect(self._cancel_copy)
        buttons.addWidget(self._cancel)
        self._copy = QPushButton(self.tr("Copy"))
        self._copy.setObjectName("PrimaryButton")
        self._copy.setCursor(Qt.PointingHandCursor)
        self._copy.setEnabled(False)
        self._copy.clicked.connect(self._start_copy)
        buttons.addWidget(self._copy)
        layout.addLayout(buttons)
        self._present()
        self._name.setFocus()

        def measure():
            try:
                return estimate_profile_copy(source, stop=self._stop), shutil.disk_usage(source.parent).free
            except Exception as exc:
                return exc

        run_in_worker(measure, self._size_ready, name="profile-copy-size")

    def _on_size_ready(self, result):
        if self._done:
            return
        if isinstance(result, Exception):
            self._space.setText(self.tr("Could not calculate disk space: {0}").format(result))
            return
        self._size, available = result
        self._show_space(self._size.required_bytes, available)
        self._validate()

    def _show_space(self, required, available):
        self._space.setText(self.tr(
            "Estimated disk space required: {0}\nAvailable disk space: {1}").format(
                fmt_size(required), fmt_size(available)))

    def _validate(self):
        if self._copying:
            return
        error = ""
        name = self._name.text().strip()
        if not name:
            error = self.tr("Enter a name for the new profile.")
        elif is_reserved_profile_name(name):
            error = self.tr("This profile name is reserved.")
        else:
            try:
                validate_copy_name(self._source, name)
            except FileExistsError:
                error = self.tr("Profile '{0}' already exists.").format(name)
            except ValueError:
                error = self.tr("Enter a valid profile folder name.")
        if not error and self._size is not None:
            try:
                available = shutil.disk_usage(self._source.parent).free
                self._show_space(self._size.required_bytes, available)
                if self._size.required_bytes > available:
                    error = self.tr("There is not enough free disk space to copy this profile.")
            except OSError as exc:
                error = self.tr("Could not calculate disk space: {0}").format(exc)
        self._error.setText(error)
        self._copy.setEnabled(not error and self._size is not None)

    def _start_copy(self):
        if self._copying:
            return
        self._validate()
        if not self._copy.isEnabled():
            return
        gate = getattr(self._window, "_can_remove_installed_wabbajack", None)
        if gate is not None and not gate():
            self._error.setText(self.tr("Wait for the current operation to finish."))
            return
        name = self._name.text().strip()
        self._window._set_tool_lock("profile-copy", self.tr("Profile copying"), True)
        self._copying = True
        self._name.setEnabled(False)
        self._copy.setEnabled(False)
        self._error.clear()
        self._space.setText(self.tr("Copying profile…"))
        self._bar.show()

        def worker():
            try:
                return copy_profile(
                    self._game, self._source, name, stop=self._stop,
                    progress_fn=lambda done, total: safe_emit(self._progress, done, total))
            except Exception as exc:
                return exc

        run_in_worker(worker, self._copy_finished, name="profile-copy")

    def _on_progress(self, done, total):
        if self._stop.is_set():
            return
        self._bar.setRange(0, 100)
        self._bar.setValue(min(100, int(done * 100 / max(1, total))))
        self._space.setText(self.tr("Copying profile… {0} / {1}").format(
            fmt_size(done), fmt_size(total)))

    def _on_copy_finished(self, result):
        self._window._set_tool_lock("profile-copy", "", False)
        self._copying = False
        if isinstance(result, InterruptedError):
            self._finish(None)
        elif isinstance(result, Exception):
            self._bar.hide()
            self._name.setEnabled(True)
            self._cancel.setEnabled(True)
            self._stop.clear()
            self._validate()
            if isinstance(result, InsufficientCopySpace):
                self._show_space(result.required, result.available)
                self._copy.setEnabled(False)
                self._error.setText(self.tr("There is not enough free disk space to copy this profile."))
            else:
                self._error.setText(self.tr("Could not copy profile: {0}").format(result))
        else:
            self._bar.setValue(100)
            self._finish(result.name)

    def _cancel_copy(self):
        self._stop.set()
        if self._copying:
            self._space.setText(self.tr("Cancelling profile copy…"))
            self._cancel.setEnabled(False)
        else:
            self._finish(None)

    def _finish(self, result=None):
        if self._copying:
            self._cancel_copy()
            return
        self._stop.set()
        super()._finish(result)
