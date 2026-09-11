from __future__ import annotations

import gzip
import json
import os
import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads.install import ManualDownloadRequired
from .games import nexus_domain
from .hashes import XXHash, canonical_hash, hash_bytes, file_hash, verify_file
from .paths import WabbajackError
from .http import download_http, safe_error as _safe_error, connection_scope, connection_slot, limited_response, download_error_scope
from .hosts import automatic_source, download_host, source_url, is_loverslab_url
from .diagnostics import emit, emit_exception, url_host
from .verification import bind_verification, file_stamp, verified_replace

_active_lock = threading.Lock()
_active = {}


class ArchiveCacheIndex:
    def __init__(self, roots, sizes, log=None):
        self.roots = tuple(dict.fromkeys(Path(root).resolve() for root in roots))
        self.sizes = set(sizes)
        self.log = log
        self._folders = {}
        self._stamps = {}
        self._last_refresh = 0.0
        self._lock = threading.Lock()

    def include_sizes(self, sizes):
        with self._lock:
            added = set(sizes) - self.sizes
            if added:
                self.sizes.update(added)
                self._stamps.clear()
                self._last_refresh = 0.0

    def refresh(self, stop=None, *, force=False):
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_refresh < 1:
                return
            for root in self.roots:
                if stop is not None and stop.is_set():
                    raise InterruptedError("Installation stopped")
                try:
                    info = root.stat()
                except OSError:
                    self._folders.pop(root, None)
                    self._stamps.pop(root, None)
                    continue
                stamp = info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns
                if not force and self._stamps.get(root) == stamp:
                    continue
                by_size = {}
                try:
                    if root.is_dir():
                        with os.scandir(root) as entries:
                            for entry in entries:
                                if stop is not None and stop.is_set():
                                    raise InterruptedError("Installation stopped")
                                try:
                                    if not entry.is_file() or entry.name.endswith((".part", ".tmp")):
                                        continue
                                    size = entry.stat().st_size
                                except OSError:
                                    continue
                                if size in self.sizes:
                                    by_size.setdefault(size, {})[Path(entry.path)] = None
                except InterruptedError:
                    raise
                except OSError as exc:
                    emit_exception(self.log, "acquisition.cache.unreadable", exc, path=root)
                    self._folders.pop(root, None)
                    self._stamps.pop(root, None)
                    continue
                self._folders[root] = by_size
                self._stamps[root] = stamp
            self._last_refresh = now

    def candidates(self, size):
        with self._lock:
            return tuple(path for folder in self._folders.values()
                         for path in folder.get(size, ()))

    def groups(self):
        with self._lock:
            by_size = {}
            for folder in self._folders.values():
                for size, paths in folder.items():
                    by_size.setdefault(size, []).extend(paths)
            return by_size

    def add(self, path):
        path = Path(path).resolve()
        size = path.stat().st_size
        with self._lock:
            if path.parent in self.roots and size in self.sizes:
                self._folders.setdefault(path.parent, {}).setdefault(size, {})[path] = None

    def discard(self, path, size):
        path = Path(path).resolve()
        with self._lock:
            self._folders.get(path.parent, {}).get(size, {}).pop(path, None)


def route_nxm(link, api=None) -> bool:
    from Utils.app_log import app_log
    key = (link.game_domain.lower(), int(link.mod_id), int(link.file_id))
    with _active_lock:
        receiver = _active.get(key)
    if receiver is None:
        emit(lambda message: app_log("[wabbajack] " + message), "nxm.unmatched",
             game=link.game_domain, mod_id=int(link.mod_id), file_id=int(link.file_id))
        return False
    receiver.put((link, api))
    emit(lambda message: app_log("[wabbajack] " + message), "nxm.routed",
         game=link.game_domain, mod_id=int(link.mod_id), file_id=int(link.file_id),
         refreshed_api=api is not None)
    return True


class _CombinedStop:
    def __init__(self, external, internal):
        self.external, self.internal = external, internal

    def is_set(self):
        return self.external.is_set() or self.internal.is_set()

    def wait(self, timeout=None):
        if self.external.is_set():
            return True
        return self.internal.wait(timeout) or self.external.is_set()


