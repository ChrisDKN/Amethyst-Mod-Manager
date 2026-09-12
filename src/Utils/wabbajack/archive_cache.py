from __future__ import annotations

import json
import stat
import threading
from pathlib import Path

from Utils.atomic_write import write_atomic
from .diagnostics import emit
from .hashes import hash_bytes
from .paths import WabbajackError, auxiliary_path, cache_path
from .verification import file_stamp


_PARALLEL_DOWNLOAD_ALLOWANCE = 4 * 1024 ** 3


def download_space(archive):
    # Leave room for CDN assembly or a retained failed transfer beside its replacement.
    return archive.size * 2


def download_budget(archives):
    sizes = [download_space(archive) for archive in archives]
    if not sizes:
        return 0
    largest = max(sizes)
    return min(sum(sizes), largest + _PARALLEL_DOWNLOAD_ALLOWANCE)


class ArchiveBudget:
    def __init__(self, limit, stop, log=None):
        self.limit, self.stop, self.log = limit, stop, log
        self.failed = threading.Event()
        self._condition = threading.Condition()
        self._reserved = {}
        self._used = 0

    def acquire(self, archive):
        size = download_space(archive)
        with self._condition:
            if size > self.limit:
                raise WabbajackError("Required downloads changed after preflight; check requirements again")
            waiting = False
            while True:
                if self.stop.is_set() or self.failed.is_set():
                    raise InterruptedError("Archive download stopped")
                if archive.key in self._reserved:
                    return
                if self._used + size <= self.limit:
                    self._reserved[archive.key] = size
                    self._used += size
                    emit(self.log, "archive.cache.reserved", archive=archive.name,
                         bytes=size, used_bytes=self._used, limit_bytes=self.limit)
                    return
                if not self._reserved:
                    raise WabbajackError("Retained failed downloads fill the archive budget; free space and resume")
                if not waiting:
                    emit(self.log, "archive.cache.waiting", archive=archive.name,
                         bytes=size, used_bytes=self._used, limit_bytes=self.limit)
                    waiting = True
                self._condition.wait(0.2)

    def release(self, archive, retained=0):
        with self._condition:
            reserved = self._reserved.pop(archive.key, 0)
            self._used -= reserved
            self._used += min(reserved, retained)
            self._condition.notify_all()

    def downloaded(self, archive, retained):
        with self._condition:
            reserved = self._reserved.get(archive.key)
            if reserved is None:
                return
            retained = min(reserved, max(0, int(retained)))
            if retained >= reserved:
                return
            self._reserved[archive.key] = retained
            self._used -= reserved - retained
            emit(self.log, "archive.cache.downloaded", archive=archive.name,
                 bytes=retained, released_bytes=reserved - retained,
                 used_bytes=self._used, limit_bytes=self.limit)
            self._condition.notify_all()

    def fail(self):
        with self._condition:
            self.failed.set()
            self._condition.notify_all()


class OwnedArchives:
    def __init__(self, request, log=None):
        self.downloads = request.downloads.resolve()
        self.owner = str(request.directory.resolve())
        self.log = log
        self.protected = [request.package.path.resolve(),
                          *(Path(path).resolve() for path in request.game_roots.values())]
        for option in request.setup_options.values():
            if isinstance(option, dict):
                self.protected.extend(Path(option[key]).resolve()
                                      for key in ("mpi", "source", "archive") if option.get(key))

    def target(self, archive):
        return cache_path(self.downloads, hash_bytes(archive.key).hex(), archive.name)

    def _managed(self, archive, path):
        path = Path(path).absolute()
        return (archive.kind != "GameFileSource" and not path.is_symlink()
                and path.parent.resolve() == self.downloads
                and path.name == self.target(archive).name
                and not any(path.resolve().is_relative_to(root) for root in self.protected))

    def record(self, archive, path):
        if not self._managed(archive, path):
            return
        stamp = file_stamp(path)[:4]
        if stamp[2] != archive.size:
            raise WabbajackError(f"Downloaded archive changed: {archive.name}")
        marker = auxiliary_path(Path(path), ".wabbajack-owned")
        write_atomic(marker, json.dumps({"owner": self.owner, "hash": archive.key,
                                        "stamp": stamp}).encode("utf-8"))

    def owns(self, archive, path):
        if not self._managed(archive, path):
            return False
        marker = auxiliary_path(Path(path), ".wabbajack-owned")
        try:
            info = marker.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                return False
            saved = json.loads(marker.read_text(encoding="utf-8"))
            return (saved.get("owner") == self.owner and saved.get("hash") == archive.key
                    and saved.get("stamp") == list(file_stamp(path)[:4]))
        except (OSError, ValueError, AttributeError):
            return False

    def remove(self, archive, path):
        if not self.owns(archive, path):
            return False
        path = Path(path)
        path.unlink()
        auxiliary_path(path, ".wabbajack-owned").unlink(missing_ok=True)
        emit(self.log, "archive.cache.cleared", archive=archive.name,
             path=path, bytes=archive.size)
        return True
