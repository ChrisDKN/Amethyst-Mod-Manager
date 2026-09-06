from __future__ import annotations

import gzip
import json
import queue
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads import bandwidth
from .games import nexus_domain
from .hashes import canonical_hash, hash_bytes, file_hash, verify_file
from .paths import WabbajackError

_active_lock = threading.Lock()
_active = {}


def _safe_error(error):
    return re.sub(r'https?://[^\s]+', lambda m: urlunparse(urlparse(m[0])._replace(query="", fragment="")), str(error))


def route_nxm(link, api=None) -> bool:
    key = (link.game_domain.lower(), int(link.mod_id), int(link.file_id))
    with _active_lock:
        receiver = _active.get(key)
    if receiver is None:
        return False
    receiver.put((link, api))
    return True


def download_http(url: str, target: Path, *, size=0, expected="", headers=None,
                  stop=None, progress=None) -> Path:
    if urlparse(url).scheme not in {"https", "http"}:
        raise WabbajackError("Download URL must use HTTP or HTTPS")
    if expected:
        expected = canonical_hash(expected)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    error = None
    for attempt in range(3):
        if stop is not None and stop.is_set():
            raise InterruptedError("Installation stopped")
        offset = part.stat().st_size if part.exists() else 0
        request_headers = {"Accept-Encoding": "identity", **dict(headers or {})}
        if offset:
            request_headers["Range"] = f"bytes={offset}-"
        try:
            with requests.get(url, headers=request_headers, stream=True, timeout=(20, 60),
                              verify=resolve_ca_bundle() or True) as response:
                if response.status_code == 416:
                    if expected and verify_file(part, expected, size, stop):
                        part.replace(target)
                        return target
                    part.unlink(missing_ok=True)
                    continue
                response.raise_for_status()
                append = offset > 0 and response.status_code == 206
                if append and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                    raise WabbajackError("Server returned an invalid download range")
                if not append:
                    offset = 0
                total = size or offset + int(response.headers.get("Content-Length", 0))
                with part.open("ab" if append else "wb") as output:
                    for chunk in response.iter_content(256 * 1024):
                        if stop is not None and stop.is_set():
                            raise InterruptedError("Installation stopped")
                        bandwidth.throttle(len(chunk), stop)
                        output.write(chunk)
                        offset += len(chunk)
                        if (size or expected) and offset > size:
                            raise WabbajackError("Download exceeds declared size")
                        if progress:
                            progress(offset, total)
            if (size or expected) and part.stat().st_size != size:
                raise WabbajackError("Download has an incorrect size")
            if expected and file_hash(part, stop) != expected:
                part.replace(part.with_name(part.name + f".invalid-{time.time_ns()}"))
                raise WabbajackError("Download checksum does not match the modlist")
            part.replace(target)
            return target
        except (requests.RequestException, WabbajackError) as exc:
            error = exc
            if stop is not None:
                stop.wait(min(attempt + 1, 3))
    raise WabbajackError(f"Download failed: {target.name}: {_safe_error(error)}")


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
    chunks = target.with_name(target.name + ".chunks")
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
    output = target.with_name(target.name + ".part")
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
        return (archive.key in self.report.cached or archive.kind in {"Http", "HTTP", "WabbajackCDN", "GameFileSource"}
                or (archive.kind == "Nexus" and self.request.premium))

    def __call__(self, archive):
        if self.control.stop.is_set():
            raise InterruptedError("Installation stopped")
        if archive.key in self.report.game_files:
            path = self.report.game_files[archive.key]
            if verify_file(path, archive.key, archive.size, self.control.stop):
                return path
            raise WabbajackError(f"Game file changed after preflight: {path.name}")
        if archive.kind == "GameFileSource":
            raise WabbajackError(f"A reusable output changed after preflight. Check requirements again to verify the source for {archive.name}.")
        cached = self.cached(archive)
        if cached:
            self._release_nxm(archive)
            return cached
        row = self.ids[archive.key]
        self.cb.on_dl_mod_start(row, archive.name, archive.size)

        def progress(cur, total):
            self.cb.on_dl_mod_update(row, cur, total)
            with self._lock:
                self._progress[row] = cur
                current = sum(self._progress.values())
            self.cb.on_agg_download(current, self.report.download_bytes,
                                   current / max(time.monotonic() - self._started, 0.1) / 1024 ** 2)

        target = self.request.downloads / (hash_bytes(archive.key).hex() + "-" + archive.name)
        try:
            if archive.kind in {"Http", "HTTP"}:
                headers = {}
                for header in archive.state.get("Headers", []):
                    key, sep, value = header.partition(":")
                    if sep:
                        headers[key.strip()] = value.strip()
                path = download_http(archive.state["Url"], target, size=archive.size,
                                     expected=archive.key, headers=headers,
                                     stop=self.control.stop, progress=progress)
            elif archive.kind == "WabbajackCDN":
                path = download_cdn(archive.state["Url"], target, archive.size, archive.key,
                                    self.control.stop, progress)
            elif archive.kind == "Nexus" and self.request.premium:
                path = self._nexus(archive, target, progress)
            else:
                path = self._manual(archive, target, progress)
            if not verify_file(path, archive.key, archive.size, self.control.stop):
                raise WabbajackError(f"Archive failed verification: {archive.name}")
            return path
        finally:
            self._release_nxm(archive)
            self.cb.on_dl_mod_finish(row)

    def _nexus(self, archive, target, progress, link=None):
        from Nexus.nexus_download import NexusDownloader, DownloadResult
        if self.request.api is None:
            raise WabbajackError("Log in to Nexus to use Mod Manager Download")
        folder = self.request.downloads / ".wabbajack" / hash_bytes(archive.key).hex()
        def stream_handler(**kwargs):
            path = download_http(kwargs["url"], folder / archive.name, size=archive.size,
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
                        cancel=self.control.stop, known_file_name=archive.name, expected_size_bytes=archive.size)
                if result.file_path or self.control.stop.is_set():
                    break
                self.cb.on_status(f"Refreshing download links for {archive.name}")
            path = Path(result.file_path) if result.file_path else None
            if not path or not verify_file(path, archive.key, archive.size, self.control.stop):
                if path and path.is_file():
                    path.rename(path.with_name(path.name + f".invalid-{time.time_ns()}"))
                raise WabbajackError(f"Nexus download failed: {archive.name}: {_safe_error(getattr(result, 'error', ''))}")
            path.replace(target)
            return target
        finally:
            downloader.close_worker_session()

    def _manual(self, archive, target, progress):
        with self._manual_lock:
            if archive.kind == "Nexus":
                domain, mod, file = self.nexus_key(archive)
                url = f"https://www.nexusmods.com/{domain}/mods/{mod}?tab=files&file_id={file}"
                inbox = self._nxm[(domain, mod, file)]
            else:
                url = next((archive.state.get(key) for key in ("Url", "URL", "FullURL", "IPS4Url") if archive.state.get(key)), "")
                if not url and archive.kind == "GoogleDrive" and archive.state.get("Id"):
                    url = "https://drive.google.com/file/d/" + str(archive.state["Id"]) + "/view"
                inbox = queue.Queue()
            if archive.state.get("Prompt"):
                self.cb.on_log(str(archive.state["Prompt"]))
            self.cb.on_manual_mod({"idx": self.ids[archive.key], "total": len(self.ids),
                "name": archive.name, "file_name": archive.name, "size": archive.size,
                "url": url, "optional": False, "upcoming": [], "required_strict": True})
            while not self.control.stop.is_set():
                try:
                    link, api = inbox.get_nowait()
                    if api is not None:
                        self.request.api = api
                    try:
                        return self._nexus(archive, target, progress, link)
                    except WabbajackError as exc:
                        self.cb.on_log(str(exc))
                        self.cb.on_status("Download link failed; use a fresh browser link or Select File.")
                except queue.Empty:
                    pass
                try:
                    selected = self.control.manual_queue.get_nowait()
                    if selected is not None and verify_file(Path(selected), archive.key, archive.size, self.control.stop):
                        from .store import Store
                        if Path(selected).resolve() != target.resolve():
                            Store._copy(Path(selected), target, stop=self.control.stop)
                        return target
                    self.cb.on_status(f"Select the exact required archive: {archive.name}")
                except queue.Empty:
                    pass
                found = self.cached(archive)
                if found:
                    return found
                self.control.stop.wait(2)
        raise InterruptedError("Installation stopped")
