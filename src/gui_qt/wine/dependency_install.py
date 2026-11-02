from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QProgressBar,
    QTreeWidget, QTreeWidgetItem,
)

from gui_qt.appearance.theme import close_button
from gui_qt.dialogs.base import OverlayBase
from gui_qt.runtime.safe_emit import safe_emit
from Utils.wine.health import COMPONENT_SPECS


class DependencyInstallOverlay(OverlayBase):
    CARD_W = 820
    CARD_H = 620
    MIN_W = 460
    MIN_H = 340

    _log_line = Signal(str)
    _progress = Signal(int, str)
    _completed = Signal(object, str)

    def __init__(self, host, game_name, prefix, dependencies, worker, on_done):
        super().__init__(host, on_done=on_done)
        self._running = True
        self._dependencies = dependencies
        self._labels = [self.tr(COMPONENT_SPECS[token].label)
                        if token in COMPONENT_SPECS else token
                        for token in dependencies]
        self._log_line.connect(self._append_log)
        self._progress.connect(self._on_progress)
        self._completed.connect(self._on_completed)

        _, layout = self._make_card("DependencyInstallCard")
        title = QLabel(self.tr("Installing Windows dependencies"))
        title.setStyleSheet("font-weight:600; font-size:15px;")
        layout.addWidget(title)
        target = QLabel(self.tr("{0}\nPrefix: {1}").format(game_name, prefix))
        target.setTextFormat(Qt.PlainText)
        target.setTextInteractionFlags(Qt.TextSelectableByMouse)
        target.setWordWrap(True)
        layout.addWidget(target)

        self._list = QTreeWidget()
        self._list.setColumnCount(2)
        self._list.setHeaderLabels([self.tr("Dependency"), self.tr("Status")])
        self._list.setRootIsDecorated(False)
        self._list.setUniformRowHeights(True)
        self._list.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self._list.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._rows = []
        for token, label in zip(dependencies, self._labels):
            row = QTreeWidgetItem([label, self.tr("Waiting")])
            row.setToolTip(0, token)
            self._list.addTopLevelItem(row)
            self._rows.append(row)
        layout.addWidget(self._list, 1)

        self._status = QLabel(self.tr("Checking Windows dependencies…"))
        self._status.setTextFormat(Qt.PlainText)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._bar = QProgressBar()
        self._bar.setRange(0, len(dependencies))
        self._bar.setValue(0)
        self._bar.setFormat(self.tr("%v of %m dependencies processed"))
        layout.addWidget(self._bar)
        self._busy = QProgressBar()
        self._busy.setRange(0, 0)
        self._busy.setTextVisible(False)
        self._busy.setFixedHeight(6)
        layout.addWidget(self._busy)

        self._logs = QPlainTextEdit()
        self._logs.setReadOnly(True)
        self._logs.setMaximumBlockCount(5000)
        layout.addWidget(self._logs, 2)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._close = close_button()
        self._close.setEnabled(False)
        self._close.clicked.connect(lambda: self._finish())
        buttons.addWidget(self._close)
        layout.addLayout(buttons)
        self._present()
        self._logs.setFocus()

        def run():
            from Utils.app_log import app_log

            def log(message):
                app_log(message)
                safe_emit(self._log_line, message)

            try:
                result = worker(log, lambda done, token: safe_emit(
                    self._progress, done, token))
            except Exception as exc:
                error = str(exc) or type(exc).__name__
                log(f"{game_name}: dependency setup failed: {error}")
                safe_emit(self._completed, None, error)
            else:
                safe_emit(self._completed, result, "")

        threading.Thread(target=run, daemon=True,
                         name="install-prefix-deps").start()

    def _append_log(self, message):
        scrollbar = self._logs.verticalScrollBar()
        at_end = scrollbar.value() >= scrollbar.maximum()
        self._logs.appendPlainText(message)
        if at_end:
            scrollbar.setValue(scrollbar.maximum())

    def _on_progress(self, done, token):
        for row in self._rows[:done]:
            row.setText(1, self.tr("Processed"))
        row = self._rows[done]
        row.setText(1, self.tr("Checking / installing"))
        self._list.scrollToItem(row)
        self._bar.setValue(done)
        self._status.setText(self.tr("Setting up {0} ({1} of {2})…").format(
            self._labels[done], done + 1, len(self._dependencies)))

    def _on_completed(self, result, error):
        self._running = False
        self._busy.hide()
        self._close.setEnabled(True)
        if error:
            for row in self._rows:
                if row.text(1) == self.tr("Checking / installing"):
                    row.setText(1, self.tr("Failed"))
                elif row.text(1) == self.tr("Waiting"):
                    row.setText(1, self.tr("Not run"))
            self._status.setText(self.tr("Dependency setup failed: {0}").format(error))
            return
        for token, row in zip(self._dependencies, self._rows):
            status = (self.tr("Failed") if token in result["failed"] else
                      self.tr("Installed") if token in result["installed"] else
                      self.tr("Skipped"))
            row.setText(1, status)
        self._bar.setValue(len(self._dependencies))
        self._status.setText(self.tr(
            "Dependency setup finished: {0} installed, {1} skipped, {2} failed."
        ).format(len(result["installed"]), len(result["skipped"]), len(result["failed"])))
        if not result["failed"]:
            self._finish()

    def _finish(self, result=None):
        if not self._running:
            super()._finish(result)
