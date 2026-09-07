from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from Utils.atomic_write import write_atomic_text
from Utils.ca_bundle import resolve_ca_bundle
from .paths import WabbajackError
from .diagnostics import emit, emit_exception, url_host

REGISTRY = "https://raw.githubusercontent.com/wabbajack-tools/mod-lists/master/repositories.json"
FEATURED = "https://raw.githubusercontent.com/wabbajack-tools/mod-lists/master/featured_lists.json"
_FEED_TOKENS = re.compile(r'"(?:[^"\\]|\\.)*"|//[^\r\n]*|/\*.*?\*/|[^\s]', re.DOTALL)


@dataclass
class GalleryEntry:
    id: str
    title: str
    author: str
    game: str
    version: str
    description: str = ""
    image: str = ""
    readme: str = ""
    community: str = ""
    download: str = ""
    nsfw: bool = False
    featured: bool = False
    unavailable: bool = False
    tags: list[str] = field(default_factory=list)
    download_size: int = 0
    install_size: int = 0
    package_size: int = 0
    package_hash: str = ""


@dataclass
class GalleryResult:
    entries: list[GalleryEntry]
    warnings: list[str]
    cached: bool


def cache_root():
    from Utils.config_paths import get_wabbajack_cache_dir
    root = get_wabbajack_cache_dir() / "gallery"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _feed_json(data, log=None):
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        emit(log, "gallery.feed.tolerant_json", line=exc.lineno, column=exc.colno,
             reason=exc.msg)
    text = data.decode(json.detect_encoding(data)) if isinstance(data, bytes) else data
    output = list(text)
    previous, comma = "", None
    for match in _FEED_TOKENS.finditer(text):
        value = match[0]
        if value.startswith(("//", "/*")):
            output[match.start():match.end()] = [c if c in "\r\n" else " " for c in value]
            continue
        if value == ",":
            comma = match.start() if previous and previous not in {"[", "{", ",", ":"} else None
        else:
            if comma is not None and value in {"}", "]"}:
                output[comma] = " "
            comma = None
        previous = value
    return json.loads("".join(output))


def _fetch(url, root, refresh, cached_only=False, stop=None, log=None, label=""):
    started = time.monotonic()
    emit(log, "gallery.feed.started", label=label, host=url_host(url),
         refresh=refresh, cached_only=cached_only)
    if stop is not None and stop.is_set():
        raise InterruptedError("Gallery loading stopped")
    if urlparse(url).scheme != "https":
        raise WabbajackError("Gallery feeds must use HTTPS")
    path = root / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    cached = None
    if path.is_file():
        try:
            cached = json.loads(path.read_text())
            age = max(0, time.time() - cached["time"])
            if cached_only or not refresh and age < 3600:
                emit(log, "gallery.feed.cache_hit", label=label, path=path,
                     age_seconds=round(age, 1))
                return cached["data"], True
            emit(log, "gallery.feed.cache_stale", label=label, path=path,
                 age_seconds=round(age, 1))
        except (ValueError, TypeError, KeyError) as exc:
            emit(log, "gallery.feed.cache_invalid", label=label, path=path,
                 exception_type=type(exc).__name__, exception=str(exc))
            cached = None
    if cached_only:
        raise WabbajackError("Gallery feed is not cached")
    try:
        response = requests.get(url, timeout=(10, 30), verify=resolve_ca_bundle() or True)
        emit(log, "gallery.feed.response", label=label, status=response.status_code,
             final_host=url_host(getattr(response, "url", url)), bytes=len(response.content),
             elapsed_seconds=round(time.monotonic() - started, 3))
        response.raise_for_status()
        data = _feed_json(response.content, log)
        write_atomic_text(path, json.dumps({"time": time.time(), "data": data}))
        emit(log, "gallery.feed.cached", label=label, path=path)
        return data, False
    except (requests.RequestException, ValueError) as exc:
        if cached is not None:
            emit_exception(log, "gallery.feed.network_fallback", exc,
                           label=label, path=path)
            return cached["data"], True
        emit_exception(log, "gallery.feed.failed", exc, label=label)
        raise


def load_gallery(*, refresh=False, root=None, cached_only=False, stop=None, log=None):
    root = root or cache_root()
    started = time.monotonic()
    emit(log, "gallery.started", root=root, refresh=refresh, cached_only=cached_only)
    registry, stale = _fetch(REGISTRY, root, refresh, cached_only, stop, log, "registry")
    emit(log, "gallery.registry.loaded", repositories=len(registry), cached=stale)
    warnings, entries = [], {}
    featured = set()
    try:
        data, old = _fetch(FEATURED, root, refresh, cached_only, stop, log, "featured")
        stale |= old
        featured = {str(value).casefold() for value in (data if isinstance(data, list) else data.keys())}
    except InterruptedError:
        raise
    except Exception as exc:
        warnings.append(f"Featured list feed unavailable: {exc}")
        emit_exception(log, "gallery.featured.failed", exc)
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="wabbajack-gallery") as pool:
        futures = {pool.submit(_fetch, url, root, refresh, cached_only, stop, log, name): name
                   for name, url in registry.items()}
        for future in as_completed(futures):
            if stop is not None and stop.is_set():
                for pending in futures:
                    pending.cancel()
                raise InterruptedError("Gallery loading stopped")
            repository = futures[future]
            try:
                rows, old = future.result()
                stale |= old
                if isinstance(rows, dict):
                    rows = [rows]
                added = 0
                for row in rows:
                    links = row.get("links") or {}
                    machine = links.get("machineURL", links.get("machineUrl", ""))
                    if not machine:
                        continue
                    identity = f"{repository}/{machine}"
                    metadata = {k.replace("_", "").lower(): v for k, v in (row.get("download_metadata") or {}).items()}
                    entries[identity] = GalleryEntry(identity, row.get("title", machine), row.get("author", ""),
                        row.get("game", ""), str(row.get("version") or ""), row.get("description", ""),
                        links.get("image", ""), links.get("readme", ""), links.get("discordURL", ""),
                        links.get("download", ""), bool(row.get("nsfw")),
                        identity.casefold() in featured or bool(row.get("official")), bool(row.get("force_down")),
                        list(row.get("tags") or []), int(metadata.get("sizeofarchives", 0)),
                        int(metadata.get("sizeofinstalledfiles", 0)), int(metadata.get("size", 0)),
                        str(metadata.get("hash", "")))
                    added += 1
                emit(log, "gallery.repository.loaded", repository=repository,
                     entries=added, cached=old)
            except Exception as exc:
                warnings.append(f"{repository}: {exc}")
                emit_exception(log, "gallery.repository.failed", exc,
                               repository=repository)
    result = GalleryResult(sorted(entries.values(), key=lambda e: (not e.featured, e.title.casefold())),
                           warnings, stale)
    emit(log, "gallery.completed", entries=len(result.entries), warnings=len(warnings),
         cached=stale, elapsed_seconds=round(time.monotonic() - started, 3))
    return result
