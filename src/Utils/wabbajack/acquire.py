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

_active_lock = threading.Lock()
_active = {}


def route_nxm(link, api=None) -> bool:
    key = (link.game_domain.lower(), int(link.mod_id), int(link.file_id))
    with _active_lock:
        receiver = _active.get(key)
    if receiver is None:
        return False
    receiver.put((link, api))
    return True


def download_cdn(url, target, size, expected, stop, progress):
    parsed = urlparse(url)
    hosts = {"wabbajack.b-cdn.net": "authored-files.wabbajack.org",
             "wabbajack-mirror.b-cdn.net": "mirror.wabbajack.org",
             "wabbajack-patches.b-cdn.net": "patches.wabbajack.org",
             "wabbajacktest.b-cdn.net": "test-files.wabbajack.org"}
    base = urlunparse(parsed._replace(netloc=hosts.get(parsed.netloc, parsed.netloc))).rstrip("/")
    with requests.get(base + "/definition.json.gz", timeout=(20, 60),
                      verify=resolve_ca_bundle() or True) as response:
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
    from .paths import auxiliary_path
    chunks = auxiliary_path(target, ".chunks")
    chunks.mkdir(parents=True, exist_ok=True)
    completed = 0
    for part in parts:
        dest = chunks / str(int(part["Index"]))
        count, digest = int(part["Size"]), canonical_hash(part["Hash"])
        if not verify_file(dest, digest, count, stop):
            download_http(base + f"/parts/{part['Index']}", dest, size=count, expected=digest,
                          stop=stop, progress=lambda cur, total: progress(completed + cur, size))
        completed += count
        progress(completed, size)
    output = auxiliary_path(target, ".part")
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
    return target


