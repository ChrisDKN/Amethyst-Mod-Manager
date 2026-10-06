"""Keep manager-owned link operations out of inventory change detection."""

from __future__ import annotations

import os
import shutil
import stat
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

import msgpack

from Utils.atomic_write import write_atomic

_guard = threading.RLock()
_active = {}
_loaded = {}
_MAX_RECORDS = 500_000


def _signature(info):
    return (info.st_size, str(info.st_mtime_ns), str(info.st_ctime_ns),
            info.st_mode, info.st_uid, info.st_gid)


def ctime_ns(info, changes):
    previous = changes.get((info.st_dev, info.st_ino))
    if previous is not None and previous[:-1] == _signature(info):
        return int(previous[-1])
    return info.st_ctime_ns


def load_changes(root):
    root = Path(root)
    with _guard:
        if root in _active:
            return _active[root][0]
    path = root / "filegraph.links"
    try:
        info = path.stat()
        stamp = info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        with _guard:
            cached = _loaded.get(root)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        if info.st_size > 128 * 1024 * 1024:
            return {}
        version, records = msgpack.unpackb(path.read_bytes(), raw=False)
        if version != 1 or len(records) > _MAX_RECORDS:
            return {}
        changes = {}
        for record in records:
            dev, ino, size, mtime, ctime, mode, uid, gid, original = record
            changes[int(dev), int(ino)] = (
                int(size), str(int(mtime)), str(int(ctime)),
                int(mode), int(uid), int(gid), str(int(original)))
        with _guard:
            if len(_loaded) >= 8:
                _loaded.pop(next(iter(_loaded)))
            _loaded[root] = stamp, changes
        return changes
    except (OSError, ValueError, TypeError, OverflowError):
        return {}


@contextmanager
def track_changes(root, log_fn=None):
    root = Path(root)
    with _guard:
        if root not in _active:
            _active[root] = [dict(load_changes(root)), 0, False]
        state = _active[root]
        state[1] += 1
    try:
        yield
    finally:
        with _guard:
            state[1] -= 1
            if not state[1]:
                del _active[root]
                if state[2]:
                    records = [(*key, *value) for key, value in state[0].items()]
                    try:
                        write_atomic(root / "filegraph.links", msgpack.packb((1, records)))
                    except (OSError, ValueError, OverflowError) as exc:
                        if log_fn is not None:
                            log_fn(f"Filegraph link cache warning: {exc}")
                    _loaded.pop(root, None)


def track_game_links(function):
    @wraps(function)
    def wrapped(game, *args, **kwargs):
        root = Path(game.get_effective_mod_staging_path()).parent
        with track_changes(root, kwargs.get("log_fn")):
            return function(game, *args, **kwargs)
    return wrapped


def _record(before, after, delta):
    before_signature, after_signature = _signature(before), _signature(after)
    if (before.st_dev != after.st_dev or before.st_ino != after.st_ino
            or after.st_nlink != before.st_nlink + delta
            or before_signature[:2] != after_signature[:2]
            or before_signature[3:] != after_signature[3:]):
        return
    key = before.st_dev, before.st_ino
    with _guard:
        for state in _active.values():
            changes = state[0]
            previous = changes.get(key)
            original = (previous[-1] if previous is not None
                        and previous[:-1] == before_signature else before_signature[2])
            if after.st_nlink:
                changes[key] = (*after_signature, original)
            else:
                changes.pop(key, None)
            if len(changes) > _MAX_RECORDS:
                changes.pop(next(iter(changes)))
            state[2] = True


def _operate(path, operation, delta, *, dir_fd=None):
    if not _active or not hasattr(os, "O_PATH"):
        return operation()
    try:
        descriptor = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=dir_fd)
    except OSError:
        return operation()
    try:
        try:
            before = os.fstat(descriptor)
        except OSError:
            return operation()
        result = operation()
        if stat.S_ISREG(before.st_mode):
            try:
                after = os.fstat(descriptor)
            except OSError:
                pass
            else:
                _record(before, after, delta)
        return result
    finally:
        os.close(descriptor)


def link(source, destination):
    return _operate(source, lambda: os.link(source, destination), 1)


def unlink(path, *, dir_fd=None, missing_ok=False):
    try:
        return _operate(path, lambda: os.unlink(path, dir_fd=dir_fd), -1, dir_fd=dir_fd)
    except FileNotFoundError:
        if not missing_ok:
            raise


def rename(source, destination):
    return _operate(source, lambda: os.rename(source, destination), 0)


def rmtree(path):
    if not _active:
        return shutil.rmtree(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for _directory, directories, files, parent in os.fwalk(
                ".", topdown=False, follow_symlinks=False, dir_fd=descriptor):
            for name in files:
                unlink(name, dir_fd=parent, missing_ok=True)
            for name in directories:
                try:
                    if stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                        unlink(name, dir_fd=parent, missing_ok=True)
                    else:
                        os.rmdir(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
    finally:
        os.close(descriptor)
    os.rmdir(path)
