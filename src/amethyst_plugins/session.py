from __future__ import annotations

import copy
from Utils import json_limits as json
import os
import selectors
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .client import PluginError


COMMON_METHODS = {"system.handshake", "system.ping", "jobs.get", "jobs.list",
                  "jobs.events", "jobs.cancel", "jobs.pause", "jobs.resume"}
MAX_MESSAGE = 4 * 1024 * 1024
FINAL_STATES = {"succeeded", "failed", "cancelled"}
UNSUPPORTED_METHODS = {"launch.start", "launch.stop", "launchers.entry.run.start"}


class Session:
    def __init__(self, plugin, game, profile, settings, *, log, preview,
                 service_factory=None, action_id=None, menu_context=None, inventory_reader=None, selection_reader=None,
                 notification_sink=None, notification_dismiss=None):
        self.plugin = plugin
        from .i18n import language_code
        self.game = copy.copy(game)
        self.context = {"api_version": 1, "game": game.game_id, "profile": profile,
                        "settings": settings, "plugin_id": plugin.id,
                        "language": language_code()}
        if action_id is not None:
            self.context.update(action_id=action_id, menu_context=copy.deepcopy(menu_context))
        from .catalog import data_path, plugin_directory, settings_path
        self.context.update(plugin_dir=str(plugin_directory(plugin)),
                            settings_path=str(settings_path(plugin)),
                            data_path=str(data_path(plugin)))
        self.log = log
        self.preview = preview
        self.service_factory = service_factory
        self.inventory_reader = inventory_reader
        self.selection_reader = selection_reader
        self.notification_sink = notification_sink
        self.notification_dismiss = notification_dismiss
        self.stop = threading.Event()
        self.answer_ready = threading.Event()
        self.approved = False
        self.changed = False
        self.service = None
        self.process = None
        self.allowed = set(plugin.methods) | COMMON_METHODS
        self.mutations = set()
        self.jobs = {}
        self.workflow = None
        self.workflow_result = None
        self.progress = lambda _value: None

    def answer(self, approved):
        self.approved = bool(approved)
        self.answer_ready.set()

    def cancel(self):
        self.stop.set()
        self.answer(False)

    def _is_mutation(self, method):
        return method.removesuffix(".start") in self.mutations

    def _call(self, method, params, *, approved=False):
        if method not in self.allowed:
            raise PluginError(f"Method is not declared by this plugin: {method}")
        if method.startswith("manager_plugins.metadata.") and params.get("plugin_id") != self.plugin.id:
            raise PluginError("Plugin metadata requests must use this plugin's namespace ID.")
        if self._is_mutation(method):
            if not approved:
                raise PluginError("Changes require a preview through host.apply.")
            self.changed = True
        if self.stop.is_set():
            raise PluginError("Plugin stopped.")
        if method == "jobs.events":
            params = {**params, "wait_ms": min(int(params.get("wait_ms", 0)), 200)}
        response = self.service.call(method, params)
        if isinstance(response, dict) and isinstance(response.get("job"), dict):
            job = response["job"]
            self.jobs[job["id"]] = job
        return response

    def dispatch(self, method, params):
        if method == "host.context":
            return self.context
        if method == "host.events":
            from .events import read_events
            if set(params) - {"after"}:
                raise PluginError("host.events accepts only an after cursor.")
            return read_events(params.get("after", 0), game_id=self.context["game"],
                               profile=self.context["profile"])
        if method == "host.notify":
            if self.notification_sink is None:
                raise PluginError("The notification host is unavailable.")
            return self.notification_sink(params)
        if method == "host.notifications.dismiss":
            if set(params) != {"id"} or self.notification_dismiss is None:
                raise PluginError("Notification dismissal requires an id and a notification host.")
            self.notification_dismiss(params["id"])
            return True
        if method == "host.selection":
            if set(params) - {"location", "source_id"} or self.selection_reader is None:
                raise PluginError("The host selection snapshot is unavailable.")
            return self.selection_reader(**params)
        if method == "host.inventory":
            if params or self.inventory_reader is None:
                raise PluginError("The host inventory snapshot is unavailable.")
            return self.inventory_reader()
        if method == "host.log":
            message = params.get("message")
            if not isinstance(message, str):
                raise PluginError("Log message must be text.")
            self.log(message[:16384])
            return True
        if method in {"host.apply", "host.apply_batch"}:
            steps = None
            if method == "host.apply_batch":
                from .workflows import validate
                steps = validate(params.get("steps"), self.allowed)
                params = {**params, "method": "host.apply_batch", "params": {"steps": steps}}
            target, arguments = params.get("method"), params.get("params")
            if steps is None and (not isinstance(target, str) or target not in self.allowed or not isinstance(arguments, dict)):
                raise PluginError("Preview must specify a declared method and its parameters.")
            for field in ("title", "summary", "details"):
                if not isinstance(params.get(field, ""), str):
                    raise PluginError("Preview text must be strings.")
            self.answer_ready.clear()
            self.approved = False
            self.preview({**params, "game": self.context["game"],
                          "profile": self.context["profile"]})
            while not self.answer_ready.wait(0.1):
                if self.stop.is_set():
                    return None
            if not self.approved or self.stop.is_set():
                return None
            if steps is not None:
                from .workflows import execute
                return execute(self, steps)
            return self._call(target, arguments, approved=True)
        return self._call(method, params)

    def _respond(self, raw):
        from amethyst_backend.service import ServiceError
        request_id = None
        try:
            request = json.loads(raw, max_bytes=MAX_MESSAGE - 1)
            if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                    or type(request.get("id")) not in (str, int)
                    or not isinstance(request.get("method"), str)
                    or not isinstance(request.get("params", {}), dict)):
                raise PluginError("Expected a JSON-RPC request with an id and object parameters.")
            request_id = request["id"]
            result = self.dispatch(request["method"], request.get("params", {}))
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (ServiceError, PluginError, ValueError, TypeError) as exc:
            response = {"jsonrpc": "2.0", "id": request_id, "error": {
                "code": getattr(exc, "rpc_code", -32602), "message": str(exc),
                "data": {"name": getattr(exc, "name", "plugin_error"),
                         "details": getattr(exc, "details", {})}}}
        try:
            encoded = json.dumps(response, max_bytes=MAX_MESSAGE - 1, ensure_ascii=True)
        except (TypeError, ValueError):
            encoded = json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32603, "message": "Plugin response is invalid or exceeds the message limit."}})
        data = encoded.encode() + b"\n"
        pending = memoryview(data)
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdin, selectors.EVENT_WRITE)
            while pending and not self.stop.is_set():
                if selector.select(0.1):
                    try:
                        pending = pending[os.write(self.process.stdin.fileno(), pending):]
                    except BlockingIOError:
                        pass

    def _exchange(self):
        streams = {"stdout": b"", "stderr": b""}
        with selectors.DefaultSelector() as selector:
            for name in streams:
                selector.register(getattr(self.process, name), selectors.EVENT_READ, name)
            while selector.get_map() and not self.stop.is_set():
                for key, _events in selector.select(0.1):
                    data = os.read(key.fileobj.fileno(), 65536)
                    name = key.data
                    if not data:
                        selector.unregister(key.fileobj)
                        if streams[name]:
                            if name == "stderr":
                                self.log(streams[name].decode("utf-8", "replace"))
                            else:
                                raise PluginError("Plugin ended with an incomplete request.")
                        continue
                    streams[name] += data
                    if len(streams[name]) > MAX_MESSAGE:
                        raise PluginError("Plugin output exceeded the message limit.")
                    while b"\n" in streams[name] and not self.stop.is_set():
                        line, streams[name] = streams[name].split(b"\n", 1)
                        if name == "stdout":
                            if line.strip():
                                self._respond(line)
                        elif line:
                            self.log(line.decode("utf-8", "replace")[:16384])

    def _finish_jobs(self):
        active = [job for job in self.jobs.values() if job["state"] not in FINAL_STATES]
        if active:
            self.log("Waiting for accepted manager operations to finish…")
        cancelled = set()
        failures = []
        while active:
            remaining = []
            for old in active:
                job = self.service.call("jobs.get", {"job_id": old["id"]})["job"]
                if job["state"] in FINAL_STATES:
                    if job["state"] == "failed":
                        failures.append((job.get("error") or {}).get("message", "Manager operation failed."))
                    continue
                if self.stop.is_set() and job["cancellable"] and job["id"] not in cancelled:
                    self.service.call("jobs.cancel", {"job_id": job["id"]})
                    cancelled.add(job["id"])
                elif job.get("paused"):
                    self.service.call("jobs.resume", {"job_id": job["id"]})
                remaining.append(job)
            active = remaining
            if active:
                threading.Event().wait(0.1)
        if failures:
            raise PluginError("\n".join(failures))

    def run(self):
        error = None
        try:
            if self.service_factory is None:
                from amethyst_backend.service import AmethystService
                self.service = AmethystService({self.game.game_id: self.game})
            else:
                self.service = self.service_factory()
            handshake = self.service.call("system.handshake", {"protocol_version": 1})
            capabilities = handshake["capabilities"]
            unavailable = self.allowed - set(capabilities["methods"])
            if unavailable:
                raise PluginError("This Amethyst version does not support: " + ", ".join(sorted(unavailable)))
            unsupported = self.allowed & UNSUPPORTED_METHODS
            if unsupported:
                raise PluginError("Managed game launches are not supported by the plugin host yet: "
                                  + ", ".join(sorted(unsupported)))
            self.mutations = set(capabilities["mutations"])
            self.allowed.update(set(self.plugin.optional_methods) & set(capabilities["methods"]) - UNSUPPORTED_METHODS)
            self.context["capabilities"] = capabilities
            self.context["available_methods"] = sorted(self.allowed)
            self.context["host_capabilities"] = {"reviewed_workflows": True, "maximum_steps": 1024, "result_references": True}
            if self.workflow is not None:
                self.workflow_result = self.dispatch("host.apply_batch", self.workflow)
            else:
                self._run_process()
        except Exception as exc:
            if not (self.stop.is_set() and isinstance(exc, BrokenPipeError)):
                error = str(exc)
        finally:
            if self.process is not None:
                if self.stop.is_set() or error is not None or self.process.poll() is None:
                    try:
                        os.killpg(self.process.pid, signal.SIGTERM)
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.process.pid, signal.SIGKILL)
                        self.process.wait()
                    except ProcessLookupError:
                        pass
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    stream.close()
            if self.service is not None:
                try:
                    self.service.disconnect()
                    self._finish_jobs()
                except Exception as exc:
                    error = str(exc) if error is None else f"{error}\n{exc}"
                finally:
                    try:
                        self.service.close()
                    except Exception as exc:
                        error = str(exc) if error is None else f"{error}\n{exc}"
        return {"error": error, "stopped": self.stop.is_set(), "changed": self.changed,
                "result": self.workflow_result}

    def _run_process(self):
        environment = dict(os.environ)
        environment["AMETHYST_PLUGIN_LANGUAGE"] = self.context["language"]
        source_root = str(Path(__file__).resolve().parent.parent)
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
            source_root, environment.get("PYTHONPATH"))))
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(self.plugin.entrypoint)],
            cwd=self.plugin.entrypoint.parent, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, bufsize=0,
        )
        os.set_blocking(self.process.stdin.fileno(), False)
        self._exchange()
        while self.process.poll() is None and not self.stop.wait(0.1):
            pass
        if not self.stop.is_set() and self.process.returncode:
            raise PluginError(f"Plugin exited with code {self.process.returncode}.")