def download_package(url, target, *, size=0, expected="", stop=None, progress=None):
    stop = stop or threading.Event()
    if expected:
        expected = canonical_hash(expected)
    if expected and size and verify_file(target, expected, size, stop):
        if progress:
            progress(size, size)
        return target
    host = urlparse(url).hostname
    if host in {"authored-files.wabbajack.org", "mirror.wabbajack.org", "patches.wabbajack.org",
                "test-files.wabbajack.org", "wabbajack.b-cdn.net", "wabbajack-mirror.b-cdn.net",
                "wabbajack-patches.b-cdn.net", "wabbajacktest.b-cdn.net"}:
        return download_cdn(url, target, size, expected, stop, progress or (lambda *_: None))
    return download_http(url, target, size=size, expected=expected, stop=stop, progress=progress)


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
        return self

    def __exit__(self, *_):
        with _active_lock:
            for key, receiver in self._nxm.items():
                if _active.get(key) is receiver:
                    del _active[key]

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
                return path
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
                    return path
        return None

    def automatic(self, archive):
        return archive.key in self.report.cached or archive.kind == "GameFileSource" or automatic_source(archive, self.request.premium)

    def prefetch(self, archive):
        if (not self.control.stop.is_set() and archive.kind == "Nexus" and self.request.premium
                and archive.key not in self.report.cached and self.request.api):
            return self.request.api.get_download_links(*self.nexus_key(archive))
        return None

    def manual(self, archive, reason=""):
        return self(archive, manual=True, reason=reason)

    def __call__(self, archive, prefetched=None, *, manual=False, reason=""):
        if self.control.stop.is_set():
            raise InterruptedError("Installation stopped")
        if archive.key in self.report.game_files:
            path = self.report.game_files[archive.key]
            if verify_file(path, archive.key, archive.size, self.control.stop):
                return path
            raise WabbajackError(f"Game file changed after preflight: {path.name}")
        if archive.key in self.report.prepared_game_files:
            from .game_files import materialize_game_file
            self.cb.on_log(f"Preparing managed 4 GB/LAA copy of {archive.name}")
            return materialize_game_file(self.request, archive,
                                         self.report.prepared_game_files[archive.key],
                                         self.control.stop)
        if archive.kind == "GameFileSource":
            raise WabbajackError(f"A reusable output changed after preflight. Start the operation again to verify the source for {archive.name}.")
        cached = self.cached(archive)
        if cached:
            self._release_nxm(archive)
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
                path = self._manual(archive, target, progress, reason)
            elif archive.kind in {"Http", "HTTP"}:
                headers = {}
                for header in archive.state.get("Headers", []):
                    key, sep, value = header.partition(":")
                    if sep:
                        headers[key.strip()] = value.strip()
                path = download_http(archive.state["Url"], target, size=archive.size,
                                     expected=archive.key, headers=headers,
                                     stop=self.control.stop, progress=progress)
            elif archive.kind == "WabbajackCDN":
                try:
                    path = download_cdn(archive.state["Url"], target, archive.size, archive.key,
                                        self.control.stop, progress)
                except (ValueError, TypeError, KeyError, gzip.BadGzipFile, EOFError) as exc:
                    raise WabbajackError("The CDN returned invalid archive information. Obtain the exact archive from the author.") from exc
            elif archive.kind == "Nexus" and self.request.premium:
                path = self._nexus(archive, target, progress, prefetched=prefetched)
            elif automatic_source(archive):
                path = download_host(archive, target, stop=self.control.stop, progress=progress)
            else:
                path = self._manual(archive, target, progress)
            if not verify_file(path, archive.key, archive.size, self.control.stop):
                raise WabbajackError(f"Archive failed verification: {archive.name}")
            progress(archive.size, archive.size)
            return path
        except (requests.RequestException, WabbajackError) as exc:
            if self.control.stop.is_set():
                raise InterruptedError("Installation stopped") from exc
            if not manual and automatic_source(archive, self.request.premium):
                reason = _safe_error(exc)
                self.cb.on_log(f"{archive.name}: automatic download needs manual assistance: {reason}")
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

    def _nexus(self, archive, target, progress, link=None, prefetched=None):
        from os import fsencode
        from Utils.atomic_write import filename_limit
        from Nexus.nexus_download import NexusDownloader, DownloadResult
        if self.request.api is None:
            raise WabbajackError("Log in to Nexus to use Mod Manager Download")
        folder = self.request.downloads / ".wabbajack" / hash_bytes(archive.key).hex()
        incoming = folder / (target.name if len(fsencode(archive.name)) > filename_limit(folder) else archive.name)
        def stream_handler(**kwargs):
            path = download_http(kwargs["url"], incoming, size=archive.size,
                                 expected=archive.key, stop=self.control.stop, progress=progress)
            return DownloadResult(success=True, file_path=path, file_name=archive.name,
                                  bytes_downloaded=archive.size, game_domain=kwargs["game_domain"],
                                  mod_id=kwargs["mod_id"], file_id=kwargs["file_id"])
        downloader = NexusDownloader(self.request.api, folder, stream_handler=stream_handler)
        try:
            for attempt in range(1 if link is not None else 2):
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
            path = Path(result.file_path) if result.file_path else None
            if not path or not verify_file(path, archive.key, archive.size, self.control.stop):
                if path and path.is_file():
                    from .paths import auxiliary_path
                    path.rename(auxiliary_path(path, f".invalid-{time.time_ns()}"))
                raise WabbajackError(f"Nexus download failed: {archive.name}: {_safe_error(getattr(result, 'error', ''))}")
            path.replace(target)
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
                    if api is not None:
                        self.request.api = api
                    try:
                        return self._nexus(archive, target, progress, link)
                    except WabbajackError as exc:
                        self.cb.on_log(str(exc))
                        status("Download link failed; use a fresh browser link or Select File.")
                except queue.Empty:
                    pass
                try:
                    selected = self.control.manual_queue.get_nowait()
                    valid = False
                    if selected is not None:
                        status("Checking the selected file's size and hash…")
                        try:
                            valid = verify_file(Path(selected), archive.key, archive.size, self.control.stop)
                        except OSError:
                            pass
                    if valid:
                        from .store import Store
                        if Path(selected).resolve() != target.resolve():
                            Store._copy(Path(selected), target, stop=self.control.stop)
                        return target
                    status(f"That file does not match the required size and hash. Select the exact archive: {archive.name}")
                except queue.Empty:
                    pass
                found = self.cached(archive)
                if found:
                    return found
                self.control.stop.wait(2)
        finally:
            self._manual_lock.release()
        raise InterruptedError("Installation stopped")
