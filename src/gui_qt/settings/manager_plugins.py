from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton)

from amethyst_plugins.i18n import translate
from gui_qt.dialogs.base import OverlayBase
from .plugin_fields import SettingField


class ManagerPluginOverlay(OverlayBase):
    CARD_W = 850
    CARD_H = 680
    _message = Signal(str)
    _preview = Signal(object)
    _completed = Signal(object)
    _job_progress = Signal(object)

    def __init__(self, host, plugin, game, profile, on_done, *, action_id=None, menu_context=None,
                 workflow=None, on_result=None, on_progress=None):
        super().__init__(host, on_done=on_done)
        from amethyst_plugins.catalog import load_settings
        self.plugin = plugin
        self.action_id = action_id
        self.menu_context = menu_context
        self.workflow = workflow
        self.on_result = on_result
        self.result_delivered = False
        self.game = game
        self.profile = profile
        self.session = None
        self.running = False
        self.changed = False
        self.pending_preview = False
        self.request_text = ""
        card, layout = self._make_card("ManagerPluginCard")
        label = next((a.label for a in plugin.context_menu_actions if a.id == action_id), "")
        if not label and action_id and action_id.startswith("command:"):
            label = next((c.label for c in plugin.commands if c.id == action_id[8:]), "")
        title = QLabel(f"{translate(plugin, plugin.name)} · {translate(plugin, label)}" if label else translate(plugin, plugin.name), card)
        title.setTextFormat(Qt.PlainText)
        title.setStyleSheet("font-size:18px; font-weight:600;")
        layout.addWidget(title)
        target = QLabel(f"{game.name} · {profile}", card)
        target.setTextFormat(Qt.PlainText)
        layout.addWidget(target)
        description = QLabel(translate(plugin, plugin.description), card)
        description.setTextFormat(Qt.PlainText)
        description.setWordWrap(True)
        layout.addWidget(description)
        trust = QLabel(self.tr("Run only plugins you trust. Plugin scripts have the same file access as Amethyst."), card)
        trust.setWordWrap(True)
        layout.addWidget(trust)
        self.fields = {}
        form = QFormLayout()
        saved = load_settings(plugin)
        for setting in (() if workflow is not None else plugin.settings):
            field = SettingField(plugin, setting, saved[setting["key"]], card)
            label = QLabel(translate(plugin, setting["label"]), card)
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
        if on_progress is not None:
            self._job_progress.connect(on_progress)
        self._present()
        if action_id is not None or workflow is not None:
            QTimer.singleShot(0, self, self._run)

    def _log(self, message):
        self.output.appendPlainText(message)
        self._host._append_log(f"[{translate(self.plugin, self.plugin.name)}] {message}")

    def _run(self):
        if self.running or self._done:
            return
        from amethyst_plugins.catalog import save_settings
        from amethyst_plugins.session import Session
        from gui_qt.runtime.worker import run_in_worker
        from amethyst_plugins.catalog import load_settings
        settings = (load_settings(self.plugin) if self.workflow is not None
                    else {key: field.value() for key, field in self.fields.items()})
        try:
            if self.workflow is None:
                save_settings(self.plugin, settings)
        except (OSError, ValueError) as exc:
            self._log(self.tr("Could not save plugin settings: {0}").format(exc))
            return
        self.session = Session(self.plugin, self.game, self.profile, settings,
                               log=self._message.emit, preview=self._preview.emit,
                               action_id=self.action_id, menu_context=self.menu_context,
                               inventory_reader=self._host._plugin_ui.events.inventory,
                               selection_reader=self._host._plugin_ui.selections.selection,
                               notification_sink=lambda value: self._host._plugin_ui.notifications.publish(
                                   self.plugin, self.game.game_id, self.profile, value),
                               notification_dismiss=lambda key: self._host._plugin_ui.notifications.dismiss(
                                   self.plugin, self.game.game_id, self.profile, key))
        self.session.workflow = self.workflow
        self.session.progress = self._job_progress.emit
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
        if self.workflow is not None:
            self.result_delivered = True
            if self.on_result is not None:
                self.on_result(result)
            self._finish()

    def _finish(self, result=None):
        if self.running:
            self.session.cancel()
            self._answer(False)
            self.close_button.setEnabled(False)
            self.status.setText(self.tr("Stopping. Waiting for accepted manager operations to finish…"))
            return
        if self.workflow is not None and not self.result_delivered:
            self.result_delivered = True
            if self.on_result is not None:
                self.on_result({"stopped": True, "changed": self.changed, "result": None})
        super()._finish(self.changed)
