from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from Utils.atomic_write import atomic_writer
from .diagnostics import emit, emit_exception
from .hashes import XXHash, file_hash
from .models import Conflict
from .paths import WabbajackError, existing_parent, relative_path, within, _has_symlink


_PUBLICATION_BATCH_FILES = 256
_PUBLICATION_BATCH_BYTES = 512 * 1024 * 1024


try:
    _SYNCFS = ctypes.CDLL(None, use_errno=True).syncfs
    _SYNCFS.argtypes = [ctypes.c_int]
    _SYNCFS.restype = ctypes.c_int
except (AttributeError, OSError):
    _SYNCFS = None


def sync_filesystem(path: Path):
    if _SYNCFS is None:
        raise OSError(errno.ENOSYS, "syncfs is unavailable", path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if _SYNCFS(fd):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), path)
    finally:
        os.close(fd)


def private_work_source(source: Path, work: Path):
    try:
        relative = source.relative_to(work)
    except ValueError:
        return False
    if ".." in relative.parts or not relative.parts:
        return False
    path = source.parent
    while path != work:
        if path.is_symlink():
            return False
        path = path.parent
    return not work.is_symlink()


def publication_copy_required(source: Path, target: Path, work: Path):
    try:
        info = source.lstat()
        return not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and private_work_source(source, work)
                    and info.st_dev == existing_parent(target).stat().st_dev)
    except OSError:
        return True


