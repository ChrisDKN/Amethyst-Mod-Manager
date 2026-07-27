"""
bsa_filemap.py
Archive conflict detection — index cache and conflict engine.

Scans archive files (Bethesda BSA/BA2, Unreal Engine .pak/.utoc) across
enabled mods, caches the contained file lists in bsa_index.bin (msgpack),
and computes archive-vs-archive conflicts using the same priority-merge
algorithm as the loose-file filemap builder. The archive formats a game
uses come from ``game.archive_extensions``; UE archives are scanned
recursively (mods stage paks in nested folders like Content/Paks/~mods/)
while BSAs keep the top-level-only scan.

Cache format — msgpack binary, v1:
    {
        "v": 1,
        "mods": [
            [mod_name, [
                [bsa_filename, mtime_float, [file_path, ...]],
                ...
            ]],
            ...
        ]
    }

File paths stored in the cache are lowercase, forward-slash separated.
"""

from __future__ import annotations

import os
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import msgpack

if TYPE_CHECKING:
    from typing import Callable, Mapping

from Utils.atomic_write import atomic_writer
from Utils.bsa_reader import read_bsa_file_list
from Utils.ue_pak_reader import UE_ARCHIVE_EXTENSIONS, read_ue_archive_file_list
from Utils.filemap import (
    CONFLICT_NONE,
    _compute_conflict_status,
    read_mod_index,
)
from Utils.modlist import read_modlist

_BSA_INDEX_VERSION = 1

# Thread pool for parallel BSA scanning. BSA parsing is largely CPU-bound
# (struct unpacking under the GIL), so more than ~4 workers adds context-
# switch overhead without proportional throughput gains.
_POOL = ThreadPoolExecutor(max_workers=4)

# In-memory cache: (path_str, mtime) → parsed index
_BsaIndex = dict[str, list[tuple[str, float, list[str]]]]
_bsa_cache: tuple[str, float, _BsaIndex] | None = None
_bsa_cache_lock = threading.Lock()

# Sentinel: index file existed but could not be parsed (distinct from "never
# existed"). Callers that mutate the index treat this as "do not overwrite".
_BSA_INDEX_CORRUPT = object()


# ---------------------------------------------------------------------------
# Index read / write
# ---------------------------------------------------------------------------

def _load_bsa_index(index_path: Path):
    """Internal: load and return the parsed index, or a sentinel.

    Returns:
        dict      — parsed index.
        None      — file does not exist.
        _BSA_INDEX_CORRUPT — file exists but is unparseable / wrong version.
    """
    global _bsa_cache
    path_str = str(index_path)
    with _bsa_cache_lock:
        try:
            mtime = index_path.stat().st_mtime
        except OSError:
            return None
        if _bsa_cache is not None and _bsa_cache[0] == path_str and _bsa_cache[1] == mtime:
            return _bsa_cache[2]
    try:
        with index_path.open("rb") as f:
            data = msgpack.unpack(f, raw=False)
        if not isinstance(data, dict) or data.get("v") != _BSA_INDEX_VERSION:
            return _BSA_INDEX_CORRUPT
        index: _BsaIndex = {}
        for mod_name, archives in data["mods"]:
            entries: list[tuple[str, float, list[str]]] = []
            for bsa_name, mt, paths in archives:
                entries.append((bsa_name, float(mt), paths))
            index[mod_name] = entries
    except Exception:
        return _BSA_INDEX_CORRUPT
    with _bsa_cache_lock:
        _bsa_cache = (path_str, mtime, index)
    return index


def read_bsa_index(
    index_path: Path,
) -> _BsaIndex | None:
    """Read bsa_index.bin and return {mod_name: [(bsa_filename, mtime, [paths])]}.

    Returns None if the index does not exist or has an unrecognised version.
    Results are cached in memory by (path, mtime).
    """
    result = _load_bsa_index(index_path)
    if result is None or result is _BSA_INDEX_CORRUPT:
        return None
    return result


