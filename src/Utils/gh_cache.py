"""
gh_cache.py
ETag-aware GitHub fetcher with per-URL throttling.

GitHub's unauthenticated REST API allows 60 requests/hour per IP. The mod
manager can exceed this in a few back-to-back launches because every startup
fetches the custom handler list, every handler file, the plugin list, every
plugin file, plus release metadata.

This module provides two reductions:

1. Conditional requests via ETag / If-None-Match. A 304 response is free
   (it does not consume rate-limit quota) and lets us reuse the on-disk copy.
2. A min-interval throttle keyed on URL — if the last successful fetch for a
   URL happened less than ``min_interval`` seconds ago we skip the request
   entirely and return the cached body.

Cache lives under ``~/.config/AmethystModManager/gh_cache/``:
    <sha1(url)>.meta.json  -> {"etag": "...", "fetched_at": 1713600000, "url": "..."}
    <sha1(url)>.body       -> the raw response bytes

Callers should tolerate the returned body being ``None`` (network failure and
no cached copy) and should not call any destructive fallback when a 304 is
converted into a cache hit.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from Utils.config_paths import get_config_dir


_USER_AGENT = "Amethyst-Mod-Manager"


def _cache_dir() -> Path:
    d = get_config_dir() / "gh_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def clear_if_version_changed(current_version: str) -> bool:
    """Wipe the cache directory if the stored app version differs.

    Called once on startup so users on a freshly-updated build always re-fetch
    handlers/plugins/release metadata instead of waiting for the per-URL
    throttle window to elapse.

    Returns True if the cache was cleared.
    """
    base = _cache_dir()
    stamp = base / "version.txt"
    try:
        prev = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else ""
    except Exception:
        prev = ""
    if prev == current_version:
        return False
    for child in base.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except Exception:
            pass
    try:
        stamp.write_text(current_version, encoding="utf-8")
    except Exception:
        pass
    return True


def _key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def _paths(url: str) -> tuple[Path, Path]:
    k = _key(url)
    base = _cache_dir()
    return base / f"{k}.meta.json", base / f"{k}.body"


def _read_meta(meta_path: Path) -> dict:
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(meta_path: Path, meta: dict) -> None:
    try:
        tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        tmp.replace(meta_path)
    except Exception:
        pass


def fetch(
    url: str,
    *,
    accept: str = "application/vnd.github.v3+json",
    timeout: float = 15.0,
    min_interval: float = 0.0,
    force: bool = False,
) -> Optional[bytes]:
    """Fetch *url* with ETag caching and optional throttling.

    Returns the response body as bytes on success (fresh fetch OR cached copy
    served because of 304 / throttle), or ``None`` if the request failed and
    no cached copy exists.

    - ``min_interval`` seconds: if the last successful fetch happened more
      recently than this and a cached body exists, skip the network entirely.
      Pass 0 to disable the throttle.
    - ``force=True`` bypasses the throttle (but still sends If-None-Match
      so a 304 remains free).
    """
    meta_path, body_path = _paths(url)
    meta = _read_meta(meta_path)
    cached_body: Optional[bytes] = None
    if body_path.is_file():
        try:
            cached_body = body_path.read_bytes()
        except Exception:
            cached_body = None

    now = time.time()
    if (
        not force
        and min_interval > 0
        and cached_body is not None
        and isinstance(meta.get("fetched_at"), (int, float))
        and (now - float(meta["fetched_at"])) < min_interval
    ):
        return cached_body

    headers = {
        "Accept": accept,
        "User-Agent": _USER_AGENT,
    }
    etag = meta.get("etag")
    if etag and cached_body is not None:
        headers["If-None-Match"] = etag

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            new_etag = resp.headers.get("ETag")
            try:
                body_path.write_bytes(body)
            except Exception:
                pass
            _write_meta(meta_path, {
                "url": url,
                "etag": new_etag or "",
                "fetched_at": now,
            })
            return body
    except urllib.error.HTTPError as e:
        if e.code == 304 and cached_body is not None:
            meta["fetched_at"] = now
            _write_meta(meta_path, meta)
            return cached_body
        return cached_body
    except Exception:
        return cached_body


def fetch_text(
    url: str,
    *,
    accept: str = "application/vnd.github.v3+json",
    timeout: float = 15.0,
    min_interval: float = 0.0,
    force: bool = False,
    encoding: str = "utf-8",
) -> Optional[str]:
    """Convenience wrapper around :func:`fetch` that decodes the body."""
    data = fetch(
        url,
        accept=accept,
        timeout=timeout,
        min_interval=min_interval,
        force=force,
    )
    if data is None:
        return None
    return data.decode(encoding, errors="replace")
