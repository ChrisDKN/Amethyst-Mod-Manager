from __future__ import annotations

import json
import threading
import uuid
import weakref
from collections import deque
from copy import copy, deepcopy
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QThread, Signal

from gui_qt.runtime.safe_emit import safe_emit


@dataclass
class _Request:
    owner: object
    plugin: object
    target: tuple
    games: dict
    method: str
    params: dict
    callback: object
    on_error: object
    on_progress: object
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    stop: threading.Event = field(default_factory=threading.Event)


class PluginReads(QObject):
    completed = Signal(str, object, object)
    progressed = Signal(str, object)

    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.requests = {}
        self.pending = deque()
        self.lock = threading.Lock()
        self.running = False
        self.stopped = False
        self.target = self.current_target()
        self.completed.connect(self.deliver)
        self.progressed.connect(self.progress)

    def current_target(self):
        state = self.controller.host._gs
        game = state.game
        return (game.game_id if game else "", state.profile, str(state.profile_dir()))

    def refresh(self):
        target = self.current_target()
        changed = target != self.target
        self.target = target
        for request in list(self.requests.values()):
            if changed or self.controller.plugins.get(request.plugin.id) != request.plugin:
                self.cancel(request)

    def read(self, owner, plugin, method, params, callback, *, on_error=None, on_progress=None):
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Submit plugin reads on the GUI thread.")
        if self.stopped or not owner.active:
            raise RuntimeError("This plugin context has been disposed.")
        if not isinstance(method, str) or method not in plugin.available_methods:
            raise ValueError(f"Undeclared plugin method: {method}")
        if (not callable(callback) or on_error is not None and not callable(on_error)
                or on_progress is not None and not callable(on_progress)):
            raise ValueError("Read callbacks must be callable.")
        params = {} if params is None else params
        if not isinstance(params, dict):
            raise ValueError("Read parameters must be a JSON object.")
        raw = json.dumps(params, allow_nan=False, ensure_ascii=True)
        if len(raw) > 1024 * 1024:
            raise ValueError("Read parameters exceed 1 MiB.")
        params = json.loads(raw)
        self.refresh()
        if params.get("game", self.target[0]) != self.target[0] or params.get("profile", self.target[1]) != self.target[1]:
            raise ValueError("Plugin reads must use the active game and profile.")
        if method.startswith("manager_plugins.metadata.") and params.get("plugin_id", plugin.id) != plugin.id:
            raise ValueError("Plugin metadata reads must use this plugin's namespace ID.")
        if len(self.requests) >= 128 or sum(r.owner() is owner for r in self.requests.values()) >= 32:
            raise ValueError("Too many pending plugin reads. Cancel obsolete requests first.")
        from Utils.games.registry import _GAMES
        games = {game.game_id: copy(game) for game in _GAMES.values()}
        game = self.controller.host._gs.game
        if game is not None:
            games[game.game_id] = copy(game)
        request = _Request(weakref.ref(owner), plugin, self.target, games, method, params,
                           callback, on_error, on_progress)
        self.requests[request.id] = request
        with self.lock:
            self.pending.append(request)
            if not self.running:
                self.running = True
                threading.Thread(target=self.drain, daemon=True, name="plugin-reads").start()
        return lambda: self.cancel_request(request.id)

    def cancel_request(self, request_id):
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Cancel plugin reads on the GUI thread.")
        request = self.requests.get(request_id)
        if request is not None:
            self.cancel(request)

    def cancel(self, request):
        request.stop.set()
        self.requests.pop(request.id, None)
        request.callback = request.on_error = request.on_progress = None
        with self.lock:
            self.pending = deque(r for r in self.pending if r is not request)

    def release(self, owner):
        for request in list(self.requests.values()):
            if request.owner() is owner:
                self.cancel(request)

    def drain(self):
        while True:
            with self.lock:
                if not self.pending:
                    self.running = False
                    return
                request = self.pending.popleft()
            result, error = None, None
            if request.stop.is_set():
                continue
            try:
                result = self.execute(request)
            except Exception as exc:
                error = {"name": getattr(exc, "name", "plugin_read_failed"), "message": str(exc),
                         "details": deepcopy(getattr(exc, "details", {}))}
            safe_emit(self.completed, request.id, result, error)

    def execute(self, request):
        from amethyst_backend.service import AmethystService
        service = AmethystService(request.games)
        try:
            record, = service.call("system.schema.get", {"method": request.method})["methods"]
            if record["mutating"]:
                raise ValueError("Mutations require the reviewed plugin operation flow.")
            params = deepcopy(request.params)
            properties = record["params"]["properties"]
            for key, value in (("game", request.target[0]), ("profile", request.target[1])):
                if key in properties:
                    params.setdefault(key, value)
            if request.method == "manager_plugins.metadata.get":
                params.setdefault("plugin_id", request.plugin.id)
            if request.method == "jobs.events":
                params["wait_ms"] = min(int(params.get("wait_ms", 0)), 200)
            if request.stop.is_set():
                return None
            response = service.call(request.method, params)
            if not request.method.endswith(".start") or not isinstance(response, dict) or "job" not in response:
                return response
            job = response["job"]
            previous, cancelled = None, False
            while True:
                update = {key: deepcopy(job.get(key)) for key in ("id", "state", "progress")}
                if update != previous:
                    safe_emit(self.progressed, request.id, update)
                    previous = update
                if job["state"] in ("succeeded", "failed", "cancelled", "interrupted"):
                    if job["state"] != "succeeded":
                        from amethyst_plugins.client import PluginError
                        error = job.get("error") or {}
                        exc = PluginError(error.get("message", "Plugin read cancelled."), error.get("details", {}))
                        exc.name = error.get("name", job["state"])
                        raise exc
                    return job.get("result")
                if request.stop.is_set() and job.get("cancellable") and not cancelled:
                    service.call("jobs.cancel", {"job_id": job["id"]})
                    cancelled = True
                if request.stop.is_set():
                    threading.Event().wait(0.1)
                else:
                    request.stop.wait(0.1)
                job = service.call("jobs.get", {"job_id": job["id"]})["job"]
        finally:
            service.close()

    def live(self, request):
        owner = request.owner()
        return (not self.stopped and not request.stop.is_set() and owner is not None and owner.active
                and self.current_target() == request.target
                and self.controller.plugins.get(request.plugin.id) == request.plugin)

    def invoke(self, callback, value, plugin):
        try:
            callback(value)
        except Exception as exc:
            self.controller._report(f"{plugin.id}: read callback failed: {exc}")

    def progress(self, request_id, value):
        request = self.requests.get(request_id)
        if request is not None and request.on_progress is not None and self.live(request):
            self.invoke(request.on_progress, value, request.plugin)

    def deliver(self, request_id, result, error):
        request = self.requests.pop(request_id, None)
        if request is None or not self.live(request):
            return
        if error is None:
            self.invoke(request.callback, result, request.plugin)
        elif request.on_error is not None:
            self.invoke(request.on_error, error, request.plugin)
        else:
            self.controller._report(f"{request.plugin.id}: {request.method}: {error['message']}")

    def shutdown(self):
        self.stopped = True
        for request in list(self.requests.values()):
            self.cancel(request)
