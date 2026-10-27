from __future__ import annotations

from amethyst_plugins.i18n import Translator

from PySide6.QtCore import QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPlainTextEdit,
                               QPushButton, QStyle, QStyledItemDelegate,
                               QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from amethyst_plugins.catalog import version_key
from amethyst_plugins.store import PluginStore
from gui_qt.appearance.theme import active_palette, _c
from gui_qt.dialogs.confirm import ConfirmOverlay
from gui_qt.runtime.safe_emit import safe_emit
from gui_qt.runtime.worker import LatestWorker, run_in_worker


class _PluginCardDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(0, option.fontMetrics.height() * 3 + 32)

    def paint(self, painter, option, index):
        card = index.data(Qt.UserRole + 1)
        palette = active_palette()
        background, foreground = {
            "installed": ("BG_GREEN_DEEP", "TEXT_OK_BRIGHT"),
            "pending": ("BG_ORANGE_DEEP", "STATUS_QUEUED"),
            "disabled": ("BG_PANEL", "TEXT_DIM"),
        }.get(card["state"], ("BG_PANEL", "TEXT_MAIN"))
        border = foreground if card["state"] in ("installed", "pending") else "BORDER"
        selected = bool(option.state & QStyle.State_Selected)
        if selected or option.state & QStyle.State_MouseOver:
            border = "ACCENT"
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(_c(palette, background)))
        painter.setPen(QPen(QColor(_c(palette, border)), 2 if selected else 1))
        rect = option.rect.adjusted(2, 2, -2, -2)
        painter.drawRoundedRect(QRectF(rect), 5, 5)
        content = rect.adjusted(10, 10, -10, -10)
        height = option.fontMetrics.height()
        line = content.topLeft()
        status_width = min(option.fontMetrics.horizontalAdvance(card["status"]) + 4, content.width() // 2)
        painter.setFont(option.font)
        painter.setPen(QColor(_c(palette, foreground)))
        painter.drawText(content.x() + content.width() - status_width, line.y(), status_width, height,
                         Qt.AlignRight | Qt.AlignVCenter,
                         option.fontMetrics.elidedText(card["status"], Qt.ElideRight, status_width))
        font = QFont(option.font)
        font.setBold(True)
        painter.setFont(font)
        name_width = max(0, content.width() - status_width - 8)
        painter.drawText(content.x(), line.y(), name_width, height, Qt.AlignLeft | Qt.AlignVCenter,
                         QFontMetrics(font).elidedText(card["name"], Qt.ElideRight, name_width))
        painter.setFont(option.font)
        painter.setPen(QColor(_c(palette, "TEXT_DIM")))
        for offset, text in enumerate((card["details"], card["source"]), 1):
            painter.drawText(content.x(), line.y() + offset * (height + 4), content.width(), height,
                             Qt.AlignLeft | Qt.AlignVCenter,
                             option.fontMetrics.elidedText(text, Qt.ElideRight, content.width()))
        painter.restore()


class PluginStoreView(QWidget):
    _completed = Signal(object)
    _readme_ready = Signal(object)

    def __init__(self, host, on_done=None, store=None):
        super().__init__(host)
        self._host = host
        self._on_done = on_done
        self._done = False
        self._readme_target = None
        self._readme_token = 0
        self._readme_worker = LatestWorker("plugin-readme")
        self.store = store or PluginStore()
        self.busy = False
        self.confirming = False
        self.settings_changed = bool(getattr(host, "_plugin_settings_changed", False))
        self.entries = {}
        self.installed = {}
        self.pending = {}
        self.rows = {}
        card = self
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
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
        self.repositories_button = QPushButton(self.tr("Repositories…"), card)
        self.repositories_button.clicked.connect(self._repositories)
        top.addWidget(self.repositories_button)
        layout.addLayout(top)
        content = QHBoxLayout()
        self.list = QListWidget(card)
        self.list.setItemDelegate(_PluginCardDelegate(self.list))
        self.list.setSpacing(3)
        self.list.setMouseTracking(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.currentItemChanged.connect(self._selection)
        content.addWidget(self.list, 2)
        self.details = QPlainTextEdit(card)
        self.details.setReadOnly(True)
        self.info_tabs = QTabWidget(card)
        self.info_tabs.addTab(self.details, self.tr("Details"))
        self.readme = QTextBrowser(card)
        self.readme.setOpenLinks(False)
        self.readme.anchorClicked.connect(self._readme_link)
        self.info_tabs.addTab(self.readme, self.tr("README"))
        self.info_tabs.setTabVisible(1, False)
        content.addWidget(self.info_tabs, 3)
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
        self.enable_button = QPushButton(self.tr("Disable"), card)
        self.enable_button.clicked.connect(self._toggle_enabled)
        buttons.addWidget(self.enable_button)
        self.settings_button = QPushButton(self.tr("Settings…"), card)
        self.settings_button.clicked.connect(self._settings)
        buttons.addWidget(self.settings_button)
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
        self._readme_ready.connect(self._readme_finished)
        self._read_local()
        self._refresh()

    def _read_local(self):
        try:
            self.installed, errors = self.store.installed()
            self.pending = self.store.pending()
            if errors:
                self.status.setText("\n".join(errors))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.status.setText(str(exc))
        self._populate()

    def _selected_key(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _selected_id(self):
        row = self.rows.get(self._selected_key())
        return row["id"] if row else None

    def _populate(self):
        selected = self._selected_key()
        self.list.blockSignals(True)
        self.list.clear()
        self.rows = {}
        candidates = dict(self.entries)
        for pid in self.installed.keys() | self.pending.keys():
            local = self.installed.get(pid, [])
            operation = self.pending.get(pid, {})
            origin = local[0].get("origin") if local else operation.get("origin")
            source_id = origin["source"]["id"] if origin else "local"
            key = f"{source_id}:{pid}"
            if key not in candidates:
                candidates[key] = None
        for key, entry in candidates.items():
            pid = entry["manifest"]["id"] if entry else key.split(":", 1)[1]
            local = self.installed.get(pid, [])
            pending = self.pending.get(pid)
            manifest = (entry["manifest"] if entry else local[0]["manifest"] if local
                        else pending.get("manifest", {}))
            plugin_tr = Translator(local[0]["directory"]) if local else str
            name = plugin_tr(str(manifest.get("name", pid)))
            description = plugin_tr(str(manifest.get("description", "")))
            suffix = (self.tr("Enabled") if local[0]["enabled"] else self.tr("Disabled")) if local else ""
            if pending:
                suffix += (" · " if suffix else "") + self.tr("Pending change")
            source = entry["source"] if entry else None
            self.rows[key] = {"id": pid, "entry": entry, "local": local, "pending": pending,
                              "manifest": manifest, "name": name, "description": description}
            text = name + (f" · {suffix}" if suffix else "")
            if source is None and local and local[0].get("origin"):
                source = local[0]["origin"]["source"]
            if source is None and pending and pending.get("origin"):
                source = pending["origin"]["source"]
            if source:
                source_label = f"{source['repository']} [{source['branch'] or self.tr('default')}:{source['folder'] or '/'}]"
            else:
                source_label = self.tr("Installed locally · source unknown")
            version = manifest.get("version")
            details = " · ".join(filter(None, (
                self.tr("Version {0}").format(version) if version else self.tr("Version unknown"),
                manifest.get("author", ""))))
            text += f"\n{details}\n{source_label}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, key)
            item.setData(Qt.UserRole + 1, {
                "name": name, "status": suffix or self.tr("Not installed"),
                "details": details, "source": source_label,
                "state": "pending" if pending else "disabled" if local and not local[0]["enabled"]
                else "installed" if local else "available",
            })
            item.setToolTip(item.text())
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
            row = self.rows[item.data(Qt.UserRole)]
            data = row["manifest"]
            text = " ".join(str(data.get(k, "")) for k in ("name", "description", "author"))
            text += " " + row["name"] + " " + row["description"]
            item.setHidden(term not in text.casefold())
            if first is None and not item.isHidden():
                first = item
        if self.list.currentItem() is None or self.list.currentItem().isHidden():
            self.list.setCurrentItem(first)
        self._selection()

    def _selection(self, *_):
        idle = not self.busy and not self.confirming
        self.refresh_button.setEnabled(idle)
        self.repositories_button.setEnabled(idle)
        self.close_button.setEnabled(idle)
        self.restart_button.setEnabled(idle and (bool(self.pending) or self.settings_changed))
        self.pending_label.setText(
            self.tr("{0} change(s) waiting for restart. Changes apply on the next manager launch.").format(len(self.pending))
            if self.pending else self.tr("Plugin changes take effect after restarting Amethyst."))
        self.install_button.setEnabled(False)
        self.remove_button.setEnabled(False)
        self.enable_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.settings_button.hide()
        self.settings_button.setEnabled(False)
        row = self.rows.get(self._selected_key())
        self._show_readme(row)
        if row is None:
            self.details.setPlainText(self.tr("No plugins found.") if not self.rows else "")
            return
        entry, local, pending, data = (row[k] for k in ("entry", "local", "pending", "manifest"))
        configurable = len(local) == 1 and bool(local[0]["manifest"].get("settings"))
        self.settings_button.setVisible(configurable)
        self.settings_button.setEnabled(idle and configurable)
        installed_version = (local[0]["manifest"].get("version") or self.tr("Installed locally · version unknown")) if local else self.tr("Not installed")
        lines = [row["name"], self.tr("Author: {0}").format(data.get("author") or self.tr("Unknown")),
                 self.tr("Available version: {0}").format(entry["manifest"].get("version") or self.tr("Unknown")) if entry else self.tr("Not in the current catalogue"),
                 self.tr("Installed version: {0}").format(installed_version),
                 self.tr("Minimum manager version: {0}").format(data.get("min_manager_version") or self.tr("Not specified")),
                 "", row["description"]]
        source = entry["source"] if entry else None
        if source:
            lines.extend(("", self.tr("Repository: {0}").format(source["url"]),
                          self.tr("Branch: {0}").format(source["branch"] or self.tr("Default branch")),
                          self.tr("Folder: {0}").format(source["folder"] or "/")))
        if local:
            origin = local[0].get("origin")
            lines.extend(("", self.tr("State: {0}").format(
                self.tr("Enabled") if local[0]["enabled"] else self.tr("Disabled"))))
            if origin:
                installed_source = origin["source"]
                lines.append(self.tr("Installed from: {0} [{1}:{2}]").format(
                    installed_source["repository"], installed_source["branch"] or self.tr("default"),
                    installed_source["folder"] or "/"))
            else:
                lines.append(self.tr("Installed source: Unknown"))
        reason = entry.get("error", "") if entry else ""
        label = self.tr("Download")
        can_install = bool(entry) and not reason
        if local:
            label = self.tr("Update")
            version = local[0]["manifest"].get("version")
            origin = local[0].get("origin")
            same_source = bool(source and origin and origin["source"]["id"] == source["id"])
            if entry and not same_source:
                label = self.tr("Replace from this repository…")
            elif not version:
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
            action = pending["action"]
            pending_text = {"remove": self.tr("Removal"), "enable": self.tr("Enable"),
                            "disable": self.tr("Disable")}.get(action)
            if pending_text is None:
                pending_text = self.tr("Install version {0}").format(pending.get("version", ""))
            lines.extend(("", self.tr("Pending: {0}").format(
                pending_text)))
            if pending.get("origin"):
                lines.append(self.tr("Pending repository: {0}").format(pending["origin"]["source"]["url"]))
        if isinstance(data.get("plugin_dependencies"), list) and data["plugin_dependencies"]:
            lines.append(self.tr("Plugin dependencies: {0}").format(
                ", ".join(f"{d['id']} [{d.get('min_version') or '*'} – {d.get('max_version') or '*'}]"
                          + (" (optional)" if d.get("optional") else "")
                          for d in data["plugin_dependencies"] if isinstance(d, dict) and "id" in d)))
        if isinstance(data.get("plugin_conflicts"), list) and data["plugin_conflicts"]:
            lines.append(self.tr("Plugin conflicts: {0}").format(", ".join(
                pid for pid in data["plugin_conflicts"] if isinstance(pid, str))))
        self.details.setPlainText("\n".join(lines))
        self.install_button.setText(label)
        self.install_button.setEnabled(idle and can_install and not pending)
        self.remove_button.setEnabled(idle and len(local) == 1 and not pending)
        self.enable_button.setText(self.tr("Disable") if local and local[0]["enabled"] else self.tr("Enable"))
        self.enable_button.setEnabled(idle and len(local) == 1 and not pending)
        self.cancel_button.setEnabled(idle and bool(pending))

    def _show_readme(self, row):
        entry = row["entry"] if row else None
        local = row["local"] if row else []
        directory = local[0]["directory"] if local else None
        target = (self._selected_key(), entry["commit"] if entry else directory) if row else None
        if target == self._readme_target:
            return
        self._readme_target = target
        self._readme_token += 1
        token = self._readme_token
        self._readme_worker.discard_pending()
        self.readme.clear()
        self.info_tabs.setTabVisible(1, row is not None)
        if row is None:
            return
        self.readme.setPlainText(self.tr("Loading README…"))

        def work():
            try:
                result = {"readme": self.store.readme(entry=entry, directory=directory)}
            except Exception as exc:
                result = {"error": str(exc)}
            safe_emit(self._readme_ready, (token, result))
        self._readme_worker.submit(work)

    def _readme_finished(self, result):
        token, data = result
        if self._done or token != self._readme_token:
            return
        if "error" in data:
            self.readme.setPlainText(self.tr("Could not load README: {0}").format(data["error"]))
        elif data["readme"] is None:
            self.info_tabs.setTabVisible(1, False)
        else:
            text, base = data["readme"]
            self.readme.document().setBaseUrl(QUrl(base))
            self.readme.setMarkdown(text)

    def _readme_link(self, url):
        if url.isRelative() and url.toString().startswith("#"):
            self.readme.scrollToAnchor(url.fragment())
            return
        url = self.readme.document().baseUrl().resolved(url)
        if url.scheme() in ("https", "http", "mailto", "file"):
            QDesktopServices.openUrl(url)

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
            self.entries = {f"{entry['source']['id']}:{entry['manifest']['id']}": entry for entry in entries}
            self._readme_target = object()
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
        row = self.rows.get(self._selected_key())
        if not row or not self.install_button.isEnabled():
            return
        local = row["local"]
        origin = local[0].get("origin") if local else None
        replace = bool(local and (not local[0]["manifest"].get("version") or not origin
                                  or origin["source"]["id"] != row["entry"]["source"]["id"]))

        def queue(_=False):
            self._run(lambda: self.store.queue_install(row["entry"], replace_local=replace),
                      self.tr("Downloading and validating plugin…"))
        if replace:
            self._confirm(self.tr("Replace plugin from this repository?"),
                          self.tr("Replace {0} with version {1} from {2} after restart? "
                                  "Saved settings and data will be preserved, and the old folder backed up.").format(
                                      row["manifest"].get("name", row["id"]),
                                      row["entry"]["manifest"]["version"], row["entry"]["source"]["url"]), queue)
        else:
            queue()

    def _remove(self):
        pid = self._selected_id()
        row = self.rows.get(self._selected_key())
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

    def _toggle_enabled(self):
        row = self.rows.get(self._selected_key())
        if row and self.enable_button.isEnabled():
            enabled = not row["local"][0]["enabled"]
            self._run(lambda: self.store.queue_enabled(row["id"], enabled),
                      self.tr("Queuing plugin enablement change…"))

    def _repositories(self):
        if self.busy or self.confirming:
            return
        from gui_qt.settings.plugin_repositories import PluginRepositoriesOverlay
        self.confirming = True
        self._selection()

        def done(changed):
            self.confirming = False
            self.repositories_overlay = None
            self._selection()
            if changed:
                self.entries = {}
                self._read_local()
                self._refresh(force=True)
        self.repositories_overlay = PluginRepositoriesOverlay(self, self.store, on_done=done)

    def _settings(self):
        row = self.rows.get(self._selected_key())
        if not row or not self.settings_button.isEnabled():
            return
        from amethyst_plugins.catalog import parse_plugin
        from gui_qt.settings.plugin_settings import PluginSettingsOverlay
        local = row["local"][0]
        try:
            plugin, _ = parse_plugin(local["manifest"], local["directory"], check_compatibility=False)
        except (OSError, ValueError, TypeError) as exc:
            self.status.setText(str(exc))
            return
        self.confirming = True
        self._selection()

        def done(saved):
            self.confirming = False
            self.settings_overlay = None
            if saved:
                self.settings_changed = True
                self._host._plugin_settings_changed = True
                self.status.setText(self.tr("Settings saved. Restart Amethyst to apply icon changes.")
                                    if plugin.icon_overrides else self.tr("Settings saved."))
                controller = getattr(self._host, "_plugin_ui", None)
                if controller is not None:
                    controller.schedule_refresh()
            self._selection()
        self.settings_overlay = PluginSettingsOverlay(self, plugin, on_done=done)

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
        if not self.tab_close_blocked():
            self._host._tabs.close_tab("plugin_store")

    def tab_close_blocked(self):
        return self.busy or self.confirming

    def tab_closing(self):
        if self._done:
            return
        self._done = True
        self._readme_worker.discard_pending()
        if self._on_done is not None:
            self._on_done(None)
