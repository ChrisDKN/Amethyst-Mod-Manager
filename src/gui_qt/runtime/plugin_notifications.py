from __future__ import annotations

import threading
from collections import OrderedDict, deque
from copy import deepcopy
from dataclasses import replace

from PySide6.QtCore import QObject, Qt, Signal, QItemSelectionModel
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QPushButton

from amethyst_plugins.metadata import provider_context
from amethyst_plugins.catalog import load_data, load_settings, plugin_directory
from amethyst_plugins.notifications import normalize_notification, notification_id
from gui_qt.dialogs.base import OverlayBase
from gui_qt.runtime.plugin_flags import PluginFlags


class ReportOverlay(OverlayBase):
    CARD_W = 760
    CARD_H = 540

    def __init__(self, host, title, text):
        super().__init__(host)
        _card, layout = self._make_card("PluginReportCard")
        label = QLabel(title)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label)
        report = QPlainTextEdit()
        report.setReadOnly(True)
        report.setPlainText(text)
        layout.addWidget(report, 1)
        close = QPushButton(self.tr("Close"))
        close.clicked.connect(lambda: self._finish())
        layout.addWidget(close)
        self._present()


class PluginNotifications(QObject):
    ready = Signal()

    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.records = OrderedDict()
        self.pending = deque()
        self.lock = threading.Lock()
        self.stopped = False
        self.report = None
        self.ready.connect(self.drain)

    def publish(self, plugin, game_id, profile, value):
        notification = normalize_notification(plugin, value)
        self.queue((plugin, game_id, profile, notification, False))
        return notification["id"]

    def dismiss(self, plugin, game_id, profile, key):
        self.queue((plugin, game_id, profile, notification_id(key), True))

    def queue(self, request):
        with self.lock:
            if self.stopped:
                raise RuntimeError("The notification host has stopped.")
            if len(self.pending) >= 128:
                raise ValueError("The plugin notification queue is full.")
            start = not self.pending
            self.pending.append(request)
        if start:
            self.ready.emit()

    def drain(self):
        with self.lock:
            pending = list(self.pending)
            self.pending.clear()
        if self.stopped:
            return
        for plugin, game, profile, value, remove in pending:
            if self.controller.plugins.get(plugin.id) != plugin:
                continue
            key = (plugin.id, game, profile, value if remove else value["id"])
            if remove:
                self.remove(key)
            else:
                self.put(plugin, game, profile, value)

    def remove(self, key):
        self.records.pop(key, None)
        self.controller.host._notif_history.remove(key)

    def put(self, plugin, game, profile, value, *, diagnostic=False, toast=None):
        key = (plugin.id, game, profile, value["id"])
        if key in self.records and self.records[key][1] == value:
            return False
        self.records[key] = (plugin, deepcopy(value), diagnostic)
        self.records.move_to_end(key)
        while len(self.records) > 256:
            oldest = next(iter(self.records))
            self.remove(oldest)
        host = self.controller.host
        title = value["title"] if value["title"] == plugin.name else f"{plugin.name}: {value['title']}"
        text = f"{title} [{game}/{profile}]\n{value['message']}"
        declarations = list(value["actions"])
        if value["details"] and not any(a["type"] == "report" for a in declarations):
            declarations.append({"type": "report", "label": self.tr("Open report")})
        actions = [(a["label"], lambda k=key, action=deepcopy(a): self.activate(k, action)) for a in declarations]
        host._notif_history.add(text, value["severity"], key=key, details=value["details"], actions=actions)
        show_toast = value["toast"] if toast is None else toast
        if show_toast:
            host._ensure_feedback()
            host._notifier.notify(text, value["severity"])
        return True

    def replace_diagnostics(self, game, profile, results):
        wanted = {(plugin.id, game, profile, value["id"])
                  for plugin, values in results for value in values}
        for key, (_plugin, _value, diagnostic) in list(self.records.items()):
            if diagnostic and key[1:3] == (game, profile) and key not in wanted:
                self.remove(key)
        for plugin, values in results:
            introduced = []
            for value in values:
                if self.put(plugin, game, profile, value, diagnostic=True, toast=False) and value["toast"]:
                    introduced.append(value)
            if introduced:
                host = self.controller.host
                host._ensure_feedback()
                severity = max(introduced, key=lambda item: ("success", "info", "warning", "error").index(item["severity"]))["severity"]
                host._notifier.notify(self.tr("{0}: {1} new or updated diagnostics").format(plugin.name, len(introduced)), severity)

    def activate(self, key, action):
        record = self.records.get(key)
        if self.stopped or record is None:
            return
        plugin, value, _diagnostic = record
        host = self.controller.host
        if action["type"] == "report":
            if self.report is not None and not self.report._done:
                self.report._finish()
            self.report = ReportOverlay(host, f"{plugin.name}: {value['title']} [{key[1]}/{key[2]}]",
                                        value["message"] + "\n\n" + value["details"])
            return
        if not self.controller.menus.available():
            return
        if key[1:3] != (host._gs.game.game_id, host._gs.profile):
            host._notify(self.tr("Select the notification's game and profile before using this action."), "info")
            return
        if self.controller.plugins.get(plugin.id) != plugin:
            host._notify(self.tr("This plugin changed or is no longer available."), "info")
            return
        try:
            kind = action["type"]
            if kind == "open_plugin":
                host._open_manager_plugin(plugin)
            elif kind == "open_tab":
                self.controller.open_tab(f"{plugin.id}/{action['target']}")
            elif kind == "run_command":
                self.controller.commands.run(plugin, action["command_id"])
            else:
                self.item_action(plugin, action)
        except Exception as exc:
            self.controller._report(f"Plugin notification action failed: {exc}")
            host._notify(str(exc), "warning")

    def item_action(self, plugin, action):
        from gui_qt.runtime.plugin_menus import matches
        from gui_qt.runtime.plugin_selection import selection_item
        host = self.controller.host
        location = action["location"]
        view = host._modlist_view if location == "modlist" else host._plugin_view
        model = view.model()
        entries = model._entries if location == "modlist" else model._rows
        known = {entry.name: i for i, entry in enumerate(entries)}
        names = action.get("names", [])
        if any(name not in known for name in names):
            raise ValueError(self.tr("An affected item is no longer available. Refresh the diagnostic."))
        if action["type"] == "select":
            view._on_clear_filters()
            search = host._modlist_search if location == "modlist" else host._plugins_search
            search.clear()
            if location == "modlist":
                model.set_all_collapsed(False)
                view.apply_collapse()
            else:
                host._select_plugin_tab(0)
            entries = model._entries if location == "modlist" else model._rows
            known = {entry.name: i for i, entry in enumerate(entries)}
            selection = view.selectionModel()
            selection.clearSelection()
            for name in names:
                index = model.index(known[name], 0)
                selection.select(index, QItemSelectionModel.Select | QItemSelectionModel.Rows)
            index = model.index(known[names[0]], 0)
            selection.setCurrentIndex(index, QItemSelectionModel.NoUpdate)
            view.scrollTo(index)
            view.setFocus()
            return
        declaration = next(a for a in plugin.context_menu_actions if a.id == action["action_id"])
        items = [selection_item(view, location, model.index(known[name], 0)) for name in names]
        context = {"location": location, "selection": items, "clicked": items[0] if items else None,
                   "notification_id": plugin.id}
        if not matches(declaration, context):
            raise ValueError(self.tr("The action no longer matches these items."))
        self.controller.menus.dispatch(plugin, declaration, host._gs.game.game_id, host._gs.profile, context)

    def shutdown(self):
        with self.lock:
            self.stopped = True
            self.pending.clear()
        if self.report is not None and not self.report._done:
            self.report._finish()
        self.records.clear()


class PluginDiagnostics(PluginFlags):
    def supports(self, plugin):
        return plugin.diagnostic_provider is not None

    def evaluate(self, plugin, snapshot):
        from gui_qt.runtime.plugin_ui import _Module
        module = _Module(replace(plugin, entrypoint=plugin.diagnostic_provider))
        try:
            context = dict(provider_context(plugin, snapshot), plugin_id=plugin.id,
                           plugin_dir=str(plugin_directory(plugin)),
                           settings=load_settings(plugin), data=load_data(plugin))
            result = module.module.evaluate_diagnostics(context)
            if not isinstance(result, list) or len(result) > 64:
                raise ValueError("evaluate_diagnostics must return at most 64 notifications.")
            values = tuple(normalize_notification(plugin, item) for item in result)
            if len({v["id"] for v in values}) != len(values):
                raise ValueError("Diagnostic IDs must be unique within a provider result.")
            return {"modlist": {plugin.id: values}}
        finally:
            module.retired = True
            module.unload()

    def apply_results(self, results):
        host = self.controller.host
        game = host._gs.game
        if game is not None:
            self.controller.notifications.replace_diagnostics(game.game_id, host._gs.profile,
                [(plugin, results["modlist"].get(plugin.id, ())) for plugin in self.plugins])
