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

import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from Utils.app_log import app_log

# Cloudflare 403s the default Python-urllib User-Agent, so an explicit one is
# mandatory for every Thunderstore request - not merely good manners.
USER_AGENT = "Amethyst-Mod-Manager"

_CHUNK = 256 * 1024
_TIMEOUT = 60


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


def fetch_json(url: str, timeout: int = 30):
    """GET *url* and decode JSON, or return None on any failure."""
    import json
    try:
        with urllib.request.urlopen(build_request(url), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
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
                while True:
                    if cancel is not None and cancel.is_set():
                        part_path.unlink(missing_ok=True)
                        result.error = "cancelled"
                        result.cancelled = True
                        app_log(f"thunderstore: cancelled {link.full_name}")
                        return result
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
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
