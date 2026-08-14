"""
archive_lookup.py
Case-insensitive random-access lookup of files inside BSA/BA2 archives.

Maps game-data-relative paths to the archive holding them and reads single
members without unpacking (bsa_extract/ba2_extract entry readers). Indexes are
cached per process by path+mtime+size; archives are searched in the order
given and the FIRST match wins, so callers order the list.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

__all__ = ["ArchiveLookup", "find_archives", "index_archive"]

# (path, mtime, size, keep_prefix) -> {rel_lower: (kind, record)}
_INDEX_CACHE: dict[tuple, dict] = {}
_INDEX_CACHE_LOCK = threading.Lock()
_INDEX_BUILD_LOCKS: dict[tuple, threading.Lock] = {}


def _cache_key(path: Path, keep_prefix: str) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size, keep_prefix)


def _index_one(path: Path, keep_prefix: str) -> dict:
    """Index a single archive, honouring the process-wide cache."""
    key = _cache_key(path, keep_prefix)
    if key is None:
        return {}
    with _INDEX_CACHE_LOCK:
        got = _INDEX_CACHE.get(key)
        if got is not None:
            return got
        # A catalogue often indexes the whole archive just before a resolver
        # asks for a few asset subtrees.  Re-filter that already-built map
        # instead of reparsing the same BSA/BA2 TOC under another cache key.
        if keep_prefix:
            full_key = _cache_key(path, "")
            full = _INDEX_CACHE.get(full_key) if full_key is not None else None
            if full is not None:
                got = {rel: rec for rel, rec in full.items()
                       if rel.startswith(keep_prefix)}
                _INDEX_CACHE[key] = got
                return got
        # NIF catalogue, texture-provider and preview workers may all touch the
        # same archive together.  One per-key lock prevents duplicate TOC
        # parsing without serialising unrelated archives.
        build_lock = _INDEX_BUILD_LOCKS.setdefault(key, threading.Lock())

    with build_lock:
        with _INDEX_CACHE_LOCK:
            got = _INDEX_CACHE.get(key)
            if got is not None:
                return got

        out: dict = {}
        ext = path.suffix.lower()
        try:
            if ext == ".ba2":
                from Utils.ba2_extract import index_ba2
                for rel, rec in index_ba2(path).items():
                    if not keep_prefix or rel.startswith(keep_prefix):
                        out[rel] = ("ba2", rec)
            else:
                from Utils.bsa_extract import index_bsa
                info, entries = index_bsa(path)
                for rel, entry in entries.items():
                    if not keep_prefix or rel.startswith(keep_prefix):
                        out[rel] = ("bsa", (info, entry))
        except Exception:                                # noqa: BLE001
            # A broken or unsupported archive must not sink the whole lookup.
            out = {}

        with _INDEX_CACHE_LOCK:
            _INDEX_CACHE[key] = out
            _INDEX_BUILD_LOCKS.pop(key, None)
        return out


def index_archive(path: Path, keep_prefix: str = "") -> dict:
    """{rel_lower: (kind, record)} for one archive - TOC only, cached by
    (path, mtime, size, keep_prefix); ArchiveLookup collapses to the first
    holder, this exposes each archive whole."""
    return _index_one(Path(path), keep_prefix)


def find_archives(roots) -> list[Path]:
    """Collect .bsa/.ba2 sitting directly in each of *roots*, in order."""
    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        try:
            with os.scandir(root) as it:
                names = sorted(e.name for e in it if e.is_file())
        except OSError:
            continue
        for name in names:
            if name.lower().endswith((".bsa", ".ba2")):
                p = root / name
                k = str(p).lower()
                if k not in seen:
                    seen.add(k)
                    found.append(p)
    return found


def normalise(rel: str) -> str:
    """Normalise a NIF-style texture path to an archive-internal path."""
    s = rel.replace("\\", "/").lower().strip()
    while s.startswith("/"):
        s = s[1:]
    if s.startswith("data/"):
        s = s[5:]
    return s


class ArchiveLookup:
    """Finds files across a list of archives; first archive to match wins."""

    def __init__(self, archives, keep_prefix: str = ""):
        self._archives = [Path(a) for a in archives]
        self._keep_prefix = keep_prefix
        self._map: dict[str, tuple[Path, str, object]] | None = None

    def _build(self) -> dict:
        if self._map is not None:
            return self._map
        merged: dict[str, tuple[Path, str, object]] = {}
        for archive in self._archives:
            for rel, (kind, rec) in _index_one(archive, self._keep_prefix).items():
                merged.setdefault(rel, (archive, kind, rec))
        self._map = merged
        return merged

    def __len__(self) -> int:
        return len(self._build())

    def has(self, rel: str) -> bool:
        return normalise(rel) in self._build()

    def read(self, rel: str) -> bytes | None:
        """Return the file's bytes, or None when no archive holds it."""
        got = self._build().get(normalise(rel))
        if got is None:
            return None
        archive, kind, rec = got
        try:
            if kind == "ba2":
                from Utils.ba2_extract import read_ba2_entry
                return read_ba2_entry(archive, rec)
            from Utils.bsa_extract import read_bsa_entry
            info, entry = rec
            return read_bsa_entry(archive, info, entry)
        except Exception:                                # noqa: BLE001
            return None
