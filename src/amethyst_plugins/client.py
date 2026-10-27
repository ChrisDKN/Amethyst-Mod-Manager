from __future__ import annotations

from Utils import json_limits as json
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
        from .i18n import Translator
        self.tr = Translator(self.context["plugin_dir"], self.context.get("language"))

    def call(self, method, params=None):
        self._sequence += 1
        request = {"jsonrpc": "2.0", "id": self._sequence,
                   "method": method, "params": params or {}}
        try:
            encoded = json.dumps(request, max_bytes=4 * 1024 * 1024 - 1, ensure_ascii=True)
        except (ValueError, TypeError) as exc:
            raise PluginError(f"Invalid plugin request: {exc}") from exc
        print(encoded, flush=True)
        line = sys.stdin.readline(4 * 1024 * 1024 + 1)
        if not line:
            raise PluginError("The plugin host disconnected.")
        try:
            response = json.loads(line, max_bytes=4 * 1024 * 1024)
        except ValueError as exc:
            raise PluginError(f"Invalid plugin host response: {exc}") from exc
        if not isinstance(response, dict) or response.get("id") != self._sequence:
            raise PluginError("Unexpected response from the plugin host.")
        if "error" in response:
            error = response["error"]
            raise PluginError(error["message"], error.get("data"))
        return response["result"]

    @property
    def target(self):
        return {key: self.context[key] for key in ("game", "profile")}

    def supports(self, method):
        return method in self.context.get("available_methods", ())

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

    def download_providers(self, source=None):
        params = {"game": self.context["game"]}
        if source is not None:
            params["source"] = source
        return self.call("manager_plugins.providers.list", params)

    def resolve_download(self, provider_id, source, *, plugin_id=None):
        return self.wait(self.call("manager_plugins.download.resolve.start", {
            "game": self.context["game"], "plugin_id": plugin_id or self.context["plugin_id"],
            "provider_id": provider_id, "source": source}))

    def download_from_provider(self, provider_id, source, revision, *, plugin_id=None,
                               title="Download archive?", summary="Download the reviewed plugin artifact.", details=""):
        return self.apply("manager_plugins.download.start", {
            "game": self.context["game"], "plugin_id": plugin_id or self.context["plugin_id"],
            "provider_id": provider_id, "source": source, "revision": revision, "confirm": True},
            title=title, summary=summary, details=details)

    def set_metadata(self, changes, revision, *, title="Save plugin metadata?", summary="Save profile and mod metadata."):
        return self.apply("manager_plugins.metadata.set.start",
            {**self.target, "plugin_id": self.context["plugin_id"], "changes": changes,
             "revision": revision, "confirm": True}, title=title, summary=summary)

    def plan_provider_install(self, items):
        graph = self.wait(self.call("manager_plugins.dependencies.get.start", {"game": self.context["game"], "items": items}))
        plans = [{"request": row, "artifact": self.resolve_download(row["provider_id"], row["source"], plugin_id=row["plugin_id"])}
                 for row in graph["items"]]
        return {"roots": items, "dependency_revision": graph["revision"], "items": plans}

    def install_provider_plan(self, plan, *, installer_policy="reject", title="Install selected mods?", summary="Download and install the listed mods and dependencies."):
        steps = provider_install_steps(self.context["game"], self.context["profile"], plan, installer_policy)
        return self.apply_batch(steps, title=title, summary=summary,
                                details=json.dumps([row["artifact"] for row in plan["items"]], indent=2))

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

    def apply_batch(self, steps, *, title="Apply workflow?", summary="Run the reviewed operations.", details=""):
        return self.call("host.apply_batch", {"steps": steps, "title": title, "summary": summary, "details": details})

    @staticmethod
    def result_ref(step, *path):
        return {"$result": {"step": step, "path": list(path)}}


def provider_install_steps(game, profile, plan, installer_policy="reject"):
    steps = [{"method": "manager_plugins.dependencies.get.start", "params": {
        "game": game, "items": plan["roots"], "revision": plan["dependency_revision"]}}]
    for row in plan["items"]:
        index = len(steps)
        steps.append({"method": "manager_plugins.download.start", "params": {
            "game": game, **row["request"], "revision": row["artifact"]["revision"], "confirm": True}})
        steps.append({"method": "downloads.install.start", "params": {
            "game": game, "profile": profile, "archive_path": Client.result_ref(index, "archive", "path"),
            "installer_policy": installer_policy}})
    return steps
