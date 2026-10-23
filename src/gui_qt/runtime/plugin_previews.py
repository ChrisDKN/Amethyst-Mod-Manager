from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication, QMenu, QVBoxLayout, QWidget

from amethyst_plugins.i18n import translate


class PreviewTab(QWidget):
    def __init__(self, runtime, plugin, declaration):
        super().__init__(runtime.controller.host)
        self.runtime = runtime
        self.plugin = plugin
        self.declaration = declaration
        self.key = f"manager-plugin-preview:{plugin.id}/{declaration.id}"
        self.active = True
        self.module = None
        self.view = None
        self.context = None
        self.owner = runtime.controller.services.context(plugin)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.destroyed.connect(lambda: self.dispose())

    def set_file(self, path, relative_path):
        stat = path.stat()
        context = self.owner.snapshot()
        context.pop("api_call", None)
        context.update(preview_id=self.declaration.id, location="preview", path=str(path),
                       relative_path=relative_path or path.name, file_size=stat.st_size,
                       modified_ns=stat.st_mtime_ns, palette=dict(self.runtime.controller.palette))
        if self.view is None:
            from gui_qt.runtime.plugin_ui import _Module
            self.module = _Module(replace(self.plugin, entrypoint=self.declaration.entrypoint))
            view = self.module.module.create_preview(context, self)
            if isinstance(view, QWidget) and self.isAncestorOf(view):
                self.view = view
            if (not isinstance(view, QWidget) or view.isWindow() or not self.isAncestorOf(view)
                    or not callable(getattr(view, "set_context", None))
                    or not callable(getattr(view, "dispose", None))):
                if isinstance(view, QWidget) and view.parent() is None:
                    view.deleteLater()
                raise ValueError("create_preview must return a child QWidget with set_context() and dispose().")
            self.layout().addWidget(view)
        elif context != self.context:
            self.view.set_context(context)
        self.context = context

    def tab_closing(self):
        self.dispose()

    def dispose(self):
        if not self.active:
            return
        self.active = False
        self.runtime.tabs.pop(self.key, None)
        self.owner.dispose()
        try:
            if self.view is not None and callable(getattr(self.view, "dispose", None)):
                self.view.dispose()
        except Exception as exc:
            self.runtime.controller._report(f"{self.plugin.id}: preview cleanup failed: {exc}")
        finally:
            if self.module is not None:
                self.module.retired = True
                self.module.unload()


class PluginPreviews(QObject):
    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.plugins = ()
        self.tabs = {}
        self.failed = set()
        self.stopped = False
        QApplication.instance()._amethyst_plugin_previews = self

    def target(self):
        state = self.controller.host._gs
        return (state.game.game_id if state.game else "", state.profile)

    def matching(self, path):
        name = str(path).lower()
        matches = [(p, d, max(len(ext) for ext in d.extensions if name.endswith(ext)))
                   for p in self.plugins for d in p.file_previews
                   if (p.id, d.id) not in self.failed and any(name.endswith(ext) for ext in d.extensions)]
        return [(p, d) for p, d, _length in sorted(matches, key=lambda item:
                (-item[2], -item[1].priority, item[0].id, item[1].id))]

    def update(self, plugins):
        wanted = tuple(p for p in plugins if p.file_previews)
        if wanted != self.plugins:
            self.failed.clear()
        self.plugins = wanted
        for key, tab in list(self.tabs.items()):
            current = next((p for p in wanted if p == tab.plugin), None)
            context = tab.context or {}
            if (current is None or (context.get("game_id"), context.get("profile")) != self.target()
                    or tab.declaration not in current.file_previews):
                self.close(key)
                continue
            try:
                tab.set_file(Path(context["path"]), context["relative_path"])
            except Exception as exc:
                self.controller._report(f"{tab.plugin.id}: preview refresh failed: {exc}")
                self.failed.add((tab.plugin.id, tab.declaration.id))
                self.close(key)

    def available(self):
        return (not self.stopped and not self.controller.locked and self.controller.valid_target()
                and getattr(self.controller.host, "_manager_plugin", None) is None)

    def open(self, path, relative_path="", *, choice=None):
        if not self.available():
            return False
        path = Path(path).absolute()
        if not path.is_file():
            return False
        for plugin, declaration in self.matching(path):
            if choice is not None and choice != (plugin.id, declaration.id):
                continue
            key = f"manager-plugin-preview:{plugin.id}/{declaration.id}"
            tab = self.tabs.get(key)
            try:
                if tab is None:
                    tab = PreviewTab(self, plugin, declaration)
                    self.tabs[key] = tab
                    tab.set_file(path, relative_path)
                    self.controller.host._tabs.open_scoped_tab(
                        tab, f"{path.name} · {translate(plugin, declaration.label)}",
                        self.controller.host._modlist_panel_stack, key=key)
                else:
                    tab.set_file(path, relative_path)
                    self.controller.host._tabs.set_tab_title(key, f"{path.name} · {translate(plugin, declaration.label)}")
                    self.controller.host._tabs.focus_key(key)
                return True
            except Exception as exc:
                self.failed.add((plugin.id, declaration.id))
                self.controller._report(f"{plugin.id}/{declaration.id}: preview failed: {exc}")
                self.close(key)
        return False

    def queue(self, menu, path, relative_path, plugin, declaration):
        target = self.target()
        while isinstance(menu.parentWidget(), QMenu):
            menu = menu.parentWidget()
        menu.close()
        def activate():
            if self.target() == target and plugin in self.plugins and declaration in plugin.file_previews:
                self.open(path, relative_path, choice=(plugin.id, declaration.id))
        QTimer.singleShot(0, self, activate)

    def close(self, key):
        tab = self.tabs.get(key)
        if tab is not None:
            tab.dispose()
            if self.controller.host._tabs.has_key(key):
                self.controller.host._tabs.close_tab(key)
            else:
                tab.deleteLater()

    def set_locked(self, locked):
        for tab in self.tabs.values():
            tab.setEnabled(not locked)

    def shutdown(self):
        self.stopped = True
        for key in list(self.tabs):
            self.close(key)
        app = QApplication.instance()
        if getattr(app, "_amethyst_plugin_previews", None) is self:
            del app._amethyst_plugin_previews


def open_plugin_preview(source, path, relative_path=""):
    runtime = getattr(QApplication.instance(), "_amethyst_plugin_previews", None)
    if runtime is None or path is None:
        return False
    game = getattr(source, "game", None) or getattr(source, "_game", None)
    profile = getattr(source, "profile_dir", None)
    profile_name = getattr(source, "_profile_name", "")
    current = getattr(source, "_context_is_current", None)
    if (game is not None and game.game_id != runtime.target()[0]
            or profile is not None and str(profile) != str(runtime.controller.host._gs.profile_dir())
            or profile_name and profile_name != runtime.target()[1]
            or current is not None and not current()):
        return False
    try:
        return runtime.open(path, relative_path)
    except (OSError, ValueError) as exc:
        runtime.controller._report(f"Plugin preview unavailable: {exc}")
        return False


def can_preview(name):
    runtime = getattr(QApplication.instance(), "_amethyst_plugin_previews", None)
    return bool(runtime is not None and runtime.available() and runtime.matching(name))
