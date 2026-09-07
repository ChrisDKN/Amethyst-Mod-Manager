from __future__ import annotations

import gzip
import json
import queue
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads.install import ManualDownloadRequired
from .games import nexus_domain
from .hashes import canonical_hash, hash_bytes, file_hash, verify_file
from .paths import WabbajackError
from .http import download_http, safe_error as _safe_error
from .hosts import automatic_source, download_host, source_url
from .diagnostics import emit, emit_exception, url_host

_active_lock = threading.Lock()
_active = {}


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


def download_cdn(url, target, size, expected, stop, progress, log=None):
    started = time.monotonic()
    parsed = urlparse(url)
    hosts = {"wabbajack.b-cdn.net": "authored-files.wabbajack.org",
             "wabbajack-mirror.b-cdn.net": "mirror.wabbajack.org",
             "wabbajack-patches.b-cdn.net": "patches.wabbajack.org",
             "wabbajacktest.b-cdn.net": "test-files.wabbajack.org"}
    base = urlunparse(parsed._replace(netloc=hosts.get(parsed.netloc, parsed.netloc))).rstrip("/")
    emit(log, "cdn.definition.started", source_host=url_host(url),
         resolved_host=url_host(base), target=target, expected_size=size,
         expected_hash=expected)
    with requests.get(base + "/definition.json.gz", timeout=(20, 60),
                      verify=resolve_ca_bundle() or True) as response:
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
    emit(log, "cdn.definition.verified", target=target, parts=len(parts),
         bytes=size, hash=expected)
    from .paths import auxiliary_path
    chunks = auxiliary_path(target, ".chunks")
    chunks.mkdir(parents=True, exist_ok=True)
    completed = 0
    reused = 0
    for part in parts:
        part_index = int(part["Index"])
        dest = chunks / str(part_index)
        count, digest = int(part["Size"]), canonical_hash(part["Hash"])
        if not verify_file(dest, digest, count, stop):
            emit(log, "cdn.part.started", target=target, part=part_index,
                 offset=int(part["Offset"]), bytes=count, hash=digest)
            download_http(base + f"/parts/{part['Index']}", dest, size=count, expected=digest,
                          stop=stop, progress=lambda cur, total: progress(completed + cur, size),
                          log=log)
        else:
            reused += 1
            emit(log, "cdn.part.reused", target=target, part=part_index, bytes=count)
        completed += count
        progress(completed, size)
    output = auxiliary_path(target, ".part")
    emit(log, "cdn.assembly.started", target=target, output=output,
         parts=len(parts), reused_parts=reused)
    with output.open("wb") as stream:
        for part in parts:
            with (chunks / str(int(part["Index"]))).open("rb") as source:
                while data := source.read(1024 * 1024):
                    if stop.is_set():
                        raise InterruptedError("Installation stopped")
                    stream.write(data)
    if not verify_file(output, expected, size, stop):
        raise WabbajackError("Assembled CDN archive failed verification")
    output.replace(target)
    emit(log, "cdn.completed", target=target, bytes=size, hash=expected,
         parts=len(parts), reused_parts=reused,
         elapsed_seconds=round(time.monotonic() - started, 3))
    return target


def download_package(url, target, *, size=0, expected="", stop=None, progress=None,
                     log=None):
    stop = stop or threading.Event()
    if expected:
        expected = canonical_hash(expected)
    if expected and size and verify_file(target, expected, size, stop):
        emit(log, "package.download.reused", target=target, bytes=size,
             hash=expected, host=url_host(url))
        if progress:
            progress(size, size)
        return target
    host = urlparse(url).hostname
    if host in {"authored-files.wabbajack.org", "mirror.wabbajack.org", "patches.wabbajack.org",
                "test-files.wabbajack.org", "wabbajack.b-cdn.net", "wabbajack-mirror.b-cdn.net",
                "wabbajack-patches.b-cdn.net", "wabbajacktest.b-cdn.net"}:
        emit(log, "package.download.route", route="wabbajack-cdn", host=host,
             target=target)
        return download_cdn(url, target, size, expected, stop,
                            progress or (lambda *_: None), log)
    emit(log, "package.download.route", route="http", host=host, target=target)
    return download_http(url, target, size=size, expected=expected, stop=stop,
                         progress=progress, log=log)


