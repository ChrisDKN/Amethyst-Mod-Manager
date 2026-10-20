from __future__ import annotations

import json
import sys
import time


class PluginError(RuntimeError):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


class Client:
    def __init__(self):
        self._sequence = 0
        self.context = self.call("host.context")

    def call(self, method, params=None):
        self._sequence += 1
        request = {"jsonrpc": "2.0", "id": self._sequence,
                   "method": method, "params": params or {}}
        print(json.dumps(request, ensure_ascii=True), flush=True)
        line = sys.stdin.readline()
        if not line:
            raise PluginError("The plugin host disconnected.")
        response = json.loads(line)
        if response.get("id") != self._sequence:
            raise PluginError("Unexpected response from the plugin host.")
        if "error" in response:
            error = response["error"]
            raise PluginError(error["message"], error.get("data"))
        return response["result"]

    @property
    def target(self):
        return {key: self.context[key] for key in ("game", "profile")}

    def log(self, message):
        self.call("host.log", {"message": str(message)})

    def inventory(self):
        return self.call("host.inventory")

    def notify(self, notification):
        return self.call("host.notify", notification)

    def metadata(self, mods=None):
        params = {**self.target, "plugin_id": self.context["plugin_id"]}
        if mods is not None:
            params["mods"] = mods
        return self.call("manager_plugins.metadata.get", params)

    def set_metadata(self, changes, revision, *, title="Save plugin metadata?", summary="Save profile and mod metadata."):
        return self.apply("manager_plugins.metadata.set.start",
            {**self.target, "plugin_id": self.context["plugin_id"], "changes": changes,
             "revision": revision, "confirm": True}, title=title, summary=summary)

    def dismiss_notification(self, notification_id):
        return self.call("host.notifications.dismiss", {"id": notification_id})

    def selection(self, location=None, *, source_id=None):
        return self.call("host.selection", {"location": location, "source_id": source_id})

    def events(self, after=0):
        return self.call("host.events", {"after": after})

    def wait(self, response):
        job = response["job"]
        previous = None
        while job["state"] not in {"succeeded", "failed", "cancelled"}:
            progress = job.get("progress")
            if progress and progress != previous:
                label = progress.get("message") or progress.get("phase") or job["kind"]
                if progress.get("total"):
                    label = f"{label} ({progress.get('done', 0)}/{progress['total']})"
                self.log(label)
                previous = progress
            time.sleep(0.15)
            job = self.call("jobs.get", {"job_id": job["id"]})["job"]
        if job["state"] != "succeeded":
            error = job.get("error") or {}
            raise PluginError(error.get("message", "Operation cancelled."), error)
        return job.get("result") or {}

    def apply(self, method, params, *, title, summary, details=""):
        return self.call("host.apply", {"method": method, "params": params,
                                       "title": title, "summary": summary,
                                       "details": details})
