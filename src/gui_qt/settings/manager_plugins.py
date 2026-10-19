from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton)

from gui_qt.dialogs.base import OverlayBase


class ManagerPluginOverlay(OverlayBase):
    CARD_W = 850
    CARD_H = 680
    _message = Signal(str)
    _preview = Signal(object)
    _completed = Signal(object)

    def __init__(self, host, plugin, game, profile, on_done):
        super().__init__(host, on_done=on_done)
        from amethyst_plugins.catalog import load_settings
        self.plugin = plugin
        self.game = game
        self.profile = profile
        self.session = None
        self.running = False
        self.changed = False
        self.pending_preview = False
        self.request_text = ""
        card, layout = self._make_card("ManagerPluginCard")
        title = QLabel(plugin.name, card)
        title.setTextFormat(Qt.PlainText)
        title.setStyleSheet("font-size:18px; font-weight:600;")
        layout.addWidget(title)
        target = QLabel(f"{game.name} · {profile}", card)
        target.setTextFormat(Qt.PlainText)
        layout.addWidget(target)
        description = QLabel(plugin.description, card)
        description.setTextFormat(Qt.PlainText)
        description.setWordWrap(True)
        layout.addWidget(description)
        trust = QLabel(self.tr("Run only plugins you trust. Plugin scripts have the same file access as Amethyst."), card)
        trust.setWordWrap(True)
        layout.addWidget(trust)
        self.fields = {}
        form = QFormLayout()
        saved = load_settings(plugin)
        for setting in plugin.settings:
            if setting["type"] == "boolean":
                field = QCheckBox(card)
                field.setChecked(saved[setting["key"]])
            else:
                field = QLineEdit(saved[setting["key"]], card)
                field.setMaxLength(4096)
            label = QLabel(setting["label"], card)
            label.setTextFormat(Qt.PlainText)
            form.addRow(label, field)
            self.fields[setting["key"]] = field
        layout.addLayout(form)
        self.status = QLabel(self.tr("Ready to run."), card)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.output = QPlainTextEdit(card)
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(2000)
        layout.addWidget(self.output, 1)
        self.details = QPlainTextEdit(card)
        self.details.setReadOnly(True)
        self.details.hide()
        layout.addWidget(self.details, 2)
        self.show_request = QCheckBox(self.tr("Show requested API operation"), card)
        self.show_request.hide()
        self.show_request.toggled.connect(self._toggle_request)
        layout.addWidget(self.show_request)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.run_button = QPushButton(self.tr("Run"), card)
        self.run_button.clicked.connect(self._run)
        buttons.addWidget(self.run_button)
        self.apply_button = QPushButton(self.tr("Apply"), card)
        self.apply_button.clicked.connect(lambda: self._answer(True))
        self.apply_button.hide()
        buttons.addWidget(self.apply_button)
        self.cancel_button = QPushButton(self.tr("Cancel changes"), card)
        self.cancel_button.clicked.connect(lambda: self._answer(False))
        self.cancel_button.hide()
        buttons.addWidget(self.cancel_button)
        self.close_button = QPushButton(self.tr("Close"), card)
        self.close_button.clicked.connect(self._finish)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self._message.connect(self._log)
        self._preview.connect(self._show_preview)
        self._completed.connect(self._finished_run)
        self._present()

    def _log(self, message):
        self.output.appendPlainText(message)
        self._host._append_log(f"[{self.plugin.name}] {message}")

    def _run(self):
        from amethyst_plugins.catalog import save_settings
        from amethyst_plugins.session import Session
        from gui_qt.runtime.worker import run_in_worker
        settings = {key: field.isChecked() if isinstance(field, QCheckBox) else field.text()
                    for key, field in self.fields.items()}
        try:
            save_settings(self.plugin, settings)
        except (OSError, ValueError) as exc:
            self._log(self.tr("Could not save plugin settings: {0}").format(exc))
            return
        self.session = Session(self.plugin, self.game, self.profile, settings,
                               log=self._message.emit, preview=self._preview.emit)
        self.running = True
        self.output.clear()
        self.details.hide()
        self.run_button.setEnabled(False)
        for field in self.fields.values():
            field.setEnabled(False)
        self.close_button.setText(self.tr("Stop"))
        self.status.setText(self.tr("Running…"))
        run_in_worker(self.session.run, self._completed, name=f"plugin:{self.plugin.id}",
                      error_result={"error": "Plugin host failed.", "changed": True})

    def _show_preview(self, preview):
        if not self.running or self.session.stop.is_set():
            self.session.answer(False)
            return
        import json
        self.pending_preview = True
        self.preview_text = preview.get("details", "")
        self.request_text = json.dumps({key: preview[key] for key in ("method", "params")},
                                       ensure_ascii=False, indent=2)
        self.status.setText(preview.get("title", self.tr("Review changes")) + "\n" + preview.get("summary", ""))
        self.show_request.setChecked(False)
        self.details.setPlainText(self.preview_text)
        self.details.show()
        self.show_request.show()
        self.apply_button.show()
        self.cancel_button.show()

    def _toggle_request(self, checked):
        self.details.setPlainText(self.request_text if checked else self.preview_text)

    def _answer(self, approved):
        if not self.pending_preview:
            return
        self.pending_preview = False
        self.apply_button.hide()
        self.cancel_button.hide()
        self.show_request.hide()
        self.status.setText(self.tr("Applying changes…") if approved else self.tr("Cancelling changes…"))
        self.session.answer(approved)

    def _finished_run(self, result):
        self.running = False
        self.changed |= result.get("changed", False)
        self.pending_preview = False
        self.apply_button.hide()
        self.cancel_button.hide()
        self.show_request.hide()
        self.close_button.setText(self.tr("Close"))
        self.close_button.setEnabled(True)
        self.run_button.setEnabled(True)
        for field in self.fields.values():
            field.setEnabled(True)
        if result.get("error"):
            self.status.setText(self.tr("Plugin failed. See the log below."))
            self._log(result["error"])
        elif result.get("stopped"):
            self.status.setText(self.tr("Stopped. Any completed changes remain applied."))
        else:
            self.status.setText(self.tr("Plugin finished."))

    def _finish(self, result=None):
        if self.running:
            self.session.cancel()
            self._answer(False)
            self.close_button.setEnabled(False)
            self.status.setText(self.tr("Stopping. Waiting for accepted manager operations to finish…"))
            return
        super()._finish(self.changed)
