"""
thunderstore_download.py
Download Thunderstore package zips.

Far simpler than the Nexus equivalent: package downloads need no API key, no
OAuth token and no one-time expiring key. ``/package/download/{ns}/{name}/
{version}/`` 302s straight to the CDN for anyone, so there is no premium/free
split and no manual-download fallback to orchestrate.

The API exposes no checksum of any kind (``PackageVersion`` has no hash field),
so ``file_size`` is the only integrity check available - it is verified here
when the caller supplies an expected size.
"""

from __future__ import annotations

import math
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from Utils.app_log import app_log

# Cloudflare 403s the default Python-urllib User-Agent, so an explicit one is
# mandatory for every Thunderstore request - not merely good manners.
USER_AGENT = "Amethyst-Mod-Manager"

_CHUNK = 256 * 1024
_TIMEOUT = 60
_API_INTERVAL = 0.5
_API_RETRIES = 3
_api_lock = threading.Lock()
_api_next_request: dict[str, float] = {}


@dataclass
class ThunderstoreDownloadResult:
    """Outcome of a package download."""

    success: bool = False
    file_path: Optional[Path] = None
    file_name: str = ""
    error: str = ""
    cancelled: bool = False   # stopped by the caller's cancel token, not a failure
    namespace: str = ""
    name: str = ""
    version: str = ""


def build_request(url: str) -> urllib.request.Request:
    """Build a Request carrying the User-Agent Cloudflare requires."""
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def _wait_for_api(host: str) -> None:
    while True:
        with _api_lock:
            now = time.monotonic()
            delay = _api_next_request.get(host, 0) - now
            if delay <= 0:
                _api_next_request[host] = now + _API_INTERVAL
                return
        time.sleep(delay)


def _retry_delay(value: str | None, attempt: int) -> float:
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            delay = 5 * (2 ** attempt)
    return max(_API_INTERVAL, delay) if math.isfinite(delay) else 5 * (2 ** attempt)


