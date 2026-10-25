from __future__ import annotations

from contextvars import ContextVar
import json
import threading

from .jobs import JobCancelled, JobError, _install_download
from .methods import REGISTRY


METHODS = {name: method.item_method for name, method in REGISTRY.items() if method.family == "batch"}
READ_METHODS = frozenset(name for name in METHODS if not REGISTRY[name].mutating)
_CACHE = ContextVar("headless_batch_read_cache", default=None)


def cached_read(key, read):
    cache = _CACHE.get()
    if cache is None:
        return read()
    if key not in cache:
        cache[key] = read()
    return cache[key]


def error_record(exc):
    return {"name": getattr(exc, "name", "operation_failed"),
            "message": str(exc), "details": getattr(exc, "details", {})}


class BatchControl:
    def __init__(self):
        self.condition = threading.Condition()
        self.cancelled = False
        self.paused = False
        self.current = None

    def cancel(self):
        with self.condition:
            self.cancelled = True
            current = self.current
            self.condition.notify_all()
        if current is not None and current.cancel_callback:
            current.cancel_callback()

    def pause(self):
        with self.condition:
            self.paused = True
            current = self.current
        if current is not None and current.pause_callback:
            current.pause_callback()

    def resume(self):
        with self.condition:
            self.paused = False
            current = self.current
            self.condition.notify_all()
        if current is not None and current.resume_callback:
            current.resume_callback()

    def checkpoint(self, report):
        with self.condition:
            while self.paused and not self.cancelled:
                self.condition.wait(0.25)
            if self.cancelled:
                raise JobCancelled("Batch cancelled between items.", {"batch": report})

    def activate(self, job):
        with self.condition:
            self.current = job
            cancelled, paused = self.cancelled, self.paused
        if job is not None:
            if cancelled and job.cancel_callback:
                job.cancel_callback()
            elif paused and job.pause_callback:
                job.pause_callback()


def start(service, method, params):
    from .service import ServiceError

    service._require_idle_state()
    service._check_params(params, {"game", "profile", "items", "on_error"}, {"game", "items"})
    items = params["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 256:
        raise ServiceError(-32602, "invalid_params", "'items' must contain 1 to 256 parameter objects.")
    on_error = params.get("on_error", "stop")
    if on_error not in ("stop", "continue"):
        raise ServiceError(-32602, "invalid_params", "'on_error' must be stop or continue.")
    common = {key: params[key] for key in ("game", "profile") if key in params}
    if method not in READ_METHODS:
        _validate_targets(method, items)
    with service._jobs.prepare() as jobs:
        for index, item in enumerate(items):
            if not isinstance(item, dict) or set(item) & {"game", "profile"}:
                raise ServiceError(-32602, "invalid_params", "Each item must be a parameter object without game/profile overrides.", {"index": index})
            try:
                service.call(METHODS[method], {**common, **item})
            except ServiceError as exc:
                raise ServiceError(exc.rpc_code, exc.name, str(exc), {**exc.details, "index": index}) from exc
    if len(jobs) != len(items):
        raise RuntimeError("Every batch item must prepare exactly one job.")
    control = BatchControl()
    requests = next((job.installer_requests for job in jobs if job.installer_requests is not None), None)
    if requests is not None:
        for child in jobs:
            if child.installer_requests is not None:
                child.installer_requests = requests
    pausable = method in {"downloads.interrupted.resume.batch.start", "manager_plugins.download.batch.start"}
    job = service._jobs.submit(
        method.removesuffix(".start"), jobs[0].game, jobs[0].game_info,
        jobs[0].profile, {"item_count": len(items), "on_error": on_error},
        runner=runner(jobs, control, on_error, read_only=method in READ_METHODS),
        cancel=control.cancel, installer_requests=requests, pause=control.pause if pausable else None,
        resume=control.resume if pausable else None)
    return {"job": job}


def _validate_targets(method, items):
    from .service import ServiceError

    seen = set()
    paths = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ServiceError(-32602, "invalid_params", "Each batch item must be a parameter object.", {"index": index})
        key = next((key for key in ("entry", "download_id", "mod", "key", "name") if item.get(key)), None)
        value = item.get(key) if key else None
        if value is not None and not isinstance(value, str):
            continue
        if key == "entry":
            parts = tuple(value.replace("\\", "/").casefold().split("/"))
            if any(parts[:len(previous)] == previous or previous[:len(parts)] == parts for previous in paths):
                raise ServiceError(-32602, "invalid_params", "Save selections cannot overlap.", {"index": index})
            paths.append(parts)
            value = parts[-1] if method == "saves.transfer.batch.start" else parts
        elif isinstance(value, str):
            value = value.casefold()
        if value is not None:
            if value in seen:
                raise ServiceError(-32602, "invalid_params", "Batch mutation targets must be unique.", {"index": index})
            seen.add(value)


def runner(jobs, control, on_error, *, read_only=False):
    def run(log, progress):
        results = []
        cache = {}
        token = _CACHE.set(cache)

        def report():
            failed = sum(row["state"] == "failed" for row in results)
            return {"ok": len(results) == len(jobs) and all(row["state"] == "succeeded" for row in results),
                    "total": len(jobs),
                    "completed": sum(row["state"] == "succeeded" for row in results),
                    "cancelled": sum(row["state"] == "cancelled" for row in results),
                    "failed": failed, "items": list(results),
                    "remaining": list(range(len(results), len(jobs)))}

        try:
            for index, job in enumerate(jobs):
                control.checkpoint(report())
                control.activate(job)
                if job.installer_requests is not None:
                    job.installer_requests.stop = job.cancel_event
                progress(index, len(jobs), f"Item {index + 1}/{len(jobs)}: {job.kind}")
                try:
                    control.checkpoint(report())
                    key = (job.kind, json.dumps(job.options, sort_keys=True))
                    def execute():
                        child_progress = lambda done, total, phase=None: progress(
                            index, len(jobs), f"Item {index + 1}/{len(jobs)}: {phase or job.kind} ({done}/{total})")
                        if job.runner:
                            return job.runner(log, child_progress)
                        if job.kind in {"downloads.install", "mods.reinstall"}:
                            return _install_download(job, log, child_progress)
                        raise RuntimeError(f"Unsupported prepared job kind: {job.kind}")
                    result = cached_read(("item", key), execute) if read_only else execute()
                    results.append({"index": index, "state": "succeeded", "result": result})
                except JobCancelled as exc:
                    results.append({"index": index, "state": "cancelled", "error": error_record(exc)})
                    raise JobCancelled(str(exc), {"batch": report()}) from exc
                except Exception as exc:
                    results.append({"index": index, "state": "failed", "error": error_record(exc)})
                    if on_error == "stop" or str(getattr(exc, "name", "")).endswith("rollback_failed"):
                        break
                finally:
                    control.activate(None)
                progress(len(results), len(jobs), f"Completed item {index + 1}/{len(jobs)}")
            outcome = report()
            if outcome["failed"]:
                raise JobError("batch_failed", "One or more batch items failed.", {"batch": outcome})
            return outcome
        finally:
            _CACHE.reset(token)
    return run
