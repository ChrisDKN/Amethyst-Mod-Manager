from __future__ import annotations

import threading
import uuid
from copy import deepcopy
from types import SimpleNamespace

from .jobs import JobCancelled, JobError


class InstallerRequests:
    def __init__(self, stop):
        self.stop = stop
        self.lock = threading.RLock()
        self.pending = {}
        self.jobs = None
        self.job_id = ""

    def bind(self, jobs, job_id):
        with self.lock:
            self.jobs, self.job_id = jobs, job_id
            for request in self.pending.values():
                jobs.set_waiting(job_id, {"type": "installer", "kind": request["type"],
                                          "request_id": request["id"], "method": "installers.requests.get"})
                self._emit(request)

    def _emit(self, request):
        if self.jobs is not None:
            self.jobs.emit(self.job_id, "installer_request", request=self._public(request))

    def _public(self, request):
        result = {key: deepcopy(value) for key, value in request.items() if not key.startswith("_")}
        wizard = request.get("_wizard")
        if wizard is not None:
            result["selections"] = deepcopy(wizard.selections)
            result["errors"] = [{"step": step, "message": error} for step, error in wizard.errors]
            result["steps"] = []
            for si in wizard.visible:
                step = wizard.config.steps[si]
                groups = []
                for gi, group in enumerate(step.groups):
                    groups.append({"index": gi, "name": group.name, "type": group.group_type,
                                   "options": [{"name": option.name, "description": option.description,
                                                "selected": option.name in wizard.selections[str(si)][group.name],
                                                "locked": wizard.states[si, gi][option.name].locked,
                                                "type": wizard.states[si, gi][option.name].plugin_type}
                                               for option in group.plugins]})
                result["steps"].append({"index": si, "name": step.name, "groups": groups})
        return result

    def current(self):
        with self.lock:
            return {"requests": [self._public(request) for request in self.pending.values()]}

    def _get(self, request_id, revision):
        request = self.pending.get(request_id)
        if request is None or request["revision"] != revision or request["_done"].is_set() or self.stop.is_set():
            raise JobError("stale_installer_request", "Read the current installer requests before answering.")
        return request

    def update(self, request_id, revision, step, group, option):
        with self.lock:
            request = self._get(request_id, revision)
            wizard = request.get("_wizard")
            try:
                if wizard is None or step not in wizard.visible or step < 0 or group < 0:
                    raise KeyError()
                wizard.states[step, group][option]
            except (KeyError, IndexError) as exc:
                raise JobError("installer_option_not_found", "The visible FOMOD option is unavailable.") from exc
            wizard.toggle(step, group, option)
            request["revision"] = uuid.uuid4().hex
            self._emit(request)
            return {"request": self._public(request)}

    def submit(self, request_id, revision, answer):
        with self.lock:
            request = self._get(request_id, revision)
            kind = request["type"]
            if kind == "fomod":
                if set(answer) != {"confirm"} or answer["confirm"] is not True:
                    raise JobError("installer_selection_invalid", "Confirm the reviewed FOMOD choices.")
                wizard = request["_wizard"]
                from .fomod import validate_choices
                validate_choices(SimpleNamespace(fomod_config=wizard.config, fomod_context=wizard.context), wizard.selections)
                value = deepcopy(wizard.selections)
            elif kind == "bain":
                value = answer.get("selected")
                available = {package["name"] for package in request["subpackages"]}
                if (set(answer) != {"selected"} or not isinstance(value, list)
                        or any(not isinstance(name, str) or name not in available for name in value)
                        or len(set(value)) != len(value)):
                    raise JobError("installer_selection_invalid", "Select available BAIN subpackages.")
                value = {"selected": value}
            elif kind == "layout":
                from .install_options import validate_options
                if set(answer) != {"content_prefix"}:
                    raise JobError("installer_selection_invalid", "Supply content_prefix for this layout.")
                value = validate_options(answer)["content_prefix"]
            else:
                value = answer.get("option")
                if set(answer) != {"option"} or value not in request["options"]:
                    raise JobError("installer_selection_invalid", "Select an available option.")
            request["_answer"] = deepcopy(value)
            request["_done"].set()
            return {"accepted": True, "request_id": request_id}

    def _ask(self, kind, **data):
        request = {"id": uuid.uuid4().hex, "revision": uuid.uuid4().hex,
                   "type": kind, **data, "_done": threading.Event()}
        with self.lock:
            self.pending[request["id"]] = request
            if self.jobs is not None:
                self.jobs.set_waiting(self.job_id, {"type": "installer", "kind": kind,
                                                   "request_id": request["id"], "method": "installers.requests.get"})
            self._emit(request)
        try:
            while not request["_done"].wait(0.2):
                if self.stop.is_set():
                    raise JobCancelled("Installation cancelled while waiting for choices.")
            if self.stop.is_set():
                raise JobCancelled("Installation cancelled while waiting for choices.")
            return request["_answer"]
        finally:
            with self.lock:
                self.pending.pop(request["id"], None)
                if self.jobs is not None:
                    self.jobs.set_waiting(self.job_id, None)

    def choice(self, *, title, prompt, options, default_index=0, **kwargs):
        return self._ask("choice", title=title, prompt=prompt, options=options, default_index=default_index)

    def layout(self, required, files, name):
        return self._ask("layout", mod=name, required_directories=sorted(required),
                         files=[destination for _source, destination, _folder in files[:100]],
                         truncated=len(files) > 100)

    def fomod(self, config, base, name, installed, active, loose, saved=None):
        from Utils.fomod.session import FomodSession
        wizard = FomodSession(config, saved=saved, context=(installed, active, loose))
        return self._ask("fomod", mod=name, title=config.name, _wizard=wizard)

    def bain(self, packages, root, name):
        return self._ask("bain", mod=name, subpackages=[
            {"name": package.name, "label": package.display_name,
             "default_selected": package.default_selected, "file_count": len(package.file_keys or ())}
            for package in packages])