class Acquisition:
    def __init__(self, request, report, callbacks, control, *, archives=None):
        self.request, self.report = request, report
        self.cb, self.control = callbacks, control
        self._hashes = {}
        self._manual_lock = threading.Lock()
        self._nxm = {}
        self._progress = {}
        self._last_emit = {}
        self._last_aggregate = 0.0
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self.ids = {a.key: i + 1 for i, a in enumerate(request.package.archives.values())}
        self.archives = list(request.package.archives.values() if archives is None else archives)

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
        return self

    def __exit__(self, *_):
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

    def cached(self, archive):
        if archive.key in self.report.cached:
            path = self.report.cached[archive.key]
            if verify_file(path, archive.key, archive.size, self.control.stop):
                emit(self.cb.on_log, "acquisition.cache.preflight_hit",
                     archive=archive.name, path=path, bytes=archive.size,
                     hash=archive.key)
                return path
            emit(self.cb.on_log, "acquisition.cache.preflight_rejected",
                 archive=archive.name, path=path, bytes=archive.size,
                 hash=archive.key)
        from Utils.downloads.core import get_scan_dirs
        for root in [self.request.downloads, *get_scan_dirs(self.request.game.name)]:
            if not root.is_dir():
                continue
            for path in root.iterdir():
                if not path.is_file() or path.name.endswith((".part", ".tmp")):
                    continue
                stat = path.stat()
                if stat.st_size != archive.size:
                    continue
                identity = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                digest = self._hashes.get(identity)
                if digest is None:
                    digest = file_hash(path, self.control.stop)
                    self._hashes[identity] = digest
                if digest == archive.key:
                    emit(self.cb.on_log, "acquisition.cache.scan_hit",
                         archive=archive.name, path=path, bytes=archive.size,
                         hash=archive.key)
                    return path
        return None

    def automatic(self, archive):
        return archive.key in self.report.cached or archive.kind == "GameFileSource" or automatic_source(archive, self.request.premium)

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
            self._release_nxm(archive)
            emit(self.cb.on_log, "acquisition.completed", archive=archive.name,
                 route="cache", path=cached,
                 elapsed_seconds=round(time.monotonic() - started, 3))
            return cached
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
                                        self.control.stop, progress, self.cb.on_log)
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
            progress(archive.size, archive.size)
            emit(self.cb.on_log, "acquisition.completed", archive=archive.name,
                 route=route, path=path, bytes=archive.size, hash=archive.key,
                 elapsed_seconds=round(time.monotonic() - started, 3))
            return path
        except (requests.RequestException, WabbajackError) as exc:
            if self.control.stop.is_set():
                raise InterruptedError("Installation stopped") from exc
            if not manual and automatic_source(archive, self.request.premium):
                reason = _safe_error(exc)
                self.cb.on_log(f"{archive.name}: automatic download needs manual assistance: {reason}")
                emit_exception(self.cb.on_log, "acquisition.deferred_to_manual", exc,
                               archive=archive.name, kind=archive.kind, reason=reason)
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
            if not path or not verify_file(path, archive.key, archive.size, self.control.stop):
                if path and path.is_file():
                    from .paths import auxiliary_path
                    invalid = auxiliary_path(path, f".invalid-{time.time_ns()}")
                    path.rename(invalid)
                    emit(self.cb.on_log, "nexus.download.rejected",
                         archive=archive.name, preserved_as=invalid)
                raise WabbajackError(f"Nexus download failed: {archive.name}: {_safe_error(getattr(result, 'error', ''))}")
            path.replace(target)
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
                        from .store import Store
                        if Path(selected).resolve() != target.resolve():
                            Store._copy(Path(selected), target, stop=self.control.stop)
                        emit(self.cb.on_log, "manual.file.verified", archive=archive.name,
                             path=selected, cached_as=target)
                        return target
                    emit(self.cb.on_log, "manual.file.rejected", archive=archive.name,
                         path=selected)
                    status(f"That file does not match the required size and hash. Select the exact archive: {archive.name}")
                except queue.Empty:
                    pass
                found = self.cached(archive)
                if found:
                    emit(self.cb.on_log, "manual.cache.detected", archive=archive.name,
                         path=found)
                    return found
                self.control.stop.wait(2)
        finally:
            self._manual_lock.release()
        raise InterruptedError("Installation stopped")
