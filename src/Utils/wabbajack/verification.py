from __future__ import annotations

import os
import stat
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar

from .paths import WabbajackError

_current = ContextVar("wabbajack_verification", default=None)


class VerificationCache:
    def __init__(self, limit=32768):
        self.limit = limit
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    def read(self, path, kind, operation, stop, progress, log=None):
        from .diagnostics import emit
        def stamp():
            value = path.stat()
            if not stat.S_ISREG(value.st_mode):
                raise WabbajackError(f"Verification requires a regular file: {path}")
            return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
        if stop is not None and stop.is_set():
            raise InterruptedError("Preflight stopped")
        before = stamp()
        key = os.path.abspath(path), kind
        with self._lock:
            found = self._entries.get(key)
            if found is not None and found[0] == before:
                self._entries.move_to_end(key)
                emit(log, "verification.cache_hit", path=path, kind=kind,
                     bytes=before[2])
                return found[1]
        if progress:
            progress(path)
        started = time.monotonic()
        emit(log, "verification.started", path=path, kind=kind,
             bytes=before[2])
        result = operation()
        if stop is not None and stop.is_set():
            raise InterruptedError("Preflight stopped")
        if stamp() != before:
            raise WabbajackError(f"File changed during verification: {path}")
        with self._lock:
            self._entries[key] = before, result
            self._entries.move_to_end(key)
            while len(self._entries) > self.limit:
                self._entries.popitem(last=False)
        emit(log, "verification.completed", path=path, kind=kind,
             bytes=before[2], elapsed_seconds=round(time.monotonic() - started, 3))
        return result


@contextmanager
def verification_scope(cache, stop=None, progress=None, log=None):
    token = _current.set((cache, stop, progress, log))
    try:
        yield
    finally:
        _current.reset(token)


def verified_read(path, kind, operation):
    context = _current.get()
    if context is None:
        return operation()
    cache, stop, progress, log = context
    return cache.read(path, kind, operation, stop, progress, log)