def _write_bsa_index(index_path: Path, index: _BsaIndex) -> None:
    """Write bsa_index.bin atomically and update the in-memory cache."""
    global _bsa_cache
    mods = []
    for mod_name, archives in index.items():
        entries = [[bsa_name, mt, paths] for bsa_name, mt, paths in archives]
        mods.append([mod_name, entries])
    payload = {"v": _BSA_INDEX_VERSION, "mods": mods}
    with atomic_writer(index_path, "wb", encoding=None) as f:
        msgpack.pack(payload, f, use_bin_type=True)
    with _bsa_cache_lock:
        try:
            mtime = index_path.stat().st_mtime
            _bsa_cache = (str(index_path), mtime, index)
        except OSError:
            _bsa_cache = None


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _read_archive_file_list(path: str, ext: str) -> list[str]:
    """Dispatch to the right TOC parser for the archive extension."""
    if ext in UE_ARCHIVE_EXTENSIONS:
        return read_ue_archive_file_list(path)
    return read_bsa_file_list(path)


def _scan_mod_bsas(
    mod_name: str,
    mod_dir: str,
    archive_extensions: frozenset[str],
    cached_archives: dict[str, tuple[str, float, list[str]]] | None = None,
) -> tuple[str, list[tuple[str, float, list[str]]], int]:
    """Scan a single mod directory for archive files and parse their TOCs.

    Bethesda archives load from the Data folder root, so BSA/BA2 games scan
    only the mod folder's top level. UE pak/utoc mods routinely nest their
    archives (Content/Paks/~mods/foo.pak), so those are scanned recursively;
    the archive key becomes the mod-relative path instead of the bare name.

    If ``cached_archives`` is provided (keyed by that archive key), any
    archive whose mtime matches the cache is returned directly from cache
    without parsing.

    Returns (mod_name, [(archive_key, mtime, [file_paths])], parse_count)
    where ``parse_count`` is the number of archives actually parsed (cache
    misses). Thread-safe — no shared mutable state.
    """
    results: list[tuple[str, float, list[str]]] = []
    parse_count = 0
    recursive = bool(archive_extensions & UE_ARCHIVE_EXTENSIONS)
    candidates: list[tuple[str, str, str, float]] = []  # (key, path, ext, mtime)
    try:
        if recursive:
            for root, dirnames, filenames in os.walk(mod_dir, followlinks=False):
                # FOMOD metadata never deploys — don't index archives in it.
                dirnames[:] = [d for d in dirnames if d.lower() != "fomod"]
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext not in archive_extensions:
                        continue
                    full = os.path.join(root, fn)
                    try:
                        mtime = os.stat(full).st_mtime
                    except OSError:
                        continue
                    rel = os.path.relpath(full, mod_dir).replace(os.sep, "/")
                    candidates.append((rel, full, ext, mtime))
        else:
            with os.scandir(mod_dir) as it:
                for entry in it:
                    try:
                        # An installed mod has no legitimate reason to
                        # contain a symlink at all, and archive extraction
                        # doesn't sanitize embedded symlinks — never follow
                        # one here. (Profile Groups used to need an
                        # exception for their own staged symlinks; the
                        # virtual-merge redesign removed that staging
                        # entirely, so this stays closed with no exceptions.)
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        ext = os.path.splitext(entry.name)[1].lower()
                        if ext in archive_extensions:
                            # Reuses the dirent's cached lstat — no extra
                            # syscall (is_file above already fetched it).
                            mtime = entry.stat(follow_symlinks=False).st_mtime
                            candidates.append((entry.name, entry.path, ext, mtime))
                    except OSError:
                        continue
    except OSError:
        return (mod_name, results, parse_count)
    for key, full_path, ext, mtime in candidates:
        # Cache check happens *before* parsing — a match skips the
        # expensive TOC read entirely.
        if cached_archives is not None:
            cached = cached_archives.get(key)
            if cached is not None and cached[1] == mtime:
                results.append(cached)
                continue
        paths = _read_archive_file_list(full_path, ext)
        parse_count += 1
        if paths:
            results.append((key, mtime, paths))
    return (mod_name, results, parse_count)


