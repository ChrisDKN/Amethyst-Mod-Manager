from __future__ import annotations

import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .jobs import JobCancelled, JobError


_ACTIVE_STATES = {"starting", "running", "stopping"}
_FINAL_STATES = {"stopped", "finished", "failed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class LaunchInProgress(RuntimeError):
    def __init__(self, launch: dict[str, Any]):
        super().__init__("A managed game launch is already active.")
        self.launch = launch


class LaunchNotFound(KeyError):
    pass


class LaunchNotStoppable(RuntimeError):
    pass


def entry_launch_runner(entry, params, manager, check_cancelled=lambda: None):
    def run(log, progress):
        from amethyst_plugins.launcher_runtime import launch_entry
        from amethyst_plugins.launchers import linked_profiles, ProviderError
        from Utils.processes import game as processes
        from Utils.processes import report as reports
        check_cancelled()
        links = linked_profiles(entry["provider_id"], [entry["id"]], installation=entry)
        deployed = [(game, profile) for game, profile in links
                    if game.get_deploy_active()
                    and profile.name == (game.get_last_deployed_profile() or "default")
                    and getattr(game, "vfs_launch_enabled", False)]
        if len(deployed) > 1:
            raise ProviderError("Multiple deployed VFS profiles match this application. Restore the extra deployments before launching.")
        target = deployed[0] if deployed and "executable" not in params and entry["installed"] else None
        identity = {"id": entry["id"], "name": entry["name"], "provider_id": entry["provider_id"]}
        if target is not None:
            identity.update(id=target[0].game_id, name=target[0].name, launcher_id=entry["id"])
        launch = manager.reserve(identity, target[1].name if target else None)
        launch_id = launch["id"]
        if target is not None:
            game, profile = target
            game._plugin_launcher_entry = dict(entry)
            if "arguments" in params:
                game._plugin_launcher_entry["arguments"] = params["arguments"]
            log(f"Launching {entry['name']} through {game.name}'s deployed VFS profile: {profile.name}.")
            return launch_runner(game, profile, launch_id, manager, check_cancelled=check_cancelled)(log, progress)
        session = manager.session(launch_id)
        def record(message):
            log(message)
            manager.log(launch_id, message)
        failures = []
        lease = None
        lease_record = None
        dispatched = False
        try:
            if links:
                from Utils.operations import acquire_game_operation, _lock_dir, _operation_resources
                from Utils.processes.leases import save_launch
                lease = acquire_game_operation(links[0][0], "launch", owner=f"headless:{launch_id}")
                lease.acquire()
                resources = sorted({resource for game, _ in links for resource in _operation_resources(game)})
                lease_record = _lock_dir() / f"launch-{launch_id}.json"
                session.set_identity_callback(lambda identity: save_launch(
                    _lock_dir(), launch_id, links[0][0], resources, identity))
            with processes.bind(session), reports.report(failures.append) as report:
                launch_entry(entry, executable=params.get("executable"),
                             arguments=params.get("arguments"), log=record, check_cancelled=check_cancelled)
                report.finish()
            if failures or not report.spawned:
                raise JobError("launch_failed", failures[-1] if failures else "No application process started.")
            result = {"launch": manager.dispatched(launch_id)}
            dispatched = True
            return result
        except Exception as exc:
            manager.fail(launch_id, "launch_failed", str(exc))
            raise
        finally:
            if lease_record is not None and not dispatched:
                from Utils.processes.game import matching_pids
                if not session._live_roots() and matching_pids(session.markers) == set():
                    lease_record.unlink(missing_ok=True)
            if lease is not None:
                lease.release()
    return run


class LaunchManager:
    def __init__(self, *, max_logs: int = 200):
        self._lock = threading.RLock()
        self._max_logs = max_logs
        self._launch: dict[str, Any] | None = None
        self._session: Any = None

    def reserve(
        self, game: dict[str, str], profile: str,
    ) -> dict[str, Any]:
        with self._lock:
            if (self._launch is not None
                    and self._launch["state"] in _ACTIVE_STATES):
                raise LaunchInProgress(self._snapshot_locked())
            launch_id = uuid.uuid4().hex
            from Utils.processes.game import LaunchSession
            session = LaunchSession(
                game.get("name") or game.get("id") or "game",
                log_fn=lambda message: self.log(launch_id, message),
            )
            session.set_state_callback(
                lambda active: self._state_changed(launch_id, active))
            self._launch = {
                "id": launch_id,
                "state": "starting",
                "game": dict(game),
                "profile": profile,
                "created_at": _utc_now(),
                "started_at": None,
                "dispatched_at": None,
                "finished_at": None,
                "error": None,
                "sequence": 0,
                "logs": deque(maxlen=self._max_logs),
            }
            self._session = session
            return self._snapshot_locked()

    def get(self, launch_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if self._launch is None:
                if launch_id is None:
                    return None
                raise LaunchNotFound(launch_id)
            if launch_id is not None and self._launch["id"] != launch_id:
                raise LaunchNotFound(launch_id)
            return self._snapshot_locked()

    def session(self, launch_id: str) -> Any:
        with self._lock:
            self._require_locked(launch_id)
            return self._session

    def log(self, launch_id: str, message: Any) -> None:
        text = str(message).replace("\x00", "")
        if len(text) > 4000:
            text = text[:3997] + "..."
        with self._lock:
            if self._launch is None or self._launch["id"] != launch_id:
                return
            self._launch["sequence"] += 1
            self._launch["logs"].append({
                "sequence": self._launch["sequence"],
                "timestamp": _utc_now(),
                "message": text,
            })

    def dispatched(self, launch_id: str) -> dict[str, Any]:
        with self._lock:
            launch = self._require_locked(launch_id)
            if launch["dispatched_at"] is None:
                launch["dispatched_at"] = _utc_now()
                launch["sequence"] += 1
            if launch["state"] == "starting" and self._session.active:
                launch["state"] = "running"
                launch["started_at"] = launch["started_at"] or _utc_now()
            return self._snapshot_locked()

    def fail(
        self,
        launch_id: str,
        name: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            if self._launch is None or self._launch["id"] != launch_id:
                return None
            if self._launch["state"] in _FINAL_STATES:
                return self._snapshot_locked()
            self._launch["state"] = "failed"
            self._launch["finished_at"] = _utc_now()
            self._launch["error"] = {
                "name": name,
                "message": str(message),
                "details": dict(details or {}),
            }
            self._launch["sequence"] += 1
            session = self._session
            snapshot = self._snapshot_locked()
        if session is not None:
            session.cancel()
        return snapshot

    def stop(self, launch_id: str) -> dict[str, Any]:
        with self._lock:
            launch = self._require_locked(launch_id)
            session = self._session
            if (launch["state"] != "running" or session is None
                    or not session.active):
                raise LaunchNotStoppable(launch_id)
            launch["state"] = "stopping"
            launch["sequence"] += 1
        if not session.stop():
            with self._lock:
                if (self._launch is not None
                        and self._launch["id"] == launch_id
                        and self._launch["state"] == "stopping"):
                    self._launch["state"] = "finished"
                    self._launch["finished_at"] = _utc_now()
                    self._launch["sequence"] += 1
            raise LaunchNotStoppable(launch_id)
        with self._lock:
            return self._snapshot_locked()

    def close(self) -> None:
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            session.cancel()

    def _state_changed(self, launch_id: str, active: bool) -> None:
        with self._lock:
            if self._launch is None or self._launch["id"] != launch_id:
                return
            state = self._launch["state"]
            if active:
                if state == "starting":
                    self._launch["state"] = "running"
                    self._launch["started_at"] = _utc_now()
                    self._launch["sequence"] += 1
                return
            if state in _FINAL_STATES:
                return
            self._launch["state"] = (
                "stopped" if state == "stopping" else "finished")
            self._launch["finished_at"] = _utc_now()
            self._launch["sequence"] += 1

    def _require_locked(self, launch_id: str) -> dict[str, Any]:
        if self._launch is None or self._launch["id"] != launch_id:
            raise LaunchNotFound(launch_id)
        return self._launch

    def _snapshot_locked(self) -> dict[str, Any]:
        assert self._launch is not None
        state = self._launch["state"]
        return {
            "id": self._launch["id"],
            "state": state,
            "game": dict(self._launch["game"]),
            "profile": self._launch["profile"],
            "created_at": self._launch["created_at"],
            "started_at": self._launch["started_at"],
            "dispatched_at": self._launch["dispatched_at"],
            "finished_at": self._launch["finished_at"],
            "error": (
                dict(self._launch["error"])
                if self._launch["error"] is not None else None),
            "active": state in _ACTIVE_STATES,
            "stop_supported": bool(
                state == "running" and self._session is not None
                and self._session.active),
            "sequence": self._launch["sequence"],
            "logs": [dict(item) for item in self._launch["logs"]],
        }


def launch_runner(
    game: Any,
    profile_dir: Path,
    launch_id: str,
    manager: LaunchManager,
    *,
    check_cancelled: Callable = lambda: None,
) -> Callable:
    def run(log: Callable, progress: Callable) -> dict[str, Any]:
        session = manager.session(launch_id)

        def launch_log(message: Any) -> None:
            text = str(message)
            manager.log(launch_id, text)
            log(text)
            from Utils.processes import report as launch_report
            launch_report.note(text)

        progress(0, 2, "prepare_launch")
        lease = None
        record = None
        launched = False
        profile_changed = False
        previous_profile = getattr(game, "_active_profile_dir", None)
        try:
            check_cancelled()
            from Utils.operations import acquire_game_operation
            lease = acquire_game_operation(
                game, "launch", owner=f"headless:{launch_id}")
            lease.acquire()
            from Utils.operations import _lock_dir
            from Utils.processes.leases import save_launch
            directory = _lock_dir()
            record = directory / f"launch-{launch_id}.json"
            session.set_identity_callback(lambda identity: save_launch(
                directory, launch_id, game, lease.resources, identity))

            game.set_active_profile_dir(profile_dir)
            profile_changed = True
            loaded = game.load_paths()
            if not loaded or not game.is_configured():
                raise JobError(
                    "launch_failed",
                    "The selected profile does not have a usable game path.",
                    {"launch_id": launch_id, "profile": profile_dir.name},
                )
            if not bool(game.get_deploy_active()):
                raise JobError(
                    "launch_requires_deployment",
                    "Deploy the selected profile before launching it.",
                    {"launch_id": launch_id, "profile": profile_dir.name},
                )
            deployed = str(game.get_last_deployed_profile() or "default")
            if deployed != profile_dir.name:
                raise JobError(
                    "launch_profile_mismatch",
                    f"Profile '{deployed}' is deployed, not "
                    f"'{profile_dir.name}'.",
                    {
                        "launch_id": launch_id,
                        "profile": profile_dir.name,
                        "deployed_profile": deployed,
                    },
                )

            progress(1, 2, "dispatch_launch")
            failures: list[str] = []

            def failed(detail: str) -> None:
                from Utils.processes import report as launch_report
                message = launch_report.strip_marker(str(detail or ""))
                message = message or "The game launch did not start a process."
                failures.append(message)
                manager.fail(
                    launch_id, "launch_failed", message,
                    {"launch_id": launch_id, "profile": profile_dir.name})

            from Utils.executables.launch import launch_game
            from Utils.processes import game as game_process
            from Utils.processes import report as launch_report
            with game_process.bind(session):
                with launch_report.report(failed) as report:
                    check_cancelled()
                    launch_game(game, log_fn=launch_log)
                    report.finish()

            if failures:
                raise JobError(
                    "launch_failed",
                    failures[-1],
                    {"launch_id": launch_id, "profile": profile_dir.name},
                )
            if not report.spawned:
                raise JobError(
                    "launch_failed",
                    "The game launch did not start a process.",
                    {"launch_id": launch_id, "profile": profile_dir.name},
                )

            progress(2, 2, "dispatch_launch")
            launch = manager.dispatched(launch_id)
            launched = True
            return {
                "ok": True,
                "dispatched": True,
                "launch": launch,
            }
        except Exception as exc:
            from Utils.operations import OperationBusy
            if isinstance(exc, JobCancelled):
                session.cancel()
                manager.fail(launch_id, "launch_cancelled", str(exc))
                raise
            if isinstance(exc, OperationBusy):
                manager.fail(
                    launch_id, "operation_busy", str(exc), exc.details)
                raise
            if isinstance(exc, JobError):
                manager.fail(launch_id, exc.name, str(exc), exc.details)
                raise
            manager.fail(
                launch_id,
                "launch_failed",
                str(exc) or type(exc).__name__,
                {"launch_id": launch_id, "exception": type(exc).__name__},
            )
            raise JobError(
                "launch_failed",
                "The game could not be launched.",
                {"launch_id": launch_id, "exception": type(exc).__name__},
            ) from exc
        finally:
            if record is not None and not launched:
                from Utils.processes.game import matching_pids
                if not session._live_roots() and matching_pids(session.markers) == set():
                    record.unlink(missing_ok=True)
            if not launched and profile_changed:
                try:
                    game.set_active_profile_dir(previous_profile)
                    game.load_paths()
                except Exception:
                    pass
            if lease is not None:
                lease.release()

    return run
