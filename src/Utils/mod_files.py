"""Toolkit-neutral logic for the Mod Files tab (Qt view drives this).

Columns: **Top Level** promotes a path by adding strip-prefix entries
(``mod_strip_prefixes``); **Root** routes files to the game root
(``root_mod_files``); **Disable** drops them from deploy
(``excluded_mod_files``).

Both per-file stores key on the RAW on-disk path (lowercase, forward-slash).
Post-strip keys can't: they collide when a promoted folder holds a same-named
file, and they rename on every Top Level edit. The engine works in index-key
space - translate at its boundaries via ``translate_exclusions_for_engine`` and
``filemap.index_keys_for_mod``. Pure stdlib + Utils.* - no GUI toolkit.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import NamedTuple

from Utils.profile_state import (
    read_excluded_mod_files, write_excluded_mod_files,
    read_mod_strip_prefixes, write_mod_strip_prefixes,
    read_root_mod_files, write_root_mod_files,
)
from Utils.filemap import OVERWRITE_NAME


# ---------------------------------------------------------------------------
# File listing (raw, no strip applied) - index fast-path + scan fallback
# ---------------------------------------------------------------------------
def load_mod_files(game, mod_name: str, index_path: Path | None,
                   full_index: dict | None = None,
                   prefer_live: bool = False) -> dict[str, str]:
    """Return {rel_key (lower) -> rel_str (raw on-disk casing)} for *mod_name*.

    Reuses modindex.bin (raw per-mod casing) for stable mods; [Overwrite] and
    mods missing from the index are scanned live. Empty dict if nothing found.

    prefer_live=True ALWAYS scans the mod folder from disk first (only the one
    displayed mod, so it's cheap), falling back to the index if the folder is
    missing. The Mod Files tab uses this so the tree matches the REAL on-disk
    structure - a stale index (flat where disk is nested) otherwise builds the
    tree with wrong paths, which orphans strip-prefix entries on toggle.
    """
    files: dict[str, str] = {}

    def _scan() -> bool:
        mod_dir = _mod_dir_for(game, mod_name)
        if mod_dir is not None and mod_dir.is_dir():
            try:
                from Utils.filemap import _scan_dir
                _name, normal, root, _invalid = _scan_dir(mod_name, str(mod_dir))
                files.update(normal)
                files.update(root)
                return True
            except Exception:
                return False
        return False

    if prefer_live and _scan():
        return files

    if full_index is None and index_path is not None and index_path.is_file():
        try:
            from Utils.filemap import read_mod_index
            full_index = read_mod_index(index_path)
        except Exception:
            full_index = None

    # [Overwrite] changes outside the index rebuild cycle → always scan live.
    idx_entry = (full_index.get(mod_name)
                 if full_index and mod_name != OVERWRITE_NAME else None)
    if idx_entry is not None:
        normal, root = idx_entry
        files.update(normal)
        files.update(root)
        return files

    _scan()
    return files


def _mod_dir_for(game, mod_name: str) -> Path | None:
    if game is None:
        return None
    try:
        from Utils.filemap import ROOT_FOLDER_NAME
        if mod_name == OVERWRITE_NAME and hasattr(game, "get_effective_overwrite_path"):
            return Path(game.get_effective_overwrite_path())
        if mod_name == ROOT_FOLDER_NAME and hasattr(game, "get_effective_root_folder_path"):
            return Path(game.get_effective_root_folder_path())
        if hasattr(game, "get_effective_mod_staging_path"):
            return Path(game.get_effective_mod_staging_path()) / mod_name
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Conflict cache - which post-strip keys are contested + the filemap winner
# ---------------------------------------------------------------------------
# Single-slot result cache. Counting every key of every enabled mod (plus
# re-parsing filemap.txt) costs seconds on a large setup, and the Mod Files
# tab rebuilds on every modlist click - keyed by the source-file mtimes (and
# index identity) so unchanged inputs return the previous result for free.
# Callers treat the result as read-only (they only do `in` / `.get`).
_conflict_cache_lock = threading.Lock()
_conflict_cache: tuple | None = None   # (key, full_index (identity), result)


class ConflictCache(NamedTuple):
    """Contest data split by deploy namespace - ask status(), not the fields.

    Root-routed files (whole-mod rootFolder mods + Root-column tags) land in
    the game root, so they contest each other and never a Data-bound namesake.
    """
    contested: set[str]                    # Data namespace, >1 enabled provider
    winner: dict[str, str]                 # from filemap.txt
    contested_root: set[str]               # game-root namespace
    winner_root: dict[str, str]            # from filemap_root.txt
    root_mods: frozenset                   # whole-mod root-flagged
    root_tags: dict[str, frozenset]        # mod -> per-file root-tagged keys
    # Game-root-relative path of the normal deploy directory.  Root entries
    # below it share a final destination with Data-bound entries after this
    # prefix is stripped (Skyrim: Data/meshes/x <-> meshes/x).
    root_data_prefix: str = ""
    # (mod, rel_key) -> UUID for BG3 paks whose module is shipped by more than
    # one enabled mod. They contest by identity, so the path-keyed sets above
    # can't see them - the loser isn't even in filemap.txt.
    pak_uuid: dict = {}

    def is_root(self, mod_name: str, rel_key: str) -> bool:
        """True when this mod's file deploys to the game root, not Data/."""
        if mod_name in self.root_mods:
            return True
        tags = self.root_tags.get(mod_name)
        return tags is not None and rel_key in tags

    def status(self, mod_name: str, rel_key: str) -> int:
        """1 this mod wins, -1 it loses, 0 no conflict - in its own namespace."""
        if self.pak_uuid and (mod_name, rel_key) in self.pak_uuid:
            # Identity conflict: only one pak per module survives into
            # filemap.txt - that one wins, every other copy loses.
            return 1 if self.winner.get(rel_key) == mod_name else -1
        if self.is_root(mod_name, rel_key):
            contested, winner = self.contested_root, self.winner_root
        else:
            contested, winner = self.contested, self.winner
        if rel_key not in contested:
            return 0
        w = winner.get(rel_key)
        if w is None:
            return 0
        return 1 if w == mod_name else -1

    @property
    def all_contested(self) -> set[str]:
        """Contested keys across both namespaces (for views that merge them)."""
        out = self.contested | self.contested_root
        if not self.root_data_prefix:
            return out
        pfx = self.root_data_prefix + "/"
        # The Data tab strips the game Data prefix from root-map entries, so
        # expose contested keys in that same display coordinate as well.
        return out | {
            k[len(pfx):] for k in self.contested_root
            if k.startswith(pfx) and len(k) > len(pfx)
        }


def conflict_root_context(game, profile_dir: Path | None) -> tuple:
    """(root mods, root-tagged keys, Data prefix) for conflict routing.

    Every build_conflict_cache caller must pass this - the cache is single-slot,
    so callers that disagree thrash it (same rule as bsa_conflict_index_path)."""
    if game is None or profile_dir is None:
        return frozenset(), {}, ""
    modlist_path = profile_dir / "modlist.txt"
    root_mods: frozenset = frozenset()
    if modlist_path.is_file():
        try:
            from Nexus.nexus_meta import collect_root_flagged_mods
            staging = Path(game.get_effective_mod_staging_path())
            root_mods = frozenset(
                collect_root_flagged_mods(modlist_path, staging) or ())
        except Exception:
            root_mods = frozenset()
    try:
        from Utils.filemap import index_keys_for_mod
        raw_tags = read_root_mod_files(profile_dir, None)
        per_mod = read_mod_strip_prefixes(profile_dir, None)
        global_strips = getattr(game, "mod_folder_strip_prefixes", None)
        tags = {
            m: frozenset(index_keys_for_mod(v, m, global_strips, per_mod,
                                            root_mods))
            for m, v in raw_tags.items() if v
        }
    except Exception:
        tags = {}
    try:
        from Utils.game_helpers import game_data_subpath
        data_prefix = game_data_subpath(game).replace("\\", "/").strip("/").lower()
    except Exception:
        data_prefix = ""
    return root_mods, tags, data_prefix


def _pak_uuid_contests(index_path: Path, full_index: dict, disabled: set,
                       pak_ctx: tuple) -> dict:
    """{(mod, rel_key): uuid} for .pak modules shipped by >1 enabled pak."""
    staging_root, overwrite_dir = pak_ctx
    try:
        from Utils.pak_identity import CACHE_NAME, OVERWRITE_NAME, PakUuidCache
    except Exception:
        return {}
    # Read-only: the filemap build owns this cache (and may be writing it on
    # another thread). A cold miss just costs this call one archive read.
    cache = PakUuidCache(index_path.parent / CACHE_NAME, readonly=True)
    counts: dict[str, int] = {}
    found: list[tuple[str, str, str]] = []
    for mn, (normal, _root) in full_index.items():
        if mn in disabled:
            continue
        mod_root = overwrite_dir if mn == OVERWRITE_NAME else staging_root / mn
        for k, rel_str in normal.items():
            if not k.endswith(".pak"):
                continue
            uuid = cache.uuid_for(mod_root / rel_str)
            if not uuid:
                continue
            counts[uuid] = counts.get(uuid, 0) + 1
            found.append((mn, k, uuid))
    # Count paks, not mods: two copies inside ONE mod folder (or the overwrite
    # folder) contest too - only one of them deploys.
    return {(mn, k): uuid for mn, k, uuid in found if counts[uuid] > 1}


def pak_uuid_context(game, index_path: Path | None) -> tuple | None:
    """(staging_root, overwrite_dir) for identity-keyed .pak contests, or None.

    BG3 only. Every view feeding build_conflict_cache must pass this - the cache
    is single-slot, so callers that disagree thrash it (as bsa_conflict_index_path).
    """
    if game is None or index_path is None:
        return None
    if not getattr(game, "pak_uuid_conflicts", False):
        return None
    try:
        from Utils.pak_identity import uuid_conflicts_enabled
        if not uuid_conflicts_enabled():
            return None
        return (Path(game.get_effective_mod_staging_path()),
                Path(game.get_effective_overwrite_path()))
    except Exception:
        return None


def build_conflict_cache(index_path: Path | None,
                         profile_dir: Path | None,
                         full_index: dict | None = None,
                         bsa_index_path: Path | None = None,
                         root_ctx: tuple | None = None,
                         pak_ctx: tuple | None = None,
                         ) -> ConflictCache:
    """Return the per-namespace contest data (see :class:`ConflictCache`).

    Keys are index-space rel_keys (lower); disabled mods never count. Root-routed
    files contest in their own namespace (filemap_root.txt) - pass *root_ctx*
    from conflict_root_context.

    With *bsa_index_path* (see bsa_conflict_index_path for the gate) a loose key
    also shipped in ANOTHER enabled mod's archive counts as contested: the engine
    loads archives first, so the loose file wins a real conflict.

    Cached by the inputs' stats + root_ctx + index identity; treat the returned
    sets/dicts as read-only.
    """
    global _conflict_cache
    if root_ctx and len(root_ctx) >= 3:
        root_mods, root_tags, root_data_prefix = root_ctx[:3]
    else:
        root_mods, root_tags = root_ctx or (frozenset(), {})
        root_data_prefix = ""
    root_data_prefix = (root_data_prefix or "").replace("\\", "/").strip("/").lower()
    if index_path is None:
        return ConflictCache(set(), {}, set(), {}, root_mods, root_tags,
                             root_data_prefix)
    fm_path = index_path.parent / "filemap.txt"
    fmr_path = index_path.parent / "filemap_root.txt"
    ml_path = (profile_dir / "modlist.txt") if profile_dir is not None else None
    ps_path = (profile_dir / "profile_state.json") if profile_dir is not None else None

    if full_index is None:
        try:
            from Utils.filemap import read_mod_index
            full_index = read_mod_index(index_path)
        except Exception:
            full_index = None

    def _stat_key(p: Path | None) -> "tuple[int, int] | None":
        # (st_mtime_ns, st_size) - a bare float mtime is too coarse on
        # FAT/exFAT SD cards (2 s resolution); see filemap._index_stat_key.
        try:
            if p is None:
                return None
            st = p.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    key = (str(index_path), _stat_key(fm_path), _stat_key(fmr_path),
           str(ml_path) if ml_path is not None else None, _stat_key(ml_path),
           _stat_key(ps_path),
           str(bsa_index_path) if bsa_index_path is not None else None,
           _stat_key(bsa_index_path), root_mods,
           tuple(sorted((m, v) for m, v in root_tags.items())),
           root_data_prefix,
           tuple(str(p) for p in (pak_ctx or ())))
    with _conflict_cache_lock:
        cached = _conflict_cache
    # read_mod_index caches by mtime, so an unchanged index returns the SAME
    # dict object - identity is the cheap staleness check for it.
    if cached is not None and cached[0] == key and cached[1] is full_index:
        return cached[2]

    def _read_winners(path: Path) -> dict[str, str]:
        # surrogateescape: filemap rel paths derive from on-disk filenames
        # whose non-UTF-8 bytes decode to surrogate code points; a plain utf-8
        # read would raise on them.
        out: dict[str, str] = {}
        if not path.is_file():
            return out
        try:
            for line in path.read_text(
                    encoding="utf-8", errors="surrogateescape").splitlines():
                if "\t" in line:
                    rk, mn = line.split("\t", 1)
                    out[rk.lower()] = mn
        except Exception:
            pass
        return out

    filemap_winner = _read_winners(fm_path)
    filemap_root_winner = _read_winners(fmr_path)

    contested: set[str] = set()
    contested_root: set[str] = set()
    pak_uuid: dict = {}
    if full_index:
        disabled: set[str] = set()
        if ml_path is not None and ml_path.is_file():
            try:
                from Utils.modlist import read_modlist
                disabled = {e.name for e in read_modlist(ml_path)
                            if not e.is_separator and not e.enabled}
            except Exception:
                disabled = set()
        # Archive contents (path → owning enabled mods). bsa_index paths are
        # already lowercase forward-slash, matching the loose index keys.
        arch_owners: dict[str, set[str]] = {}
        if bsa_index_path is not None:
            try:
                from Utils.bsa_filemap import read_bsa_index
                bsa_index = read_bsa_index(bsa_index_path) or {}
            except Exception:
                bsa_index = {}
            for mn, archives in bsa_index.items():
                if mn in disabled:
                    continue
                for _bsa, _mt, paths in archives:
                    for p in paths:
                        arch_owners.setdefault(p, set()).add(mn)
        counts: dict[str, int] = {}
        counts_root: dict[str, int] = {}
        for mn, (normal, root) in full_index.items():
            if mn in disabled:
                continue
            # Root-routed files land in the game root, so they contest only
            # each other - count them in their own namespace.
            whole_root = mn in root_mods
            tags = root_tags.get(mn)
            for k in normal:
                if whole_root or (tags is not None and k in tags):
                    counts_root[k] = counts_root.get(k, 0) + 1
                    continue
                counts[k] = counts.get(k, 0) + 1
                if arch_owners:
                    ow = arch_owners.get(k)
                    # Contested only when some OTHER mod's archive ships it -
                    # a mod's own BSA copy of its own loose file isn't one.
                    if ow is not None and (len(ow) > 1 or mn not in ow):
                        contested.add(k)
            # Legacy root keys deploy outside Data/ and can't collide with
            # archive contents, so they get the loose-only count.
            for k in root:
                if whole_root:
                    counts_root[k] = counts_root.get(k, 0) + 1
                else:
                    counts[k] = counts.get(k, 0) + 1
        contested |= {k for k, c in counts.items() if c > 1}
        contested_root |= {k for k, c in counts_root.items() if c > 1}
        # Whole-mod root installs keep their leading Data/ segment so they can
        # also contain game-root DLLs.  Entries below that segment nevertheless
        # collide with ordinary Data-bound files.  Root deployment runs last,
        # making its file the effective winner in the merged view.
        if root_data_prefix:
            pfx = root_data_prefix + "/"
            for root_key in counts_root:
                if (not root_key.startswith(pfx)
                        or len(root_key) <= len(pfx)):
                    continue
                data_key = root_key[len(pfx):]
                if counts.get(data_key, 0) <= 0:
                    continue
                contested.add(data_key)
                contested_root.add(root_key)
                root_winner = filemap_root_winner.get(root_key)
                if root_winner is not None:
                    filemap_winner[data_key] = root_winner
        if pak_ctx is not None:
            pak_uuid = _pak_uuid_contests(index_path, full_index, disabled,
                                          pak_ctx)
    result = ConflictCache(contested, filemap_winner, contested_root,
                           filemap_root_winner, root_mods, root_tags,
                           root_data_prefix, pak_uuid)
    with _conflict_cache_lock:
        _conflict_cache = (key, full_index, result)
    return result


def bsa_conflict_index_path(game, index_path: Path | None) -> Path | None:
    """bsa_index.bin path for build_conflict_cache's archive-aware contest, or
    None when it doesn't apply: non-archive game, UE pak game (the engine never
    loads loose assets over pak contents), "Hide BSA conflicts" on, or no index
    on disk. Mirrors the gate in game_state._build_bsa_conflicts.

    Every view feeding build_conflict_cache must go through this: the cache is
    single-slot, so callers alternating bsa/no-bsa would thrash it."""
    if game is None or index_path is None:
        return None
    exts = frozenset(getattr(game, "archive_extensions", ()) or ())
    if not exts:
        return None
    try:
        from Utils.ue_pak_reader import UE_ARCHIVE_EXTENSIONS
        if exts & UE_ARCHIVE_EXTENSIONS:
            return None
        from Utils.ui_config import load_hide_bsa_conflicts
        if load_hide_bsa_conflicts():
            return None
    except Exception:
        return None
    p = index_path.parent / "bsa_index.bin"
    return p if p.is_file() else None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def parent_path(path: str) -> str:
    """Parent folder path of *path* (or '')."""
    p = path.replace("\\", "/").rstrip("/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def ancestor_paths(path: str) -> list[str]:
    """Ancestor folder paths of *path*, root → parent."""
    p = path.replace("\\", "/").rstrip("/")
    if "/" not in p:
        return []
    out: list[str] = []
    cur = ""
    for seg in p.split("/")[:-1]:
        cur = f"{cur}/{seg}" if cur else seg
        out.append(cur)
    return out


def rel_key_after_strip(raw_rel_key: str, stripped_paths: set[str]) -> str:
    """Apply the saved strip prefixes (longest-match-first, like _scan_dir) to a
    raw rel_key so it can be looked up in the post-strip conflict/filemap data."""
    k = raw_rel_key
    for s in sorted(stripped_paths, key=len, reverse=True):
        sl = s.lower()
        if k == sl or k.startswith(sl + "/"):
            return k[len(sl):].lstrip("/")
    return k


def is_top_level(path: str, stripped_paths: set[str]) -> bool:
    """True if *path* deploys at the top level given the strip list (its parent
    path is fully covered by a strip entry, or it has no parent)."""
    parent = parent_path(path)
    if not parent:
        return True
    return parent.lower() in stripped_paths


# ---------------------------------------------------------------------------
# Tree building
# ---------------------------------------------------------------------------
def build_tree(files: dict[str, str], *,
               keep_rel_key=None) -> dict:
    """Build a nested dict from {rel_key: rel_str}. Folders are sub-dicts; files
    live in a "__files__" list of (filename, rel_key, rel_str). *keep_rel_key*
    is an optional predicate(rel_key, rel_str) -> bool to drop filtered rows."""
    tree: dict = {}
    for rel_key, rel_str in sorted(files.items()):
        if keep_rel_key is not None and not keep_rel_key(rel_key, rel_str):
            continue
        parts = rel_str.replace("\\", "/").split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault("__files__", []).append((parts[-1], rel_key, rel_str))
    return tree


# ---------------------------------------------------------------------------
# Strip-prefix (Top Level) toggle - returns the new strip set
# ---------------------------------------------------------------------------
def toggle_top_level(path: str, stripped_paths: set[str]) -> set[str]:
    """Compute the new strip-prefix set after toggling *path*'s Top Level box.

    - path already stripped → un-strip it (+ any stripped descendants).
    - path currently top-level (a promoted row) → demote: remove the FULL
      ancestor chain that made it top-level, so a second click on the same row
      cleanly REVERSES the promotion (no orphaned `meshes`/`meshes/actors`
      leftovers). Ancestors still needed by another promoted sibling are kept.
    - else → promote: strip every ancestor segment up to it.

    Returns a NEW set (does not mutate the input). Root-level rows that are
    already top-level return the set unchanged (nothing to demote).
    """
    out = set(stripped_paths)
    path_l = path.lower()

    def _unstrip_subtree(root_l: str):
        prefix = root_l + "/"
        for s in list(out):
            if s == root_l or s.startswith(prefix):
                out.discard(s)

    if path_l in out:
        _unstrip_subtree(path_l)
    elif is_top_level(path, out):
        ancestors = [a.lower() for a in ancestor_paths(path)]
        if not ancestors:
            return out                      # root-level: nothing to demote
        # Remove only the ancestor strip entries that aren't ALSO promoting some
        # other branch. An ancestor `a` is still needed if a different stripped
        # entry sits strictly below `a` on a path that doesn't lead to `path`.
        to_remove = set(ancestors)
        keep_path = path_l
        for s in out:
            if s in to_remove:
                # Does another stripped descendant of `s` exist that ISN'T on
                # this row's own ancestor chain? If so, `s` is shared - keep it.
                for other in out:
                    if other == s:
                        continue
                    if other.startswith(s + "/") and not (
                            keep_path == other or keep_path.startswith(other + "/")
                            or other.startswith(keep_path + "/")):
                        to_remove.discard(s)
                        break
        for a in to_remove:
            out.discard(a)
    else:
        for anc in ancestor_paths(path):
            out.add(anc.lower())
    return out


def save_strip_prefixes(profile_dir: Path, mod_name: str,
                        stripped_paths: set[str],
                        case_hints: dict[str, str] | None = None) -> list[str]:
    """Persist *stripped_paths* for *mod_name*, preferring original-case forms
    from *case_hints* (lower → original). Returns the merged list written."""
    case_hints = case_hints or {}
    strip_map = read_mod_strip_prefixes(profile_dir, None)
    for e in strip_map.get(mod_name, []):
        if e:
            case_hints.setdefault(e.lower(), e)
    merged = sorted({case_hints.get(s, s) for s in stripped_paths if s})
    if merged:
        strip_map[mod_name] = merged
    else:
        strip_map.pop(mod_name, None)
    write_mod_strip_prefixes(profile_dir, strip_map)
    return merged


# ---------------------------------------------------------------------------
# Exclusion (Disable) save - merge visible state with preserved filtered rows
# ---------------------------------------------------------------------------
def save_exclusions(profile_dir: Path, mod_name: str,
                    visible_keys: set[str], excluded_visible: set[str]) -> set[str]:
    """Persist exclusions for *mod_name* (RAW keys), keeping entries for rows
    the active filter hides. Returns the full new excluded set."""
    all_excluded = read_excluded_mod_files(profile_dir, None)
    preserved = {k for k in all_excluded.get(mod_name, set())
                 if k not in visible_keys}
    excluded = preserved | excluded_visible
    if excluded:
        all_excluded[mod_name] = sorted(excluded)
    else:
        all_excluded.pop(mod_name, None)
    write_excluded_mod_files(profile_dir, all_excluded)
    return excluded


def read_exclusions(profile_dir: Path, mod_name: str) -> set[str]:
    """Saved excluded RAW keys for *mod_name* (empty set if none)."""
    if profile_dir is None:
        return set()
    return set(read_excluded_mod_files(profile_dir, None).get(mod_name, set()))


def save_root_tags(profile_dir: Path, mod_name: str,
                   visible_keys: set[str], tagged_visible: set[str]) -> set[str]:
    """Persist root tags for *mod_name* (RAW keys), keeping tags on rows the
    active filter hides. Returns the full new tagged set."""
    all_tags = read_root_mod_files(profile_dir, None)
    preserved = {k for k in all_tags.get(mod_name, set())
                 if k not in visible_keys}
    tagged = preserved | tagged_visible
    if tagged:
        all_tags[mod_name] = sorted(tagged)
    else:
        all_tags.pop(mod_name, None)
    write_root_mod_files(profile_dir, all_tags)
    return tagged


def read_root_tags(profile_dir: Path, mod_name: str) -> set[str]:
    """Saved root-tagged RAW keys for *mod_name* (empty set if none)."""
    if profile_dir is None:
        return set()
    return set(read_root_mod_files(profile_dir, None).get(mod_name, set()))


def _migrate_keys_to_raw(reader, writer, profile_dir: Path, mod_name: str,
                         raw_keys, stripped: set[str],
                         keep_unmatched: bool) -> bool:
    """Convert one mod's stored keys from legacy post-strip to raw.

    A key that is some file's post-strip key becomes that file - all of them
    when several collapse onto it, preserving the ticks the user sees."""
    data = reader(profile_dir, None)
    cur = data.get(mod_name)
    if not cur:
        return False
    raw_set = set(raw_keys)
    by_old: dict[str, list[str]] = {}
    for raw in raw_keys:
        by_old.setdefault(rel_key_after_strip(raw, stripped), []).append(raw)
    converted: set[str] = set()
    for k in cur:
        if k in raw_set:
            converted.add(k)
        elif k in by_old:
            converted.update(by_old[k])
        elif keep_unmatched:
            converted.add(k)
    if converted == set(cur):
        return False
    if converted:
        data[mod_name] = sorted(converted)
    else:
        data.pop(mod_name, None)
    writer(profile_dir, data)
    return True


def migrate_root_tags_to_raw(profile_dir: Path | None, mod_name: str | None,
                             raw_keys, stripped: set[str]) -> bool:
    """Convert a mod's root tags from legacy post-strip keys to raw paths.
    No-op for mods with no strip prefixes - the two spaces are identical."""
    if profile_dir is None or not mod_name or not stripped or not raw_keys:
        return False
    return _migrate_keys_to_raw(read_root_mod_files, write_root_mod_files,
                                profile_dir, mod_name, raw_keys, stripped,
                                keep_unmatched=False)


def migrate_exclusions_to_raw(profile_dir: Path | None, mod_name: str | None,
                              raw_keys, stripped: set[str]) -> bool:
    """Convert a mod's exclusions to raw paths, keeping keys that match no file
    - BSA pack excludes loose files it then deletes, and unpack needs those."""
    if profile_dir is None or not mod_name or not stripped or not raw_keys:
        return False
    return _migrate_keys_to_raw(read_excluded_mod_files,
                                write_excluded_mod_files,
                                profile_dir, mod_name, raw_keys, stripped,
                                keep_unmatched=True)


def translate_exclusions_for_engine(
        profile_dir: Path | None, staging_root: Path | None,
        strip_prefixes=None, per_mod_strip_prefixes: dict | None = None,
        root_folder_mods=None) -> dict[str, set[str]]:
    """Stored (raw-keyed) exclusions → engine/index key space, collision-safe.

    An index key is excluded only when EVERY raw file collapsing onto it is -
    a partial exclusion means the user picked a variant, so the key survives and
    the deploy source resolver skips the excluded ones. Keys matching no file
    pass through (BSA-packed entries whose loose files were deleted)."""
    if profile_dir is None:
        return {}
    from Utils.filemap import mod_strip_args, index_key_for_raw, _scan_dir
    raw_exc = read_excluded_mod_files(profile_dir, None)
    out: dict[str, set[str]] = {}
    for mod, keys in raw_exc.items():
        kset = {k.lower() for k in keys if k}
        if not kset:
            continue
        strips, paths = mod_strip_args(mod, strip_prefixes,
                                       per_mod_strip_prefixes, root_folder_mods)
        if not strips and not paths:
            out[mod] = kset          # raw space == index space
            continue
        mod_dir = staging_root / mod if staging_root is not None else None
        if mod_dir is None or not mod_dir.is_dir():
            out[mod] = kset          # can't verify - mod deploys nothing anyway
            continue
        try:
            _n, raw_files, _r, _i = _scan_dir(mod, str(mod_dir))
        except Exception:
            out[mod] = kset
            continue
        groups: dict[str, list[str]] = {}
        for raw in raw_files:
            groups.setdefault(
                index_key_for_raw(raw, strips, paths), []).append(raw)
        translated = {k for k in kset if k not in raw_files}   # passthrough
        translated |= {ik for ik, members in groups.items()
                       if all(m in kset for m in members)}
        if translated:
            out[mod] = translated
    return out


def index_keys_to_raw(mod_dir: Path | None, mod_name: str, index_keys,
                      strip_prefixes=None,
                      per_mod_strip_prefixes: dict | None = None,
                      root_folder_mods=None) -> set[str]:
    """Raw keys under *mod_dir* whose index key is in *index_keys*.

    The inverse of index_keys_for_mod, for callers that work in raw space (BSA
    packing walks the mod folder). Returns the input untouched when the mod has
    no strips or the folder can't be scanned - there the spaces are the same."""
    keys = {k.lower() for k in index_keys if k}
    if not keys:
        return set()
    from Utils.filemap import mod_strip_args, index_key_for_raw, _scan_dir
    strips, paths = mod_strip_args(mod_name, strip_prefixes,
                                   per_mod_strip_prefixes, root_folder_mods)
    if not strips and not paths:
        return keys
    if mod_dir is None or not mod_dir.is_dir():
        return keys
    try:
        _n, raw_files, _r, _i = _scan_dir(mod_name, str(mod_dir))
    except Exception:
        return keys
    return {raw for raw in raw_files
            if index_key_for_raw(raw, strips, paths) in keys}


def excluded_raw_by_mod(profile_dir: Path | None) -> dict[str, set[str]]:
    """Per-mod raw excluded keys (lowercase) for deploy source resolution."""
    if profile_dir is None:
        return {}
    return {m: {k.lower() for k in v if k}
            for m, v in read_excluded_mod_files(profile_dir, None).items() if v}


def prune_orphan_root_tags(profile_dir: Path | None, mod_name: str | None,
                           valid_keys: set[str]) -> bool:
    """Drop root tags matching no current file. No-op on an empty file list -
    an unreadable/symlinked mod folder scans empty and must not wipe tags."""
    if profile_dir is None or not mod_name or not valid_keys:
        return False
    tags = read_root_mod_files(profile_dir, None)
    cur = tags.get(mod_name)
    if not cur:
        return False
    kept = [k for k in cur if k in valid_keys]
    if len(kept) == len(cur):
        return False
    if kept:
        tags[mod_name] = kept
    else:
        tags.pop(mod_name, None)
    write_root_mod_files(profile_dir, tags)
    return True


def mod_has_changes(profile_dir: Path | None, mod_name: str | None) -> bool:
    """True when *mod_name* has any Mod Files tab state (strip / root / exclude)."""
    if profile_dir is None or not mod_name:
        return False
    try:
        return bool(read_mod_strip_prefixes(profile_dir, None).get(mod_name)
                    or read_root_mod_files(profile_dir, None).get(mod_name)
                    or read_excluded_mod_files(profile_dir, None).get(mod_name))
    except Exception:
        return False


def reset_mod_state(profile_dir: Path | None, mod_name: str | None) -> bool:
    """Drop every Mod Files edit for *mod_name*; True if anything was cleared."""
    if profile_dir is None or not mod_name:
        return False
    changed = False
    for reader, writer in ((read_mod_strip_prefixes, write_mod_strip_prefixes),
                           (read_root_mod_files, write_root_mod_files),
                           (read_excluded_mod_files, write_excluded_mod_files)):
        data = reader(profile_dir, None)
        if data.pop(mod_name, None):
            writer(profile_dir, data)
            changed = True
    return changed


def read_strip_prefixes(profile_dir: Path, mod_name: str) -> set[str]:
    """Saved strip-prefix entries (lowercased) for *mod_name*."""
    if profile_dir is None:
        return set()
    return {e.lower() for e in read_mod_strip_prefixes(profile_dir, None).get(mod_name, [])
            if e}
