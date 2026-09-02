"""Shared process-local locking for ``meta.ini`` transactions.

Nexus, mod.io and Thunderstore metadata share the same file.  Update checks may
run concurrently, so each complete read/modify/write transaction must use the
same lock even though the integrations own different sections or keys.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from functools import wraps


# A fixed stripe table avoids an unbounded path-to-lock cache. Hash collisions
# only serialize unrelated metadata writes; they cannot affect correctness.
_LOCKS = tuple(threading.RLock() for _ in range(64))


def _lock_for(path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    return _LOCKS[hash(key) % len(_LOCKS)]


@contextmanager
def meta_file_lock(path):
    """Hold the shared in-process lock for one metadata file."""
    lock = _lock_for(path)
    with lock:
        yield


def locked_meta_write(func):
    """Decorate a writer whose first argument is a ``meta.ini`` path."""
    @wraps(func)
    def wrapped(meta_ini_path, *args, **kwargs):
        with meta_file_lock(meta_ini_path):
            return func(meta_ini_path, *args, **kwargs)
    return wrapped