def cdn_definition(url, size, expected, stop=None, log=None):
    parsed = urlparse(url)
    hosts = {"wabbajack.b-cdn.net": "authored-files.wabbajack.org",
             "wabbajack-mirror.b-cdn.net": "mirror.wabbajack.org",
             "wabbajack-patches.b-cdn.net": "patches.wabbajack.org",
             "wabbajacktest.b-cdn.net": "test-files.wabbajack.org"}
    base = urlunparse(parsed._replace(netloc=hosts.get(parsed.netloc, parsed.netloc))).rstrip("/")
    emit(log, "cdn.definition.started", source_host=url_host(url),
         resolved_host=url_host(base), expected_size=size,
         expected_hash=expected)
    with limited_response(lambda: requests.get(base + "/definition.json.gz", timeout=(20, 60),
                      verify=resolve_ca_bundle() or True), stop) as response:
        emit(log, "cdn.definition.response", status=response.status_code,
             final_host=url_host(getattr(response, "url", base)),
             bytes=len(response.content))
        response.raise_for_status()
        definition = json.loads(gzip.decompress(response.content))
    if (size and int(definition["Size"]) != size) or (expected and canonical_hash(definition["Hash"]) != expected):
        raise WabbajackError("CDN definition does not match requested archive")
    size, expected = int(definition["Size"]), canonical_hash(definition["Hash"])
    parts = sorted(definition["Parts"], key=lambda p: int(p["Offset"]))
    cursor, indexes = 0, set()
    for part in parts:
        index, offset, count = int(part["Index"]), int(part["Offset"]), int(part["Size"])
        if offset != cursor or count <= 0 or index < 0 or index in indexes:
            raise WabbajackError("Invalid CDN part layout")
        indexes.add(index)
        cursor += count
    if cursor != size:
        raise WabbajackError("CDN parts do not cover the archive")
    emit(log, "cdn.definition.verified", parts=len(parts), bytes=size, hash=expected)
    return base, size, expected, parts


def download_cdn(url, target, size, expected, stop, progress, log=None, *, workers=1, pool=None):
    started = time.monotonic()
    base, size, expected, parts = cdn_definition(url, size, expected, stop, log)
    from .paths import auxiliary_path
    chunks = auxiliary_path(target, ".chunks")
    if chunks.is_symlink():
        raise WabbajackError("CDN chunks cannot be a symbolic link")
    chunks.mkdir(parents=True, exist_ok=True)
    workers = max(1, min(int(workers), len(parts)))
    completed = 0
    reused = 0
    active = {}
    progress_lock = threading.Lock()
    failed = threading.Event()
    combined_stop = _CombinedStop(stop, failed)
    session_state = threading.local()
    sessions = []
    sessions_lock = threading.Lock()

    def report_part(part_index, current):
        with progress_lock:
            active[part_index] = current
            progress(min(completed + sum(active.values()), size), size)

    def fetch_part(part):
        part_index = int(part["Index"])
        dest = chunks / str(part_index)
        count, digest = int(part["Size"]), canonical_hash(part["Hash"])
        if not verify_file(dest, digest, count, combined_stop):
            emit(log, "cdn.part.started", target=target, part=part_index,
                 offset=int(part["Offset"]), bytes=count, hash=digest)
            part_url = base + f"/parts/{part['Index']}"
            session = getattr(session_state, "session", None)
            if session is None:
                session = requests.Session()
                session_state.session = session
                with sessions_lock:
                    sessions.append(session)
            download_http(part_url, dest, size=count, expected=digest,
                          stop=combined_stop,
                          progress=lambda cur, total: report_part(part_index, cur),
                          open_response=lambda headers: session.get(
                              part_url, headers=headers, stream=True, timeout=(20, 60),
                              verify=resolve_ca_bundle() or True),
                          log=log)
            return part_index, count, False
        emit(log, "cdn.part.reused", target=target, part=part_index, bytes=count)
        return part_index, count, True

    emit(log, "cdn.parts.started", target=target, parts=len(parts), workers=workers)
    iterator = iter(parts)
    pending = set()
    try:
        with (nullcontext(pool) if pool is not None else ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="wabbajack-cdn")) as executor:
            try:
                while True:
                    if stop.is_set():
                        raise InterruptedError("Installation stopped")
                    while len(pending) < workers * 2:
                        try:
                            part = next(iterator)
                        except StopIteration:
                            break
                        pending.add(executor.submit(bind_verification(fetch_part), part))
                    if not pending:
                        break
                    finished, pending = wait(pending, timeout=0.1,
                                             return_when=FIRST_COMPLETED)
                    for future in finished:
                        part_index, count, was_reused = future.result()
                        with progress_lock:
                            active.pop(part_index, None)
                            completed += count
                            reused += int(was_reused)
                            progress(min(completed + sum(active.values()), size), size)
            except BaseException:
                failed.set()
                for future in pending:
                    future.cancel()
                wait(pending)
                raise
    finally:
        for session in sessions:
            session.close()
    output = auxiliary_path(target, ".part")
    emit(log, "cdn.assembly.started", target=target, output=output,
         parts=len(parts), reused_parts=reused)
    digest = XXHash()
    with output.open("wb") as stream:
        for part in parts:
            with (chunks / str(int(part["Index"]))).open("rb") as source:
                while data := source.read(1024 * 1024):
                    if stop.is_set():
                        raise InterruptedError("Installation stopped")
                    stream.write(data)
                    digest.update(data)
        stream.flush()
        os.fsync(stream.fileno())
    stamp = file_stamp(output)
    if stamp[2] != size or digest.digest() != expected:
        raise WabbajackError("Assembled CDN archive failed verification")
    verified_replace(output, target, digest=expected, stamp=stamp, stop=stop)
    cleanup_cdn_chunks(target, log)
    emit(log, "cdn.completed", target=target, bytes=size, hash=expected,
         parts=len(parts), reused_parts=reused,
         elapsed_seconds=round(time.monotonic() - started, 3))
    return target