def rebuild_bsa_index(
    index_path: Path,
    staging_root: "Path | Mapping[str, Path]",
    archive_extensions: frozenset[str],
    log_fn: "Callable[[str], None] | None" = None,
) -> None:
    """Scan all mod folders for BSA files and write bsa_index.bin.

    Uses the existing BSA index for incremental updates: only re-parses
    BSAs whose mtime has changed since the last scan.

    staging_root is a plain Path for a regular profile (scanned directly) or
    a {mod_name: real_mod_dir} mapping for a Profile Group (each mod's
    location is already known — see resolve_mod_dir in deploy_shared.py).
    """
    # Read existing index for mtime-based cache reuse in the scanner.
    old_index = read_bsa_index(index_path) or {}
    old_archive_map: dict[str, dict[str, tuple[str, float, list[str]]]] = {}
    for mod_name, archives in old_index.items():
        old_archive_map[mod_name] = {a[0]: a for a in archives}

    # Collect mod directories to scan
    mod_dirs: list[tuple[str, str]] = []
    if isinstance(staging_root, Path):
        if not staging_root.is_dir():
            return
        try:
            with os.scandir(str(staging_root)) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        mod_dirs.append((entry.name, entry.path))
        except OSError:
            return
    else:
        mod_dirs = [
            (name, str(mod_dir)) for name, mod_dir in staging_root.items()
            if mod_dir.is_dir()
        ]

    # Submit parallel scans, each with its mod's cached archives for mtime reuse.
    futures = []
    for mod_name, mod_path in mod_dirs:
        cached = old_archive_map.get(mod_name)
        futures.append(_POOL.submit(
            _scan_mod_bsas, mod_name, mod_path, archive_extensions, cached,
        ))

    index: _BsaIndex = {}
    total_bsa = 0
    total_files = 0
    total_parsed = 0
    for future in futures:
        mod_name, archives, parsed = future.result()
        total_parsed += parsed
        if archives:
            index[mod_name] = archives
            total_bsa += len(archives)
            for _bsa, _mt, paths in archives:
                total_files += len(paths)

    _write_bsa_index(index_path, index)
    if log_fn:
        log_fn(
            f"BSA index: {total_bsa} archive(s), {total_files} file(s) "
            f"across {len(index)} mod(s) — parsed {total_parsed}, "
            f"reused {total_bsa - total_parsed} from cache."
        )


def update_bsa_index(
    index_path: Path,
    mod_name: str,
    mod_dir: Path | str,
    archive_extensions: frozenset[str],
) -> None:
    """Add or replace a single mod's BSA entries in the index.

    Call this after installing a mod. If the index file exists but fails
    to parse, this is a no-op (better to leave a stale full-rebuild for
    the modlist panel than to wipe every other mod's cached entries).
    """
    loaded = _load_bsa_index(index_path)
    if loaded is _BSA_INDEX_CORRUPT:
        return
    index = loaded if isinstance(loaded, dict) else {}
    _, archives, _ = _scan_mod_bsas(mod_name, str(mod_dir), archive_extensions)
    if archives:
        index[mod_name] = archives
    else:
        index.pop(mod_name, None)
    _write_bsa_index(index_path, index)


def remove_from_bsa_index(
    index_path: Path,
    mod_names: list[str] | str,
) -> None:
    """Remove one or more mods' BSA entries from the index.

    Call this after removing mod folders from staging.
    No-op if the index does not exist, is corrupt, or the mod is not in it.
    """
    if isinstance(mod_names, str):
        mod_names = [mod_names]
    loaded = _load_bsa_index(index_path)
    if loaded is None or loaded is _BSA_INDEX_CORRUPT:
        return
    index = loaded
    if not index:
        return
    changed = False
    for name in mod_names:
        if name in index:
            del index[name]
            changed = True
    if changed:
        _write_bsa_index(index_path, index)


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

def _bsa_owning_plugin(
    bsa_basename: str,
    plugin_basenames: set[str],
) -> str | None:
    """Return the plugin basename (no ext, lowercase) that loads this BSA, or None.

    Skyrim/Fallout load a BSA only if a plugin with a matching basename exists
    in the same Data folder. Matching rules:
      * Exact:  'RaceMenu.bsa'                          ↔ 'RaceMenu.esp'
      * Suffix: 'RaceMenu - Textures.bsa'               ↔ 'RaceMenu.esp'
      * Suffix: 'VividFallout - AiO - BC - Main.ba2'    ↔ 'VividFallout - AiO - BC.esp'
    plugin_basenames is lowercased, without extension.

    Walks ' - ' boundaries from the right so a plugin like 'A - B - C' wins
    over a shorter false match 'A' when both exist.
    """
    if not plugin_basenames:
        return None
    name = bsa_basename.lower()
    if name in plugin_basenames:
        return name
    # Suffix form: "<plugin> - <suffix-tokens>". Try the longest possible
    # plugin stem first (rightmost ' - ') and shrink leftward.
    end = len(name)
    while True:
        dash = name.rfind(" - ", 0, end)
        if dash <= 0:
            return None
        stem = name[:dash]
        if stem in plugin_basenames:
            return stem
        end = dash