def fetch_json(url: str, timeout: int = 30):
    """GET JSON with shared pacing and bounded rate-limit retries."""
    import json
    host = urlsplit(url).netloc.lower()
    for attempt in range(_API_RETRIES + 1):
        _wait_for_api(host)
        try:
            with urllib.request.urlopen(build_request(url), timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                delay = _retry_delay((exc.headers or {}).get("Retry-After"), attempt)
                with _api_lock:
                    _api_next_request[host] = max(
                        _api_next_request.get(host, 0), time.monotonic() + delay)
                if attempt < _API_RETRIES:
                    app_log(f"thunderstore: rate limited; retrying in {delay:.1f}s")
                    exc.close()
                    continue
            app_log(f"thunderstore: fetch failed for {url}: {exc}")
            exc.close()
            return None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            app_log(f"thunderstore: fetch failed for {url}: {exc}")
            return None


def fetch_version_info(link) -> dict | None:
    """Fetch cyberstorm version detail for a parsed ror2mm link (best-effort)."""
    return fetch_json(link.version_api_url)


def resolve_communities(link) -> list[str]:
    """Return the community slugs a package is listed under.

    The ror2mm:// URI carries no community, so this is the only way to learn
    which game(s) a package belongs to. A package can be listed under several
    (BepInExPack appears under both Subnautica communities), so callers must
    treat the result as a set to test membership against - not a single answer.
    """
    data = fetch_json(link.api_url)
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for entry in data.get("community_listings") or []:
        slug = (entry or {}).get("community")
        if slug and slug not in out:
            out.append(slug)
    return out


def download_package(link, dest_dir: Path,
                     expected_size: int = 0,
                     progress_cb: Optional[Callable[[int, int], None]] = None,
                     cancel=None,
                     operation_locked: bool = False,
                     verify_archive: Optional[Callable[[Path], None]] = None,
                     ) -> ThunderstoreDownloadResult:
    from contextlib import nullcontext
    from Utils.operations import OperationCancelled, acquire_resource_operation

    destination = Path(dest_dir)
    filename = f"{link.full_name}.zip"
    if any(value in filename for value in ("/", "\\", "\0", "\r", "\n")):
        return ThunderstoreDownloadResult(error="unsafe Thunderstore archive filename")
    final = destination / filename
    try:
        with (nullcontext() if operation_locked else acquire_resource_operation(
                f"thunderstore:{final.resolve()}", "Thunderstore download", cancel=cancel)):
            if final.is_symlink() or final.with_suffix(final.suffix + ".part").is_symlink():
                return ThunderstoreDownloadResult(error="Thunderstore download path is a symbolic link")
            return _download_package_unlocked(link, destination, expected_size, progress_cb, cancel, verify_archive)
    except OperationCancelled:
        return ThunderstoreDownloadResult(error="cancelled", cancelled=True)


def _download_package_unlocked(link, dest_dir: Path,
                               expected_size: int = 0, progress_cb=None, cancel=None,
                               verify_archive=None,
                               ) -> ThunderstoreDownloadResult:
    """
    Download the package zip for a parsed :class:`Ror2mmLink` into *dest_dir*.

    The file is named ``{namespace}-{name}-{version}.zip`` to match what
    Thunderstore itself serves. *progress_cb* receives ``(done, total)`` byte
    counts; *total* is 0 when the server sends no Content-Length.

    *cancel* is an ``threading.Event``-like object checked between chunks, so a
    cancelled profile import stops inside a large package instead of only at
    the next one. The partial ``.part`` file is removed on the way out.
    """
    result = ThunderstoreDownloadResult(
        namespace=link.namespace, name=link.name, version=link.version)

    from Utils.downloads.control import wait_if_paused
    if wait_if_paused(cancel):
        result.error = "cancelled"
        result.cancelled = True
        return result

    file_name = f"{link.full_name}.zip"
    result.file_name = file_name
    dest_dir = Path(dest_dir)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.error = f"cannot create download folder: {exc}"
        return result

    final_path = dest_dir / file_name
    # Download to a .part sidecar so an interrupted transfer can never be
    # mistaken for a complete archive by the Downloads tab.
    part_path = dest_dir / f"{file_name}.part"

    app_log(f"thunderstore: downloading {link.full_name} → {final_path}")
    try:
        with urllib.request.urlopen(build_request(link.download_url),
                                    timeout=_TIMEOUT) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            if progress_cb:
                progress_cb(0, total)
            with open(part_path, "wb") as fh:
                from Utils.downloads import bandwidth
                from Utils.downloads.resources import current_resources
                resources = current_resources()
                while True:
                    if wait_if_paused(cancel):
                        part_path.unlink(missing_ok=True)
                        result.error = "cancelled"
                        result.cancelled = True
                        app_log(f"thunderstore: cancelled {link.full_name}")
                        return result
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    if resources is not None:
                        resources.throttle_download(len(chunk), cancel)
                        if wait_if_paused(cancel):
                            part_path.unlink(missing_ok=True)
                            result.error = "cancelled"
                            result.cancelled = True
                            return result
                        resources.write_download(fh, chunk)
                    else:
                        fh.write(chunk)
                    done += len(chunk)
                    bandwidth.throttle(len(chunk), cancel)
                    if progress_cb:
                        progress_cb(done, total)
    except urllib.error.HTTPError as exc:
        part_path.unlink(missing_ok=True)
        result.error = f"HTTP {exc.code} fetching {link.full_name}"
        return result
    except (urllib.error.URLError, OSError) as exc:
        part_path.unlink(missing_ok=True)
        result.error = str(exc)
        return result

    # file_size is the ONLY integrity check Thunderstore offers - no hashes.
    if expected_size:
        actual = part_path.stat().st_size
        if actual != expected_size:
            part_path.unlink(missing_ok=True)
            result.error = (f"size mismatch for {link.full_name}: "
                            f"got {actual} bytes, expected {expected_size}")
            app_log(f"thunderstore: {result.error}")
            return result

    if verify_archive is not None:
        try:
            verify_archive(part_path)
        except Exception as exc:
            part_path.unlink(missing_ok=True)
            result.error = str(exc)
            return result

    if wait_if_paused(cancel):
        part_path.unlink(missing_ok=True)
        result.error = "cancelled"
        result.cancelled = True
        return result

    try:
        part_path.replace(final_path)
    except OSError as exc:
        part_path.unlink(missing_ok=True)
        result.error = f"could not finalise download: {exc}"
        return result

    result.success = True
    result.file_path = final_path
    app_log(f"thunderstore: downloaded {file_name} "
            f"({final_path.stat().st_size} bytes)")
    return result