def cleanup_cdn_chunks(target, log=None):
    from .paths import auxiliary_path
    from .store import Store
    chunks = auxiliary_path(target, ".chunks")
    if chunks.is_symlink() or not chunks.is_dir():
        return
    try:
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        Store._sync_directory(target.parent)
        removed = 0
        for path in chunks.iterdir():
            if path.name.isdecimal() and not path.is_symlink() and path.is_file():
                path.unlink()
                removed += 1
        try:
            chunks.rmdir()
        except OSError:
            pass
        Store._sync_directory(target.parent)
        emit(log, "cdn.chunks.cleaned", target=target, removed=removed)
    except OSError as exc:
        emit_exception(log, "cdn.chunks.cleanup_deferred", exc, target=target)


def partial_download_space(archive, downloads, stop=None, log=None):
    if archive.kind not in {"Http", "HTTP", "WabbajackCDN"}:
        return 0
    from .paths import auxiliary_path, cache_path
    target = cache_path(downloads, hash_bytes(archive.key).hex(), archive.name)
    def allocated(path, limit, expected=""):
        if path.is_symlink():
            return 0
        try:
            info = path.stat()
            if not path.is_file() or info.st_size > limit:
                return 0
            if expected and info.st_size == limit and not verify_file(path, expected, limit, stop):
                return 0
            return min(info.st_size, info.st_blocks * 512, limit)
        except InterruptedError:
            raise
        except (OSError, WabbajackError):
            return 0
    if archive.kind in {"Http", "HTTP"}:
        return allocated(auxiliary_path(target, ".part"), archive.size, archive.key)
    credit = allocated(auxiliary_path(target, ".part"), archive.size)
    chunks = auxiliary_path(target, ".chunks")
    if chunks.is_symlink() or not chunks.is_dir():
        return credit
    try:
        _, _, _, parts = cdn_definition(archive.state["Url"], archive.size, archive.key, stop, log)
        for part in parts:
            path = chunks / str(int(part["Index"]))
            size, digest = int(part["Size"]), canonical_hash(part["Hash"])
            if not path.is_symlink() and verify_file(path, digest, size, stop):
                credit += allocated(path, size)
            else:
                credit += allocated(auxiliary_path(path, ".part"), size, digest)
    except InterruptedError:
        raise
    except (OSError, ValueError, KeyError, TypeError, requests.RequestException) as exc:
        emit_exception(log, "cdn.partial_space.unavailable", exc, archive=archive.name)
    return min(credit, archive.size * 2)