def _pak_name_rank(archive_key: str) -> tuple[int, str]:
    """UE pak mount rank component: (patch_flag, basename).

    The engine mounts paks alphabetically and the most-recently-mounted pak
    wins conflicts, so a later basename beats an earlier one ("zzz_" beats
    "aaa_"). A stem ending in ``_P`` additionally gets a mount-priority
    boost, so any ``_P`` pak beats any non-``_P`` pak regardless of name.
    """
    base = archive_key.rsplit("/", 1)[-1].lower()
    stem = base.rsplit(".", 1)[0]
    return (1 if stem.endswith("_p") else 0, base)


def _compute_bsa_load_order(
    index: _BsaIndex,
    mods_low_to_high: list[str],
    plugin_order_low_to_high: list[str] | None,
    plugin_extensions: frozenset[str] | None,
    loose_index_path: Path | None,
    archive_name_ordering: bool = False,
) -> list[tuple[str, list[tuple[str, float, list[str]]]]]:
    """Return BSA scan units ordered low→high by engine load rank.

    ``archive_name_ordering`` selects the UE pak mount rule: rank by
    (``_P`` boost, basename alphabetical), matching how the engine itself
    resolves two paks shipping the same asset — mod priority only breaks
    exact-basename ties (where the deploy step dedupes to the higher-
    priority mod's copy, so it is also what actually lands on disk).

    Each element is (mod_name, [(bsa_name, mtime, paths)...]). A mod may
    appear multiple times — once per BSA — so different BSAs from the same
    mod can interleave with BSAs from other mods by plugin load order.

    Rank rules (Bethesda):
      * A BSA tied to a plugin uses that plugin's load index (higher = later).
      * A BSA with no matching plugin (orphan / sArchiveList-style) loads
        *before* any plugin-tied BSA — that matches how the engine loads
        sArchiveList entries first and then plugin-tied archives. Within
        the orphan group, ties break on mod priority then BSA filename.
    """
    if archive_name_ordering:
        mod_priority = {name: i for i, name in enumerate(mods_low_to_high)}
        units_ue: list[tuple[int, str, int, str, list]] = []
        for mod_name in mods_low_to_high:
            archives = index.get(mod_name)
            if not archives:
                continue
            mp = mod_priority[mod_name]
            for entry in archives:
                p_flag, base = _pak_name_rank(entry[0])
                units_ue.append((p_flag, base, mp, mod_name, [entry]))
        units_ue.sort(key=lambda u: (u[0], u[1], u[2]))
        return [(u[3], u[4]) for u in units_ue]

    if not plugin_order_low_to_high or not plugin_extensions:
        # No plugin load order available — fall back to pure mod order.
        return [(m, index.get(m) or []) for m in mods_low_to_high if index.get(m)]

    # Map plugin basename (lowercase, no ext) → load rank. Engine behavior:
    # the *last* occurrence in load order decides which rank a basename gets,
    # since a later plugin overrides an earlier one with the same stem.
    plugin_rank: dict[str, int] = {}
    exts_lower = {e.lower() for e in plugin_extensions}
    for rank, pname in enumerate(plugin_order_low_to_high):
        stem, _, ext = pname.rpartition(".")
        if not stem or f".{ext.lower()}" not in exts_lower:
            continue
        plugin_rank[stem.lower()] = rank

    # Map mod → set of plugin basenames it ships (lowercased, no ext).
    #
    # Only *loose* plugins count here: Skyrim/Fallout do not load plugins that
    # live inside a BSA, so a mod whose only .esp is archived will correctly
    # have no tied plugins and all its BSAs will be treated as orphans.
    # Root-deployed files are excluded because they sit outside Data/ and so
    # cannot possibly be the plugin that activates a sibling BSA.
    mod_plugins: dict[str, set[str]] = {}
    if loose_index_path is not None:
        loose_index = read_mod_index(loose_index_path)
        if loose_index:
            for mod_name in mods_low_to_high:
                entry = loose_index.get(mod_name)
                if not entry:
                    continue
                normal, _ = entry
                bases: set[str] = set()
                for rel_key in normal:
                    # rel_key is lowercase with forward slashes
                    fname = rel_key.rsplit("/", 1)[-1]
                    dot = fname.rfind(".")
                    if dot < 0:
                        continue
                    if fname[dot:] in exts_lower:
                        bases.add(fname[:dot])
                if bases:
                    mod_plugins[mod_name] = bases

    mod_priority = {name: i for i, name in enumerate(mods_low_to_high)}

    units: list[tuple[int, int, int, str, str, list[tuple[str, float, list[str]]]]] = []
    # Tuple: (group, primary_rank, mod_priority, bsa_filename, mod_name, [bsa_entry])
    #   group: 0 = orphan (loads first, overridden by plugin-tied BSAs),
    #          1 = plugin-tied
    for mod_name in mods_low_to_high:
        archives = index.get(mod_name)
        if not archives:
            continue
        own_plugins = mod_plugins.get(mod_name, set())
        mp = mod_priority[mod_name]
        for bsa_entry in archives:
            bsa_name = bsa_entry[0]
            stem = bsa_name.rsplit(".", 1)[0]
            owning = _bsa_owning_plugin(stem, own_plugins)
            if owning is not None and owning in plugin_rank:
                units.append((1, plugin_rank[owning], mp, bsa_name.lower(), mod_name, [bsa_entry]))
            else:
                units.append((0, mp, mp, bsa_name.lower(), mod_name, [bsa_entry]))

    units.sort(key=lambda u: (u[0], u[1], u[2], u[3]))
    return [(u[4], u[5]) for u in units]


