from __future__ import annotations

import threading
import weakref
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication

from amethyst_plugins.events import publish_event
from gui_qt.runtime.plugin_menus import menu_item


LOCATIONS = frozenset({"modlist", "plugins", "mod_files", "text_files", "data",
                       "downloads", "saves", "overrides"})


def bind_selection(source, tree, location, source_id=None):
    source._amethyst_selection_binding = (
        tree, location, source_id or getattr(source, "_selection_source_id", None) or location)
    runtime = getattr(QApplication.instance(), "_amethyst_plugin_selection", None)
    if runtime is not None:
        runtime.attach(source)


def selection_item(source, location, index):
    if not index.isValid():
        return None
    model = index.model()
    node = model.node(index) if hasattr(model, "node") else index.data(Qt.UserRole + 1)
    if node is None:
        return None
    if location == "modlist":
        from gui_qt.modlist.external import external
        if external(node):
            return menu_item('external_separator' if node.is_separator else 'external_mod', node.item_id,
                             display_name=node.display_name, plugin_id=node.plugin_id, section_id=node.section_id,
                             read_only=True)
        from Utils.filegraph.constants import OVERWRITE_NAME, ROOT_FOLDER_NAME
        kind = ("overwrite" if node.name == OVERWRITE_NAME else
                "root_folder" if node.name == ROOT_FOLDER_NAME else
                "separator" if node.is_separator else "mod")
        staging = getattr(source, "staging_dir", None)
        return menu_item(kind, node.name, Path(staging) / node.name if staging and kind == "mod" else None,
                         display_name=node.display_name, enabled=node.enabled)
    if location == "plugins":
        path = getattr(source, "resolved_paths", {}).get(node.name.lower())
        return menu_item("game_plugin", node.name, path, enabled=node.enabled,
                         vanilla=node.vanilla, masters=list(node.masters or ()))
    if location == "downloads":
        return menu_item("file", node.path.name, node.path) if node.path is not None else None
    if location == "saves":
        from gui_qt.saves.view import _GRP_LOCATION
        item = source._tree.itemFromIndex(index)
        directory = bool(index.data(Qt.UserRole + 2)) or getattr(item, "_group", None) == _GRP_LOCATION
        return menu_item("directory" if directory else "save", Path(node).name, node)
    if location == "mod_files":
        path = None if node.synthetic else source._disk_path_for(node)
        return menu_item("directory" if node.is_dir else "file", node.name, path,
                         relative_path=node.path, mod_name=source._mod_name, synthetic=node.synthetic)
    if location == "text_files":
        return menu_item("directory" if node.is_dir else "file", node.name, node.full_path,
                         relative_path=node.rel_path, mod_name=node.mod)
    if location == "data":
        return menu_item("directory" if node.is_dir else "file", node.name,
                         source._source_path_for(node), relative_path=node.path, mod_name=node.mod)
    if location == "overrides":
        path = source._root_path / str(node.payload) if source._root_path is not None else None
        return menu_item("directory" if node.is_dir else "file", node.name, path,
                         relative_path=str(node.payload),
                         boundary="root_folder" if source._root_folder else "overwrite")
    return None