def download_package(url, target, *, size=0, expected="", stop=None, progress=None,
                     log=None):
    stop = stop or threading.Event()
    if expected:
        expected = canonical_hash(expected)
    if expected and size and verify_file(target, expected, size, stop):
        emit(log, "package.download.reused", target=target, bytes=size,
             hash=expected, host=url_host(url))
        cleanup_cdn_chunks(target, log)
        if progress:
            progress(size, size)
        return target
    host = urlparse(url).hostname
    if host in {"authored-files.wabbajack.org", "mirror.wabbajack.org", "patches.wabbajack.org",
                "test-files.wabbajack.org", "wabbajack.b-cdn.net", "wabbajack-mirror.b-cdn.net",
                "wabbajack-patches.b-cdn.net", "wabbajacktest.b-cdn.net"}:
        from Utils.ui.config import load_collection_settings
        workers = load_collection_settings()["max_concurrent"]
        emit(log, "package.download.route", route="wabbajack-cdn", host=host,
             target=target, workers=workers)
        with connection_scope(threading.BoundedSemaphore(max(1, int(workers)))):
            return download_cdn(url, target, size, expected, stop,
                                progress or (lambda *_: None), log, workers=workers)
    emit(log, "package.download.route", route="http", host=host, target=target)
    return download_http(url, target, size=size, expected=expected, stop=stop,
                         progress=progress, log=log)


