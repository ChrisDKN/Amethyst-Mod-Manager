from __future__ import annotations

import queue
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


_FINAL_STATES = {"succeeded", "failed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class _Job:
    id: str
    kind: str
    game: Any = field(repr=False)
    game_info: dict[str, str]
    profile: str
    options: dict[str, Any]
    state: str = "queued"
    created_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    sequence: int = 0
    events: deque[dict[str, Any]] = field(default_factory=deque)
    last_progress_event_at: float = field(default=0.0, repr=False)
    last_progress_phase: str | None = field(default=None, repr=False)


class JobManager:
    def __init__(self, *, max_jobs: int = 100, max_events: int = 2048):
        self._max_jobs = max_jobs
        self._max_events = max_events
        self._jobs: dict[str, _Job] = {}
        self._condition = threading.Condition()
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._closed = False
        self._worker = threading.Thread(
            target=self._work, name="amethyst-headless-jobs", daemon=True)
        self._worker.start()

    def submit(
        self,
        kind: str,
        game: Any,
        game_info: dict[str, str],
        profile: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job = _Job(
            id=uuid.uuid4().hex,
            kind=kind,
            game=game,
            game_info=dict(game_info),
            profile=profile,
            options=dict(options or {}),
            events=deque(maxlen=self._max_events),
        )
        with self._condition:
            if self._closed:
                raise RuntimeError("The job manager is closed.")
            self._prune_locked()
            self._jobs[job.id] = job
            self._event_locked(job, "state", state="queued")
            snapshot = self._snapshot_locked(job)
            self._queue.put(job)
        return snapshot

    def get(self, job_id: str) -> dict[str, Any]:
        with self._condition:
            return self._snapshot_locked(self._require_locked(job_id))

    def list(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        with self._condition:
            jobs = list(self._jobs.values())
            if active_only:
                jobs = [job for job in jobs if job.state not in _FINAL_STATES]
            jobs.reverse()
            return [self._snapshot_locked(job) for job in jobs]

    def active(self) -> dict[str, Any] | None:
        with self._condition:
            for job in self._jobs.values():
                if job.state not in _FINAL_STATES:
                    return self._snapshot_locked(job)
        return None

    def events(
        self,
        job_id: str,
        *,
        after: int = 0,
        wait_ms: int = 0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + (wait_ms / 1000)
        with self._condition:
            job = self._require_locked(job_id)
            while (job.sequence <= after
                   and job.state not in _FINAL_STATES
                   and wait_ms > 0):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
                job = self._require_locked(job_id)

            oldest = job.events[0]["sequence"] if job.events else job.sequence + 1
            events = [event for event in job.events
                      if event["sequence"] > after]
            return {
                "job_id": job.id,
                "events": [dict(event) for event in events],
                "next_sequence": (
                    events[-1]["sequence"] if events else after),
                "oldest_sequence": oldest,
                "truncated": bool(job.events and after < oldest - 1),
                "complete": job.state in _FINAL_STATES,
            }

    def close(self, *, wait: bool = True) -> None:
        with self._condition:
            if self._closed:
                worker = self._worker
            else:
                self._closed = True
                worker = self._worker
                self._queue.put(None)
        if wait and worker is not threading.current_thread():
            worker.join()

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                self._run(job)
            finally:
                self._queue.task_done()

    def _run(self, job: _Job) -> None:
        with self._condition:
            job.state = "running"
            job.started_at = _utc_now()
            self._event_locked(job, "state", state="running")

        def log(message: Any) -> None:
            with self._condition:
                self._event_locked(job, "log", message=str(message))

        def progress(done: Any, total: Any, phase: Any = None) -> None:
            try:
                done_value = int(done)
            except (TypeError, ValueError):
                done_value = 0
            try:
                total_value = int(total)
            except (TypeError, ValueError):
                total_value = 0
            value = {
                "done": done_value,
                "total": total_value,
                "phase": str(phase) if phase is not None else None,
            }
            with self._condition:
                job.progress = value
                now = time.monotonic()
                phase_changed = value["phase"] != job.last_progress_phase
                complete = total_value > 0 and done_value >= total_value
                if (phase_changed or complete
                        or now - job.last_progress_event_at >= 0.1):
                    job.last_progress_event_at = now
                    job.last_progress_phase = value["phase"]
                    self._event_locked(job, "progress", **value)

        try:
            result: dict[str, Any]
            if job.kind == "deploy":
                from Utils.deployment.pipeline import run_deploy_pipeline
                ok = run_deploy_pipeline(
                    job.game,
                    job.profile,
                    log_fn=log,
                    progress_fn=progress,
                    root_folder_enabled=bool(
                        job.options.get("root_folder_enabled", True)),
                    do_backup=bool(job.options.get("backup", True)),
                    operation_owner=f"headless:{job.id}",
                    raise_busy=True,
                )
                result = {"ok": True}
            elif job.kind == "restore":
                from Utils.deployment.pipeline import run_restore_pipeline
                ok = run_restore_pipeline(
                    job.game,
                    job.profile,
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                    raise_busy=True,
                )
                result = {"ok": True}
            elif job.kind == "mods.set_enabled":
                from Utils.mods.state import set_mods_enabled
                changes = [
                    (str(change["name"]), bool(change["enabled"]))
                    for change in job.options.get("changes", [])
                ]
                result = set_mods_enabled(
                    job.game,
                    job.game.get_profile_root() / "profiles" / job.profile,
                    changes,
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "mods.move":
                from Utils.mods.state import move_mods
                position = dict(job.options.get("position", {}))
                placement, anchor = next(iter(position.items()))
                result = move_mods(
                    job.game,
                    job.game.get_profile_root() / "profiles" / job.profile,
                    [str(name) for name in job.options.get("mods", [])],
                    anchor=str(anchor),
                    after=placement == "after",
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "mods.remove":
                from Utils.mods.state import remove_profile_mods
                result = remove_profile_mods(
                    job.game,
                    job.game.get_profile_root() / "profiles" / job.profile,
                    [str(name) for name in job.options.get("mods", [])],
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            else:
                raise RuntimeError(f"Unsupported job kind: {job.kind}")
        except Exception as exc:
            from Utils.operations import OperationBusy
            if isinstance(exc, OperationBusy):
                error = {
                    "name": "operation_busy",
                    "message": str(exc),
                    "details": exc.details,
                }
            else:
                from Utils.mods.state import ModStateError
                if isinstance(exc, ModStateError):
                    error = {
                        "name": exc.name,
                        "message": str(exc),
                        "details": exc.details,
                    }
                else:
                    error = {
                        "name": "internal_error",
                        "message": str(exc) or type(exc).__name__,
                        "details": {"exception": type(exc).__name__},
                    }
                    try:
                        print(
                            f"[headless] {job.kind} job {job.id} failed: "
                            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                            file=sys.stderr,
                            flush=True,
                        )
                    except Exception:
                        pass
            self._finish(job, "failed", error=error)
            return

        if ok:
            self._finish(job, "succeeded", result=result)
        else:
            self._finish(job, "failed", error={
                "name": f"{job.kind}_failed",
                "message": f"{job.kind.capitalize()} did not complete.",
            })

    def _finish(
        self,
        job: _Job,
        state: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        with self._condition:
            job.state = state
            job.finished_at = _utc_now()
            job.result = result
            job.error = error
            data: dict[str, Any] = {"state": state}
            if result is not None:
                data["result"] = result
            if error is not None:
                data["error"] = error
            self._event_locked(job, "state", **data)

    def _event_locked(self, job: _Job, event_type: str, **data: Any) -> None:
        job.sequence += 1
        job.events.append({
            "sequence": job.sequence,
            "timestamp": _utc_now(),
            "type": event_type,
            **data,
        })
        self._condition.notify_all()

    def _require_locked(self, job_id: str) -> _Job:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise KeyError(job_id) from exc

    @staticmethod
    def _snapshot_locked(job: _Job) -> dict[str, Any]:
        return {
            "id": job.id,
            "kind": job.kind,
            "state": job.state,
            "game": dict(job.game_info),
            "profile": job.profile,
            "options": dict(job.options),
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "progress": dict(job.progress) if job.progress else None,
            "result": dict(job.result) if job.result else None,
            "error": dict(job.error) if job.error else None,
            "event_sequence": job.sequence,
        }

    def _prune_locked(self) -> None:
        while len(self._jobs) >= self._max_jobs:
            completed = next(
                (job_id for job_id, job in self._jobs.items()
                 if job.state in _FINAL_STATES),
                None,
            )
            if completed is None:
                return
            del self._jobs[completed]
