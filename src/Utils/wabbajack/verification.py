from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from pathlib import Path

from .diagnostics import emit, emit_exception
from .paths import WabbajackError

_current = ContextVar("wabbajack_verification", default=None)
VERIFICATION_WORKERS = min(4, os.cpu_count() or 1)


def parallel_verify(operation, items, stop=None, *, size=None):
    items = iter(items)
    pending = set()
    with ThreadPoolExecutor(max_workers=VERIFICATION_WORKERS,
                            thread_name_prefix="wabbajack-check") as pool:
        try:
            while True:
                if stop is not None and stop.is_set():
                    raise InterruptedError("Preflight stopped")
                while len(pending) < VERIFICATION_WORKERS * 2:
                    if stop is not None and stop.is_set():
                        raise InterruptedError("Preflight stopped")
                    try:
                        item = next(items)
                    except StopIteration:
                        break
                    if size is not None and size(item) < 1024 * 1024:
                        yield operation(item)
                    else:
                        pending.add(pool.submit(copy_context().run, operation, item))
                if not pending:
                    break
                finished, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in finished:
                    yield future.result()
        finally:
            for future in pending:
                future.cancel()


class VerificationCache:
    def __init__(self, limit=32768, *, directory=None):
        self.limit = limit
        self.directory = Path(directory) if directory is not None else None
        self._entries = OrderedDict()
        self._lock = threading.Lock()
        self._db = None
        self._database_stamp = None
        self._pending = 0
        self._hits = 0
        self._misses = 0
        self._hit_bytes = 0
        self._hit_emitted = 0.0

    def open(self, log=None):
        self._hits = self._misses = 0
        self._hit_bytes = 0
        self._hit_emitted = time.monotonic()
        if self.directory is None:
            return
        self._entries.clear()
        path = self.directory / "verification.sqlite"
        try:
            if self.directory.is_symlink() or any(
                    path.with_name(path.name + suffix).is_symlink()
                    for suffix in ("", "-wal", "-shm")):
                raise OSError("Verification cache cannot be a symbolic link")
            self.directory.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path, timeout=0.2, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"Unsupported verification cache version: {version}")
            self._db.execute("CREATE TABLE IF NOT EXISTS verified ("
                             "path TEXT NOT NULL, kind TEXT NOT NULL, stamp TEXT NOT NULL, "
                             "result TEXT NOT NULL, PRIMARY KEY(path, kind)) WITHOUT ROWID")
            self._db.execute("PRAGMA user_version=1")
            self._db.commit()
            info = path.stat()
            self._database_stamp = info.st_dev, info.st_ino
            emit(log, "verification.cache.opened", path=path, version=1)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self._disable(exc, log)

    def _disable(self, exc, log):
        emit_exception(log, "verification.cache.unavailable", exc,
                       directory=self.directory)
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
        self._db = None
        self._pending = 0
        code = getattr(exc, "sqlite_errorcode", 0) & 0xff
        if code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
            path = self.directory / "verification.sqlite"
            try:
                for suffix in ("", "-wal", "-shm"):
                    path.with_name(path.name + suffix).unlink(missing_ok=True)
            except OSError as cleanup_error:
                emit_exception(log, "verification.cache.reset_failed", cleanup_error,
                               path=path)

    def _database_exists(self, log):
        if self._db is None:
            return False
        try:
            info = (self.directory / "verification.sqlite").stat()
            if (info.st_dev, info.st_ino) != self._database_stamp:
                raise OSError("Verification cache was replaced")
        except OSError as exc:
            self._disable(exc, log)
            self._entries.clear()
            return False
        return True

    def close(self, log=None):
        if self._database_exists(log):
            try:
                self._db.commit()
                self._db.close()
            except sqlite3.Error as exc:
                self._disable(exc, log)
        self._db = None
        self._pending = 0
        emit(log, "verification.cache.summary", directory=self.directory,
             reused=self._hits, verified=self._misses, reused_bytes=self._hit_bytes)

    def _hit(self, path, size, log):
        self._hits += 1
        self._hit_bytes += size
        now = time.monotonic()
        if now - self._hit_emitted >= 1.0:
            emit(log, "verification.cache.progress", reused=self._hits,
                 reused_bytes=self._hit_bytes, last_path=path)
            self._hit_emitted = now

    def _cached_rows(self, paths, log):
        found = {}
        with self._lock:
            if self._database_exists(log):
                try:
                    placeholders = ",".join("?" for _ in paths)
                    rows = self._db.execute(
                        f"SELECT path,stamp,result FROM verified WHERE kind=? AND path IN ({placeholders})",
                        ('"xxhash64"', *paths))
                    found = {path: (tuple(json.loads(stamp)), json.loads(result))
                             for path, stamp, result in rows}
                except (sqlite3.Error, ValueError, TypeError) as exc:
                    self._disable(exc, log)
            for path in paths:
                row = self._entries.get((path, "xxhash64"))
                if row is not None:
                    found[path] = row
        return found

    def cached_files(self, root, relatives, stop=None, progress=None, log=None, *, verify=False):
        from .hashes import _file_hash
        from .paths import relative_path
        root = os.path.abspath(root)
        prefix = root.rstrip(os.sep) + os.sep
        with self._lock:
            if not verify and self._database_exists(log):
                try:
                    any_rows = self._db.execute(
                        "SELECT 1 FROM verified WHERE path>=? AND path<? AND kind=? LIMIT 1",
                        (prefix, prefix[:-1] + chr(ord(os.sep) + 1), '"xxhash64"')).fetchone()
                except sqlite3.Error as exc:
                    self._disable(exc, log)
                    any_rows = None
            else:
                any_rows = None
            if not verify and not any_rows and not any(
                    kind == "xxhash64" and path.startswith(prefix)
                    for path, kind in self._entries):
                return
        folders, ancestors = {}, set()
        for relative in relatives:
            if stop is not None and stop.is_set():
                raise InterruptedError("Verification stopped")
            name = relative_path(relative)
            parent, _, leaf = name.rpartition("/")
            folders.setdefault(parent, []).append(leaf)
            while parent and parent not in ancestors:
                ancestors.add(parent)
                parent = parent.rpartition("/")[0]
        def unreadable(exc):
            emit_exception(log, "verification.cache.directory_unreadable", exc)
        def candidates():
            try:
                for directory, dirs, _, fd in os.fwalk(root, onerror=unreadable, follow_symlinks=False):
                    if stop is not None and stop.is_set():
                        raise InterruptedError("Verification stopped")
                    relative = os.path.relpath(directory, root)
                    prefix = "" if relative == "." else relative + "/"
                    dirs[:] = [name for name in dirs if prefix + name in ancestors]
                    names = folders.get(prefix.rstrip("/"), ())
                    for offset in range(0, len(names), 256):
                        batch = names[offset:offset + 256]
                        paths = [os.path.join(directory, name) for name in batch]
                        rows = self._cached_rows(paths, log)
                        for name, path in zip(batch, paths):
                            if stop is not None and stop.is_set():
                                raise InterruptedError("Verification stopped")
                            row = rows.get(path)
                            if row is None and not verify:
                                continue
                            try:
                                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                            except OSError:
                                continue
                            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                            if stat.S_ISREG(info.st_mode):
                                cached = row is not None and row[0] == stamp
                                if cached or verify:
                                    yield prefix + name, Path(path), info, row[1] if cached else None
                        if progress:
                            progress(Path(paths[-1]))
            except InterruptedError:
                raise
            except OSError as exc:
                unreadable(exc)
        def checked(candidate):
            relative, path, info, digest = candidate
            if digest is None:
                digest = self.read(path, "xxhash64", lambda: _file_hash(path, stop),
                                   stop, progress, log,
                                   detailed=info.st_size >= 1024 * 1024)
            else:
                with self._lock:
                    self._hit(path, info.st_size, log)
            return relative, digest, info
        if verify:
            yield from parallel_verify(checked, candidates(), stop,
                size=lambda item: 0 if item[3] is not None else item[2].st_size)
        else:
            yield from map(checked, candidates())

    def read(self, path, kind, operation, stop, progress, log=None, *, detailed=True):
        def stamp():
            value = path.stat()
            if not stat.S_ISREG(value.st_mode):
                raise WabbajackError(f"Verification requires a regular file: {path}")
            return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
        if stop is not None and stop.is_set():
            raise InterruptedError("Preflight stopped")
        before = stamp()
        key = os.path.abspath(path), kind
        disk_key = key[0], json.dumps(kind, separators=(",", ":"))
        disk_stamp = json.dumps(before, separators=(",", ":"))
        with self._lock:
            persistent = self._database_exists(log)
            found = self._entries.get(key)
            if found is None and persistent:
                try:
                    row = self._db.execute(
                        "SELECT result FROM verified WHERE path=? AND kind=? AND stamp=?",
                        (*disk_key, disk_stamp)).fetchone()
                    if row is not None:
                        found = before, json.loads(row[0])
                        self._entries[key] = found
                        self._trim()
                except (sqlite3.Error, ValueError, TypeError) as exc:
                    self._disable(exc, log)
            if found is not None and found[0] == before:
                self._entries.move_to_end(key)
                self._hit(path, before[2], log)
                return found[1]
            self._misses += 1
        if progress:
            progress(path)
        started = time.monotonic()
        if detailed:
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
            self._trim()
            if self._database_exists(log):
                try:
                    self._db.execute("INSERT OR REPLACE INTO verified VALUES (?,?,?,?)",
                        (*disk_key, disk_stamp, json.dumps(result, separators=(",", ":"))))
                    self._pending += 1
                    if self._pending >= 256:
                        self._db.commit()
                        self._pending = 0
                except (sqlite3.Error, ValueError, TypeError) as exc:
                    self._disable(exc, log)
        if detailed:
            emit(log, "verification.completed", path=path, kind=kind,
                 bytes=before[2], elapsed_seconds=round(time.monotonic() - started, 3))
        return result

    def _trim(self):
        while len(self._entries) > self.limit:
            self._entries.popitem(last=False)


@contextmanager
def verification_scope(cache, stop=None, progress=None, log=None):
    cache.open(log)
    token = _current.set((cache, stop, progress, log))
    try:
        yield
    finally:
        _current.reset(token)
        cache.close(log)


def verified_read(path, kind, operation):
    context = _current.get()
    if context is None:
        return operation()
    cache, stop, progress, log = context
    return cache.read(path, kind, operation, stop, progress, log)


def cached_files(root, relatives, *, verify=False):
    context = _current.get()
    if context is not None:
        cache, stop, progress, log = context
        yield from cache.cached_files(root, relatives, stop, progress, log, verify=verify)
