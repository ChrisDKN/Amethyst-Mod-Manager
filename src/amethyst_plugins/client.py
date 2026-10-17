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
