"""Toolkit-neutral helpers for a Nexus collection's manifest (collection.json).

The GraphQL ``get_collection_detail`` call gives the mod list + optional flags but
NOT off-site info or the authoritative load order - those live in the collection
archive's ``collection.json``. Fetching it means downloading + extracting the
(large) ``.7z``, so it is cached per game at
``<download-cache>/<slug>_rev<rev>.7z`` and read from cache when present.

Extracted from the Tk ``gui/collections_dialog.py`` (the cache-read-or-fetch block
and the off-site loop) so the Qt collection panel + the reset-load-order menu can
reuse it. Pure I/O - no GUI imports.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from Utils.config_paths import get_download_cache_dir_for_game


def parse_collection_url(url: str) -> "tuple[str, str, int | None]":
    """Extract ``(slug, game_domain, revision_number)`` from a Nexus collection URL,
    or ``('', '', None)`` if it doesn't match. Ported from the Tk
    ``collections_dialog._parse_collection_url``. Handles:
      …nexusmods.com/skyrimspecialedition/collections/x2ezso
      …nexusmods.com/games/skyrimspecialedition/collections/x2ezso
      next.nexusmods.com/skyrimspecialedition/collections/x2ezso
      …nexusmods.com/games/stardewvalley/collections/tckf0m/revisions/97
    """
    m = re.search(
        r'nexusmods\.com/(?:games/)?([^/?#]+)/collections/([A-Za-z0-9_\-]+)'
        r'(?:/revisions/(\d+))?',
        url or "",
    )
    if m:
        rev = int(m.group(3)) if m.group(3) else None
        return m.group(2), m.group(1), rev
    return "", "", None


def fmt_size(n_bytes: int) -> str:
    """Human-readable file size (ported from collections_dialog._fmt_size)."""
    n_bytes = int(n_bytes or 0)
    if n_bytes <= 0:
        return "-"
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n_bytes >= threshold:
            return f"{n_bytes / threshold:.1f} {unit}"
    return f"{n_bytes} B"


def _read_manifest_from_cache(cache_path: Path, log_fn=None) -> dict:
    """Extract ``collection.json`` from a cached collection ``.7z`` archive.
    Returns {} if the archive is missing/unreadable or has no manifest."""
    log = log_fn or (lambda _m: None)
    if not cache_path.is_file():
        return {}
    try:
        import tempfile
        import py7zr
        with tempfile.TemporaryDirectory() as td:
            with py7zr.SevenZipFile(str(cache_path), mode="r") as arc:
                names = arc.getnames()
                target = next(
                    (n for n in names if n.lstrip("/") == "collection.json"), None)
                if target is None:
                    return {}
                arc.extract(path=td, targets=[target])
                cj_path = Path(td) / target.lstrip("/")
                if cj_path.is_file():
                    cj = json.loads(cj_path.read_text(encoding="utf-8"))
                    log(f"Collection: loaded {cache_path.name} from cache")
                    return cj if isinstance(cj, dict) else {}
    except Exception as exc:
        log(f"Collection: cached archive read failed ({exc}) - re-downloading")
    return {}


def load_collection_manifest(api, game_name: str, slug: str,
                             revision: "int | None", download_link_path: str,
                             log_fn=None) -> dict:
    """Return the collection's ``collection.json`` as a dict (empty on failure).

    Reads from the per-game download cache first (``<slug>_rev<rev>.7z``); on a
    miss, downloads via ``api.get_collection_archive_json`` and (when the revision
    is known) keeps the archive in the cache for next time. Never raises.
    """
    log = log_fn or (lambda _m: None)
    if not download_link_path:
        return {}
    slug = slug or "collection"
    cache_dir = get_download_cache_dir_for_game(game_name or "")

    if revision is not None:
        cj = _read_manifest_from_cache(
            cache_dir / f"{slug}_rev{int(revision)}.7z", log_fn=log)
        if cj:
            return cj

    try:
        if revision is None:
            return api.get_collection_archive_json(download_link_path) or {}
        keep = str(cache_dir / f"{slug}_rev{int(revision)}.7z")
        cj = api.get_collection_archive_json(
            download_link_path, keep_archive_at=keep) or {}
        if cj:
            log(f"Collection: archive saved to {keep}")
        return cj
    except Exception as exc:
        log(f"Collection: could not fetch collection.json: {exc}")
        return {}


def extract_offsite_mods(manifest: dict) -> "list[tuple[str, str]]":
    """Return ``[(mod_name, url), …]`` for off-site mods (source.type browse/direct)
    from a collection manifest. Bundled/Nexus entries are skipped."""
    offsite: list[tuple[str, str]] = []
    for m in (manifest or {}).get("mods", []):
        src = m.get("source") or {}
        if (src.get("type") or "").lower() in ("browse", "direct"):
            url = src.get("url") or src.get("fileUrl") or ""
            if url:
                offsite.append((m.get("name") or "", url))
    return offsite
