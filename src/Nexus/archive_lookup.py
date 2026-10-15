from __future__ import annotations

import hashlib
from pathlib import Path

from Nexus.nexus_download import (
    _md5_cache_get, _md5_cache_put, _md5_file_stamp,
)
from Nexus.nexus_meta import normalise_game_domain


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Nexus archive lookup stopped")


def find_archive_pages(archive, domains, api, cancel=None):
    """Return distinct Nexus mod pages matching the archive's MD5."""
    archive = Path(archive)
    _check_cancel(cancel)
    stamp = _md5_file_stamp(archive)
    if stamp is None:
        raise OSError(f"Archive is no longer accessible: {archive.name}")
    digest = _md5_cache_get(archive)
    if not digest:
        hashed = hashlib.md5()
        with archive.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                _check_cancel(cancel)
                hashed.update(chunk)
        digest = hashed.hexdigest()
        if not _md5_cache_put(archive, digest, expected_stamp=stamp):
            raise OSError("Archive changed while computing its MD5")
    if _md5_file_stamp(archive) != stamp:
        raise OSError("Archive changed while computing its MD5")
    pages = {}
    searched = set()
    for raw_domain in domains:
        _check_cancel(cancel)
        domain = normalise_game_domain(raw_domain, preserve_case=True)
        key = normalise_game_domain(domain)
        if not key or key in searched:
            continue
        searched.add(key)
        hits = api.get_file_by_md5(domain, digest)
        if not isinstance(hits, list):
            raise ValueError("Nexus returned an invalid MD5 search response")
        for hit in hits:
            _check_cancel(cancel)
            if not isinstance(hit, dict):
                continue
            mod = hit.get("mod") or {}
            if not isinstance(mod, dict) or mod.get("available") is False:
                continue
            if str(mod.get("status", "")).casefold() in {
                    "deleted", "discarded", "hidden", "removed"}:
                continue
            try:
                mod_id = int(mod.get("mod_id") or 0)
            except (ValueError, TypeError):
                continue
            if mod_id <= 0 or not mod.get("name"):
                continue
            reported_domain = mod.get("domain_name")
            if reported_domain and (not isinstance(reported_domain, str)
                                    or normalise_game_domain(reported_domain) != key):
                continue
            pages[key, mod_id] = {
                "name": str(mod["name"]), "domain": domain, "mod_id": mod_id,
                "url": f"https://www.nexusmods.com/{domain}/mods/{mod_id}",
            }
    _check_cancel(cancel)
    if _md5_file_stamp(archive) != stamp:
        raise OSError("Archive changed during Nexus lookup")
    return sorted(pages.values(), key=lambda page: (
        page["name"].casefold(), page["domain"].casefold(), page["mod_id"]))
