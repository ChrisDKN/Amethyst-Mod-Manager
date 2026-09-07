from __future__ import annotations

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
from contextlib import contextmanager
from pathlib import Path

from Utils.atomic_write import atomic_writer
from .diagnostics import emit, emit_exception
from .hashes import file_hash
from .models import Conflict
from .paths import WabbajackError, within


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
        self._pending_completed = {}
        self._verified = {}
        self._linked_placements = 0
        self._copied_placements = 0
        self._hardlink_error_logged = False
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

    def target(self, key):
        kind, sep, relative = key.partition("/")
        if not sep:
            raise WabbajackError("Invalid installation output key")
        if kind == "root":
            target = within(self.root, relative)
            root = self.root
        elif kind == "profiles":
            if relative.split("/")[0] not in self.get("profile_names", {}).values():
                raise WabbajackError("Output is not owned by an installed profile")
            root = self.profile_root / "profiles"
            target = within(root, relative)
        else:
            raise WabbajackError(f"Invalid installation output key: {key}")
        path = target
        while path != root:
            if path.is_symlink():
                raise WabbajackError(f"Owned output traverses a symbolic link: {key}")
            path = path.parent
        return target

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

    def record_completed(self, path, signature, actual_hash):
        with self.lock:
            self._pending_completed[path] = (signature, actual_hash)
            if len(self._pending_completed) >= 64:
                self.flush_completed()

    def flush_completed(self):
        count = len(self._pending_completed)
        with self.lock, self.db:
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

    @staticmethod
    def _copy(source, target, stop=None, progress=None, *, sync_directory=True):
        total = source.stat().st_size if progress else 0
        completed = 0
        with source.open("rb") as incoming, atomic_writer(target, "wb", encoding=None) as outgoing:
            while data := incoming.read(1024 * 1024):
                if stop is not None and stop.is_set():
                    raise InterruptedError("File publication stopped")
                outgoing.write(data)
                completed += len(data)
                if progress:
                    progress(completed, total)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        shutil.copymode(source, target)
        if sync_directory:
            Store._sync_directory(target.parent)

    def _place(self, source, target, wanted, stop=None, progress=None):
        if stop is not None and stop.is_set():
            raise InterruptedError("File publication stopped")
        info = source.lstat()
        private = (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                   and source.resolve().is_relative_to(self.work.resolve()))
        linked = False
        if private:
            target.parent.mkdir(parents=True, exist_ok=True)
            from Utils.atomic_write import _tmp_for
            temporary = _tmp_for(target)
            try:
                with source.open("rb") as incoming:
                    os.fsync(incoming.fileno())
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
            finally:
                temporary.unlink(missing_ok=True)
        if not linked:
            self._copy(source, target, stop=stop, progress=progress, sync_directory=False)
            self._copied_placements += 1
        else:
            self._linked_placements += 1
        actual = self._current_hash(target, stop)
        if actual != wanted:
            raise WabbajackError(f"Staged output changed before publication: {source}")
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
            return cached[1]
        actual = file_hash(path, stop)
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
        merged_files = protected_files = preserved_files = 0
        for index, key in enumerate(keys):
            if stop is not None and stop.is_set():
                raise InterruptedError("Installation comparison stopped")
            if progress:
                progress("Checking installed files", index, len(keys), key)
            target = self.target(key)
            if target.is_symlink():
                raise WabbajackError(f"Owned output was replaced by a symbolic link: {key}")
            if target.exists() and not target.is_file():
                raise WabbajackError(f"Output is occupied by a directory: {key}")
            actual = self._current_hash(target, stop)
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
                                          before, actual, after, str(target), row["source"] if row else ""))
        if progress:
            progress("Checking installed files", len(keys), len(keys), "Local changes compared with the authored files")
        emit(self.log, "store.preview.completed", repair=repair, old_outputs=len(old),
             desired_outputs=len(desired), compared=len(keys), conflicts=len(conflicts),
             conflict_paths=[item.path for item in conflicts[:50]],
             conflict_paths_truncated=len(conflicts) > 50, merged=merged_files,
             protected=protected_files, preserved_profile_outputs=preserved_files,
             elapsed_seconds=round(time.monotonic() - started, 3))
        return current, conflicts

    def publish(self, desired, choices, current, metadata, stop=None, progress=None):
        started = time.monotonic()
        from .merge import protected
        old = self.outputs()
        operations = []
        preserved = set(self.get("preserved_profiles", []))
        for key in sorted(old.keys() | desired.keys()):
            if key.startswith("profiles/") and key.split("/")[1] in preserved:
                continue
            before = old.get(key, {}).get("authored_hash")
            after = desired.get(key, {}).get("authored_hash")
            actual = current[key]
            choice = choices.get(key)
            keep = (choice == "keep" or (protected(key) and key in old)
                    or (choice is None and actual != before and before == after and not desired.get(key, {}).get("merged")))
            if keep or actual == after:
                continue
            if key in desired and not desired[key].get("source"):
                raise WabbajackError(f"Missing reconstructed output: {key}")
            operations.append((key, desired.get(key, {}).get("source")))
        backup_root = self.directory / "backups" / str(time.time_ns())
        emit(self.log, "store.publish.plan", previous_outputs=len(old),
             desired_outputs=len(desired), operations=len(operations),
             replacements=sum(source is not None and current[key] is not None
                              for key, source in operations),
             additions=sum(source is not None and current[key] is None
                           for key, source in operations),
             removals=sum(source is None for _, source in operations),
             choices=dict(Counter(choices.values())), backup_root=backup_root)
        self.set("status", "committing")
        touched = 0
        linked_before = getattr(self, "_linked_placements", 0)
        copied_before = getattr(self, "_copied_placements", 0)
        try:
            for offset in range(0, len(operations), 64):
                batch = operations[offset:offset + 64]
                batch_started = time.monotonic()
                emit(self.log, "store.publish.batch_started", offset=offset,
                     operations=len(batch), first_path=batch[0][0] if batch else None,
                     last_path=batch[-1][0] if batch else None)
                journal = []
                directories = set()
                for sequence, (key, source) in enumerate(batch, offset):
                    if progress:
                        progress("Applying verified files", offset, len(operations), f"Preparing {key}")
                    target = self.target(key)
                    actual = self._current_hash(target, stop)
                    if actual != current[key]:
                        raise WabbajackError(f"File changed during update review: {key}")
                    backup = backup_root / str(sequence)
                    if actual is not None:
                        self._copy(target, backup, stop=stop, sync_directory=False,
                            progress=(lambda cur, total, key=key: progress("Applying verified files", offset,
                                len(operations), f"Backing up {key} ({cur / 1024 ** 2:.1f} / {total / 1024 ** 2:.1f} MB)")) if progress else None)
                        if self._current_hash(backup, stop) != actual:
                            raise WabbajackError(f"File changed while creating update backup: {key}")
                    journal.append((sequence, key, backup.relative_to(self.directory).as_posix(), int(actual is not None)))
                if backup_root.exists():
                    self._sync_directory(backup_root)
                    self._sync_directory(backup_root.parent)
                    self._sync_directory(self.directory)
                with self.db:
                    self.db.executemany("INSERT INTO journal VALUES (?,?,?,?,1)", journal)
                for sequence, (key, source) in enumerate(batch, offset):
                    def copying(current_bytes, total_bytes):
                        if progress:
                            progress("Applying verified files", sequence, len(operations),
                                     f"{key} ({current_bytes / 1024 ** 2:.1f} / {total_bytes / 1024 ** 2:.1f} MB)")
                    if progress:
                        progress("Applying verified files", sequence, len(operations), key)
                    target = self.target(key)
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
                        self._place(Path(source), target, wanted, stop=stop, progress=copying)
                    if progress:
                        progress("Applying verified files", sequence + 1, len(operations), key)
                for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                    self._sync_directory(directory)
                emit(self.log, "store.publish.batch_completed", offset=offset,
                     operations=len(batch),
                     elapsed_seconds=round(time.monotonic() - batch_started, 3))
            with self.db:
                self.db.execute("DELETE FROM outputs")
                self.db.execute("DELETE FROM baselines")
                for index, (key, row) in enumerate(desired.items()):
                    if progress:
                        progress("Recording installed files", index, len(desired), key)
                    if stop is not None and stop.is_set():
                        raise InterruptedError("File publication stopped")
                    target = self.target(key)
                    actual = self._current_hash(target, stop)
                    self.db.execute("INSERT INTO outputs VALUES (?,?,?,?)",
                        (key, row["authored_hash"], row["signature"], actual))
                    if "baseline_source" in row:
                        source = Path(row["baseline_source"])
                        if file_hash(source, stop) != row["authored_hash"]:
                            raise WabbajackError(f"Authored baseline changed before publication: {key}")
                        self.db.execute("INSERT INTO baselines VALUES (?,?)", (key, source.read_bytes()))
                for key, value in metadata.items():
                    self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('status', '\"published\"')")
                self.db.execute("DELETE FROM journal")
            if progress:
                progress("Recording installed files", len(desired), len(desired), "Installation changes saved")
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
                 elapsed_seconds=round(time.monotonic() - started, 3))
        except BaseException as exc:
            emit_exception(self.log, "store.publish.failed", exc,
                           operations=len(operations), touched=touched,
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
