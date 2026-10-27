from __future__ import annotations

import copy
from Utils import json_limits as json
import time

from .client import PluginError


def _check_interactive(method, params):
    if method == "nexus.download.manual.start" or any(
            params.get(key, False) is not False for key in ("interactive", "manual_interactive")):
        raise PluginError("Interactive operations must run outside host.apply_batch so the plugin can answer requests.")
    if method.endswith(".batch.start") and isinstance(params.get("items"), list):
        for item in params["items"]:
            if isinstance(item, dict):
                _check_interactive(method.removesuffix(".batch.start") + ".start", item)


def validate(steps, allowed):
    if not isinstance(steps, list) or not 1 <= len(steps) <= 1024:
        raise PluginError("A workflow requires 1 to 1024 steps.")
    detached = json.loads(json.dumps(steps, max_bytes=3 * 1024 * 1024), max_bytes=3 * 1024 * 1024)
    def check(value, index):
        if isinstance(value, dict):
            if "$result" in value:
                ref = value["$result"]
                if (set(value) != {"$result"} or not isinstance(ref, dict)
                        or set(ref) != {"step", "path"} or type(ref["step"]) is not int
                        or not 0 <= ref["step"] < index or not isinstance(ref["path"], list)
                        or len(ref["path"]) > 16 or any(type(key) not in (str, int) for key in ref["path"])):
                    raise PluginError("Result references must select a field from an earlier step.")
            else:
                for child in value.values():
                    check(child, index)
        elif isinstance(value, list):
            for child in value:
                check(child, index)
    for index, step in enumerate(detached):
        if (not isinstance(step, dict) or set(step) != {"method", "params"}
                or not isinstance(step["method"], str) or step["method"] not in allowed
                or step["method"].startswith(("host.", "jobs.")) or not isinstance(step["params"], dict)
                or "$result" in step["params"]):
            raise PluginError("Each workflow step needs a declared backend method and parameter object.")
        check(step["params"], index)
        _check_interactive(step["method"], step["params"])
    return detached


def arguments(value, results):
    if isinstance(value, dict):
        if "$result" in value:
            ref = value["$result"]
            selected = results[ref["step"]]["result"]
            try:
                for key in ref["path"]:
                    if isinstance(selected, list):
                        if type(key) is not int or not 0 <= key < len(selected):
                            raise KeyError(key)
                    elif not isinstance(selected, dict) or not isinstance(key, str):
                        raise KeyError(key)
                    selected = selected[key]
            except (KeyError, IndexError, TypeError) as exc:
                raise PluginError("A workflow result reference could not be resolved.") from exc
            return copy.deepcopy(selected)
        return {key: arguments(child, results) for key, child in value.items()}
    if isinstance(value, list):
        return [arguments(child, results) for child in value]
    return value


def execute(session, steps):
    results = []
    for index, step in enumerate(steps):
        if session.stop.is_set():
            return {"ok": False, "state": "cancelled", "items": results, "remaining": list(range(index, len(steps)))}
        try:
            params = arguments(step["params"], results)
            _check_interactive(step["method"], params)
            response = session._call(step["method"], params, approved=True)
            if isinstance(response, dict) and isinstance(response.get("job"), dict):
                job = response["job"]
                previous = None
                while job["state"] not in {"succeeded", "failed", "cancelled"}:
                    if session.stop.is_set() and job.get("cancellable"):
                        session.service.call("jobs.cancel", {"job_id": job["id"]})
                    if job.get("progress") != previous:
                        previous = job.get("progress")
                        session.progress({"step": index, "total_steps": len(steps), "job_id": job["id"],
                                          "state": job["state"], "progress": copy.deepcopy(previous)})
                        session.log(f"Step {index + 1}/{len(steps)}: {previous or job['kind']}")
                    time.sleep(0.15)
                    job = session.service.call("jobs.get", {"job_id": job["id"]})["job"]
                    session.jobs[job["id"]] = job
                if job["state"] != "succeeded":
                    results.append({"index": index, "state": job["state"], "error": job.get("error")})
                    return {"ok": False, "state": job["state"], "items": results, "remaining": list(range(index + 1, len(steps)))}
                response = job.get("result")
            results.append({"index": index, "state": "succeeded", "result": response})
        except Exception as exc:
            results.append({"index": index, "state": "failed", "error": {"name": getattr(exc, "name", "workflow_failed"),
                            "message": str(exc), "details": getattr(exc, "details", {})}})
            return {"ok": False, "state": "failed", "items": results, "remaining": list(range(index + 1, len(steps)))}
    return {"ok": True, "state": "succeeded", "items": results, "remaining": []}
