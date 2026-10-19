from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPlainTextEdit,
                               QPushButton)

from amethyst_plugins.catalog import version_key
from amethyst_plugins.store import PluginStore
from gui_qt.dialogs.base import OverlayBase
from gui_qt.dialogs.confirm import ConfirmOverlay
from gui_qt.runtime.worker import run_in_worker


class PluginStoreOverlay(OverlayBase):
    CARD_W = 920
    CARD_H = 650
    _completed = Signal(object)

    def __init__(self, host, on_done=None, store=None):
        super().__init__(host, on_done=on_done)
        self.store = store or PluginStore()
        self.busy = False
        self.confirming = False
        self.entries = {}
        self.installed = {}
        self.pending = {}
        self.rows = {}
        card, layout = self._make_card("PluginStoreCard")
        title = QLabel(self.tr("Plugin store"), card)
        title.setStyleSheet("font-size:18px; font-weight:600;")
        layout.addWidget(title)
        trust = QLabel(self.tr("Plugins run Python with the same file access as Amethyst. "
                               "Install only plugins you trust."), card)
        trust.setWordWrap(True)
        layout.addWidget(trust)
        top = QHBoxLayout()
        self.search = QLineEdit(card)
        self.search.setPlaceholderText(self.tr("Search plugins"))
        self.search.textChanged.connect(self._filter)
        top.addWidget(self.search, 1)
        self.refresh_button = QPushButton(self.tr("Refresh"), card)
        self.refresh_button.clicked.connect(lambda: self._refresh(force=True))
        top.addWidget(self.refresh_button)
        layout.addLayout(top)
        content = QHBoxLayout()
        self.list = QListWidget(card)
        self.list.currentItemChanged.connect(self._selection)
        content.addWidget(self.list, 2)
        self.details = QPlainTextEdit(card)
        self.details.setReadOnly(True)
        content.addWidget(self.details, 3)
        layout.addLayout(content, 1)
        self.status = QLabel(card)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.pending_label = QLabel(card)
        self.pending_label.setWordWrap(True)
        layout.addWidget(self.pending_label)
        buttons = QHBoxLayout()
        self.install_button = QPushButton(self.tr("Download"), card)
        self.install_button.clicked.connect(self._install)
        buttons.addWidget(self.install_button)
        self.remove_button = QPushButton(self.tr("Remove"), card)
        self.remove_button.clicked.connect(self._remove)
        buttons.addWidget(self.remove_button)
        self.cancel_button = QPushButton(self.tr("Cancel pending change"), card)
        self.cancel_button.clicked.connect(self._cancel)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch()
        self.restart_button = QPushButton(self.tr("Restart manager"), card)
        self.restart_button.clicked.connect(self._restart)
        buttons.addWidget(self.restart_button)
        self.close_button = QPushButton(self.tr("Close"), card)
        self.close_button.clicked.connect(self._finish)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self._completed.connect(self._finished)
        self._read_local()
        self._present()
        self._refresh()

    def _read_local(self):
        try:
            self.installed, errors = self.store.installed()
            self.pending = self.store.pending()
            if errors:
                self.status.setText("\n".join(errors))
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
        self._populate()

    def _selected_id(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _populate(self):
        selected = self._selected_id()
        self.list.blockSignals(True)
        self.list.clear()
        self.rows = {}
        for pid in self.entries.keys() | self.installed.keys() | self.pending.keys():
            entry = self.entries.get(pid)
            local = self.installed.get(pid, [])
            pending = self.pending.get(pid)
            manifest = (entry["manifest"] if entry else local[0]["manifest"] if local
                        else pending.get("manifest", {}))
            name = str(manifest.get("name", pid))
            suffix = self.tr("Pending change") if pending else self.tr("Installed") if local else ""
            self.rows[pid] = {"entry": entry, "local": local, "pending": pending,
                              "manifest": manifest, "name": name}
            item = QListWidgetItem(name + (f" · {suffix}" if suffix else ""))
            item.setData(Qt.UserRole, pid)
            self.list.addItem(item)
        self.list.sortItems()
        self.list.blockSignals(False)
        self._filter()
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.data(Qt.UserRole) == selected and not item.isHidden():
                self.list.setCurrentItem(item)
                break
        self._selection()

    def _filter(self):
        term = self.search.text().strip().casefold()
        first = None
        for index in range(self.list.count()):
            item = self.list.item(index)
            data = self.rows[item.data(Qt.UserRole)]["manifest"]
            text = " ".join(str(data.get(k, "")) for k in ("name", "description", "author"))
            item.setHidden(term not in text.casefold())
            if first is None and not item.isHidden():
                first = item
        if self.list.currentItem() is None or self.list.currentItem().isHidden():
            self.list.setCurrentItem(first)
        self._selection()

    def _selection(self, *_):
        idle = not self.busy and not self.confirming
        self.refresh_button.setEnabled(idle)
        self.close_button.setEnabled(idle)
        self.restart_button.setEnabled(idle and bool(self.pending))
        self.pending_label.setText(
            self.tr("{0} change(s) waiting for restart. Changes apply on the next manager launch.").format(len(self.pending))
            if self.pending else self.tr("Downloads, updates and removals take effect after restarting Amethyst."))
        self.install_button.setEnabled(False)
        self.remove_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        row = self.rows.get(self._selected_id())
        if row is None:
            self.details.setPlainText(self.tr("No plugins found.") if not self.rows else "")
            return
        entry, local, pending, data = (row[k] for k in ("entry", "local", "pending", "manifest"))
        installed_version = (local[0]["manifest"].get("version") or self.tr("Installed locally · version unknown")) if local else self.tr("Not installed")
        lines = [row["name"], self.tr("Author: {0}").format(data.get("author") or self.tr("Unknown")),
                 self.tr("Available version: {0}").format(entry["manifest"].get("version") or self.tr("Unknown")) if entry else self.tr("Not in the current catalogue"),
                 self.tr("Installed version: {0}").format(installed_version),
                 self.tr("Minimum manager version: {0}").format(data.get("min_manager_version") or self.tr("Not specified")),
                 "", str(data.get("description", ""))]
        reason = entry.get("error", "") if entry else ""
        label = self.tr("Download")
        can_install = bool(entry) and not reason
        if local:
            label = self.tr("Update")
            version = local[0]["manifest"].get("version")
            if not version:
                label = self.tr("Replace with store version")
            elif can_install:
                try:
                    if version_key(version) >= version_key(entry["manifest"]["version"]):
                        can_install = False
                        lines.extend(("", self.tr("The installed plugin is up to date or newer.")))
                except ValueError as exc:
                    reason = str(exc)
                    can_install = False
        if len(local) > 1:
            reason = self.tr("Duplicate installed plugin id. Resolve the duplicate folders first.")
            can_install = False
        if reason:
            lines.extend(("", reason))
        if pending:
            lines.extend(("", self.tr("Pending: {0}").format(
                self.tr("Removal") if pending["action"] == "remove" else
                self.tr("Install version {0}").format(pending.get("version", "")))))
        self.details.setPlainText("\n".join(lines))
        self.install_button.setText(label)
        self.install_button.setEnabled(idle and can_install and not pending)
        self.remove_button.setEnabled(idle and len(local) == 1 and not pending)
        self.cancel_button.setEnabled(idle and bool(pending))

    def _run(self, operation, message, success=None):
        if self.busy:
            return
        self.busy = True
        self.success_message = success or self.tr("Change saved. Restart Amethyst to apply it.")
        self.status.setText(message)
        self._selection()

        def work():
            try:
                return {"result": operation()}
            except Exception as exc:
                return {"error": str(exc)}
        run_in_worker(work, self._completed, name="plugin-store",
                      error_result={"error": "Plugin store operation failed."})

    def _refresh(self, force=False):
        self._run(lambda: self.store.catalogue(force=force), self.tr("Loading plugin catalogue…"))

    def _finished(self, result):
        self.busy = False
        if "error" in result:
            self.status.setText(result["error"])
        elif result["result"] is not None:
            entries, warning = result["result"]
            self.entries = {entry["manifest"]["id"]: entry for entry in entries}
            self.status.setText(warning or self.tr("Catalogue loaded."))
        else:
            self.status.setText(self.success_message)
        self._read_local()

    def _confirm(self, title, body, action, *, removal=False):
        self.confirming = True
        self._selection()

        def done(approved):
            self.confirming = False
            self._selection()
            if approved:
                action(check.isChecked() if removal else False)
        overlay = ConfirmOverlay(self, title, body, done,
                                 confirm_label=self.tr("Remove") if removal else self.tr("Replace"),
                                 card_h=300)
        if removal:
            check = QCheckBox(self.tr("Delete saved data"), overlay._card)
            overlay._card.layout().insertWidget(2, check)
        for label in overlay.findChildren(QLabel):
            label.setTextFormat(Qt.PlainText)

    def _install(self):
        row = self.rows.get(self._selected_id())
        if not row or not self.install_button.isEnabled():
            return
        replace = bool(row["local"] and not row["local"][0]["manifest"].get("version"))

        def queue(_=False):
            self._run(lambda: self.store.queue_install(row["entry"], replace_local=replace),
                      self.tr("Downloading and validating plugin…"))
        if replace:
            self._confirm(self.tr("Replace local plugin?"),
                          self.tr("Replace {0} with the store version after restart? "
                                  "Saved settings and data will be preserved, and the old folder backed up.").format(row["name"]), queue)
        else:
            queue()

    def _remove(self):
        pid = self._selected_id()
        row = self.rows.get(pid)
        if not row or not self.remove_button.isEnabled():
            return
        self._confirm(self.tr("Remove plugin?"),
                      self.tr("Remove {0} after restart? Saved data is kept for reinstalling unless "
                              "you choose to delete it. Deletion also removes this plugin's store backups.").format(row["name"]),
                      lambda delete: self._run(lambda: self.store.queue_remove(pid, delete_data=delete),
                                               self.tr("Queuing plugin removal…")), removal=True)

    def _cancel(self):
        if self.cancel_button.isEnabled():
            pid = self._selected_id()
            self._run(lambda: self.store.cancel(pid), self.tr("Cancelling pending change…"),
                      self.tr("Pending change cancelled."))

    def _restart(self):
        if not self.restart_button.isEnabled():
            return
        busy = any(bool(getattr(self._host, name, False)) for name in (
            "_manager_plugin", "_pending_install_batches", "_deploy_running", "_restore_running",
            "_install_running", "_col_install_running", "_tool_busy", "_staged_finish_running",
            "_updates_running", "_filegraph_loading",
            "_bsa_op_running", "_proton_busy", "_reset_running", "_active_downloads", "_play_session",
            "_staged_finish_queue", "_xpanel_busy", "_copy_running", "_manifest_dl_running",
            "_sort_running", "_overlap_running", "_requirement_save_running", "_manual_app_update_running"))
        if busy or getattr(getattr(self._host, "_installed_wabbajack_view", None), "_busy", False):
            self.status.setText(self.tr("Wait for current manager operations to finish before restarting."))
            return
        self._finish()
        self._host._request_restart()

    def _finish(self, result=None):
        if not self.busy and not self.confirming:
            super()._finish(result)