def compute_bsa_winner_map(
    index: _BsaIndex,
    priority_low_to_high: list[str],
    plugin_order_low_to_high: list[str] | None,
    plugin_extensions: frozenset[str] | None,
    loose_index_path: Path | None,
    archive_name_ordering: bool = False,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return (bsa_winner, bsa_losers) by replaying the engine load order.

    This is the shared helper used by the modlist dot indicator, the Archive
    tab, and the per-mod conflicts dialog so they all agree on who wins
    which BSA path.

    * bsa_winner: file_path → winning mod_name
    * bsa_losers: file_path → [losing mod_name, ...] (in load order)
    """
    scan_order = _compute_bsa_load_order(
        index, priority_low_to_high,
        plugin_order_low_to_high, plugin_extensions,
        loose_index_path, archive_name_ordering,
    )
    bsa_winner: dict[str, str] = {}
    seen_by_path: dict[str, list[str]] = {}
    for name, mod_archives in scan_order:
        if not mod_archives:
            continue
        for _bsa, _mt, paths in mod_archives:
            for fp in paths:
                lst = seen_by_path.get(fp)
                if lst is None:
                    seen_by_path[fp] = [name]
                elif lst[-1] != name:
                    lst.append(name)
                bsa_winner[fp] = name
    bsa_losers: dict[str, list[str]] = {
        fp: [m for m in mods if m != bsa_winner[fp]]
        for fp, mods in seen_by_path.items()
        if len(mods) > 1
    }
    return bsa_winner, bsa_losers


def build_bsa_conflicts(
    modlist_path: Path,
    index_path: Path,
    archive_extensions: frozenset[str],
    loose_index_path: Path | None = None,
    plugin_order: list[str] | None = None,
    plugin_extensions: frozenset[str] | None = None,
    archive_name_ordering: bool = False,
    log_fn: "Callable[[str], None] | None" = None,
) -> tuple[
    dict[str, int],
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, set[str]],
]:
    """Compute BSA archive conflicts.

    Walks enabled mods' BSAs in engine load order (derived from plugin
    load order when available; otherwise mod priority). For each file
    path, the later-loaded BSA wins — matching how Skyrim/Fallout resolve
    BSA contents.

    plugin_order is the full plugin load order (typically from
    loadorder.txt), given low→high. Only plugin names actually tied to
    a BSA via basename match contribute to ranking; unmatched BSAs fall
    back to mod priority.

    When loose_index_path is provided, the *winning* loose-file mod at any
    BSA path always overrides that BSA — regardless of load order — because
    the Skyrim/Fallout engines load BSAs first and then apply loose files on
    top. The BSA mod is recorded as losing via the BSA conflict flag. The
    winning loose-file mod's override is returned separately so the caller
    can fold it into the loose-file conflict flag system.

    Returns
        (bsa_conflict_map, bsa_overrides, bsa_overridden_by,
         loose_overrides_bsa, loose_overridden_by_bsa)
    where the last two dicts map loose_mod → {bsa_mod, ...} and
    bsa_mod → {loose_mod, ...} respectively.
    """
    entries = read_modlist(modlist_path)
    enabled = [e for e in entries if not e.is_separator and e.enabled]
    enabled_low_to_high = list(reversed(enabled))

    priority_order = [e.name for e in enabled_low_to_high]

    index = read_bsa_index(index_path)
    if index is None:
        # No index — return empty results. Use defaultdicts so _compute_conflict_status
        # can still key into them without KeyErrors.
        empty_map = {name: CONFLICT_NONE for name in priority_order}
        empty_a: dict[str, set[str]] = defaultdict(set)
        empty_b: dict[str, set[str]] = defaultdict(set)
        return empty_map, empty_a, empty_b, {}, {}

    scan_order = _compute_bsa_load_order(
        index, priority_order, plugin_order, plugin_extensions,
        loose_index_path, archive_name_ordering,
    )

    # Single-pass merge in engine load order (low → high). Using defaultdicts
    # avoids pre-allocating empty sets for every mod on huge profiles where
    # only a fraction ship BSAs.
    bsa_winner: dict[str, str] = {}  # file_path → mod_name
    overrides:     dict[str, set[str]] = defaultdict(set)
    overridden_by: dict[str, set[str]] = defaultdict(set)
    win_count: dict[str, int] = {}
    mods_with_files: set[str] = set()

    for name, mod_archives in scan_order:
        if not mod_archives:
            continue
        had_file = False
        for _bsa_name, _mtime, paths in mod_archives:
            for file_path in paths:
                had_file = True
                prev = bsa_winner.get(file_path)
                if prev is not None and prev != name:
                    win_count[prev] = win_count.get(prev, 0) - 1
                    overrides[name].add(prev)
                    overridden_by[prev].add(name)
                if prev != name:
                    win_count[name] = win_count.get(name, 0) + 1
                bsa_winner[file_path] = name
        if had_file:
            mods_with_files.add(name)

    # Cross-compare against loose files: the winning loose-file mod at a BSA
    # path always overrides the BSA. A loose file that loses its own loose
    # fight never reaches the game and shouldn't be marked as overriding a BSA.
    loose_overrides_bsa: dict[str, set[str]] = {}
    loose_overridden_by_bsa: dict[str, set[str]] = {}
    # "Loose beats archive" is a Bethesda engine rule; UE games (name
    # ordering) never load loose assets over pak contents, so skip the
    # cross-check there — a loose file whose staged path happens to match a
    # pak-internal asset path must not fake an override.
    if loose_index_path is not None and bsa_winner and not archive_name_ordering:
        loose_index = read_mod_index(loose_index_path)
        if loose_index:
            # Only paths the BSAs actually ship can possibly be overridden by
            # loose files, so scope loose_winner to that set instead of walking
            # every mod's entire loose file list. Root-deployed files (`root`
            # dict) live outside Data/ and cannot conflict with a Data-side BSA
            # entry, so we only consult the `normal` dict.
            bsa_paths = set(bsa_winner)
            loose_winner: dict[str, str] = {}
            for mod_name in priority_order:
                entry = loose_index.get(mod_name)
                if not entry:
                    continue
                normal, _ = entry
                # Iterate whichever side is smaller — typically bsa_paths is
                # smaller than a mod with thousands of loose files.
                if len(normal) < len(bsa_paths):
                    for rel_key in normal:
                        if rel_key in bsa_paths:
                            loose_winner[rel_key] = mod_name
                else:
                    for rel_key in bsa_paths:
                        if rel_key in normal:
                            loose_winner[rel_key] = mod_name
            for file_path, bsa_mod in bsa_winner.items():
                loose_mod = loose_winner.get(file_path)
                if loose_mod is None or loose_mod == bsa_mod:
                    continue
                # BSA side: record the loss via the BSA flag.
                win_count[bsa_mod] = win_count.get(bsa_mod, 0) - 1
                overridden_by[bsa_mod].add(loose_mod)
                # Loose side: returned separately so the caller can merge it
                # into the loose-file conflict flag.
                loose_overrides_bsa.setdefault(loose_mod, set()).add(bsa_mod)
                loose_overridden_by_bsa.setdefault(bsa_mod, set()).add(loose_mod)

    conflict_map = _compute_conflict_status(
        priority_order, overrides, overridden_by, win_count, mods_with_files,
    )

    return (
        conflict_map,
        overrides,
        overridden_by,
        loose_overrides_bsa,
        loose_overridden_by_bsa,
    )
