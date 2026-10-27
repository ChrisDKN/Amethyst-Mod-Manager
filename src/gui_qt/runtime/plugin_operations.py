from __future__ import annotations

import weakref
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, QTimer

from Utils import json_limits as json
from amethyst_plugins.catalog import discover
from amethyst_plugins.session import UNSUPPORTED_METHODS
from amethyst_plugins.workflows import validate


@dataclass
class _Operation:
    owner: object
    plugin: object
    target: tuple
    workflow: dict
    callback: object
    on_error: object
    on_progress: object
    single: bool
    cancelled: bool = False
    overlay: object = None


class PluginOperations(QObject):
    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.current = None
        self.stopped = False

    def target(self):
        state = self.controller.host._gs
        return (state.game.game_id if state.game else "", state.profile, str(state.profile_dir()))

    def submit(self, owner, plugin, steps, callback, *, title="Apply plugin changes?",
               summary="Run the reviewed operations.", details="", on_error=None, on_progress=None, single=False):
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Submit plugin operations on the GUI thread.")
        if self.stopped or not owner.active:
            raise RuntimeError("This plugin context has been disposed.")
        if self.current is not None:
            raise ValueError("Another plugin operation is already pending.")
        if (not callable(callback) or on_error is not None and not callable(on_error)
                or on_progress is not None and not callable(on_progress)):
            raise ValueError("Operation callbacks must be callable.")
        if not all(isinstance(value, str) for value in (title, summary, details)):
            raise ValueError("Review title, summary and details must be text.")
        if not self.controller.valid_target():
            raise ValueError("Select a configured game and profile first.")
        target = self.target()
        steps = validate(steps, set(plugin.available_methods) - UNSUPPORTED_METHODS)
        from amethyst_backend.methods import REGISTRY
        for step in steps:
            properties = (REGISTRY[step["method"]].required + " " + REGISTRY[step["method"]].optional).split()
            for key, value in (("game", target[0]), ("profile", target[1])):
                if key in properties:
                    if step["params"].get(key, value) != value:
                        raise ValueError("Plugin operations must use the captured game and profile.")
                    step["params"].setdefault(key, value)
            if step["method"].startswith("manager_plugins.metadata."):
                if step["params"].get("plugin_id", plugin.id) != plugin.id:
                    raise ValueError("Plugin metadata operations must use this plugin's namespace.")
                step["params"].setdefault("plugin_id", plugin.id)
        workflow = json.loads(json.dumps({"steps": steps, "title": title, "summary": summary, "details": details},
                                         max_bytes=3 * 1024 * 1024), max_bytes=3 * 1024 * 1024)
        request = _Operation(weakref.ref(owner), plugin, target, workflow, callback, on_error, on_progress, single)
        self.current = request
        QTimer.singleShot(0, self, lambda: self.start(request))
        return lambda: self.cancel(request)

    def live(self, request):
        owner = request.owner()
        return (not self.stopped and not request.cancelled and owner is not None and owner.active
                and request.target == self.target()
                and self.controller.plugins.get(request.plugin.id) == request.plugin)

    def invoke(self, callback, value, request):
        if callback is None or not self.live(request):
            return
        try:
            callback(value)
        except Exception as exc:
            self.controller._report(f"{request.plugin.id}: operation callback failed: {exc}")

    def start(self, request):
        if request is not self.current:
            return
        if not self.live(request):
            self.current = None
            return
        host = self.controller.host
        try:
            plugins, _ = discover(request.target[0])
            if next((p for p in plugins if p.id == request.plugin.id), None) != request.plugin:
                raise ValueError("The plugin changed. Request the operation again.")
            from gui_qt.settings.qt_plugin_overlay import QtPluginOverlay
            parent = getattr(host, "_manager_plugin", None)
            result = lambda value: self.finished(request, value)
            progress = lambda value: self.invoke(request.on_progress, value, request)
            if isinstance(parent, QtPluginOverlay) and parent._plugin_context is request.owner():
                from gui_qt.settings.manager_plugins import ManagerPluginOverlay
                def closed(changed):
                    parent.changed |= bool(changed)
                    host._manager_plugin = parent
                    if parent._close_after_operation:
                        QTimer.singleShot(0, parent, lambda: parent._complete(parent.changed))
                    elif not parent._done:
                        parent.show()
                        parent.raise_()
                parent.hide()
                try:
                    request.overlay = ManagerPluginOverlay(host, request.plugin, host._gs.game, request.target[1],
                        closed, workflow=request.workflow, on_result=result, on_progress=progress)
                    host._manager_plugin = request.overlay
                except Exception:
                    closed(False)
                    raise
            elif parent is None:
                request.overlay = host._open_manager_plugin(request.plugin, workflow=request.workflow,
                                                           operation_result=result, operation_progress=progress)
            if request.overlay is None:
                raise ValueError("The manager is busy. Request the operation again when it becomes available.")
        except Exception as exc:
            self.finished(request, {"error": str(exc)})

    def finished(self, request, outcome):
        QTimer.singleShot(0, self, lambda: self.deliver(request, outcome))

    def deliver(self, request, outcome):
        if request is not self.current:
            return
        self.current = None
        result = outcome.get("result")
        error = outcome.get("error")
        if not error and result is not None and not result["ok"]:
            error = f"Plugin workflow {result['state']}. Completed changes remain applied."
        if error:
            value = {"name": "plugin_operation_failed", "message": error, "workflow": result}
            if request.on_error is not None:
                self.invoke(request.on_error, value, request)
            elif self.live(request):
                self.controller._report(f"{request.plugin.id}: {error}")
                self.controller.host._notify(error, "error")
        else:
            if request.single and result is not None:
                result = result["items"][0]["result"]
            self.invoke(request.callback, result, request)

    def cancel(self, request):
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Cancel plugin operations on the GUI thread.")
        request.cancelled = True
        if request.overlay is not None and not request.overlay._done:
            request.overlay._finish()
        elif request is self.current:
            self.current = None

    def release(self, owner):
        if self.current is not None and self.current.owner() is owner:
            self.cancel(self.current)

    def shutdown(self):
        self.stopped = True
        if self.current is not None:
            self.cancel(self.current)