class PluginSelection(QObject):
    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.sources = {}
        self.pending = {}
        self.focus_pending = set()
        self.cache = {}
        self.active = None
        self.last_locations = {}
        self.target = self.current_target()
        self.lock = threading.RLock()
        self.stopped = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.flush)
        app = QApplication.instance()
        app._amethyst_plugin_selection = self
        app.installEventFilter(self)
        for widget in app.allWidgets():
            if hasattr(widget, "_amethyst_selection_binding"):
                self.attach(widget)
        if "modlist" in self.sources:
            self.active = "modlist"
            self.last_locations["modlist"] = "modlist"

    def current_target(self):
        state = self.controller.host._gs
        return (state.game.game_id if state.game else "", state.profile)

    def attach(self, source):
        tree, location, key = source._amethyst_selection_binding
        previous = self.sources.get(key)
        if previous is not None and previous() is source:
            return
        self.sources[key] = reference = weakref.ref(source)
        def schedule(*_args):
            if self.sources.get(key) is reference:
                self.schedule(key)
        tree.selectionModel().selectionChanged.connect(schedule)
        tree.selectionModel().currentChanged.connect(schedule)
        for signal in (tree.model().modelReset, tree.model().layoutChanged,
                       tree.model().rowsRemoved, tree.model().dataChanged):
            signal.connect(schedule)
        def remove():
            if self.sources.get(key) is reference:
                self.sources.pop(key, None)
                with self.lock:
                    previous = self.cache.pop(key, None)
                    if self.active == key:
                        self.active = None
                if previous is not None and not self.stopped:
                    publish_event("selection.changed", *self.current_target(), active=False,
                                  **self.empty(location, key))
        source.destroyed.connect(remove)
        self.schedule(key)

    def schedule(self, key, *, active=False):
        if self.stopped:
            return
        with self.lock:
            if active or self.active is None:
                if self.active != key:
                    self.focus_pending.add(key)
                self.active = key
                source = self.sources.get(key, lambda: None)()
                if source is not None:
                    self.last_locations[source._amethyst_selection_binding[1]] = key
            self.pending[key] = self.current_target()
        self.timer.start()

    def refresh_target(self):
        target = self.current_target()
        with self.lock:
            if target != self.target:
                self.target = target
                self.cache.clear()
                self.pending = {key: value for key, value in self.pending.items() if value == target}
                publish_event("selection.changed", *target, active=False, **self.empty())

    def empty(self, location="", source_id=""):
        return {"location": location, "source_id": source_id, "items": [],
                "current": None, "selection_count": 0, "truncated": False}

    def selection(self, location=None, *, source_id=None):
        if location is not None and (not isinstance(location, str) or location not in LOCATIONS):
            raise ValueError("Unknown selection location.")
        if source_id is not None and not isinstance(source_id, str):
            raise ValueError("source_id must be text.")
        with self.lock:
            target = self.current_target()
            key = source_id or self.active
            if self.target != target:
                result = self.empty(location or "", source_id or "")
            else:
                if location and (key not in self.cache or self.cache[key]["location"] != location):
                    key = (self.last_locations.get(location) if source_id is None else None)
                    if key not in self.cache and source_id is None:
                        key = next((key for key, value in reversed(self.cache.items()) if value["location"] == location), None)
                result = deepcopy(self.cache.get(key, self.empty(location or "", source_id or "")))
            return dict(result, active=self.target == target and key == self.active and key is not None,
                        game_id=target[0], profile=target[1])

    def capture(self, source):
        tree, location, key = source._amethyst_selection_binding
        result = self.empty(location, key)
        game = getattr(source, "game", None) or getattr(source, "_game", None)
        if game is not None and game.game_id != self.target[0]:
            return result
        profile = getattr(source, "profile_dir", None)
        if profile is not None and str(profile) != str(self.controller.host._gs.profile_dir()):
            return result
        profile_name = getattr(source, "_profile_name", "")
        if profile_name and profile_name != self.target[1]:
            return result
        current = getattr(source, "_context_is_current", None)
        if current is not None and not current():
            return result
        indices = tree.selectionModel().selectedRows(0)
        result["selection_count"] = len(indices)
        result["truncated"] = len(indices) > 256
        result["items"] = [item for index in indices[:256]
                           if (item := selection_item(source, location, index)) is not None]
        index = tree.currentIndex()
        result["current"] = selection_item(source, location, index.siblingAtColumn(0)) if index.isValid() else None
        return result

    def flush(self):
        if self.stopped or self.controller.locked or getattr(self.controller.host, "_manager_plugin", None) is not None:
            return
        self.refresh_target()
        with self.lock:
            pending, self.pending = self.pending, {}
            focused, self.focus_pending = self.focus_pending, set()
        for key, target in pending.items():
            source = self.sources.get(key, lambda: None)()
            if source is None or target != self.target:
                continue
            try:
                result = self.capture(source)
                with self.lock:
                    previous = self.cache.get(key)
                    self.cache[key] = result
                if result != previous or key in focused:
                    publish_event("selection.changed", *self.target, active=key == self.active, **result)
            except Exception as exc:
                self.controller._report(f"Plugin selection {key} failed: {exc}")

    def eventFilter(self, source, event):
        if event.type() in (QEvent.FocusIn, QEvent.MouseButtonPress):
            for key, reference in tuple(self.sources.items()):
                owner = reference()
                if owner is not None:
                    tree = owner._amethyst_selection_binding[0]
                    if source is tree or source is tree.viewport():
                        self.schedule(key, active=True)
                        break
        return False

    def shutdown(self):
        self.stopped = True
        self.timer.stop()
        app = QApplication.instance()
        app.removeEventFilter(self)
        if getattr(app, "_amethyst_plugin_selection", None) is self:
            del app._amethyst_plugin_selection
        self.sources.clear()
        with self.lock:
            self.cache.clear()
            self.pending.clear()
            self.focus_pending.clear()
            self.last_locations.clear()
