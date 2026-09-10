from __future__ import annotations

import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads import bandwidth
from .hashes import canonical_hash, file_hash, verify_file
from .paths import WabbajackError
from .diagnostics import emit, emit_exception, url_host
from .verification import file_stamp, verified_replace


def safe_error(error):
    return re.sub(r'https?://[^\s]+', lambda m: urlunparse(urlparse(m[0])._replace(
        netloc=urlparse(m[0]).hostname or "", path="", params="", query="", fragment="")), str(error))


class DownloadUnavailable(WabbajackError):
    pass


_connections = ContextVar("wabbajack_connections", default=None)


@contextmanager
def connection_scope(limit):
    token = _connections.set(limit)
    try:
        yield
    finally:
        _connections.reset(token)


@contextmanager
def connection_slot(stop=None):
    limit = _connections.get()
    if limit is not None:
        while not limit.acquire(timeout=0.1):
            if stop is not None and stop.is_set():
                raise InterruptedError("Download stopped")
    try:
        if stop is not None and stop.is_set():
            raise InterruptedError("Download stopped")
        yield
    finally:
        if limit is not None:
            limit.release()


@contextmanager
def limited_response(operation, stop=None):
    with connection_slot(stop), operation() as response:
        yield response


def download_http(url: str, target: Path, *, size=0, expected="", headers=None,
                  stop=None, progress=None, open_response=None, transform=None,
                  validate=None, log=None) -> Path:
    started = time.monotonic()
    if stop is not None and stop.is_set():
        raise InterruptedError("Installation stopped")
    if urlparse(url).scheme not in {"https", "http"}:
        raise WabbajackError("Download URL must use HTTP or HTTPS")
    if expected:
        expected = canonical_hash(expected)
    emit(log, "http.started", host=url_host(url), target=target, expected_size=size,
         expected_hash=expected, header_names=sorted((headers or {}).keys()),
         custom_response=bool(open_response), transformed=bool(transform),
         validated=bool(validate))
    target.parent.mkdir(parents=True, exist_ok=True)
    from .paths import auxiliary_path
    part = auxiliary_path(target, ".part")
    if expected and part.is_file() and part.stat().st_size >= size:
        stamp = file_stamp(part)
        if verify_file(part, expected, size, stop):
            if validate:
                validate(part)
            verified_replace(part, target, digest=expected, stamp=stamp, stop=stop)
            emit(log, "http.partial_verified", target=target, bytes=size,
                 elapsed_seconds=round(time.monotonic() - started, 3))
            return target
        invalid = auxiliary_path(part, f".invalid-{time.time_ns()}")
        part.replace(invalid)
        emit(log, "http.partial_rejected", target=target, preserved_as=invalid)
    error = None
    for attempt in range(3):
        if stop is not None and stop.is_set():
            raise InterruptedError("Installation stopped")
        offset = part.stat().st_size if part.exists() else 0
        request_headers = {"Accept-Encoding": "identity", **dict(headers or {})}
        if offset:
            request_headers["Range"] = f"bytes={offset}-"
        try:
            emit(log, "http.attempt", host=url_host(url), target=target,
                 attempt=attempt + 1, resume_offset=offset)
            response = limited_response(lambda: (open_response(request_headers) if open_response else
                        requests.get(url, headers=request_headers, stream=True, timeout=(20, 60),
                                     verify=resolve_ca_bundle() or True)), stop)
            with response as response:
                emit(log, "http.response", target=target, attempt=attempt + 1,
                     status=response.status_code,
                     final_host=url_host(getattr(response, "url", url)),
                     content_type=response.headers.get("Content-Type", ""),
                     content_length=response.headers.get("Content-Length", ""),
                     content_range=response.headers.get("Content-Range", ""),
                     accept_ranges=response.headers.get("Accept-Ranges", ""),
                     encoding=response.headers.get("Content-Encoding", ""))
                if response.status_code == 416:
                    stamp = file_stamp(part) if part.is_file() else None
                    if expected and verify_file(part, expected, size, stop):
                        if validate:
                            validate(part)
                        verified_replace(part, target, digest=expected, stamp=stamp, stop=stop)
                        emit(log, "http.range_complete", target=target, bytes=size)
                        return target
                    part.unlink(missing_ok=True)
                    emit(log, "http.range_reset", target=target,
                         reason="server returned 416 and partial verification failed")
                    continue
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if expected and content_type in {"text/html", "application/xhtml+xml"} and target.suffix.lower() not in {".html", ".htm"}:
                    raise DownloadUnavailable("The host returned a web page instead of the required file. Open the download page to check for sign-in, confirmation or download limits.")
                append = offset > 0 and response.status_code == 206
                if response.status_code == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", response.headers.get("Content-Range", ""))
                    if (not match or int(match[1]) != offset or int(match[2]) < offset
                            or ((size or expected) and (int(match[2]) >= size or match[3] != str(size)))):
                        raise WabbajackError("Server returned an invalid download range")
                if not append:
                    offset = 0
                try:
                    total = size or offset + int(response.headers.get("Content-Length", 0))
                except ValueError as exc:
                    raise WabbajackError("Server returned an invalid download size") from exc
                decode = transform(offset) if transform else None
                with part.open("ab" if append else "wb") as output:
                    for chunk in response.iter_content(256 * 1024):
                        if stop is not None and stop.is_set():
                            raise InterruptedError("Installation stopped")
                        bandwidth.throttle(len(chunk), stop)
                        if (size or expected) and offset + len(chunk) > size:
                            raise WabbajackError("Download exceeds declared size")
                        output.write(decode(chunk) if decode else chunk)
                        offset += len(chunk)
                        if progress:
                            progress(offset, total)
            stamp = file_stamp(part)
            if (size or expected) and stamp[2] != size:
                raise WabbajackError("Download has an incorrect size")
            if expected and file_hash(part, stop) != expected:
                part.replace(auxiliary_path(part, f".invalid-{time.time_ns()}"))
                raise WabbajackError("Download checksum does not match the modlist")
            if validate:
                try:
                    validate(part)
                except WabbajackError:
                    part.replace(auxiliary_path(part, f".invalid-{time.time_ns()}"))
                    raise
            if stop is not None and stop.is_set():
                raise InterruptedError("Installation stopped")
            verified_replace(part, target, digest=expected or None,
                             stamp=stamp, stop=stop)
            emit(log, "http.completed", target=target, bytes=target.stat().st_size,
                 attempts=attempt + 1, elapsed_seconds=round(time.monotonic() - started, 3))
            return target
        except DownloadUnavailable as exc:
            emit(log, "http.manual_required", target=target, attempt=attempt + 1,
                 exception_type=type(exc).__name__, exception=str(exc))
            raise
        except (requests.RequestException, WabbajackError) as exc:
            error = exc
            try:
                partial_bytes = part.stat().st_size if part.is_file() else 0
            except OSError:
                partial_bytes = None
            emit_exception(log, "http.attempt_failed", exc, target=target,
                           attempt=attempt + 1, partial_bytes=partial_bytes)
            if stop is not None and attempt < 2:
                stop.wait(min(attempt + 1, 3))
    if stop is not None and stop.is_set():
        raise InterruptedError("Installation stopped")
    failure = WabbajackError(f"Download failed: {target.name}: {safe_error(error)}")
    emit(log, "http.failed", target=target, exception=str(failure),
         elapsed_seconds=round(time.monotonic() - started, 3))
    raise failure
