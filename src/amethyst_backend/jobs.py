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
from typing import Any, Callable


_FINAL_STATES = {"succeeded", "failed", "cancelled"}


class JobError(RuntimeError):
    def __init__(self, name: str, message: str,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


class JobCancelled(RuntimeError):
    pass


class JobNotCancellable(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _install_download(job: "_Job", log: Callable, progress: Callable) -> dict:
    from pathlib import Path

    from Utils.downloads.inventory import build_inventory
    from Utils.operations import acquire_game_operation

    profile_dir = (
        Path(job.game.get_profile_root()) / "profiles" / str(job.profile or ""))
    with acquire_game_operation(
        job.game, "install", owner=f"headless:{job.id}",
    ):
        if not profile_dir.is_dir():
            raise JobError(
                "profile_not_found",
                f"Profile '{job.profile}' is no longer available.",
                {"profile": job.profile},
            )
        from Utils.profiles.groups import get_members, is_group
        if is_group(profile_dir):
            raise JobError(
                "profile_group_requires_member",
                "Install into a member profile rather than a profile group.",
                {
                    "profile": profile_dir.name,
                    "members": get_members(profile_dir),
                },
            )

        progress(0, 1, "locate_archive")
        inventory = build_inventory(job.game, profile_dir)
        archive_id = str(job.options.get("archive_id") or "")
        archive = next(
            (item for item in inventory["archives"]
             if item.get("id") == archive_id),
            None,
        )
        if archive is None:
            raise JobError(
                "archive_not_found",
                "The selected downloaded archive is no longer available.",
                {"archive_id": archive_id},
            )
        locations = {
            item["id"]: item for item in inventory["locations"]}
        location = locations.get(archive.get("location_id"))
        candidate = Path(str(archive.get("path") or ""))
        try:
            root = Path(str(location["path"])).resolve(strict=True)
            resolved = candidate.resolve(strict=True)
            safe = (
                location is not None
                and not candidate.is_symlink()
                and candidate.is_file()
                and resolved.parent == root
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            safe = False
            resolved = candidate
        if not safe:
            raise JobError(
                "unsafe_archive",
                "The selected archive is not a regular file in its download location.",
                {"archive_id": archive_id},
            )
        progress(1, 1, "locate_archive")

        from Utils.mods.install import (
            UnsafeInstallPath,
            finish_install,
            prepare_archive,
        )

        prepared = None
        try:
            prepared = prepare_archive(
                str(resolved),
                job.game,
                profile_dir,
                log_fn=log,
                progress_fn=progress,
                preferred_name=str(job.options.get("name") or ""),
            )
            if prepared is None:
                raise JobError(
                    "install_failed",
                    "The archive could not be prepared for installation.",
                    {"archive_id": archive_id},
                )
            if prepared.is_scripted_fomod():
                raise JobError(
                    "installer_unsupported",
                    "Scripted FOMOD installers cannot run headlessly.",
                    {
                        "installer": "scripted_fomod",
                        "mod": prepared.mod_name,
                    },
                )
            if prepared.fomod_parse_failed:
                raise JobError(
                    "installer_unsupported",
                    "The FOMOD installer configuration could not be parsed.",
                    {"installer": "fomod", "mod": prepared.mod_name},
                )
            if prepared.is_multi_mod():
                raise JobError(
                    "installer_unsupported",
                    "Multi-mod archives are not supported by this headless method.",
                    {"installer": "multi_mod", "mod": prepared.mod_name},
                )

            if prepared.is_fomod():
                installer = "fomod"
                choices = prepared.fomod_has_steps()
            elif prepared.is_bain():
                installer = "bain"
                choices = True
            elif prepared.is_bundle():
                installer = "bundle"
                choices = True
            else:
                installer = "plain"
                choices = False
            installer_policy = str(
                job.options.get("installer_policy") or "reject")
            if choices and installer_policy != "defaults":
                raise JobError(
                    "installer_choices_required",
                    "The archive needs installer choices; explicitly allow defaults.",
                    {
                        "installer": installer,
                        "mod": prepared.mod_name,
                        "supported_policies": ["reject", "defaults"],
                    },
                )

            from Utils.mods.copy import resolve_target_staging
            staging_root = Path(resolve_target_staging(job.game, profile_dir))
            destination = staging_root / prepared.mod_name
            try:
                source_inside_destination = resolved.is_relative_to(
                    destination.resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                source_inside_destination = True
            if source_inside_destination:
                raise JobError(
                    "unsafe_install_destination",
                    "The destination mod folder contains the source archive.",
                    {"mod": prepared.mod_name},
                )
            if destination.is_symlink():
                raise JobError(
                    "unsafe_install_destination",
                    "The destination mod folder is a symbolic link.",
                    {"mod": prepared.mod_name},
                )
            existing = destination.exists()
            if existing and not destination.is_dir():
                raise JobError(
                    "unsafe_install_destination",
                    "The destination exists but is not a mod folder.",
                    {"mod": prepared.mod_name},
                )
            if_existing = str(job.options.get("if_exists") or "fail")
            if existing and if_existing != "replace":
                raise JobError(
                    "mod_already_exists",
                    f"Mod '{prepared.mod_name}' already exists.",
                    {"mod": prepared.mod_name},
                )

            late_conflict = {"seen": False}

            def reject_existing(_name, _conflict, _prepared):
                late_conflict["seen"] = True
                return "cancel"

            installed = finish_install(
                prepared,
                None,
                log_fn=log,
                progress_fn=progress,
                on_exists=None if if_existing == "replace" else reject_existing,
                interactive=False,
            )
            if installed is None:
                if late_conflict["seen"]:
                    raise JobError(
                        "mod_already_exists",
                        "The destination mod folder appeared during installation.",
                        {"mod": prepared.mod_name},
                    )
                raise JobError(
                    "install_failed",
                    "The archive did not produce an installed mod.",
                    {"archive_id": archive_id},
                )

            installed_path = staging_root / installed
            enabled = None
            try:
                from Utils.mods.modlist import read_modlist
                entry = next(
                    (item for item in read_modlist(profile_dir / "modlist.txt")
                     if not item.is_separator and item.name == installed),
                    None,
                )
                if entry is not None:
                    enabled = bool(entry.enabled)
            except Exception:
                pass
            return {
                "ok": True,
                "profile": profile_dir.name,
                "archive": {
                    "id": archive_id,
                    "name": archive.get("name"),
                    "path": str(resolved),
                    "location_kind": archive.get("location_kind"),
                    "nexus": archive.get("nexus"),
                    "retained": resolved.is_file(),
                },
                "mod": {
                    "name": installed,
                    "path": str(installed_path),
                    "enabled": enabled,
                },
                "installer": {
                    "type": installer,
                    "policy": installer_policy,
                    "used_defaults": bool(choices),
                },
                "replaced": existing,
            }
        except JobError:
            raise
        except UnsafeInstallPath as exc:
            raise JobError(
                "unsafe_install_destination",
                "The archive resolved to an unsafe mod folder name.",
            ) from exc
        except Exception as exc:
            raise JobError(
                "install_failed",
                "The archive could not be installed.",
                {"exception": type(exc).__name__},
            ) from exc
        finally:
            if prepared is not None:
                prepared.cleanup()


@dataclass
class _Job:
    id: str
    kind: str
    game: Any = field(repr=False)
    game_info: dict[str, str] | None
    profile: str | None
    options: dict[str, Any]
    runner: Callable[[Callable[..., None], Callable[..., None]],
                     dict[str, Any]] | None = field(default=None, repr=False)
    cancel_callback: Callable[[], None] | None = field(
        default=None, repr=False)
    cancellable: bool = False
    cancel_requested: bool = False
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
        game_info: dict[str, str] | None,
        profile: str | None,
        options: dict[str, Any] | None = None,
        *,
        runner: Callable[[Callable[..., None], Callable[..., None]],
                         dict[str, Any]] | None = None,
        cancel: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        job = _Job(
            id=uuid.uuid4().hex,
            kind=kind,
            game=game,
            game_info=dict(game_info) if game_info is not None else None,
            profile=profile,
            options=dict(options or {}),
            runner=runner,
            cancel_callback=cancel,
            cancellable=cancel is not None,
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

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._condition:
            job = self._require_locked(job_id)
            if job.state in _FINAL_STATES:
                return self._snapshot_locked(job)
            if not job.cancellable or job.cancel_callback is None:
                raise JobNotCancellable(job_id)
            callback = job.cancel_callback
            if not job.cancel_requested:
                job.cancel_requested = True
                self._event_locked(job, "cancel", requested=True)
            snapshot = self._snapshot_locked(job)
        callback()
        return snapshot

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
            if job.cancel_requested:
                raise JobCancelled("Job cancelled.")
            if job.runner is not None:
                result = job.runner(log, progress)
                if not isinstance(result, dict):
                    raise TypeError("A job runner must return a dictionary.")
                ok = True
            elif job.kind == "downloads.install":
                result = _install_download(job, log, progress)
                ok = True
            elif job.kind == "deploy":
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
            elif job.kind == "profiles.create":
                from Utils.profiles.manage import create_profile
                result = create_profile(
                    job.game,
                    job.profile,
                    profile_specific_mods=bool(
                        job.options.get("profile_specific_mods", False)),
                    copy_from=job.options.get("copy_from"),
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "profiles.rename":
                from Utils.profiles.manage import rename_profile
                result = rename_profile(
                    job.game,
                    job.profile,
                    str(job.options.get("new_name", "")),
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "profiles.remove":
                from Utils.profiles.manage import remove_profile
                result = remove_profile(
                    job.game,
                    job.profile,
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            elif job.kind == "profiles.select":
                from Utils.profiles.manage import select_profile
                result = select_profile(
                    job.game,
                    job.profile,
                    log_fn=log,
                    progress_fn=progress,
                    operation_owner=f"headless:{job.id}",
                )
                ok = True
            else:
                raise RuntimeError(f"Unsupported job kind: {job.kind}")
        except Exception as exc:
            if isinstance(exc, JobCancelled):
                self._finish(job, "cancelled", error={
                    "name": "job_cancelled",
                    "message": str(exc) or "Job cancelled.",
                    "details": {},
                })
                return
            if isinstance(exc, JobError):
                error = {
                    "name": exc.name,
                    "message": str(exc),
                    "details": exc.details,
                }
            else:
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
                        from Utils.profiles.manage import ProfileError
                        if isinstance(exc, ProfileError):
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
                                    f"{type(exc).__name__}: {exc}\n"
                                    f"{traceback.format_exc()}",
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
            job.runner = None
            job.cancel_callback = None
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
            "game": dict(job.game_info) if job.game_info is not None else None,
            "profile": job.profile,
            "options": dict(job.options),
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "progress": dict(job.progress) if job.progress else None,
            "result": dict(job.result) if job.result else None,
            "error": dict(job.error) if job.error else None,
            "cancellable": job.cancellable,
            "cancel_requested": job.cancel_requested,
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
