from __future__ import annotations

import threading
import weakref
from collections import deque
from copy import deepcopy

from PySide6.QtCore import QObject, QTimer, Signal

from amethyst_plugins.events import EVENT_NAMES, listen, publish_event


class PluginEvents(QObject):
    received = Signal(object)

    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.stopped = False
        self.subscriptions = {}
        self.sequence = 0
        self.pending = deque(maxlen=512)
        self.cache = {}
        self.cache_lock = threading.Lock()
        self.previous = None
        self.target = None
        self.received.connect(self.receive)
        self.unsubscribe_bus = listen(self.received.emit)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(75)
        self.timer.timeout.connect(self.refresh)
        for model in (controller.host._modlist_model, controller.host._plugin_model):
            for signal in (model.modelReset, model.layoutChanged, model.rowsInserted,
                           model.rowsRemoved, model.rowsMoved):
                signal.connect(self.schedule)
            model.dataChanged.connect(self.data_changed)

    def data_changed(self, _first, _last, roles=()):
        from gui_qt.runtime.row_flags import RowFlagsRole
        from gui_qt.runtime.plugin_columns import ColumnValuesRole
        from gui_qt.modlist.model import HighlightRole
        from gui_qt.plugins.model import PHighlightRole
        if not roles or set(roles) - {RowFlagsRole, ColumnValuesRole, HighlightRole, PHighlightRole}:
            self.schedule()

    def schedule(self, *_args):
        if not self.stopped:
            self.timer.start()

    def inventory(self):
        with self.cache_lock:
            return deepcopy(self.cache)

    def subscribe(self, owner, names, callback, *, scope="current"):
        if self.stopped or not owner.active:
            raise RuntimeError("This plugin context has been disposed.")
        if isinstance(names, str):
            names = [names]
        if (not isinstance(names, (list, tuple)) or not names or len(names) > 32
                or any(not isinstance(n, str) or n not in EVENT_NAMES for n in names)
                or not callable(callback) or scope not in ("current", "all")):
            raise ValueError("Subscribe requires known event names, a callback and current/all scope.")
        if sum(item[0]() is owner for item in self.subscriptions.values()) >= 32:
            raise ValueError("At most 32 subscriptions are supported per plugin context.")
        self.sequence += 1
        key = self.sequence
        self.subscriptions[key] = (weakref.ref(owner), frozenset(names), callback, scope)
        def unsubscribe():
            self.subscriptions.pop(key, None)
        return unsubscribe

    def release(self, owner):
        for key, item in list(self.subscriptions.items()):
            if item[0]() is owner or item[0]() is None:
                self.subscriptions.pop(key, None)

    def refresh(self, *, deliver=True):
        if self.stopped or self.controller.locked or getattr(self.controller.host, "_manager_plugin", None) is not None:
            return
        host = self.controller.host
        game = host._gs.game
        target = (game.game_id if game else "", host._gs.profile)
        if deliver and target != self.target and self.target is not None:
            self.controller.schedule_refresh()
            return
        try:
            snapshot = (self.controller.flags.snapshot() if self.controller.valid_target() else
                        {"api_version": 1, "game_id": target[0], "profile": target[1],
                         "modlist": [], "plugins": []})
        except Exception as exc:
            self.controller._report(f"Plugin inventory snapshot failed: {exc}")
            return
        with self.cache_lock:
            self.cache = deepcopy(snapshot)
        previous = self.previous if target == self.target else None
        if target != self.target:
            old = self.target
            self.target = target
            publish_event("context.changed", *target, previous=list(old) if old else None)
        self.previous = snapshot
        if previous is not None:
            for panel, prefix in (("modlist", "mods"), ("plugins", "plugins")):
                before, after = previous[panel], snapshot[panel]
                if before == after:
                    continue
                publish_event(f"{prefix}.changed", *target)
                old = {r["name"]: r for r in before}
                new = {r["name"]: r for r in after}
                for kind, names in (("added", [n for n in new if n not in old]),
                                    ("removed", [n for n in old if n not in new])):
                    if names:
                        publish_event(f"{prefix}.{kind}", *target, names=names)
                changes = [{"name": n, "enabled": new[n]["enabled"]} for n in new
                           if n in old and new[n]["enabled"] != old[n]["enabled"]]
                if changes:
                    publish_event(f"{prefix}.enabled", *target, changes=changes)
                if [n for n in old if n in new] != [n for n in new if n in old]:
                    publish_event(f"{prefix}.reordered", *target, names=list(new))
        if deliver:
            self.flush()

    def receive(self, event):
        if self.stopped:
            return
        if len(self.pending) == self.pending.maxlen:
            self.controller._report("Plugin event queue overflowed; refresh the inventory snapshot.")
        self.pending.append(event)
        self.timer.start()

    def flush(self):
        if self.controller.locked:
            return
        events = list(self.pending)
        self.pending.clear()
        for index, event in enumerate(events):
            if self.controller.locked or getattr(self.controller.host, "_manager_plugin", None) is not None:
                self.pending.extend(events[index:])
                return
            for key, (reference, names, callback, scope) in list(self.subscriptions.items()):
                owner = reference()
                if key not in self.subscriptions:
                    continue
                if owner is None or not owner.active:
                    self.subscriptions.pop(key, None)
                    continue
                if event["type"] not in names:
                    continue
                if scope == "current" and self.target != (event["game_id"], event["profile"]):
                    continue
                try:
                    callback(deepcopy(event))
                except Exception as exc:
                    self.subscriptions.pop(key, None)
                    self.controller._report(f"Plugin event subscriber failed: {exc}")

    def shutdown(self):
        self.stopped = True
        self.timer.stop()
        self.unsubscribe_bus()
        self.subscriptions.clear()
        self.pending.clear()
