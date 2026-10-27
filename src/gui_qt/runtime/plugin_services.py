from __future__ import annotations

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer
from PySide6.QtWidgets import QApplication

from amethyst_plugins.catalog import (data_path, load_data, load_settings,
                                      plugin_directory, save_data, settings_path)
from gui_qt.appearance.theme import apply_theme


class PluginContext:
    def __init__(self, runtime, plugin):
        self.runtime = runtime
        self.plugin = plugin
        self.active = True

    def check_active(self):
        if not self.active or self.runtime.stopped:
            raise RuntimeError("This plugin context has been disposed.")

    def data_path(self):
        return data_path(self.plugin)

    def load_data(self):
        return load_data(self.plugin)

    def save_data(self, data):
        self.check_active()
        save_data(self.plugin, data)
        self.runtime.controller.schedule_refresh()

    def inventory(self):
        self.check_active()
        return self.runtime.controller.events.inventory()

    def selection(self, location=None, **options):
        self.check_active()
        return self.runtime.controller.selections.selection(location, **options)

    def run_command(self, command_id):
        self.check_active()
        return self.runtime.controller.commands.run(self.plugin, command_id, self)

    def read_async(self, method, params, callback, **options):
        self.check_active()
        return self.runtime.controller.reads.read(self, self.plugin, method, params, callback, **options)

    def apply_async(self, method, params, callback, **options):
        return self.apply_batch_async([{"method": method, "params": params}], callback, single=True, **options)

    def apply_batch_async(self, steps, callback, **options):
        self.check_active()
        return self.runtime.controller.operations.submit(self, self.plugin, steps, callback, **options)

    def subscribe(self, names, callback, *, scope="current"):
        self.check_active()
        return self.runtime.controller.events.subscribe(self, names, callback, scope=scope)

    def notify(self, value):
        self.check_active()
        return self.runtime.controller.notify(self, self.plugin, value)

    def dismiss_notification(self, key):
        self.check_active()
        return self.runtime.controller.notify(self, self.plugin, key, dismiss=True)

    def dispose(self):
        self.active = False
        self.runtime.controller.operations.release(self)
        self.runtime.controller.rows.release(self)
        self.runtime.controller.events.release(self)
        self.runtime.controller.reads.release(self)

    def api_call(self, method, params=None):
        from amethyst_backend.appearance import get_appearance, list_themes, set_appearance
        self.check_active()
        params = {} if params is None else params
        if method not in self.plugin.available_methods:
            raise ValueError(f"Undeclared plugin method: {method}")
        if not isinstance(params, dict):
            raise ValueError("Parameters must be a JSON object.")
        if method == "appearance.set":
            if set(params) != {"theme_id", "confirm"} or params["confirm"] is not True:
                raise ValueError("appearance.set requires theme_id and confirm:true.")
            result = set_appearance(params["theme_id"])
            self.runtime.sync_theme(force=True)
            return result
        if params:
            raise ValueError("This method accepts no parameters.")
        if method == "appearance.get":
            return get_appearance()
        if method == "appearance.themes.list":
            return list_themes()
        raise ValueError(f"Method is unavailable in the GUI plugin context: {method}")

    def set_modlist_rows(self, section, rows, **options):
        self.check_active()
        return self.runtime.controller.rows.publish(self, self.plugin, section, rows, **options)

    def open_plugin(self):
        self.check_active()
        controller = self.runtime.controller
        if (not controller.locked and getattr(controller.host, '_manager_plugin', None) is None
                and (controller.valid_target() or not self.plugin.requires_game)):
            controller.host._open_manager_plugin(self.plugin)

    def snapshot(self):
        from amethyst_plugins.i18n import language_code
        from Utils.games.registry import _GAMES
        host = self.runtime.controller.host
        game = host._gs.game
        return {
            "api_version": 1, "plugin_id": self.plugin.id,
            "available_methods": list(self.plugin.available_methods),
            "host_capabilities": {"reviewed_workflows": True, "maximum_steps": 1024,
                                  "result_references": True, "asynchronous_writes": True},
            "language": language_code(),
            "plugin_dir": str(plugin_directory(self.plugin)),
            "settings_path": str(settings_path(self.plugin)),
            "data_path": str(self.data_path()),
            "game": game.name if game else "", "game_id": game.game_id if game else "",
            "profile": host._gs.profile, "settings": load_settings(self.plugin),
            "configured_games": sorted(
                ({"id": g.game_id, "name": g.name} for g in _GAMES.values()
                 if g.is_configured()), key=lambda g: (g["name"].casefold(), g["id"])),
            "data": self.load_data(), "save_data": self.save_data,
            "api_call": self.api_call,
            "inventory": self.inventory, "subscribe": self.subscribe, "selection": self.selection,
            "notify": self.notify, "dismiss_notification": self.dismiss_notification,
            "run_command": self.run_command,
            "read_async": self.read_async,
            "apply_async": self.apply_async, "apply_batch_async": self.apply_batch_async,
            "set_modlist_rows": self.set_modlist_rows, "open_plugin": self.open_plugin,
        }


