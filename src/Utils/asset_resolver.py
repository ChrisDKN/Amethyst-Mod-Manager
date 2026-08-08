"""
asset_resolver.py
Resolve a game-data-relative asset path to the bytes the GAME would load.

Order mirrors the engine: winning mod's loose file (filemap.txt), loose file
in the game data folder, winning mod's archive (bsa_filemap winner map), then
vanilla archives. Loose always beats archived. Tables build lazily and are
cached per process by mtime; pass keep_prefix to bound them to subtrees.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["AssetResolver"]

# Per-process caches keyed by input mtimes (filemap can be ~200k lines).
_LOOSE_CACHE: dict[tuple, dict] = {}
_WINNER_CACHE: dict[tuple, tuple] = {}


def _stamp(path) -> tuple:
    try:
        st = os.stat(path)
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), 0, -1)


def normalise(rel: str) -> str:
    """Normalise an asset path to the lowercase forward-slash form used as a key."""
    s = rel.replace("\\", "/").lower().strip()
    while s.startswith("/"):
        s = s[1:]
    if s.startswith("data/"):
        s = s[5:]
    return s


class DirCache:
    """Case-insensitive path resolution, one scandir per directory.

    A lowercase name can map to SEVERAL real entries (mods ship Textures/ AND
    textures/); every candidate branch is tried.
    """

    def __init__(self):
        self._dirs: dict[str, dict[str, list[str]]] = {}

    def _entries(self, path: str) -> dict[str, list[str]]:
        got = self._dirs.get(path)
        if got is None:
            got = {}
            try:
                with os.scandir(path) as it:
                    for e in it:
                        got.setdefault(e.name.lower(), []).append(e.name)
            except OSError:
                pass
            self._dirs[path] = got
        return got

    def resolve(self, root: Path, rel: str) -> Path | None:
        parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
        if not parts:
            return None
        last = len(parts) - 1
        stack = [(str(root), 0)]
        while stack:
            cur, i = stack.pop()
            for actual in self._entries(cur).get(parts[i].lower(), ()):
                nxt = os.path.join(cur, actual)
                if i == last:
                    if os.path.isfile(nxt):
                        return Path(nxt)
                elif os.path.isdir(nxt):
                    stack.append((nxt, i + 1))
        return None


_DirCache = DirCache            # backwards-compatible alias


class AssetResolver:
    """Resolves asset paths the way the running game would."""

    def __init__(self, staging_dir=None, modlist_path=None, profile_dir=None,
                 data_dir=None, game=None, keep_prefix: str = ""):
        self.staging = Path(staging_dir) if staging_dir else None
        self.modlist_path = Path(modlist_path) if modlist_path else None
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self.data_dir = Path(data_dir) if data_dir else None
        self.game = game
        self.keep_prefix = keep_prefix

        self._dirs = _DirCache()
        self._loose: dict[str, str] | None = None      # rel_key -> mod name
        self._bsa_winner: dict[str, str] | None = None  # rel_key -> mod name
        self._bsa_index = None
        self._vanilla = None                            # ArchiveLookup, lazy
        self._stats = {"loose_mod": 0, "loose_data": 0,
                       "archive_mod": 0, "archive_data": 0, "missing": 0}

    # -- lazy tables --------------------------------------------------------
    def _loose_map(self) -> dict[str, str]:
        """rel_key -> winning mod name, straight from the resolved deploy map."""
        if self._loose is not None:
            return self._loose
        out: dict[str, str] = {}
        if self.staging is not None:
            fm = self.staging.parent / "filemap.txt"
            ckey = (_stamp(fm), self.keep_prefix)
            cached = _LOOSE_CACHE.get(ckey)
            if cached is not None:
                self._loose = cached
                return cached
            try:
                # surrogateescape: filemap paths come from on-disk names that
                # are not guaranteed to be valid UTF-8.
                with open(fm, "r", encoding="utf-8",
                          errors="surrogateescape") as f:
                    for line in f:
                        tab = line.find("\t")
                        if tab < 0:
                            continue
                        key = line[:tab].replace("\\", "/").lower()
                        if self.keep_prefix and not key.startswith(self.keep_prefix):
                            continue
                        out[key] = line[tab + 1:].rstrip("\r\n")
            except OSError:
                pass
            _LOOSE_CACHE[ckey] = out
        self._loose = out
        return out

    def _archive_winner(self) -> dict[str, str]:
        """rel_key -> winning mod name, replaying the engine's archive order."""
        if self._bsa_winner is not None:
            return self._bsa_winner
        winner: dict[str, str] = {}
        index = {}
        ckey = None
        if self.staging is not None and self.modlist_path is not None:
            ckey = (_stamp(self.staging.parent / "bsa_index.bin"),
                    _stamp(self.modlist_path),
                    _stamp((self.profile_dir / "loadorder.txt")
                           if self.profile_dir is not None else "-"),
                    self.keep_prefix)
            cached = _WINNER_CACHE.get(ckey)
            if cached is not None:
                self._bsa_winner, self._bsa_index = cached
                return self._bsa_winner
        try:
            if self.staging is not None and self.modlist_path is not None:
                from Utils.bsa_filemap import (
                    compute_bsa_winner_map, read_bsa_index,
                )
                from Utils.modlist import read_modlist
                from Utils.ue_pak_reader import UE_ARCHIVE_EXTENSIONS

                index = read_bsa_index(self.staging.parent / "bsa_index.bin") or {}
                enabled = [e for e in read_modlist(self.modlist_path)
                           if not e.is_separator and e.enabled]
                prio = [e.name for e in reversed(enabled)]
                exts = frozenset(
                    getattr(self.game, "archive_extensions", frozenset())
                    or frozenset())
                plugin_order = None
                plugin_exts = None
                if getattr(self.game, "archive_plugin_ordering", True) \
                        and self.profile_dir is not None:
                    from Utils.plugins import read_loadorder
                    plugin_order = read_loadorder(
                        self.profile_dir / "loadorder.txt")
                    plugin_exts = frozenset(
                        getattr(self.game, "plugin_extensions", []) or [])
                is_ue = bool(exts & UE_ARCHIVE_EXTENSIONS)
                full, _losers = compute_bsa_winner_map(
                    index, prio, plugin_order or None, plugin_exts or None,
                    self.staging.parent / "modindex.bin", is_ue)
                if self.keep_prefix:
                    winner = {k: v for k, v in full.items()
                              if k.startswith(self.keep_prefix)}
                else:
                    winner = full
        except Exception:                                # noqa: BLE001
            winner = {}
            index = {}
        if ckey is not None:
            _WINNER_CACHE[ckey] = (winner, index)
        self._bsa_winner = winner
        self._bsa_index = index
        return winner

    def _vanilla_archives(self):
        if self._vanilla is None:
            from Utils.archive_lookup import ArchiveLookup, find_archives
            roots = [self.data_dir] if self.data_dir is not None else []
            self._vanilla = ArchiveLookup(find_archives(roots),
                                          keep_prefix=self.keep_prefix)
        return self._vanilla

    # -- enumeration --------------------------------------------------------
    def loose_winners(self) -> dict[str, str]:
        """rel_key -> mod whose LOOSE copy wins (the resolved deploy map)."""
        return self._loose_map()

    def archive_winners(self) -> dict[str, str]:
        """rel_key -> mod whose ARCHIVED copy wins, once loose files are out."""
        return self._archive_winner()

    # -- resolution ---------------------------------------------------------
    def loose_path(self, rel: str) -> Path | None:
        """The loose file the game would load, or None if it is archived only."""
        key = normalise(rel)

        mod = self._loose_map().get(key)
        if mod and self.staging is not None:
            hit = self._dirs.resolve(self.staging / mod, key)
            if hit is not None:
                return hit

        if self.data_dir is not None:
            hit = self._dirs.resolve(self.data_dir, key)
            if hit is not None:
                return hit
        return None

    def read(self, rel: str) -> bytes | None:
        """Return the winning copy's bytes, or None when nothing provides it."""
        key = normalise(rel)

        path = self.loose_path(key)
        if path is not None:
            try:
                data = path.read_bytes()
            except OSError:
                data = None
            if data:
                owner = self._loose_map().get(key)
                self._stats["loose_mod" if owner else "loose_data"] += 1
                return data

        mod = self._archive_winner().get(key)
        if mod and self.staging is not None:
            data = self._read_from_mod_archives(mod, key)
            if data:
                self._stats["archive_mod"] += 1
                return data

        data = self._vanilla_archives().read(key)
        if data:
            self._stats["archive_data"] += 1
            return data

        self._stats["missing"] += 1
        return None

    def _read_from_mod_archives(self, mod: str, key: str) -> bytes | None:
        """Pull *key* out of whichever of *mod*'s archives contains it."""
        for archive_name, _mtime, paths in (self._bsa_index or {}).get(mod, []):
            if key not in paths:
                continue
            archive = self._dirs.resolve(self.staging / mod, archive_name)
            if archive is None:
                continue
            try:
                if archive.suffix.lower() == ".ba2":
                    from Utils.ba2_extract import index_ba2, read_ba2_entry
                    rec = index_ba2(archive).get(key)
                    return read_ba2_entry(archive, rec) if rec else None
                from Utils.bsa_extract import index_bsa, read_bsa_entry
                info, entries = index_bsa(archive)
                entry = entries.get(key)
                return read_bsa_entry(archive, info, entry) if entry else None
            except Exception:                            # noqa: BLE001
                return None
        return None

    @property
    def stats(self) -> dict:
        """Where resolved assets came from - useful for diagnosing a preview."""
        return dict(self._stats)
