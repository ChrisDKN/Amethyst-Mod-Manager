from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

from Utils.ca_bundle import resolve_ca_bundle
from Utils.downloads import bandwidth
from .hashes import canonical_hash, file_hash, verify_file
from .paths import WabbajackError


def safe_error(error):
    return re.sub(r'https?://[^\s]+', lambda m: urlunparse(urlparse(m[0])._replace(
        netloc=urlparse(m[0]).hostname or "", path="", params="", query="", fragment="")), str(error))


class DownloadUnavailable(WabbajackError):
    pass


def download_http(url: str, target: Path, *, size=0, expected="", headers=None,
                  stop=None, progress=None, open_response=None, transform=None,
                  validate=None) -> Path:
    if stop is not None and stop.is_set():
        raise InterruptedError("Installation stopped")
    if urlparse(url).scheme not in {"https", "http"}:
        raise WabbajackError("Download URL must use HTTP or HTTPS")
    if expected:
        expected = canonical_hash(expected)
    target.parent.mkdir(parents=True, exist_ok=True)
    from .paths import auxiliary_path
    part = auxiliary_path(target, ".part")
    if expected and part.is_file() and part.stat().st_size >= size:
        if verify_file(part, expected, size, stop):
            if validate:
                validate(part)
            part.replace(target)
            return target
        part.replace(auxiliary_path(part, f".invalid-{time.time_ns()}"))
    error = None
    for attempt in range(3):
        if stop is not None and stop.is_set():
            raise InterruptedError("Installation stopped")
        offset = part.stat().st_size if part.exists() else 0
        request_headers = {"Accept-Encoding": "identity", **dict(headers or {})}
        if offset:
            request_headers["Range"] = f"bytes={offset}-"
        try:
            response = (open_response(request_headers) if open_response else
                        requests.get(url, headers=request_headers, stream=True, timeout=(20, 60),
                                     verify=resolve_ca_bundle() or True))
            with response:
                if response.status_code == 416:
                    if expected and verify_file(part, expected, size, stop):
                        if validate:
                            validate(part)
                        part.replace(target)
                        return target
                    part.unlink(missing_ok=True)
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
            if (size or expected) and part.stat().st_size != size:
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
            part.replace(target)
            return target
        except DownloadUnavailable:
            raise
        except (requests.RequestException, WabbajackError) as exc:
            error = exc
            if stop is not None and attempt < 2:
                stop.wait(min(attempt + 1, 3))
    if stop is not None and stop.is_set():
        raise InterruptedError("Installation stopped")
    raise WabbajackError(f"Download failed: {target.name}: {safe_error(error)}")
