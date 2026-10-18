from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_PROCESS_LOCK = threading.Lock()
_PROCESS_STATE_LOCK = threading.Lock()
_PROCESS_HOLDER: dict[str, Any] | None = None


class OperationBusy(RuntimeError):
    def __init__(self, details: dict[str, Any]):
        self.details = details
        holder = details.get("holder") or {}
        operation = holder.get("operation") or "operation"
        game_name = holder.get("game_name")
        pid = holder.get("pid")
        suffix = f" (process {pid})" if pid is not None else ""
        target = f" for {game_name}" if game_name else ""
        super().__init__(
            f"An Amethyst {operation} is already running{target}{suffix}")


class OperationCancelled(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_call(game: Any, name: str) -> Any:
    try:
        method = getattr(game, name, None)
        return method() if callable(method) else None
    except Exception:
        return None


def _path_resource(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return f"path:{Path(value).expanduser().resolve(strict=False)}"
    except (OSError, TypeError, ValueError):
        return None


def _operation_resources(game: Any) -> list[str]:
    game_id = str(getattr(game, "game_id", "") or "").strip()
    game_name = str(getattr(game, "name", "") or "").strip()
    resources = {
        f"game:{(game_id or game_name).casefold()}",
    }
    for name in (
        "get_game_path",
        "get_profile_root",
        "get_mod_staging_path",
        "get_effective_mod_staging_path",
    ):
        resource = _path_resource(_safe_call(game, name))
        if resource:
            resources.add(resource)
    targets = _safe_call(game, "get_hardlink_deploy_targets") or ()
    try:
        for _label, path in targets:
            resource = _path_resource(path)
            if resource:
                resources.add(resource)
    except (TypeError, ValueError):
        pass
    return sorted(resources)


def _lock_dir() -> Path:
    override = os.environ.get("AMETHYST_OPERATION_LOCK_DIR")
    # HOME is shared by native, AppImage, and our Flatpak; their XDG dirs are not.
    path = (Path(override).expanduser() if override else
            Path.home() / ".local" / "share" / "AmethystModManager"
            / "operation-locks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _lock_path(directory: Path, resource: str) -> Path:
    digest = hashlib.sha256(resource.encode("utf-8")).hexdigest()
    return directory / f"{digest}.lock"


def _read_metadata(fd: int) -> dict[str, Any]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64 * 1024)
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _write_metadata(fd: int, metadata: dict[str, Any]) -> None:
    payload = json.dumps(
        metadata, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        remaining = remaining[written:]
    os.fsync(fd)


class OperationLease:
    def __init__(self, game: Any, operation: str, owner: str | None = None):
        self.game = game
        self.operation = operation
        self.owner = owner or operation
        self.resources = _operation_resources(game)
        self._files: list[tuple[int, str]] = []
        self._process_acquired = False

    def acquire(self) -> "OperationLease":
        global _PROCESS_HOLDER

        metadata = {
            "pid": os.getpid(),
            "operation": self.operation,
            "owner": self.owner,
            "game_id": str(getattr(self.game, "game_id", "") or ""),
            "game_name": str(getattr(self.game, "name", "") or ""),
            "started_at": _utc_now(),
            "resources": self.resources,
        }
        if not _PROCESS_LOCK.acquire(blocking=False):
            with _PROCESS_STATE_LOCK:
                holder = dict(_PROCESS_HOLDER or {})
            raise OperationBusy({
                "requested_operation": self.operation,
                "requested_owner": self.owner,
                "resource": "process",
                "holder": holder,
            })
        self._process_acquired = True
        with _PROCESS_STATE_LOCK:
            _PROCESS_HOLDER = metadata

        try:
            directory = _lock_dir()
            flags = os.O_RDWR | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            for resource in self.resources:
                path = _lock_path(directory, resource)
                fd = os.open(path, flags, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    holder = (_read_metadata(fd)
                              if exc.errno in (errno.EACCES, errno.EAGAIN)
                              else None)
                    os.close(fd)
                    if holder is None:
                        raise
                    raise OperationBusy({
                        "requested_operation": self.operation,
                        "requested_owner": self.owner,
                        "resource": resource,
                        "holder": holder,
                    }) from exc
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
                try:
                    _write_metadata(fd, metadata)
                except OSError:
                    pass
                self._files.append((fd, resource))
            from Utils.processes.leases import active_launches
            launches = active_launches(directory, self.resources, prune=True)
            if launches:
                raise OperationBusy({
                    "requested_operation": self.operation,
                    "requested_owner": self.owner,
                    "resource": "managed_launch",
                    "holder": launches[0],
                })
        except Exception:
            self.release()
            raise
        return self

    def __enter__(self) -> "OperationLease":
        return self.acquire()

    def release(self) -> None:
        global _PROCESS_HOLDER

        for fd, _resource in reversed(self._files):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        self._files.clear()
        if self._process_acquired:
            with _PROCESS_STATE_LOCK:
                _PROCESS_HOLDER = None
            _PROCESS_LOCK.release()
            self._process_acquired = False

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


class ResourceLease:
    def __init__(
        self,
        resource: str,
        operation: str,
        *,
        owner: str | None = None,
        cancel: Any = None,
    ):
        self.resource = resource
        self.operation = operation
        self.owner = owner or operation
        self.cancel = cancel
        self._fd: int | None = None

    def _cancelled(self) -> bool:
        return bool(self.cancel is not None and self.cancel.is_set())

    def acquire(self) -> "ResourceLease":
        metadata = {
            "pid": os.getpid(),
            "operation": self.operation,
            "owner": self.owner,
            "started_at": _utc_now(),
            "resources": [self.resource],
        }
        directory = _lock_dir()
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(_lock_path(directory, self.resource), flags, 0o600)
        try:
            while True:
                if self._cancelled():
                    raise OperationCancelled(self.operation)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if self.cancel is not None:
                        self.cancel.wait(0.1)
                    else:
                        time.sleep(0.1)
            if self._cancelled():
                raise OperationCancelled(self.operation)
            try:
                os.fchmod(fd, 0o600)
                _write_metadata(fd, metadata)
            except OSError:
                pass
            self._fd = fd
        except Exception:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        return self

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> "ResourceLease":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def acquire_game_operation(
    game: Any,
    operation: str,
    *,
    owner: str | None = None,
) -> OperationLease:
    return OperationLease(game, operation, owner)


def acquire_resource_operation(
    resource: str,
    operation: str,
    *,
    owner: str | None = None,
    cancel: Any = None,
) -> ResourceLease:
    return ResourceLease(
        resource, operation, owner=owner, cancel=cancel)