class Store:
    def __init__(self, directory: Path, profile_root: Path, log=None):
        self.directory = directory
        self.profile_root = profile_root
        self.log = log
        if directory.resolve().parent != profile_root.resolve() / ".wabbajack":
            raise WabbajackError("Installation is outside managed storage")
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise WabbajackError("Installation directory cannot be a symbolic link")
        for name in ("root", "work", "backups", "state.sqlite", "state.sqlite-wal", "state.sqlite-shm", "install.lock"):
            if (directory / name).is_symlink():
                raise WabbajackError(f"Managed installation entry cannot be a symbolic link: {name}")
        self.root = directory / "root"
        self.work = directory / "work"
        self.root.mkdir(exist_ok=True)
        self.work.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self._directory_lock = threading.Lock()
        self._pending_completed = {}
        self._prepared_directories = {}
        self._verified = {}
        self._proof_lock = threading.Lock()
        self._synced = {}
        self._sync_pool = None
        self._hash_metrics = Counter()
        self._linked_placements = 0
        self._copied_placements = 0
        self._hardlink_error_logged = False
        self._staging_hardlink_error_logged = False
        self.db = sqlite3.connect(directory / "state.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise WabbajackError(f"Unsupported installation database version: {version}")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outputs(path TEXT PRIMARY KEY, authored_hash TEXT, signature TEXT, actual_hash TEXT);
            CREATE TABLE IF NOT EXISTS completed(path TEXT PRIMARY KEY, signature TEXT, actual_hash TEXT);
            CREATE TABLE IF NOT EXISTS baselines(path TEXT PRIMARY KEY, content BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS journal(sequence INTEGER PRIMARY KEY, path TEXT, backup TEXT, existed INTEGER, applied INTEGER);
            PRAGMA user_version=1;
        """)
        if not self.get("id"):
            self.set("id", uuid.uuid4().hex)
            self.set("profile_root", str(profile_root.resolve()))
        elif Path(self.get("profile_root")).resolve() != profile_root.resolve():
            raise WabbajackError("Installation belongs to a different game profile directory")
        emit(self.log, "store.opened", directory=directory, profile_root=profile_root,
             installation_id=self.get("id"), database_version=version or 1,
             status=self.get("status", "new"), outputs=len(self.outputs()),
             completed=self.db.execute("SELECT COUNT(*) FROM completed").fetchone()[0],
             pending_journal=self.db.execute("SELECT COUNT(*) FROM journal").fetchone()[0])

    def close(self):
        if self._sync_pool is not None:
            self._sync_pool.shutdown(wait=True, cancel_futures=True)
        self.flush_completed()
        self.db.close()
        emit(self.log, "store.closed", directory=self.directory)

    @contextmanager
    def exclusive(self, progress=None):
        started = time.monotonic()
        emit(self.log, "store.lock.waiting", path=self.directory / "install.lock")
        with (self.directory / "install.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                emit_exception(self.log, "store.lock.rejected", exc,
                               path=self.directory / "install.lock")
                raise WabbajackError("This modlist already has an active operation") from exc
            emit(self.log, "store.lock.acquired", path=self.directory / "install.lock")
            try:
                self.recover(progress=progress)
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                emit(self.log, "store.lock.released", path=self.directory / "install.lock",
                     elapsed_seconds=round(time.monotonic() - started, 3))

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))
        if key == "status":
            emit(self.log, "store.status", status=value)

    def _target_parts(self, key, profile_names=None):
        kind, sep, relative = key.partition("/")
        if not sep:
            raise WabbajackError("Invalid installation output key")
        if kind == "root":
            root = self.root
        elif kind == "profiles":
            names = self.get("profile_names", {}) if profile_names is None else profile_names
            if relative.split("/")[0] not in names.values():
                raise WabbajackError("Output is not owned by an installed profile")
            root = self.profile_root / "profiles"
        else:
            raise WabbajackError(f"Invalid installation output key: {key}")
        return root, relative_path(relative)

    def target(self, key):
        root, relative = self._target_parts(key)
        target = root / relative
        if _has_symlink(root, target):
            raise WabbajackError(f"Owned output traverses a symbolic link: {key}")
        return target

    def _existing_targets(self, keys, stop=None):
        roots, found = {}, set()
        names = self.get("profile_names", {})
        for key in keys:
            if stop is not None and stop.is_set():
                raise InterruptedError("Installation comparison stopped")
            root, relative = self._target_parts(key, names)
            parent, _, name = relative.rpartition("/")
            roots.setdefault(root, {}).setdefault(parent, {})[name] = key
        def failed(error):
            raise error
        for root, parents in roots.items():
            try:
                mode = root.lstat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                raise WabbajackError(f"Expected a managed directory: {root}")
            ancestors = {""}
            for parent in parents:
                while parent not in ancestors:
                    ancestors.add(parent)
                    parent = parent.rpartition("/")[0]
            for base, directories, files, fd in os.fwalk(root, onerror=failed, follow_symlinks=False):
                if stop is not None and stop.is_set():
                    raise InterruptedError("Installation comparison stopped")
                relative = os.path.relpath(base, root)
                relative = "" if relative == "." else relative
                expected = parents.get(relative, {})
                prefix = relative + "/" if relative else ""
                for name in directories:
                    if name in expected:
                        raise WabbajackError(f"Output is occupied by a directory: {expected[name]}")
                directories[:] = [name for name in directories if prefix + name in ancestors]
                for name in directories:
                    if not stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                        raise WabbajackError(f"Owned output traverses a symbolic link: {Path(base) / name}")
                for name in files:
                    if prefix + name in ancestors:
                        raise WabbajackError(f"Expected an output directory: {Path(base) / name}")
                    if name in expected:
                        if not stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                            raise WabbajackError(f"Expected a regular installed file: {expected[name]}")
                        found.add(expected[name])
        return found

    def outputs(self):
        with self.lock:
            return {r["path"]: dict(r) for r in self.db.execute("SELECT * FROM outputs")}

    def completed(self, path, signature):
        with self.lock:
            pending = self._pending_completed.get(path)
            if pending and pending[0] == signature:
                return pending[1]
            row = self.db.execute("SELECT actual_hash FROM completed WHERE path=? AND signature=?",
                                  (path, signature)).fetchone()
        return row[0] if row else None

    def completed_output(self, path):
        with self.lock:
            row = self._pending_completed.get(path)
            if row is None:
                row = self.db.execute("SELECT signature,actual_hash FROM completed WHERE path=?",
                                      (path,)).fetchone()
        return tuple(row) if row else None

    def completed_paths(self, paths):
        paths = tuple(dict.fromkeys(paths))
        with self.lock:
            found = set(self._pending_completed).intersection(paths)
            for offset in range(0, len(paths), 500):
                batch = paths[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                found.update(row[0] for row in self.db.execute(
                    f"SELECT path FROM completed WHERE path IN ({placeholders})",
                    batch))
        return found

    def completed_outputs(self):
        with self.lock:
            found = {path: (sig, digest) for path, sig, digest in self.db.execute(
                "SELECT path,signature,actual_hash FROM completed")}
            found.update(self._pending_completed)
        return found

    def record_completed(self, path, signature, actual_hash):
        with self.lock:
            self._pending_completed[path] = (signature, actual_hash)
            if len(self._pending_completed) >= 1024:
                self.flush_completed()

    def flush_completed(self):
        with self.lock, self.db:
            count = len(self._pending_completed)
            self.db.executemany("INSERT OR REPLACE INTO completed VALUES (?,?,?)",
                [(path, sig, digest) for path, (sig, digest) in self._pending_completed.items()])
            self._pending_completed.clear()
        if count:
            emit(self.log, "store.completed_flushed", outputs=count)

    def recover(self, progress=None):
        if self.get("status") != "committing":
            emit(self.log, "store.recovery.not_required", status=self.get("status", "new"))
            return
        started = time.monotonic()
        rows = self.db.execute("SELECT * FROM journal ORDER BY sequence DESC").fetchall()
        emit(self.log, "store.recovery.started", journal_entries=len(rows),
             backups=sum(bool(row["existed"]) for row in rows))
        for index, row in enumerate(rows):
            if progress:
                progress("Restoring previous files", index, len(rows), row["path"])
            if not row["applied"]:
                continue
            target = self.target(row["path"])
            backup = within(self.directory, row["backup"])
            if row["existed"]:
                if not backup.is_file():
                    raise WabbajackError(f"Recovery backup is missing: {row['path']}")
                self._copy(backup, target)
            else:
                target.unlink(missing_ok=True)
                if target.parent.is_dir():
                    self._sync_directory(target.parent)
        with self.db:
            self.db.execute("DELETE FROM journal")
        self.set("status", "interrupted")
        emit(self.log, "store.recovery.completed", restored=len(rows),
             elapsed_seconds=round(time.monotonic() - started, 3))
        if progress:
            progress("Restoring previous files", len(rows), len(rows), "Previous installation restored")

    def prepare_directory(self, path):
        with self._directory_lock:
            known = self._prepared_directories.get(path)
        if known is not None:
            try:
                info = path.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISDIR(info.st_mode):
                    raise WabbajackError(f"Expected a managed directory: {path}")
                if (info.st_dev, info.st_ino) == known:
                    return
        path.mkdir(parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise WabbajackError(f"Expected a managed directory: {path}")
        with self._directory_lock:
            self._prepared_directories[path] = (info.st_dev, info.st_ino)

    @staticmethod
    def _copy(source, target, stop=None, progress=None, *, sync_directory=True, sync_file=True,
              prepare_parent=None, expected=None):
        total = source.stat().st_size if progress else 0
        completed = 0
        digest = XXHash() if expected is not None else None
        with source.open("rb") as incoming, atomic_writer(
                target, "wb", encoding=None, prepare_parent=prepare_parent) as outgoing:
            while data := incoming.read(1024 * 1024):
                if stop is not None and stop.is_set():
                    raise InterruptedError("File publication stopped")
                outgoing.write(data)
                if digest is not None:
                    digest.update(data)
                completed += len(data)
                if progress:
                    progress(completed, total)
            if digest is not None and digest.digest() != expected:
                raise WabbajackError(f"Copied output failed verification: {source}")
            outgoing.flush()
            if sync_file:
                os.fsync(outgoing.fileno())
        shutil.copymode(source, target)
        if sync_directory:
            Store._sync_directory(target.parent)
        if digest is not None:
            return Store._stamp(target.lstat()), digest.digest()

    def remember_source(self, path, digest, info=None):
        info = info if info is not None else path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise WabbajackError(f"Expected a regular staged file: {path}")
        with self._proof_lock:
            self._verified[path] = self._stamp(info), digest

    def clean_scratch(self, scratch, paths):
        proofs = []
        for path in dict.fromkeys(paths):
            info = path.lstat()
            with self._proof_lock:
                known = self._verified.get(path)
            if known and known[0] == self._stamp(info) and info.st_nlink > 1:
                proofs.append((path, info, known[1]))
        shutil.rmtree(scratch)
        for path, before, digest in proofs:
            after = path.lstat()
            if (self._stamp(before)[:4] == self._stamp(after)[:4]
                    and before.st_mode == after.st_mode
                    and after.st_nlink < before.st_nlink):
                self.remember_source(path, digest, after)

    def _sync_cached(self, source, stop=None, *, expected=None):
        if stop is not None and stop.is_set():
            raise InterruptedError("File publication stopped")
        info = source.lstat()
        stamp = self._stamp(info)
        if expected is not None and stamp != expected:
            raise WabbajackError(f"Staged output changed before syncing: {source}")
        with self._proof_lock:
            if self._synced.get(stamp[:2]) == stamp:
                return stamp, True
        synced = self._sync_source(source, stop, expected=expected)
        if synced is not None:
            with self._proof_lock:
                self._synced[synced[:2]] = synced
        return synced, False

    def sync_sources(self, sources, stop=None):
        started = time.monotonic()
        with self._proof_lock:
            if self._sync_pool is None:
                self._sync_pool = ThreadPoolExecutor(max_workers=4,
                                                    thread_name_prefix="wabbajack-sync")
        def sync(item):
            source, expected = item
            before = time.monotonic()
            _, reused = self._sync_cached(source, stop, expected=expected)
            return time.monotonic() - before, reused
        seconds = reused = count = 0
        iterator = iter(sources)
        from itertools import islice
        while batch := list(islice(iterator, 128)):
            futures = [self._sync_pool.submit(sync, item) for item in batch]
            try:
                for future in futures:
                    elapsed, skipped = future.result()
                    seconds += elapsed
                    reused += skipped
                    count += 1
            finally:
                for future in futures:
                    future.cancel()
                for future in futures:
                    if not future.cancelled():
                        future.result()
        return {"files": count, "reused": reused, "worker_seconds": round(seconds, 3),
                "elapsed_seconds": round(time.monotonic() - started, 3)}

    def _stage(self, source, target, stop=None, progress=None, size=None):
        if stop is not None and stop.is_set():
            raise InterruptedError("File reconstruction stopped")
        self.prepare_directory(target.parent)
        target.unlink(missing_ok=True)
        try:
            os.link(source, target, follow_symlinks=False)
            if progress:
                total = size if size is not None else source.stat().st_size
                progress(total, total)
            return True
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EMLINK, errno.ENOSYS,
                                 errno.EOPNOTSUPP, errno.EPERM, errno.EXDEV}:
                raise
            if not self._staging_hardlink_error_logged:
                emit_exception(self.log, "store.stage.hardlink_unavailable", exc,
                               source=source, target=target)
                self._staging_hardlink_error_logged = True
        self._copy(source, target, stop=stop, progress=progress,
                   sync_directory=False, prepare_parent=self.prepare_directory)
        return False

    @staticmethod
    def _sync_source(source, stop=None, *, expected=None):
        if stop is not None and stop.is_set():
            raise InterruptedError("File publication stopped")
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise WabbajackError(f"Expected a regular staged file: {source}")
        if expected is not None and Store._stamp(info) != expected:
            raise WabbajackError(f"Published output changed before syncing: {source}")
        if expected is None and info.st_nlink != 1:
            return None
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if Store._stamp(os.fstat(fd)) != Store._stamp(info):
                raise WabbajackError(f"Staged output changed before publication: {source}")
            os.fsync(fd)
            if Store._stamp(os.fstat(fd)) != Store._stamp(info):
                raise WabbajackError(f"Output changed while syncing: {source}")
            if stop is not None and stop.is_set():
                raise InterruptedError("File publication stopped")
            return Store._stamp(info)
        finally:
            os.close(fd)

    def _private_source(self, source):
        return private_work_source(source, self.work)

    def _place(self, source, target, wanted, stop=None, progress=None, *, synced=None,
               deferred_sync=None, metrics=None):
        if stop is not None and stop.is_set():
            raise InterruptedError("File publication stopped")
        info = source.lstat()
        private = (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                   and self._private_source(source))
        linked = False
        proof = None
        if private:
            actual = self._current_hash(source, stop)
            if actual != wanted:
                raise WabbajackError(f"Staged output changed before publication: {source}")
            info = source.lstat()
            proof = self._verified.get(source)
            if proof is None or proof[0] != self._stamp(info):
                raise WabbajackError(f"Staged output changed before publication: {source}")
            self.prepare_directory(target.parent)
            from Utils.atomic_write import _tmp_for
            temporary = _tmp_for(target)
            try:
                if synced != self._stamp(info):
                    self._sync_cached(source, stop)
                try:
                    os.link(source, temporary, follow_symlinks=False)
                    linked = True
                except OSError as exc:
                    if not self._hardlink_error_logged:
                        emit_exception(self.log, "store.publish.hardlink_unavailable", exc,
                                       source=source, target=target,
                                       source_device=info.st_dev)
                        self._hardlink_error_logged = True
                if linked:
                    temporary.replace(target)
                    after = target.lstat()
                    if (self._stamp(after)[:4] == proof[0][:4]
                            and after.st_mode == info.st_mode
                            and after.st_nlink == info.st_nlink + 1
                            and self._stamp(source.lstat()) == self._stamp(after)):
                        self.remember_source(target, actual, after)
                        if metrics is not None:
                            metrics["hash_handoffs"] += 1
            finally:
                temporary.unlink(missing_ok=True)
        if not linked:
            copy_started = time.monotonic()
            copied = self._copy(source, target, stop=stop, progress=progress, sync_directory=False,
                                sync_file=deferred_sync is None, prepare_parent=self.prepare_directory,
                                expected=wanted)
            if copied is not None:
                with self._proof_lock:
                    self._verified[target] = copied
            if metrics is not None:
                metrics["copy_seconds"] += time.monotonic() - copy_started
                metrics["copied_bytes"] += info.st_size
            self._copied_placements += 1
        else:
            self._linked_placements += 1
        actual = self._current_hash(target, stop)
        if actual != wanted:
            raise WabbajackError(f"Staged output changed before publication: {source}")
        if not linked and deferred_sync is not None:
            deferred_sync.append((target, self._verified[target][0]))
        return actual

    @staticmethod
    def _stamp(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_mode)

    def _current_hash(self, path, stop=None):
        if stop is not None and stop.is_set():
            raise InterruptedError("File verification stopped")
        try:
            info = path.lstat()
        except FileNotFoundError:
            self._verified.pop(path, None)
            return None
        if not stat.S_ISREG(info.st_mode):
            raise WabbajackError(f"Expected a regular installed file: {path}")
        stamp = self._stamp(info)
        cached = self._verified.get(path)
        if cached and cached[0] == stamp:
            self._hash_metrics["hash_cache_hits"] += 1
            return cached[1]
        hash_started = time.monotonic()
        actual = file_hash(path, stop)
        self._hash_metrics["hash_seconds"] += time.monotonic() - hash_started
        self._hash_metrics["hashed_files"] += 1
        self._hash_metrics["hashed_bytes"] += info.st_size
        if self._stamp(path.lstat()) != stamp:
            raise WabbajackError(f"File changed during verification: {path}")
        self._verified[path] = (stamp, actual)
        return actual

    @staticmethod
    def _sync_directory(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def preview(self, desired, *, repair=False, stop=None, progress=None):
        started = time.monotonic()
        from .merge import baseline_candidate, merge_content, protected
        old = self.outputs()
        current, conflicts = {}, []
        preserved = set(self.get("preserved_profiles", []))
        keys = old.keys() | desired.keys()
        if progress:
            progress("Checking installed files", 0, len(keys), "Finding existing installation files")
        existing = self._existing_targets(keys, stop)
        merged_files = protected_files = preserved_files = 0
        for index, key in enumerate(keys):
            if stop is not None and stop.is_set():
                raise InterruptedError("Installation comparison stopped")
            if progress:
                progress("Checking installed files", index, len(keys), key)
            target = self.target(key) if key in existing else None
            actual = self._current_hash(target, stop) if target is not None else None
            current[key] = actual
            before = old.get(key, {}).get("authored_hash")
            after = desired.get(key, {}).get("authored_hash")
            if key.startswith("profiles/") and key.split("/")[1] in preserved:
                preserved_files += 1
                continue
            row = desired.get(key)
            if row and baseline_candidate(key) and Path(row["source"]).stat().st_size <= 2 * 1024 * 1024:
                row["baseline_source"] = row["source"]
            if protected(key) and key in old:
                protected_files += 1
                if row:
                    row["preserve"] = True
                continue
            if actual != before and actual != after and (before != after or repair):
                baseline = self.db.execute("SELECT content FROM baselines WHERE path=?", (key,)).fetchone()
                if row and baseline and actual is not None and "baseline_source" in row and target.stat().st_size <= 2 * 1024 * 1024:
                    try:
                        merged = merge_content(key, baseline[0], target.read_bytes(),
                                               Path(row["baseline_source"]).read_bytes())
                        merged_path = self.work / "merged" / str(len(current))
                        with atomic_writer(merged_path, "wb", encoding=None) as out:
                            out.write(merged)
                        row["source"] = str(merged_path)
                        row["merged"] = True
                        row["merged_hash"] = file_hash(merged_path)
                        merged_files += 1
                        continue
                    except (ValueError, UnicodeError) as exc:
                        emit_exception(self.log, "store.preview.merge_failed", exc,
                                       path=key)
                conflicts.append(Conflict(key, "Removed by author" if after is None else "Changed locally",
                                          before, actual, after, str(target if target is not None else self.target(key)),
                                          row["source"] if row else ""))
        if progress:
            progress("Checking installed files", len(keys), len(keys), "Local changes compared with the authored files")
        emit(self.log, "store.preview.completed", repair=repair, old_outputs=len(old),
             desired_outputs=len(desired), compared=len(keys), conflicts=len(conflicts),
             existing_outputs=len(existing),
             conflict_paths=[item.path for item in conflicts[:50]],
             conflict_paths_truncated=len(conflicts) > 50, merged=merged_files,
             protected=protected_files, preserved_profile_outputs=preserved_files,
             elapsed_seconds=round(time.monotonic() - started, 3))
        return current, conflicts

    def publish(self, desired, choices, current, metadata, stop=None, progress=None):
        started = time.monotonic()
        apply_started = None
        metrics = Counter()
        hash_before = self._hash_metrics.copy()
        def timed_sync(source, *, expected=None):
            sync_started = time.monotonic()
            stamp, _ = self._sync_cached(source, stop, expected=expected)
            return stamp, time.monotonic() - sync_started
        def sync_directory(path):
            sync_started = time.monotonic()
            self._sync_directory(path)
            metrics["directory_sync_seconds"] += time.monotonic() - sync_started
            metrics["directories_synced"] += 1
        def target_for(key):
            target_started = time.monotonic()
            target = self.target(key)
            metrics["path_check_seconds"] += time.monotonic() - target_started
            return target
        def measurements():
            values = dict(metrics)
            values.update(self._hash_metrics - hash_before)
            return {key: round(value, 6) if isinstance(value, float) else value
                    for key, value in values.items()}
        from .merge import protected
        old = self.outputs()
        operations = []
        retained = []
        preserved = set(self.get("preserved_profiles", []))
        for key in sorted(old.keys() | desired.keys()):
            if key.startswith("profiles/") and key.split("/")[1] in preserved:
                if key in desired:
                    retained.append(key)
                continue
            before = old.get(key, {}).get("authored_hash")
            after = desired.get(key, {}).get("authored_hash")
            actual = current[key]
            choice = choices.get(key)
            keep = (choice == "keep" or (protected(key) and key in old)
                    or (choice is None and actual != before and before == after and not desired.get(key, {}).get("merged")))
            if keep or actual == after:
                if key in desired:
                    retained.append(key)
                continue
            if key in desired and not desired[key].get("source"):
                raise WabbajackError(f"Missing reconstructed output: {key}")
            operations.append((key, desired.get(key, {}).get("source")))
        backup_root = self.directory / "backups" / str(time.time_ns())
        batch_size = _PUBLICATION_BATCH_FILES
        batch_bytes = _PUBLICATION_BATCH_BYTES
        sync_workers = min(4, os.cpu_count() or 1)
        target_devices = set()
        for kind in {key.partition("/")[0] for key, _ in operations}:
            if kind == "root":
                root = self.root
            elif kind == "profiles":
                root = self.profile_root / "profiles"
            else:
                raise WabbajackError(f"Invalid installation output key: {kind}")
            target_devices.add(existing_parent(root).stat().st_dev)
        filesystem_barrier = bool(operations) and _SYNCFS is not None and not old and not retained \
            and target_devices == {self.directory.stat().st_dev} \
            and all(source is not None and current[key] is None for key, source in operations)

        def copy_bytes(item):
            key, source = item
            total = 0
            if current[key] is not None:
                try:
                    total += target_for(key).stat().st_size
                except OSError:
                    pass
            if source is None:
                return total
            path = Path(source)
            try:
                info = path.lstat()
                device = next(iter(target_devices)) if len(target_devices) == 1 else None
                if (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                        and path.is_relative_to(self.work) and info.st_dev == device):
                    return total
                return total + info.st_size
            except OSError:
                return total

        batches = []
        batch = []
        estimated_bytes = 0
        offset = 0
        for item in operations:
            item_bytes = copy_bytes(item)
            if batch and (len(batch) >= batch_size
                          or estimated_bytes + item_bytes > batch_bytes):
                batches.append((offset, batch, estimated_bytes))
                offset += len(batch)
                batch, estimated_bytes = [], 0
            batch.append(item)
            estimated_bytes += item_bytes
        if batch:
            batches.append((offset, batch, estimated_bytes))

        def detail_with_eta(detail, completed):
            if apply_started is None:
                return detail
            elapsed = time.monotonic() - apply_started
            if completed <= 0 or elapsed < 5 or completed >= len(operations):
                return detail
            remaining = (len(operations) - completed) * elapsed / completed
            if remaining < 90:
                estimate = f"about {max(1, round(remaining))}s remaining"
            elif remaining < 90 * 60:
                estimate = f"about {max(1, round(remaining / 60))}m remaining"
            else:
                hours, minutes = divmod(max(1, round(remaining / 60)), 60)
                estimate = f"about {hours}h {minutes:02d}m remaining"
            return f"{detail} · {estimate}"

        with self.db:
            self.db.execute("CREATE TEMP TABLE IF NOT EXISTS publication_outputs ("
                            "path TEXT PRIMARY KEY, authored_hash TEXT, signature TEXT, actual_hash TEXT) WITHOUT ROWID")
            self.db.execute("CREATE TEMP TABLE IF NOT EXISTS publication_baselines ("
                            "path TEXT PRIMARY KEY, content BLOB NOT NULL) WITHOUT ROWID")
            self.db.execute("DELETE FROM publication_outputs")
            self.db.execute("DELETE FROM publication_baselines")
        def record(rows):
            record_started = time.monotonic()
            with self.db:
                self.db.executemany("INSERT INTO publication_outputs VALUES (?,?,?,?)",
                    ((key, desired[key]["authored_hash"], desired[key]["signature"], actual)
                     for key, actual in rows))
                for key, _ in rows:
                    if stop is not None and stop.is_set():
                        raise InterruptedError("File publication stopped")
                    row = desired[key]
                    if "baseline_source" in row:
                        with Path(row["baseline_source"]).open("rb") as source:
                            content = source.read(2 * 1024 * 1024 + 1)
                        digest = XXHash()
                        digest.update(content)
                        if len(content) > 2 * 1024 * 1024 or digest.digest() != row["authored_hash"]:
                            raise WabbajackError(f"Authored baseline changed before publication: {key}")
                        self.db.execute("INSERT INTO publication_baselines VALUES (?,?)", (key, content))
            metrics["record_seconds"] += time.monotonic() - record_started
        emit(self.log, "store.publish.plan", previous_outputs=len(old),
             desired_outputs=len(desired), operations=len(operations),
             replacements=sum(source is not None and current[key] is not None
                              for key, source in operations),
             additions=sum(source is not None and current[key] is None
                           for key, source in operations),
             removals=sum(source is None for _, source in operations),
             choices=dict(Counter(choices.values())), backup_root=backup_root,
             batch_size=batch_size, sync_workers=sync_workers,
             max_batch_bytes=batch_bytes, batches=len(batches),
             durability_mode="filesystem_barrier" if filesystem_barrier else "per_file",
             retained_outputs=len(retained), record_during_publication=True)
        self.set("status", "committing")
        touched = 0
        linked_before = getattr(self, "_linked_placements", 0)
        copied_before = getattr(self, "_copied_placements", 0)
        try:
            for offset in range(0, len(retained), batch_size):
                rows = []
                for index, key in enumerate(retained[offset:offset + batch_size], offset):
                    if progress:
                        progress("Checking retained files", index, len(retained), key)
                    rows.append((key, self._current_hash(target_for(key), stop)))
                record(rows)
            apply_started = time.monotonic()
            with ThreadPoolExecutor(max_workers=sync_workers,
                                    thread_name_prefix="wabbajack-flush") as pool:
                def sync_batch(batch):
                    if filesystem_barrier:
                        return {}
                    return {source: pool.submit(timed_sync, Path(source))
                            for source in dict.fromkeys(source for _, source in batch
                                if source is not None and Path(source).is_relative_to(self.work))}
                next_synced = sync_batch(batches[0][1]) if batches else {}
                for batch_index, (offset, batch, estimated_copy_bytes) in enumerate(batches):
                    batch_started = time.monotonic()
                    batch_before = measurements()
                    emit(self.log, "store.publish.batch_started", offset=offset,
                         operations=len(batch), first_path=batch[0][0] if batch else None,
                         last_path=batch[-1][0] if batch else None,
                         estimated_copy_bytes=estimated_copy_bytes)
                    synced = next_synced
                    journal = []
                    directories = set()
                    for sequence, (key, source) in enumerate(batch, offset):
                        if progress:
                            progress("Applying verified files", offset, len(operations),
                                     detail_with_eta(f"Preparing {key}", offset))
                        target = target_for(key)
                        actual = self._current_hash(target, stop)
                        if actual != current[key]:
                            raise WabbajackError(f"File changed during update review: {key}")
                        backup = backup_root / str(sequence)
                        if actual is not None:
                            backup_started = time.monotonic()
                            self._copy(target, backup, stop=stop, sync_directory=False,
                                prepare_parent=self.prepare_directory,
                                progress=(lambda cur, total, key=key: progress("Applying verified files", offset,
                                    len(operations), detail_with_eta(
                                        f"Backing up {key} ({cur / 1024 ** 2:.1f} / {total / 1024 ** 2:.1f} MB)",
                                        offset))) if progress else None)
                            metrics["backup_copy_seconds"] += time.monotonic() - backup_started
                            if self._current_hash(backup, stop) != actual:
                                raise WabbajackError(f"File changed while creating update backup: {key}")
                        journal.append((sequence, key, backup.relative_to(self.directory).as_posix(), int(actual is not None)))
                    if backup_root.exists():
                        sync_directory(backup_root)
                        sync_directory(backup_root.parent)
                        sync_directory(self.directory)
                    journal_started = time.monotonic()
                    with self.db:
                        self.db.executemany("INSERT INTO journal VALUES (?,?,?,?,1)", journal)
                    metrics["journal_seconds"] += time.monotonic() - journal_started
                    prepared = time.monotonic()
                    next_synced = sync_batch(batches[batch_index + 1][1]) \
                        if batch_index + 1 < len(batches) else {}
                    flushed_outputs, records = [], []
                    for sequence, (key, source) in enumerate(batch, offset):
                        def copying(current_bytes, total_bytes):
                            if progress:
                                progress("Applying verified files", sequence, len(operations),
                                         detail_with_eta(
                                             f"{key} ({current_bytes / 1024 ** 2:.1f} / {total_bytes / 1024 ** 2:.1f} MB)",
                                             sequence))
                        if progress:
                            progress("Applying verified files", sequence, len(operations),
                                     detail_with_eta(key, sequence))
                        if filesystem_barrier and source is not None:
                            flushed = self._stamp(Path(source).lstat())
                        else:
                            sync_started = time.monotonic()
                            flushed = synced[source].result()[0] if source in synced else None
                            metrics["source_sync_wait_seconds"] += time.monotonic() - sync_started
                        target = target_for(key)
                        if self._current_hash(target, stop) != current[key]:
                            raise WabbajackError(f"File changed during update review: {key}")
                        touched = sequence + 1
                        parent = target.parent
                        directories.add(parent)
                        while not parent.exists():
                            parent = parent.parent
                            directories.add(parent)
                        if source is None:
                            target.unlink(missing_ok=True)
                        else:
                            wanted = desired[key].get("merged_hash", desired[key]["authored_hash"])
                            deferred = []
                            actual = self._place(Path(source), target, wanted, stop=stop,
                                                 progress=copying, synced=flushed, deferred_sync=deferred,
                                                 metrics=metrics)
                            records.append((key, actual))
                            if not filesystem_barrier:
                                flushed_outputs.extend(pool.submit(timed_sync, path, expected=stamp)
                                                       for path, stamp in deferred)
                        if progress:
                            progress("Applying verified files", sequence + 1, len(operations),
                                     detail_with_eta(key, sequence + 1))
                    metrics["source_sync_worker_seconds"] += sum(future.result()[1] for future in synced.values())
                    sync_started = time.monotonic()
                    for future in flushed_outputs:
                        metrics["output_sync_worker_seconds"] += future.result()[1]
                    metrics["output_sync_wait_seconds"] += time.monotonic() - sync_started
                    if not filesystem_barrier:
                        for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                            sync_directory(directory)
                    record(records)
                    emit(self.log, "store.publish.batch_completed", offset=offset,
                         operations=len(batch),
                         prepare_seconds=round(prepared - batch_started, 3),
                         apply_seconds=round(time.monotonic() - prepared, 3),
                         timings={key: round(value - batch_before.get(key, 0), 6)
                                  for key, value in measurements().items()},
                         elapsed_seconds=round(time.monotonic() - batch_started, 3))
            if filesystem_barrier:
                if progress:
                    progress("Applying verified files", len(operations), len(operations),
                             "Making installed files durable")
                barrier_started = time.monotonic()
                emit(self.log, "store.publish.filesystem_sync.started",
                     path=self.directory, operations=len(operations))
                sync_filesystem(self.directory)
                metrics["filesystem_sync_seconds"] += time.monotonic() - barrier_started
                emit(self.log, "store.publish.filesystem_sync.completed",
                     path=self.directory, operations=len(operations),
                     elapsed_seconds=round(time.monotonic() - barrier_started, 3))
            if progress:
                progress("Saving installation records", 0, 1, "Committing the verified file records")
            if stop is not None and stop.is_set():
                raise InterruptedError("File publication stopped")
            if self.db.execute("SELECT COUNT(*) FROM publication_outputs").fetchone()[0] != len(desired):
                raise WabbajackError("Publication is missing verified output records")
            with self.db:
                self.db.execute("DELETE FROM outputs")
                self.db.execute("DELETE FROM baselines")
                self.db.execute("INSERT INTO outputs SELECT * FROM publication_outputs")
                self.db.execute("INSERT INTO baselines SELECT * FROM publication_baselines")
                for key, value in metadata.items():
                    self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('status', '\"published\"')")
                self.db.execute("DELETE FROM journal")
            if progress:
                progress("Saving installation records", 1, 1, "Installation changes saved")
            directories = set()
            for key in old.keys() - desired.keys():
                if key.startswith("root/"):
                    path = self.target(key).parent
                    while path != self.root:
                        directories.add(path)
                        path = path.parent
            for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                try:
                    path.rmdir()
                except OSError:
                    pass
            emit(self.log, "store.publish.completed", operations=len(operations),
                 linked=getattr(self, "_linked_placements", 0) - linked_before,
                 copied=getattr(self, "_copied_placements", 0) - copied_before,
                 timings=measurements(),
                 elapsed_seconds=round(time.monotonic() - started, 3))
        except BaseException as exc:
            emit_exception(self.log, "store.publish.failed", exc,
                           operations=len(operations), touched=touched,
                           timings=measurements(),
                           elapsed_seconds=round(time.monotonic() - started, 3))
            with self.db:
                self.db.execute("DELETE FROM journal WHERE sequence>=?", (touched,))
            self.recover(progress=progress)
            raise


def installation_info(directory: Path, log=None):
    path = directory / "state.sqlite"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            result = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM metadata")}
            try:
                outputs = db.execute("SELECT COUNT(*) FROM outputs").fetchone()[0]
            except sqlite3.Error:
                outputs = None
        result["directory"] = str(directory)
        emit(log, "installation.state.loaded", directory=directory,
             installation_id=result.get("id"), name=result.get("name"),
             status=result.get("status"), version=result.get("version"),
             outputs=outputs)
        return result
    except (sqlite3.Error, ValueError) as exc:
        emit_exception(log, "installation.state.failed", exc,
                       directory=directory)
        return None


def installations(profile_root: Path, log=None):
    root = profile_root / ".wabbajack"
    if not root.is_dir() or root.is_symlink():
        return []
    result = [info for p in root.iterdir() if p.is_dir() and not p.is_symlink()
              if (info := installation_info(p, log))]
    emit(log, "installation.scan.completed", profile_root=profile_root,
         installations=len(result))
    return result