class PluginServices(QObject):
    def __init__(self, controller):
        super().__init__(controller)
        from Utils.ui.config import get_appearance_mode, get_ui_config_path
        self.controller = controller
        self.services = {}
        self.failed = {}
        self.stopped = False
        self.theme_id = get_appearance_mode()
        self.theme_path = get_ui_config_path()
        self.watcher = QFileSystemWatcher([str(self.theme_path.parent)], self)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.sync_theme)
        self.watcher.directoryChanged.connect(lambda _path: self.timer.start())
        self.watcher.fileChanged.connect(lambda _path: self.timer.start())
        self.sync_theme()

    def sync_theme(self, force=False):
        from Utils.ui.config import get_appearance_mode
        if self.stopped:
            return
        path = str(self.theme_path)
        if self.theme_path.is_file() and path not in self.watcher.files():
            self.watcher.addPath(path)
        mode = get_appearance_mode()
        if force or mode != self.theme_id:
            try:
                apply_theme(QApplication.instance())
                self.theme_id = mode
            except Exception as exc:
                self.controller._report(f"Theme refresh failed: {exc}")

    def context(self, plugin):
        return PluginContext(self, plugin)

    def refresh(self, plugins):
        from gui_qt.runtime.plugin_ui import _Module
        wanted = {p.id: p for p in plugins if p.service}
        for pid, (plugin, module, context, service, snapshot) in list(self.services.items()):
            if wanted.get(pid) != plugin:
                self.remove(pid)
        for pid, plugin in wanted.items():
            if pid in self.failed:
                continue
            try:
                if pid not in self.services:
                    module = _Module(plugin)
                    context = self.context(plugin)
                    self.services[pid] = (plugin, module, context, None, None)
                    snapshot = context.snapshot()
                    service = module.module.create_service(snapshot)
                    self.services[pid] = (plugin, module, context, service, snapshot)
                    if (not callable(getattr(service, "set_context", None))
                            or not callable(getattr(service, "dispose", None))):
                        raise ValueError("create_service must return an object with set_context() and dispose().")
                else:
                    _, module, context, service, previous = self.services[pid]
                    snapshot = context.snapshot()
                    if snapshot != previous:
                        self.services[pid] = (plugin, module, context, service, snapshot)
                        service.set_context(snapshot)
            except Exception as exc:
                self.failed[pid] = f"{pid}: service failed: {exc}"
                self.controller._report(self.failed[pid])
                self.remove(pid)

    def remove(self, pid):
        instance = self.services.pop(pid, None)
        if instance is None:
            return
        _, module, context, service, _ = instance
        context.dispose()
        try:
            if callable(getattr(service, "dispose", None)):
                service.dispose()
        except Exception as exc:
            self.controller._report(f"{pid}: service cleanup failed: {exc}")
        finally:
            module.retired = True
            module.unload()

    def shutdown(self):
        self.stopped = True
        self.timer.stop()
        self.watcher.blockSignals(True)
        for pid in list(self.services):
            self.remove(pid)
