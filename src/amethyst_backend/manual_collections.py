from __future__ import annotations

import threading
from pathlib import Path
from typing import Any


class ManualRequestError(Exception):
    def __init__(self, name: str, message: str):
        super().__init__(message)
        self.name = name


class ManualCollectionSession:
    def __init__(self, control: Any):
        self._control = control
        self._lock = threading.Lock()
        self._prompt: dict[str, Any] | None = None
        self._submitted = False
        self._jobs = None
        self._job_id = ""

    def bind(self, jobs: Any, job_id: str) -> None:
        with self._lock:
            self._jobs = jobs
            self._job_id = job_id
            if self._prompt is not None:
                jobs.set_waiting(job_id, {"type": "collection_archive",
                                          "request_id": self._prompt.get("request_id"),
                                          "method": "collections.manual.get"})
                jobs.emit(job_id, "manual_request", request=dict(self._prompt))

    def on_prompt(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._prompt = dict(payload)
            self._submitted = False
            if self._jobs is not None:
                self._jobs.set_waiting(self._job_id, {"type": "collection_archive",
                                                    "request_id": payload.get("request_id"),
                                                    "method": "collections.manual.get"})
                self._jobs.emit(self._job_id, "manual_request",
                                request=dict(self._prompt))

    def on_resolved(self, request_id: int) -> None:
        with self._lock:
            if (self._prompt is None
                    or self._prompt.get("request_id") != request_id):
                return
            self._prompt = None
            self._submitted = False
            if self._jobs is not None:
                self._jobs.set_waiting(self._job_id, None)
                self._jobs.emit(self._job_id, "manual_resolved",
                                request_id=request_id)

    def on_rejected(self, request_id: int) -> None:
        with self._lock:
            if (self._prompt is None
                    or self._prompt.get("request_id") != request_id):
                return
            self._submitted = False
            if self._jobs is not None:
                self._jobs.emit(self._job_id, "manual_rejected",
                                request_id=request_id)

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            if self._prompt is None:
                return None
            return {**self._prompt, "submitted": self._submitted}

    def submit(self, request_id: int, path: Path | None) -> dict[str, Any]:
        with self._lock:
            prompt = self._prompt
            if prompt is None or prompt.get("request_id") != request_id:
                raise ManualRequestError(
                    "collection_manual_request_stale",
                    "The manual archive request is no longer current.")
            if self._submitted:
                raise ManualRequestError(
                    "collection_manual_already_submitted",
                    "This manual archive request already has a response.")
            snapshot = dict(prompt)

        if path is None:
            if not snapshot.get("optional"):
                raise ManualRequestError(
                    "collection_manual_required",
                    "A required collection archive cannot be skipped.")
        else:
            from Nexus.nexus_download import _md5_matches, _zip_is_intact

            try:
                available = (path.is_absolute() and not path.is_symlink()
                             and path.is_file())
                actual_size = path.stat().st_size if available else 0
            except OSError:
                available = False
                actual_size = 0
            if not available:
                raise ManualRequestError(
                    "collection_manual_archive_unavailable",
                    "The selected manual archive is unavailable or unsafe.")
            expected_size = int(snapshot.get("size") or 0)
            if (expected_size > 0 and abs(actual_size - expected_size)
                    > expected_size * 0.01):
                raise ManualRequestError(
                    "collection_manual_archive_mismatch",
                    "The selected manual archive has the wrong size.")
            if (not _md5_matches(path, str(snapshot.get("md5") or ""))
                    or not _zip_is_intact(path)):
                raise ManualRequestError(
                    "collection_manual_archive_mismatch",
                    "The selected manual archive does not match the collection.")

        with self._lock:
            if (self._prompt is None
                    or self._prompt.get("request_id") != request_id):
                raise ManualRequestError(
                    "collection_manual_request_stale",
                    "The manual archive request is no longer current.")
            if self._submitted:
                raise ManualRequestError(
                    "collection_manual_already_submitted",
                    "This manual archive request already has a response.")
            self._control.manual_queue.put((request_id, path))
            self._submitted = True
            if self._jobs is not None:
                self._jobs.emit(self._job_id, "manual_response",
                                request_id=request_id,
                                action="skip" if path is None else "select")
            return {"request_id": request_id, "submitted": True}
