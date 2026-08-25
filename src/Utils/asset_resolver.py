"""
asset_resolver.py
Resolve a game-data-relative asset path to the bytes the GAME would load.

Order mirrors the engine: Filegraph's winning loose provider, a loose file in
the game data folder, Filegraph's winning archive provider, then vanilla
archives. Loose always beats archived. Tables build lazily; pass keep_prefix
to bound archive and catalog queries to asset subtrees.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["AssetResolver"]

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
                 data_dir=None, game=None, keep_prefix: str | tuple[str, ...] = "",
                 snapshot=None):
        self.staging = Path(staging_dir) if staging_dir else None
        self.modlist_path = Path(modlist_path) if modlist_path else None
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self.data_dir = Path(data_dir) if data_dir else None
        self.game = game
        self.keep_prefix = keep_prefix
        if snapshot is None and game is not None:
            try:
                from Utils.filegraph_service import active_snapshot
                snapshot = active_snapshot(game)
            except Exception:
                snapshot = None
        self.snapshot = snapshot

        self._dirs = _DirCache()
        self._loose: dict[str, str] | None = None      # rel_key -> mod name
        self._bsa_winner: dict[str, str] | None = None  # rel_key -> mod name
        self._bsa_index = None
        self._loose_sources: dict[str, Path] = {}
        self._archive_sources: dict[str, Path] = {}
        self._vanilla = None                            # ArchiveLookup, lazy
        self._mod_archive_lookups: dict[Path, object] = {}
        self._stats = {"loose_mod": 0, "loose_data": 0,
                       "archive_mod": 0, "archive_data": 0, "missing": 0}

    def _query_prefixes(self) -> tuple[str, ...]:
        """Catalog prefixes, including the game-data coordinate used by root
        providers.  A root mod may expose ``Data Files/textures/...`` while
        callers consistently ask for ``textures/...``.
        """
        raw = ((self.keep_prefix,) if isinstance(self.keep_prefix, str)
               else tuple(self.keep_prefix))
        prefixes = tuple(
            value.replace("\\", "/").lower().lstrip("/")
            for value in raw if value
        )
        if not prefixes or self.game is None:
            return prefixes
        try:
            from Utils.game_helpers import game_data_subpath
            data_prefix = game_data_subpath(self.game).replace(
                "\\", "/").lower().strip("/")
        except Exception:
            data_prefix = ""
        if not data_prefix:
            return prefixes
        return prefixes + tuple(f"{data_prefix}/{value}" for value in prefixes)

    def _wanted_asset(self, key: str) -> bool:
        raw = ((self.keep_prefix,) if isinstance(self.keep_prefix, str)
               else tuple(self.keep_prefix))
        prefixes = tuple(
            value.replace("\\", "/").lower().lstrip("/")
            for value in raw if value
        )
        return not prefixes or key.startswith(prefixes)

    # -- lazy tables --------------------------------------------------------
    def _loose_map(self) -> dict[str, str]:
        """Asset-relative loose winners from the pinned graph generation."""
        if self._loose is not None:
            return self._loose
        out: dict[str, str] = {}
        if self.snapshot is not None and self.game is not None:
            from Utils.filegraph_service import source_path
            for winner in self.snapshot.asset_winners(self._query_prefixes()):
                if winner.namespace == "archive":
                    continue
                key = self._asset_key(winner.legacy_rel, winner.namespace)
                if not self._wanted_asset(key):
                    continue
                out[key] = winner.mod_name
                self._loose_sources[key] = source_path(
                    self.game, winner.mod_name, winner.source_rel)
        self._loose = out
        return out

    def _archive_winner(self) -> dict[str, str]:
        """Asset-relative archive winners from the pinned graph generation."""
        if self._bsa_winner is not None:
            return self._bsa_winner
        winner: dict[str, str] = {}
        if self.snapshot is not None and self.game is not None:
            from Utils.filegraph_service import source_path
            for record in self.snapshot.asset_winners(self._query_prefixes()):
                if record.namespace != "archive":
                    continue
                key = self._asset_key(record.legacy_rel, record.namespace)
                if not self._wanted_asset(key):
                    continue
                winner[key] = record.mod_name
                self._archive_sources[key] = source_path(
                    self.game, record.mod_name, record.source_rel)
        self._bsa_winner = winner
        return winner

    def _asset_key(self, relative: str, namespace: str) -> str:
        path = relative.replace("\\", "/").lstrip("/")
        if namespace == "root" and self.game is not None:
            try:
                from Utils.game_helpers import game_data_subpath
                prefix = game_data_subpath(self.game).replace(
                    "\\", "/").strip("/")
            except Exception:
                prefix = ""
            if prefix and path.lower().startswith(prefix.lower() + "/"):
                path = path[len(prefix) + 1:]
        return normalise(path)

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

        self._loose_map()
        hit = self._loose_sources.get(key)
        if hit is not None and hit.is_file():
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
        if mod:
            data = self._read_from_mod_archives(key)
            if data:
                self._stats["archive_mod"] += 1
                return data

        data = self._vanilla_archives().read(key)
        if data:
            self._stats["archive_data"] += 1
            return data

        self._stats["missing"] += 1
        return None

    def _read_from_mod_archives(self, key: str) -> bytes | None:
        """Pull *key* from the exact archive provider selected by Filegraph."""
        archive = self._archive_sources.get(key)
        if archive is None:
            return None
        try:
            lookup = self._mod_archive_lookups.get(archive)
            if lookup is None:
                from Utils.archive_lookup import ArchiveLookup
                lookup = ArchiveLookup([archive], keep_prefix=self.keep_prefix)
                self._mod_archive_lookups[archive] = lookup
            return lookup.read(key)
        except Exception:                                # noqa: BLE001
            return None

    @property
    def stats(self) -> dict:
        """Where resolved assets came from - useful for diagnosing a preview."""
        return dict(self._stats)
