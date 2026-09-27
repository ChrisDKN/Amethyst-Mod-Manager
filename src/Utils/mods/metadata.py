"""Shared locking for ``meta.ini`` transactions.

Nexus, mod.io and Thunderstore metadata share the same file.  Update checks may
run concurrently, so each complete read/modify/write transaction must use the
same lock even though the integrations own different sections or keys.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


# A fixed stripe table avoids an unbounded path-to-lock cache. Hash collisions
# only serialize unrelated metadata writes; they cannot affect correctness.
_LOCKS = tuple(threading.RLock() for _ in range(64))
_held = threading.local()


def _lock_for(path) -> threading.RLock:
    key = os.path.normcase(str(Path(path).resolve(strict=False)))
    return _LOCKS[hash(key) % len(_LOCKS)]


@contextmanager
def meta_file_lock(path):
    from Utils.operations import acquire_resource_operation

    key = str(Path(path).resolve(strict=False))
    lock = _lock_for(path)
    with lock:
        held = getattr(_held, "paths", None)
        if held is None:
            held = _held.paths = set()
        if key in held:
            yield
            return
        with acquire_resource_operation(f"mod-metadata:{key}", "Mod metadata edit"):
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)


def meta_file_identity(path):
    info = os.stat(path)
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)


def locked_meta_write(func):
    """Decorate a writer whose first argument is a ``meta.ini`` path."""
    @wraps(func)
    def wrapped(meta_ini_path, *args, **kwargs):
        with meta_file_lock(meta_ini_path):
            return func(meta_ini_path, *args, **kwargs)
    return wrapped