class Acquisition:
    def __init__(self, request, report, callbacks, control, *, archives=None, budget=None):
        self.request, self.report = request, report
        self.cb, self.control = callbacks, control
        self.budget = budget
        from .archive_cache import OwnedArchives
        self.owned = OwnedArchives(request, callbacks.on_log)
        self._hashes = {}
        self._manual_lock = threading.Lock()
        self._nxm = {}
        self._progress = {}
        self._last_emit = {}
        self._last_aggregate = 0.0
        self._lock = threading.Lock()
        self._started = time.monotonic()
        from Utils.ui.config import load_collection_settings
        self.workers = max(1, int(load_collection_settings()["max_concurrent"]))
        self._connections = connection_scope(threading.BoundedSemaphore(self.workers))
        self._download_errors = download_error_scope(budget.fail if budget else None)
        self._cdn_pool = None
        self.ids = {a.key: i + 1 for i, a in enumerate(request.package.archives.values())}
        self.archives = list(request.package.archives.values() if archives is None else archives)
        self._loverslab_credentials = None
        self._loverslab_client = None
        if any(a.key not in report.cached and is_loverslab_url(source_url(a)) for a in self.archives):
            from Utils.loverslab.credentials import load_loverslab_credentials, CredentialStorageError
            try:
                self._loverslab_credentials = load_loverslab_credentials()
            except CredentialStorageError as exc:
                self.cb.on_log(str(exc))
        self.cache_index = getattr(report, "cache_index", None)
        if self.cache_index is None:
            from Utils.downloads.core import get_scan_dirs
            self.cache_index = ArchiveCacheIndex(
                [request.downloads, *get_scan_dirs(request.game.name)],
                (a.size for a in self.archives), log=self.cb.on_log)
            self.cache_index.refresh(control.stop)
        else:
            self.cache_index.include_sizes(a.size for a in self.archives)

    def __enter__(self):
        with _active_lock:
            for archive in self.archives:
                if archive.kind == "Nexus":
                    key = self.nexus_key(archive)
                    if key in _active:
                        raise WabbajackError("Another installation is waiting for these Nexus files")
                    self._nxm[key] = queue.Queue()
            _active.update(self._nxm)
        emit(self.cb.on_log, "acquisition.routes.registered",
             archives=len(self.archives), nexus_routes=len(self._nxm))
        self._connections.__enter__()
        self._download_errors.__enter__()
        self._cdn_pool = ThreadPoolExecutor(max_workers=self.workers,
                                           thread_name_prefix="wabbajack-cdn")
        return self

    def __exit__(self, *_):
        if self._cdn_pool is not None:
            self._cdn_pool.shutdown(wait=True, cancel_futures=True)
        self._connections.__exit__(None, None, None)
        self._download_errors.__exit__(None, None, None)
        if self._loverslab_client is not None:
            self._loverslab_client.close()
        self._loverslab_credentials = None
        with _active_lock:
            for key, receiver in self._nxm.items():
                if _active.get(key) is receiver:
                    del _active[key]
        emit(self.cb.on_log, "acquisition.routes.released",
             nexus_routes=len(self._nxm))

    def nexus_key(self, archive):
        state = archive.state
        return (nexus_domain(state.get("GameName", state.get("Game", self.request.package.game))),
                int(state["ModID"]), int(state["FileID"]))

    def _release_nxm(self, archive):
        if archive.kind == "Nexus":
            key = self.nexus_key(archive)
            with _active_lock:
                if _active.get(key) is self._nxm.get(key):
                    _active.pop(key, None)

    def cached(self, archive, *, refresh=False):
        if archive.key in self.report.cached:
            path = self.report.cached[archive.key]
            try:
                valid = verify_file(path, archive.key, archive.size, self.control.stop)
            except InterruptedError:
                raise
            except (OSError, WabbajackError) as exc:
                emit_exception(self.cb.on_log, "acquisition.cache.unreadable", exc, path=path)
                valid = False
            if valid:
                emit(self.cb.on_log, "acquisition.cache.preflight_hit",
                     archive=archive.name, path=path, bytes=archive.size,
                     hash=archive.key)
                return path
            emit(self.cb.on_log, "acquisition.cache.preflight_rejected",
                 archive=archive.name, path=path, bytes=archive.size,
                 hash=archive.key)
        self.cache_index.refresh(self.control.stop, force=refresh)
        for path in self.cache_index.candidates(archive.size):
            try:
                stamp = file_stamp(path)
                if stamp[2] != archive.size:
                    continue
                identity = (str(path), *stamp)
                with self._lock:
                    digest = self._hashes.get(identity)
                if digest is None:
                    digest = file_hash(path, self.control.stop)
                    if file_stamp(path) != stamp:
                        continue
                    with self._lock:
                        self._hashes[identity] = digest
            except InterruptedError:
                raise
            except (OSError, WabbajackError) as exc:
                emit_exception(self.cb.on_log, "acquisition.cache.unreadable", exc, path=path)
                continue
            if digest == archive.key:
                emit(self.cb.on_log, "acquisition.cache.scan_hit",
                     archive=archive.name, path=path, bytes=archive.size,
                     hash=archive.key)
                return path
        return None

    def automatic(self, archive):
        return archive.key in self.report.cached or archive.kind == "GameFileSource" or self._automatic_source(archive)

    def _automatic_source(self, archive):
        return automatic_source(archive, self.request.premium,
                                loverslab_available=self._loverslab_credentials is not None)

    def _download_loverslab(self, archive, target, progress):
        from Utils.loverslab.client import LoversLabClient
        with self._lock:
            if self._loverslab_client is None:
                self._loverslab_client = LoversLabClient(
                    self._loverslab_credentials, stop=self.control.stop, log=self.cb.on_log)
            client = self._loverslab_client
        return client.download(archive, target, progress=progress)

    def prefetch(self, archive):
        if (not self.control.stop.is_set() and archive.kind == "Nexus" and self.request.premium
                and archive.key not in self.report.cached and self.request.api):
            key = self.nexus_key(archive)
            emit(self.cb.on_log, "nexus.links.prefetch.started", archive=archive.name,
                 game=key[0], mod_id=key[1], file_id=key[2])
            links = self.request.api.get_download_links(*key)
            emit(self.cb.on_log, "nexus.links.prefetch.completed", archive=archive.name,
                 links=len(links or []))
            return links
        return None

    def manual(self, archive, reason=""):
        return self(archive, manual=True, reason=reason)

    def clear_archive(self, archive, path, *, required=False):
        if not self.budget or archive.kind == "GameFileSource":
            return
        removed = self.owned.remove(archive, path)
        if required and not removed:
            raise WabbajackError(f"Archive changed before cleanup: {archive.name}; check requirements again")
        if removed:
            self.cache_index.discard(path, archive.size)
        retained = self._clear_partial_downloads(archive, path)
        if not removed and Path(path).resolve() == self.owned.target(archive):
            retained += Path(path).stat().st_size
        self.budget.release(archive, retained)

    def _clear_partial_downloads(self, archive, source):
        from Utils.atomic_write import filename_limit
        from .paths import auxiliary_path
        target = self.owned.target(archive)
        paths = [auxiliary_path(target, ".part")]
        if archive.kind == "Nexus":
            folder = self.request.downloads / ".wabbajack" / hash_bytes(archive.key).hex()
            name = target.name if len(os.fsencode(archive.name)) > filename_limit(folder) else archive.name
            paths.append(auxiliary_path(folder / name, ".part"))
        chunks = auxiliary_path(target, ".chunks")
        if not chunks.is_symlink() and chunks.is_dir():
            paths.extend(path for path in chunks.iterdir()
                         if path.name.isdecimal() or path.name.removesuffix(".part").isdecimal())
        retained = removed = 0
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            if not path.resolve().is_relative_to(self.owned.downloads):
                raise WabbajackError("Partial download is outside managed downloads")
            size = path.stat().st_size
            if (path.resolve() == Path(source).resolve()
                    or any(path.resolve().is_relative_to(root) for root in self.owned.protected)):
                retained += size
            else:
                path.unlink()
                removed += size
        if removed:
            emit(self.cb.on_log, "archive.partials.cleared", archive=archive.name, bytes=removed)
        return retained

    def __call__(self, archive, prefetched=None, *, manual=False, reason=""):
        started = time.monotonic()
        emit(self.cb.on_log, "acquisition.started", archive=archive.name,
             kind=archive.kind, bytes=archive.size, hash=archive.key,
             manual=manual, prefetched_links=len(prefetched or []))
        if self.control.stop.is_set():
            raise InterruptedError("Installation stopped")
        if archive.key in self.report.game_files:
            path = self.report.game_files[archive.key]
            if verify_file(path, archive.key, archive.size, self.control.stop):
                emit(self.cb.on_log, "acquisition.game_file.verified",
                     archive=archive.name, path=path)
                return path
            raise WabbajackError(f"Game file changed after preflight: {path.name}")
        if archive.key in self.report.prepared_game_files:
            from .game_files import materialize_game_file
            self.cb.on_log(f"Preparing managed 4 GB/LAA copy of {archive.name}")
            path = materialize_game_file(self.request, archive,
                                         self.report.prepared_game_files[archive.key],
                                         self.control.stop, self.cb.on_log)
            emit(self.cb.on_log, "acquisition.game_file.prepared",
                 archive=archive.name, path=path,
                 elapsed_seconds=round(time.monotonic() - started, 3))
            return path
        if archive.kind == "GameFileSource":
            raise WabbajackError(f"A reusable output changed after preflight. Start the operation again to verify the source for {archive.name}.")
        cached = self.cached(archive)
        if cached:
            if archive.kind == "WabbajackCDN":
                cleanup_cdn_chunks(cached, self.cb.on_log)
            self._release_nxm(archive)
            emit(self.cb.on_log, "acquisition.completed", archive=archive.name,
                 route="cache", path=cached,
                 elapsed_seconds=round(time.monotonic() - started, 3))
            return cached
        if self.budget:
            self.budget.acquire(archive)
        row = self.ids[archive.key]
        self.cb.on_dl_mod_start(row, archive.name, archive.size)

        def progress(cur, total):
            now = time.monotonic()
            with self._lock:
                self._progress[row] = cur
                current = sum(self._progress.values())
                if now - self._last_emit.get(row, 0) >= 0.1 or cur == total:
                    self.cb.on_dl_mod_update(row, cur, total)
                    self._last_emit[row] = now
                if now - self._last_aggregate >= 0.1 or cur == total:
                    self.cb.on_agg_download(current, self.report.download_bytes,
                                           current / max(now - self._started, 0.1) / 1024 ** 2)
                    self._last_aggregate = now

        from .paths import cache_path
        target = cache_path(self.request.downloads, hash_bytes(archive.key).hex(), archive.name)
        deferred = False
        try:
            if manual:
                route = "manual"
                path = self._manual(archive, target, progress, reason)
            elif is_loverslab_url(source_url(archive)):
                if self._loverslab_credentials is not None:
                    route = "loverslab"
                    with connection_slot(self.control.stop):
                        path = self._download_loverslab(archive, target, progress)
                else:
                    route = "manual"
                    path = self._manual(archive, target, progress,
                                        "Connect LoversLab in Settings → Connections to enable automatic downloads.")
            elif archive.kind in {"Http", "HTTP"}:
                route = "http"
                headers = {}
                for header in archive.state.get("Headers", []):
                    key, sep, value = header.partition(":")
                    if sep:
                        headers[key.strip()] = value.strip()
                path = download_http(archive.state["Url"], target, size=archive.size,
                                     expected=archive.key, headers=headers,
                                     stop=self.control.stop, progress=progress,
                                     log=self.cb.on_log)
            elif archive.kind == "WabbajackCDN":
                route = "wabbajack-cdn"
                try:
                    path = download_cdn(archive.state["Url"], target, archive.size, archive.key,
                                        self.control.stop, progress, self.cb.on_log,
                                        workers=self.workers, pool=self._cdn_pool)
                except (ValueError, TypeError, KeyError, gzip.BadGzipFile, EOFError) as exc:
                    raise WabbajackError("The CDN returned invalid archive information. Obtain the exact archive from the author.") from exc
            elif archive.kind == "Nexus" and self.request.premium:
                route = "nexus-premium"
                path = self._nexus(archive, target, progress, prefetched=prefetched)
            elif automatic_source(archive):
                route = archive.kind.casefold()
                path = download_host(archive, target, stop=self.control.stop,
                                     progress=progress, log=self.cb.on_log)
            else:
                route = "manual"
                path = self._manual(archive, target, progress)
            if not verify_file(path, archive.key, archive.size, self.control.stop):
                raise WabbajackError(f"Archive failed verification: {archive.name}")
            if route != "manual":
                self.owned.record(archive, path)
            self.cache_index.add(path)
            progress(archive.size, archive.size)
            emit(self.cb.on_log, "acquisition.completed", archive=archive.name,
                 route=route, path=path, bytes=archive.size, hash=archive.key,
                 elapsed_seconds=round(time.monotonic() - started, 3))
            return path
        except (requests.RequestException, WabbajackError) as exc:
            if self.control.stop.is_set():
                raise InterruptedError("Installation stopped") from exc
            if not manual and self._automatic_source(archive):
                reason = _safe_error(exc)
                self.cb.on_log(f"{archive.name}: automatic download needs manual assistance: {reason}")
                emit_exception(self.cb.on_log, "acquisition.deferred_to_manual", exc,
                               archive=archive.name, kind=archive.kind, reason=reason)
                if self.budget:
                    return self.manual(archive, reason)
                self.cb.on_status(f"Waiting for a manual download: {archive.name}. Other downloads continue.")
                deferred = True
                raise ManualDownloadRequired(reason) from exc
            raise
        finally:
            if not deferred:
                self._release_nxm(archive)
            self.cb.on_dl_mod_finish(row)

    def finish_progress(self):
        with self._lock:
            self.cb.on_agg_download(sum(self._progress.values()), self.report.download_bytes, 0.0)
        emit(self.cb.on_log, "acquisition.progress.completed",
             downloaded_bytes=sum(self._progress.values()),
             planned_bytes=self.report.download_bytes)

    def _nexus(self, archive, target, progress, link=None, prefetched=None):
        from os import fsencode
        from Utils.atomic_write import filename_limit
        from Nexus.nexus_download import NexusDownloader, DownloadResult
        if self.request.api is None:
            raise WabbajackError("Log in to Nexus to use Mod Manager Download")
        folder = self.request.downloads / ".wabbajack" / hash_bytes(archive.key).hex()
        incoming = folder / (target.name if len(fsencode(archive.name)) > filename_limit(folder) else archive.name)
        key = self.nexus_key(archive)
        emit(self.cb.on_log, "nexus.download.started", archive=archive.name,
             game=key[0], mod_id=key[1], file_id=key[2], incoming=incoming,
             browser_link=link is not None, prefetched_links=len(prefetched or []))
        def stream_handler(**kwargs):
            path = download_http(kwargs["url"], incoming, size=archive.size,
                                 expected=archive.key, stop=self.control.stop,
                                 progress=progress, log=self.cb.on_log)
            return DownloadResult(success=True, file_path=path, file_name=archive.name,
                                  bytes_downloaded=archive.size, game_domain=kwargs["game_domain"],
                                  mod_id=kwargs["mod_id"], file_id=kwargs["file_id"])
        downloader = NexusDownloader(self.request.api, folder, stream_handler=stream_handler)
        try:
            for attempt in range(1 if link is not None else 2):
                emit(self.cb.on_log, "nexus.download.attempt", archive=archive.name,
                     attempt=attempt + 1, browser_link=link is not None,
                     prefetched=bool(prefetched if attempt == 0 else None))
                if link is not None:
                    result = downloader.download_from_nxm(link, progress_cb=progress,
                        cancel=self.control.stop, known_file_name=archive.name)
                else:
                    result = downloader.download_file(*self.nexus_key(archive), progress_cb=progress,
                        cancel=self.control.stop, known_file_name=archive.name, expected_size_bytes=archive.size,
                        prefetched_links=prefetched if attempt == 0 else None)
                if result.file_path or self.control.stop.is_set():
                    break
                self.cb.on_status(f"Refreshing download links for {archive.name}")
                emit(self.cb.on_log, "nexus.links.refresh", archive=archive.name,
                     attempt=attempt + 1)
            path = Path(result.file_path) if result.file_path else None
            stamp = file_stamp(path) if path and path.is_file() else None
            if not path or not verify_file(path, archive.key, archive.size, self.control.stop):
                if path and path.is_file():
                    from .paths import auxiliary_path
                    invalid = auxiliary_path(path, f".invalid-{time.time_ns()}")
                    path.rename(invalid)
                    emit(self.cb.on_log, "nexus.download.rejected",
                         archive=archive.name, preserved_as=invalid)
                raise WabbajackError(f"Nexus download failed: {archive.name}: {_safe_error(getattr(result, 'error', ''))}")
            verified_replace(path, target, digest=archive.key, stamp=stamp,
                             stop=self.control.stop)
            self.owned.record(archive, target)
            emit(self.cb.on_log, "nexus.download.completed", archive=archive.name,
                 target=target, bytes=archive.size)
            return target
        finally:
            downloader.close_worker_session()

    def _manual(self, archive, target, progress, reason=""):
        while not self._manual_lock.acquire(timeout=0.2):
            if self.control.stop.is_set():
                raise InterruptedError("Installation stopped")
        try:
            if self.control.stop.is_set():
                raise InterruptedError("Installation stopped")
            if archive.kind == "Nexus":
                domain, mod, file = self.nexus_key(archive)
                url = f"https://www.nexusmods.com/{domain}/mods/{mod}?tab=files&file_id={file}"
                inbox = self._nxm.get((domain, mod, file), queue.Queue())
            else:
                url = source_url(archive)
                inbox = queue.Queue()
            emit(self.cb.on_log, "manual.waiting", archive=archive.name,
                 kind=archive.kind, host=url_host(url), expected_name=archive.name,
                 expected_size=archive.size, expected_hash=archive.key, reason=reason)
            if archive.state.get("Prompt"):
                self.cb.on_log(str(archive.state["Prompt"]))
            payload = {"idx": self.ids[archive.key], "total": len(self.ids),
                "name": archive.name, "file_name": archive.name, "size": archive.size,
                "url": url, "optional": False, "upcoming": [], "required_strict": True,
                "source": archive.kind, "reason": reason, "instructions": str(archive.state.get("Prompt") or ""),
                "status": ""}
            self.cb.on_manual_mod(payload.copy())
            def status(message):
                payload["status"] = message
                self.cb.on_manual_mod(payload.copy())
                self.cb.on_status(message)
            while not self.control.stop.is_set():
                try:
                    link, api = inbox.get_nowait()
                    emit(self.cb.on_log, "manual.nxm.received", archive=archive.name,
                         refreshed_api=api is not None)
                    if api is not None:
                        self.request.api = api
                    try:
                        return self._nexus(archive, target, progress, link)
                    except WabbajackError as exc:
                        if self.budget:
                            raise
                        self.cb.on_log(str(exc))
                        emit_exception(self.cb.on_log, "manual.nxm.failed", exc,
                                       archive=archive.name)
                        status("Download link failed; use a fresh browser link or Select File.")
                except queue.Empty:
                    pass
                try:
                    selected = self.control.manual_queue.get_nowait()
                    valid = False
                    if selected is not None:
                        selected_path = Path(selected)
                        try:
                            selected_size = (selected_path.stat().st_size
                                             if selected_path.is_file() else None)
                        except OSError:
                            selected_size = None
                        emit(self.cb.on_log, "manual.file.selected", archive=archive.name,
                             path=selected_path, actual_size=selected_size,
                             expected_size=archive.size, expected_hash=archive.key)
                        status("Checking the selected file's size and hash…")
                        try:
                            valid = verify_file(Path(selected), archive.key, archive.size, self.control.stop)
                        except OSError as exc:
                            emit_exception(self.cb.on_log, "manual.file.probe_failed", exc,
                                           archive=archive.name, path=selected_path)
                    if valid:
                        if self.budget:
                            return Path(selected)
                        from .store import Store
                        if Path(selected).resolve() != target.resolve():
                            Store._copy(Path(selected), target, stop=self.control.stop)
                            self.owned.record(archive, target)
                        emit(self.cb.on_log, "manual.file.verified", archive=archive.name,
                             path=selected, cached_as=target)
                        return target
                    emit(self.cb.on_log, "manual.file.rejected", archive=archive.name,
                         path=selected)
                    status(f"That file does not match the required size and hash. Select the exact archive: {archive.name}")
                except queue.Empty:
                    pass
                found = self.cached(archive, refresh=True)
                if found:
                    emit(self.cb.on_log, "manual.cache.detected", archive=archive.name,
                         path=found)
                    return found
                self.control.stop.wait(2)
        finally:
            self._manual_lock.release()
        raise InterruptedError("Installation stopped")
