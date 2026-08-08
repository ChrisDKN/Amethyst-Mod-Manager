"""
filemap.py
Build and write a filemap.txt that resolves mod file conflicts.

Algorithm: walk enabled mods from lowest priority to highest priority.
For each file, record (relative_path, source_mod). Higher-priority mods
overwrite lower-priority entries - no conflicts remain in the output.

Format (one line per file):
    <relative/path/to/file>\t<mod_name>

Paths are stored in their original case but deduplicated case-insensitively
so that Windows-style case-insensitive conflicts are handled correctly.

Mod Index
---------
modindex.bin lives next to filemap.txt and caches the file list of every
mod so that build_filemap() can skip the expensive disk scan on every
enable/disable/reorder.  The index is only updated when mods are installed
or removed (or when the user hits the Refresh button).

Index format - msgpack binary, v4:
    {"v": 4, "mods": [[mod_name, [[rel_key, rel_str, kind], ...]], ...]}
where <kind> is "n" (normal) or "r" (unused legacy, kept for format compatibility).
Paths stored in the index reflect the raw on-disk casing of each mod's files.
build_filemap() normalizes folder-case across mods when assembling the merged
filemap output, but the index itself stays a faithful mirror of disk so that
deploy can construct correct source paths regardless of cross-mod casing.
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
import time
from bisect import bisect_left
from collections import Counter
from concurrent.futures import (
    ThreadPoolExecutor, as_completed,
    TimeoutError as _FuturesTimeoutError,  # builtin alias on 3.11+, distinct before
)
import threading
from pathlib import Path
from functools import lru_cache
from typing import TYPE_CHECKING

import msgpack

if TYPE_CHECKING:
    from typing import Callable

from Utils.atomic_write import atomic_writer, write_atomic_text
from Utils.modlist import read_modlist
from Utils import perftrace

# Conflict status constants (returned per-mod in build_filemap result)
CONFLICT_NONE    = 0   # no conflicts at all
CONFLICT_WINS    = 1   # wins some/all conflicts, loses none (green dot)
CONFLICT_LOSES   = 2   # loses some conflicts, wins none (red dot)
CONFLICT_PARTIAL = 3   # wins some, loses some (yellow dot)
CONFLICT_FULL    = 4   # all files overridden - nothing reaches the game (white dot)

# Sentinel name used in filemap.txt and conflict dicts for the overwrite folder
OVERWRITE_NAME   = "[Overwrite]"

# Sentinel name for the root folder - files deploy to the game root, not mod data path
ROOT_FOLDER_NAME = "[Root_Folder]"

# Not real game files: MO2 meta.ini, the manager's restore-sweep log
# (deploy_shared.OVERWRITE_LOG_NAME), and the Script Merger inventory
# snapshot (script_merger_inventory.SNAPSHOT_NAME) - must never reach
# the filemap.
_EXCLUDE_NAMES = frozenset({"meta.ini", ".mm_overwrite_log.txt",
                            ".mm_merge_inventory.xml"})


def _is_macos_junk(name: str) -> bool:
    """True for macOS metadata that should never deploy into a game folder.

    AppleDouble sidecars (``._<name>``) and ``.DS_Store`` ride along in mods
    zipped on macOS. They are not mod content; deploying them bloats the game
    folder and breaks tools that enumerate directories (e.g. Alternative
    Textures' GetDirectories scan reads ``._Foo`` as a texture folder).
    ``__MACOSX`` is the archive-level container for the same junk.
    """
    return (
        name.startswith("._")
        or name == ".DS_Store"
        or name == "__MACOSX"
    )

# Reuse a modest thread pool across calls rather than creating one per call
_POOL = ThreadPoolExecutor(max_workers=20)

# Overall deadline for one index scan (rebuild/rescan). A hung mount (dead
# FUSE/network share, dying SD card) blocks a _scan_dir worker in
# uninterruptible I/O; without a deadline the rebuild never returns, the
# index is never written, and the caller's build lock stays held forever.
# scandir-only walks finish in seconds even on large libraries, so a scan
# that exceeds this is stuck, not slow.
_SCAN_TIMEOUT = 300.0

_INDEX_VERSION = 4

# In-memory cache: path_str → ((mtime_ns, size), parsed index)
# Avoids re-parsing the ~5 MB index file on every filemap rebuild. Small
# multi-entry dict (insertion-ordered, oldest evicted past the cap) so
# alternating between two profiles' index paths doesn't thrash re-parses.
_IndexCache = dict[str, tuple[dict[str, str], dict[str, str]]]
_INDEX_CACHE_MAX = 4
_index_cache: dict[str, tuple[tuple, _IndexCache]] = {}
_index_cache_lock = threading.Lock()


def _index_cache_store(path_str: str, stat_key: tuple, index) -> None:
    """Insert/refresh a cache entry, evicting the oldest past the cap.

    Caller must hold _index_cache_lock."""
    _index_cache.pop(path_str, None)
    _index_cache[path_str] = (stat_key, index)
    while len(_index_cache) > _INDEX_CACHE_MAX:
        _index_cache.pop(next(iter(_index_cache)))

# Serializes every read-modify-write of modindex.bin across threads.
# update/remove/rename/rescan all read the index, mutate, and write it back;
# without this, two writers on different threads (e.g. an install finishing
# during a Refresh rebuild) interleave and one's changes are lost - or their
# temp files collide and a half-written index gets renamed into place.
# RLock because the RMW helpers hold it while calling _write_mod_index,
# which takes it too.
_index_write_lock = threading.RLock()


def _index_stat_key(index_path: Path):
    """(st_mtime_ns, st_size) - cache-validity key for modindex.bin.

    A bare float mtime is too coarse on FAT/exFAT SD cards (2 s resolution):
    a rewrite by another process inside the same tick would serve stale cache.
    Size + ns-mtime makes an unnoticed change vanishingly unlikely."""
    st = index_path.stat()
    return (st.st_mtime_ns, st.st_size)

# Per-output-path cache of the last filemap_winner dict.
# If the new winner dict is identical, we skip the file write entirely.
# Maps output_path_str → frozenset of (rel_key, mod_name) pairs.
_filemap_winner_cache: dict[str, frozenset] = {}
_filemap_winner_cache_lock = threading.Lock()

# Cache for the lowercase-set form of `disabled_plugins`, keyed by a
# fingerprint of the dict's ACTUAL contents (mod names + their plugin names).
# id()-based keying was unsound: id reuse after GC, or swapping WHICH plugin
# is disabled while list lengths stay equal, served a stale set. Skips
# re-lowercasing on every filemap rebuild when the data hasn't changed.
_disabled_lower_cache: tuple[tuple, dict[str, frozenset[str]]] | None = None
_disabled_lower_cache_lock = threading.Lock()


def _get_disabled_lower(disabled_plugins: dict[str, list[str]]) -> dict[str, frozenset[str]]:
    global _disabled_lower_cache
    dp_fp = tuple(sorted((m, tuple(sorted(n)))
                         for m, n in disabled_plugins.items()))
    with _disabled_lower_cache_lock:
        cached = _disabled_lower_cache
        if cached is not None and cached[0] == dp_fp:
            return cached[1]
    built = {
        mod: frozenset(n.lower() for n in names)
        for mod, names in disabled_plugins.items()
    }
    with _disabled_lower_cache_lock:
        _disabled_lower_cache = (dp_fp, built)
    return built


# ---------------------------------------------------------------------------
# Incremental filemap state
# ---------------------------------------------------------------------------

class _IncrementalState:
    """Snapshot of one build_filemap() output, ready for delta application."""
    __slots__ = (
        "lock", "fingerprint", "index_path", "index_mtime",
        "prev_priority_order",
        "providers", "providers_root", "contested", "contested_root",
        "files_count", "pair_counts", "losses",
        "filemap", "sorted_keys", "lines",
        "filemap_root", "sorted_keys_root", "lines_root",
        "dirty", "last_disabled_frozen",
        "casing_strategy", "dir_refcount", "ctx_variants", "canonical",
        "dir_rewrite", "casing_ties_present", "casing_pins",
    )

    def __init__(self):
        self.lock = threading.Lock()
        self.fingerprint: tuple = ()
        self.index_path = ""
        self.index_mtime = 0.0
        self.prev_priority_order: list[str] = []
        # rel_key → provider mod (plain str, common case) or list of providers
        # in low→high processing order (= priority order at population time).
        self.providers: dict[str, "str | list[str]"] = {}
        self.providers_root: dict[str, "str | list[str]"] = {}
        self.contested: set[str] = set()
        self.contested_root: set[str] = set()
        self.files_count: dict[str, int] = {}
        # Conflict-relation refcounts over contested paths:
        #   pair_counts[(lo, hi)] = #paths where hi directly beats lo
        #   losses[mod]           = #paths where mod is beaten by its direct
        #                           successor (== sum of its lo-side pairs)
        # Maintained per-touched-path on toggles; rebuilt by a full sweep on
        # reorders (relative provider order changes globally there).
        self.pair_counts: dict[tuple[str, str], int] = {}
        self.losses: dict[str, int] = {}
        # Persistent write cache (lines are unfiltered by disabled_plugins).
        self.filemap: dict[str, tuple[str, str]] = {}
        self.sorted_keys: list[str] = []
        self.lines: list[str] = []
        self.filemap_root: dict[str, tuple[str, str]] = {}
        self.sorted_keys_root: list[str] = []
        self.lines_root: list[str] = []
        self.dirty = False
        self.last_disabled_frozen: frozenset = frozenset()
        # Casing bookkeeping (upper/lower strategies only; None = not tracked).
        self.casing_strategy: "str | None" = None
        self.dir_refcount: Counter = Counter()          # raw parent dir → #winning entries
        self.ctx_variants: dict[tuple[str, str], Counter] = {}  # ctx → variant → #dirs
        self.canonical: dict[tuple[str, str], str] = {}
        self.dir_rewrite: dict[str, "str | None"] = {}
        self.casing_ties_present = False
        # Per-folder casing pins (lowercase segment → exact casing) applied
        # after canonical normalization; empty = no pins.  See _pin_rel_str.
        self.casing_pins: dict[str, str] = {}

    def clone(self) -> "_IncrementalState":
        """Deep-enough copy for a dry-run delta (verify mode).

        deepcopy() would choke on the lock; every mutable container the delta
        touches is copied, immutable values are shared.
        """
        st = _IncrementalState()
        st.fingerprint = self.fingerprint
        st.index_path = self.index_path
        st.index_mtime = self.index_mtime
        st.prev_priority_order = list(self.prev_priority_order)
        st.providers = {
            k: (v if type(v) is str else list(v))
            for k, v in self.providers.items()
        }
        st.providers_root = {
            k: (v if type(v) is str else list(v))
            for k, v in self.providers_root.items()
        }
        st.contested = set(self.contested)
        st.contested_root = set(self.contested_root)
        st.files_count = dict(self.files_count)
        st.pair_counts = dict(self.pair_counts)
        st.losses = dict(self.losses)
        st.filemap = dict(self.filemap)
        st.sorted_keys = list(self.sorted_keys)
        st.lines = list(self.lines)
        st.filemap_root = dict(self.filemap_root)
        st.sorted_keys_root = list(self.sorted_keys_root)
        st.lines_root = list(self.lines_root)
        st.dirty = self.dirty
        st.last_disabled_frozen = self.last_disabled_frozen
        st.casing_strategy = self.casing_strategy
        st.dir_refcount = Counter(self.dir_refcount)
        st.ctx_variants = {k: Counter(v) for k, v in self.ctx_variants.items()}
        st.canonical = dict(self.canonical)
        st.dir_rewrite = dict(self.dir_rewrite)
        st.casing_ties_present = self.casing_ties_present
        st.casing_pins = self.casing_pins  # immutable per build; share
        return st


_incr_states: dict[str, _IncrementalState] = {}   # str(output_path) → state
_incr_states_lock = threading.Lock()
_INCR_MAX_STATES = 2                              # LRU cap (per-profile states are ~25-30 MB)

# Debug counters (read by tests to assert fast vs full path was taken).
_incr_stats = {"fast": 0, "full": 0, "fallback": 0}


def _incr_note(msg: str) -> None:
    """Diagnostic line for incremental skip/fallback reasons.

    perftrace.mark drops sub-threshold durations, so print directly - these
    are rare and explain exactly why a rebuild took the slow path.
    """
    if perftrace.is_enabled():
        print(f"[PERF] {msg}", file=sys.stderr)


def _incremental_enabled() -> bool:
    """Kill switch for the incremental fast path.

    Enabled by default; set AMM_FILEMAP_INCREMENTAL=0 to force every rebuild
    down the full-merge path (e.g. when hunting a suspected conflict-data
    divergence - pair with AMM_FILEMAP_VERIFY=1 to compare both paths live).
    """
    return os.environ.get("AMM_FILEMAP_INCREMENTAL") != "0"


def _get_incr_state(output_key: str) -> "_IncrementalState | None":
    with _incr_states_lock:
        return _incr_states.get(output_key)


def _put_incr_state(output_key: str, state: _IncrementalState) -> None:
    with _incr_states_lock:
        _incr_states.pop(output_key, None)
        _incr_states[output_key] = state
        while len(_incr_states) > _INCR_MAX_STATES:
            _incr_states.pop(next(iter(_incr_states)))


def _drop_incr_state(output_key: str) -> None:
    with _incr_states_lock:
        _incr_states.pop(output_key, None)


def _drop_incr_states_under(profile_dir: str) -> None:
    """Drop all states whose output path lives under *profile_dir*."""
    with _incr_states_lock:
        for key in list(_incr_states):
            if key.startswith(profile_dir):
                del _incr_states[key]


def _build_incr_fingerprint(
    index_path: Path,
    index_mtime: float,
    modlist_path: Path,
    staging_root: Path,
    strip_prefixes,
    per_mod_strip_prefixes,
    allowed_extensions,
    exclude_dirs,
    conflict_ignore_filenames,
    conflict_ignore_foldernames,
    excluded_loose_filenames,
    allowed_top_level_folders,
    excluded_mod_files,
    normalize_folder_case: bool,
    filemap_casing: str,
    filemap_casing_pins,
    conflict_key_fn,
    root_folder_mods,
    root_mod_files,
    utf8_bad: frozenset,
) -> tuple:
    """Everything (besides the modlist itself) that the merge output depends on.

    A stored state is only reusable when this matches exactly; the modlist
    diff is handled separately by the delta logic. disabled_plugins is
    deliberately absent - it only affects the written file, tracked at write
    time via last_disabled_frozen.
    """
    return (
        str(index_path), index_mtime, str(modlist_path), str(staging_root),
        frozenset(strip_prefixes or ()),
        tuple(sorted(
            (m, tuple(sorted(v)))
            for m, v in (per_mod_strip_prefixes or {}).items()
        )),
        frozenset(allowed_extensions or ()),
        frozenset(exclude_dirs or ()),
        frozenset(conflict_ignore_filenames or ()),
        frozenset(conflict_ignore_foldernames or ()),
        frozenset(excluded_loose_filenames or ()),
        frozenset(allowed_top_level_folders or ()),
        tuple(sorted(
            (m, frozenset(v))
            for m, v in (excluded_mod_files or {}).items()
        )),
        normalize_folder_case, filemap_casing,
        tuple(sorted((filemap_casing_pins or {}).items())),
        conflict_key_fn is None,
        frozenset(root_folder_mods or ()),
        tuple(sorted(
            (m, frozenset(v))
            for m, v in (root_mod_files or {}).items()
        )),
        utf8_bad,
    )


def _build_ctx_variants(
    unique_dirs: "dict[str, None] | set[str]",
) -> dict[tuple[str, str], Counter]:
    """Count casing variants per folder-segment context across unique dirs.

    Same segment walk as _collect_canonical, but keeps ALL variants with the
    number of distinct dirs carrying each - the incremental path uses this to
    detect when adding/removing a mod's dirs would change a canonical pick.
    """
    ctx_variants: dict[tuple[str, str], Counter] = {}
    for dir_str in unique_dirs:
        parent = ""
        for seg in dir_str.split("/"):
            ctx_key = (parent, seg.lower())
            c = ctx_variants.get(ctx_key)
            if c is None:
                c = ctx_variants[ctx_key] = Counter()
            c[seg] += 1
            parent = parent + seg.lower() + "/"
    return ctx_variants


def _ctx_ties_present(
    ctx_variants: dict[tuple[str, str], Counter],
    strategy: str,
) -> bool:
    """True when any ctx has ≥2 distinct variants tied on the winning score.

    Tie-breaks in _collect_canonical are first-seen (fold-order dependent),
    which incremental history cannot reproduce - such profiles always take
    the full rebuild.
    """
    prefer_lower = strategy == FILEMAP_CASING_LOWER
    for counts in ctx_variants.values():
        if len(counts) < 2:
            continue
        scores = [_upper_count(v) for v in counts]
        best = min(scores) if prefer_lower else max(scores)
        if scores.count(best) > 1:
            return True
    return False


def _make_incr_state(
    fingerprint: tuple,
    index_path: Path,
    index_mtime: float,
    priority_order: list[str],
    providers, providers_root, contested, contested_root, files_count,
    pair_counts, losses,
    filemap, sorted_keys, lines,
    filemap_root, sorted_keys_root, lines_root,
    disabled_frozen: frozenset,
    casing_strategy, dir_refcount, ctx_variants, canonical, dir_rewrite,
    casing_ties: bool,
    casing_pins: dict[str, str],
) -> _IncrementalState:
    """Assemble a fresh _IncrementalState from full-merge intermediates."""
    st = _IncrementalState()
    st.fingerprint = fingerprint
    st.index_path = str(index_path)
    st.index_mtime = index_mtime
    st.prev_priority_order = list(priority_order)
    st.providers = providers
    st.providers_root = providers_root
    st.contested = contested
    st.contested_root = contested_root
    st.files_count = files_count
    st.pair_counts = pair_counts
    st.losses = losses
    st.filemap = filemap
    st.sorted_keys = sorted_keys
    st.lines = lines
    st.filemap_root = filemap_root
    st.sorted_keys_root = sorted_keys_root
    st.lines_root = lines_root
    st.last_disabled_frozen = disabled_frozen
    st.casing_strategy = casing_strategy
    st.dir_refcount = dir_refcount
    st.ctx_variants = ctx_variants
    st.canonical = canonical
    st.dir_rewrite = dir_rewrite
    st.casing_ties_present = casing_ties
    st.casing_pins = casing_pins
    return st


class _IncrFallback(Exception):
    """Internal: the incremental delta hit a case requiring a full rebuild."""


def _pairs_update(
    pair_counts: dict,
    losses: dict,
    stack: list,
    posmap: dict,
    sign: int,
) -> None:
    """Add (+1) or subtract (−1) one contested stack's consecutive pairs.

    Callers bracket every stack mutation with a −1/+1 pair so pair_counts and
    losses stay exact without re-sweeping all contested paths on each toggle.
    """
    ss = sorted(stack, key=posmap.__getitem__)
    prev = ss[0]
    for cur in ss[1:]:
        k = (prev, cur)
        n = pair_counts.get(k, 0) + sign
        if n:
            pair_counts[k] = n
        else:
            pair_counts.pop(k, None)
        n2 = losses.get(prev, 0) + sign
        if n2:
            losses[prev] = n2
        else:
            losses.pop(prev, None)
        prev = cur


def _casing_dir_added(
    st: _IncrementalState,
    dir_str: str,
    pick_changes: list,
) -> None:
    """Register one more winning entry whose raw parent dir is *dir_str*.

    Only a 0→1 refcount transition introduces the dir's casing variants.
    A variant that BEATS the current canonical pick replaces it and the ctx
    is appended to *pick_changes* - the caller then re-normalizes every
    entry under that ctx (they share a lowercase key prefix). Pick changes
    are deterministic (strictly better score); only score TIES fall back,
    because a fresh rebuild resolves those by fold order.
    """
    rc = st.dir_refcount
    cur = rc.get(dir_str, 0)
    rc[dir_str] = cur + 1
    if cur:
        return  # dir already known; variant sets unchanged
    strategy = st.casing_strategy or FILEMAP_CASING_UPPER
    parent = ""
    for seg in dir_str.split("/"):
        ctx = (parent, seg.lower())
        counts = st.ctx_variants.get(ctx)
        if counts is None:
            counts = st.ctx_variants[ctx] = Counter()
        n = counts.get(seg, 0)
        counts[seg] = n + 1
        if n == 0:
            pick = st.canonical.get(ctx)
            if pick is None:
                st.canonical[ctx] = seg
            elif seg != pick:
                if _upper_count(seg) == _upper_count(pick):
                    raise _IncrFallback(f"casing tie introduced at {ctx}")
                if _pick_canonical_segment(pick, seg, strategy) != pick:
                    # New variant wins - adopt it and re-normalize below.
                    st.canonical[ctx] = seg
                    st.dir_rewrite = {}  # memo computed under the old pick
                    pick_changes.append(ctx)
        parent = parent + seg.lower() + "/"


def _casing_dir_removed(
    st: _IncrementalState,
    dir_str: str,
    pick_changes: list,
) -> None:
    """Drop one winning entry whose raw parent dir was *dir_str*.

    Only a 1→0 refcount transition retires the dir's casing variants.
    When the retired variant WAS the canonical pick and other variants
    remain, the new best-scoring variant is adopted and the ctx appended to
    *pick_changes* for re-normalization (deterministic unless the remaining
    best scores tie - that still falls back, fold-order dependent).
    """
    rc = st.dir_refcount
    cur = rc.get(dir_str, 0)
    if cur <= 0:
        raise _IncrFallback(f"dir refcount underflow for {dir_str!r}")
    if cur > 1:
        rc[dir_str] = cur - 1
        return
    del rc[dir_str]
    prefer_lower = st.casing_strategy == FILEMAP_CASING_LOWER
    ctx_deleted = False
    parent = ""
    for seg in dir_str.split("/"):
        ctx = (parent, seg.lower())
        counts = st.ctx_variants.get(ctx)
        n = counts.get(seg, 0) if counts else 0
        if n <= 0:
            raise _IncrFallback(f"ctx variant underflow at {ctx}")
        if n == 1:
            del counts[seg]
            if not counts:
                # No dirs use this ctx at all any more - retire it cleanly.
                del st.ctx_variants[ctx]
                st.canonical.pop(ctx, None)
                ctx_deleted = True
            elif st.canonical.get(ctx) == seg:
                # The winning variant is gone - re-pick among the remaining.
                best = None
                best_score = None
                tie = False
                for v in counts:
                    s = _upper_count(v)
                    if best is None or (s < best_score if prefer_lower
                                        else s > best_score):
                        best, best_score, tie = v, s, False
                    elif s == best_score:
                        tie = True
                if tie:
                    raise _IncrFallback(f"casing re-pick tie at {ctx}")
                st.canonical[ctx] = best
                st.dir_rewrite = {}  # memo computed under the old pick
                pick_changes.append(ctx)
        else:
            counts[seg] = n - 1
        parent = parent + seg.lower() + "/"
    if ctx_deleted:
        # The dir_rewrite memo may hold entries computed under a now-retired
        # ctx; if a same-cased dir returns later with a different pick the
        # stale memo would win. The memo is a pure cache - clearing is safe
        # and it repopulates lazily per touched dir.
        st.dir_rewrite = {}


_MEMO_MISS = object()  # sentinel: None is a valid dir_rewrite value ("unchanged")


def _normalize_one_rel_str(st: _IncrementalState, raw: str) -> str:
    """Normalize one raw rel_str's folder casing with the current picks.

    Mirrors _apply_canonical/_rewrite_rel_strs for a single path, memoized
    per raw parent dir in st.dir_rewrite.  Filename pins applied after.
    """
    slash = raw.rfind("/")
    if slash < 0:
        return _pin_rel_str(raw, st.casing_pins) if st.casing_pins else raw
    d = raw[:slash]
    nd = st.dir_rewrite.get(d, _MEMO_MISS)
    if nd is _MEMO_MISS:
        pins = st.casing_pins
        parent = ""
        parts: list[str] = []
        changed = False
        for seg in d.split("/"):
            c = st.canonical.get((parent, seg.lower()), seg)
            # Pins win over the canonical pick (segment-name match, any depth).
            pinned = pins.get(seg.lower()) if pins else None
            if pinned is not None:
                c = pinned
            if c != seg:
                changed = True
            parts.append(c)
            parent = parent + seg.lower() + "/"
        nd = "/".join(parts) if changed else None
        st.dir_rewrite[d] = nd
    out = raw if nd is None else nd + raw[slash:]
    if st.casing_pins:
        name = out[out.rfind("/") + 1:]
        pinned = st.casing_pins.get(name.lower())
        if pinned is not None and pinned != name:
            out = out[:out.rfind("/") + 1] + pinned
    return out


# Above this many structural changes, one O(n) two-pointer merge beats
# per-key list.insert/del memmoves.
_SPLICE_MERGE_THRESHOLD = 512


def _splice_rendered(
    keys: list[str],
    lines: list[str],
    fmap: dict[str, tuple[str, str]],
    inserted: list[str],
    deleted: list[str],
    replaced: list[str],
) -> None:
    """Update the sorted-keys/lines render cache in place for a delta.

    replaced - value change at an existing key (line rewrite, O(log n) each).
    inserted/deleted - structural; spliced via bisect for small deltas or one
    O(n) two-pointer merge for large ones. Raises _IncrFallback on any
    key/cache disagreement (corruption guard).
    """
    for rel_key in replaced:
        i = bisect_left(keys, rel_key)
        if i >= len(keys) or keys[i] != rel_key:
            raise _IncrFallback(f"render cache missing replaced key {rel_key!r}")
        rel_str, mod_name = fmap[rel_key]
        lines[i] = f"{rel_str}\t{mod_name}\n"

    n_structural = len(inserted) + len(deleted)
    if not n_structural:
        return
    if n_structural <= _SPLICE_MERGE_THRESHOLD:
        for rel_key in deleted:
            i = bisect_left(keys, rel_key)
            if i >= len(keys) or keys[i] != rel_key:
                raise _IncrFallback(f"render cache missing deleted key {rel_key!r}")
            del keys[i]
            del lines[i]
        for rel_key in inserted:
            i = bisect_left(keys, rel_key)
            if i < len(keys) and keys[i] == rel_key:
                raise _IncrFallback(f"render cache already has inserted key {rel_key!r}")
            rel_str, mod_name = fmap[rel_key]
            keys.insert(i, rel_key)
            lines.insert(i, f"{rel_str}\t{mod_name}\n")
        return

    # Large delta: one merge pass.
    del_set = set(deleted)
    ins = sorted(inserted)
    out_keys: list[str] = []
    out_lines: list[str] = []
    ii = 0
    n_ins = len(ins)
    for k, ln in zip(keys, lines):
        while ii < n_ins and ins[ii] < k:
            ok = ins[ii]
            rel_str, mod_name = fmap[ok]
            out_keys.append(ok)
            out_lines.append(f"{rel_str}\t{mod_name}\n")
            ii += 1
        if k in del_set:
            continue
        if ii < n_ins and ins[ii] == k:
            raise _IncrFallback(f"render cache already has inserted key {k!r}")
        out_keys.append(k)
        out_lines.append(ln)
    while ii < n_ins:
        ok = ins[ii]
        rel_str, mod_name = fmap[ok]
        out_keys.append(ok)
        out_lines.append(f"{rel_str}\t{mod_name}\n")
        ii += 1
    keys[:] = out_keys
    lines[:] = out_lines


def _try_incremental(
    output_key: str,
    fingerprint: tuple,
    priority_order: list[str],
    index: dict,
    pf: _PathFilters,
    utf8_bad: frozenset,
    root_folder_mods,
    root_mod_files: dict[str, frozenset[str]],
    disabled_lower: dict[str, frozenset[str]],
    disabled_frozen: frozenset,
    output_path: Path,
    normalize_folder_case: bool,
    filemap_casing: str,
    log_fn,
    dry_run: bool = False,
) -> "tuple | None":
    """Apply the modlist delta to the stored state instead of a full merge.

    Returns (count, conflict_map, overrides, overridden_by, dry_texts) on
    success, or None to signal "do the full rebuild" (state missing/invalid,
    casing pick change, oversized delta, or any internal error - in which
    case the state is dropped first so the full path repopulates cleanly).

    dry_run (verify mode): operates on a clone, touches no files and no
    registry entries; dry_texts = (filemap_text, root_text_or_None).
    """
    st0 = _get_incr_state(output_key)
    if st0 is None:
        _incr_note("filemap: incremental skip (no state)")
        return None
    if st0.fingerprint != fingerprint:
        # Name the mismatching components - essential for diagnosing why a
        # profile never takes the fast path.
        _fp_names = (
            "index_path", "index_mtime", "modlist_path", "staging_root",
            "strip_prefixes", "per_mod_strip_prefixes", "allowed_extensions",
            "exclude_dirs", "conflict_ignore_filenames",
            "conflict_ignore_foldernames",
            "excluded_loose_filenames", "allowed_top_level_folders",
            "excluded_mod_files", "normalize_folder_case", "filemap_casing",
            "filemap_casing_pins",
            "no_conflict_key_fn", "root_folder_mods", "root_mod_files",
            "utf8_bad",
        )
        _bad = [
            _fp_names[i] if i < len(_fp_names) else str(i)
            for i, (a, b) in enumerate(zip(st0.fingerprint, fingerprint))
            if a != b
        ] or ["length"]
        _incr_note(f"filemap: incremental skip (fingerprint: {', '.join(_bad)})")
        return None
    if st0.casing_ties_present:
        _incr_note("filemap: incremental skip (casing ties)")
        return None

    strategy_norm = (filemap_casing if filemap_casing in _VALID_FILEMAP_CASINGS
                     else FILEMAP_CASING_UPPER)
    expect_canonical = normalize_folder_case and strategy_norm in (
        FILEMAP_CASING_UPPER, FILEMAP_CASING_LOWER)
    if expect_canonical and st0.casing_strategy is None:
        # State was populated from an empty filemap - no casing baseline to
        # maintain. The full rebuild (trivial at that size) repopulates it.
        _incr_note("filemap: incremental skip (no casing baseline)")
        return None
    force_strategy = (strategy_norm if normalize_folder_case and strategy_norm in
                      (FILEMAP_CASING_FORCE_LOWER, FILEMAP_CASING_FORCE_UPPER)
                      else None)

    if dry_run:
        with st0.lock:
            st = st0.clone()
    else:
        st0.lock.acquire()
        st = st0
    try:
        old = st.prev_priority_order
        new = priority_order
        if old == new:
            added: list[str] = []
            removed: list[str] = []
            survivors_reordered = False
        else:
            old_set = set(old)
            new_set = set(new)
            added = [m for m in new if m not in old_set]
            removed = [m for m in old if m not in new_set]
            survivors_reordered = (
                [m for m in old if m in new_set] != [m for m in new if m in old_set]
            )

        # Oversized deltas lose to the (index-cached) full merge - bail early.
        total = sum(st.files_count.values()) or 1
        delta_files = 0
        for m in added:
            e = index.get(m)
            if e:
                delta_files += len(e[0])
        for m in removed:
            e = index.get(m)
            if e:
                delta_files += len(e[0])
        if delta_files > 0.4 * total:
            raise _IncrFallback(f"delta too large ({delta_files} files vs {total})")

        root_set = root_folder_mods or frozenset()
        track_casing = expect_canonical  # st.casing_strategy is set (checked above)
        touched: set[str] = set()
        touched_root: set[str] = set()
        # Position maps: removals bracket pair updates with the OLD order
        # (stacks still hold to-be-removed mods), additions with the NEW
        # (stacks then hold added mods). For pure toggles the survivors'
        # relative order is identical in both; if a reorder is mixed in,
        # survivors_reordered triggers a full refcount rebuild below anyway.
        old_pos = {name: i for i, name in enumerate(old)}
        pos = {name: i for i, name in enumerate(new)}
        _delta_t0 = time.perf_counter()

        # --- apply removals -------------------------------------------------
        for m in removed:
            entry = index.get(m)
            if m in utf8_bad or not entry or not entry[0]:
                st.files_count.pop(m, None)
                continue
            is_root = m in root_set
            rfm = None if is_root else root_mod_files.get(m)
            for rel_key in entry[0]:
                if not pf.accepts(m, rel_key):
                    continue
                entry_root = is_root or (rfm is not None and rel_key in rfm)
                prov_ns = st.providers_root if entry_root else st.providers
                cont_ns = st.contested_root if entry_root else st.contested
                t_ns = touched_root if entry_root else touched
                stack = prov_ns.get(rel_key)
                if stack is None:
                    raise _IncrFallback(f"missing provider stack for {rel_key!r}")
                if type(stack) is str:
                    if stack != m:
                        raise _IncrFallback(f"provider mismatch for {rel_key!r}")
                    del prov_ns[rel_key]
                else:
                    _pairs_update(st.pair_counts, st.losses, stack, old_pos, -1)
                    stack.remove(m)  # ValueError ⇒ corruption ⇒ fallback
                    if len(stack) == 1:
                        prov_ns[rel_key] = stack[0]
                        cont_ns.discard(rel_key)
                    else:
                        _pairs_update(st.pair_counts, st.losses, stack, old_pos, 1)
                t_ns.add(rel_key)
            st.files_count.pop(m, None)

        # --- apply additions ------------------------------------------------
        for m in added:
            entry = index.get(m)
            if m in utf8_bad:
                if log_fn is not None and entry:
                    bad = [rs for rs in entry[0].values() if not _is_utf8_safe(rs)]
                    try:  # escaped + guarded (see rebuild_mod_index)
                        log_fn(
                            f"WARN: Mod \"{_safe_log_str(m)}\" skipped - contains "
                            f"file(s) with non-UTF-8 name(s): "
                            f"{', '.join(_safe_log_str(n) for n in bad[:5])}"
                        )
                    except Exception:
                        pass
                continue
            if not entry or not entry[0]:
                # Newly-enabled mod with no index entry / zero files → it will
                # deploy nothing (same silent-drop as the full-merge path). The
                # [Overwrite] boundary is legitimately empty when unused; warn
                # only for a real mod so a symlinked/unscanned mod is visible.
                if log_fn is not None and m != OVERWRITE_NAME:
                    log_fn(f"WARN: enabled mod \"{m}\" has no index entry / zero "
                           f"files (incremental) - it will deploy no files; "
                           f"check for a symlinked/unreadable folder or Refresh")
                continue
            is_root = m in root_set
            rfm = None if is_root else root_mod_files.get(m)
            acc = 0
            for rel_key in entry[0]:
                if not pf.accepts(m, rel_key):
                    continue
                entry_root = is_root or (rfm is not None and rel_key in rfm)
                prov_ns = st.providers_root if entry_root else st.providers
                cont_ns = st.contested_root if entry_root else st.contested
                t_ns = touched_root if entry_root else touched
                acc += 1
                stack = prov_ns.get(rel_key)
                if stack is None:
                    prov_ns[rel_key] = m
                elif type(stack) is str:
                    new_stack = [stack, m]
                    prov_ns[rel_key] = new_stack
                    cont_ns.add(rel_key)
                    _pairs_update(st.pair_counts, st.losses, new_stack, pos, 1)
                else:
                    _pairs_update(st.pair_counts, st.losses, stack, pos, -1)
                    stack.append(m)
                    _pairs_update(st.pair_counts, st.losses, stack, pos, 1)
                t_ns.add(rel_key)
            if acc:
                st.files_count[m] = acc

        # --- reorder: only contested paths can change winners ----------------
        if survivors_reordered:
            touched |= st.contested
            touched_root |= st.contested_root

        # --- recompute winners for touched keys ------------------------------
        pick_changes: list = []

        def _apply_touched(prov_ns, fmap_ns, t_keys, inserted, deleted, replaced):
            for rel_key in t_keys:
                stack = prov_ns.get(rel_key)
                entry_old = fmap_ns.get(rel_key)
                old_winner = entry_old[1] if entry_old is not None else None
                if stack is None:
                    # No provider left - entry disappears.
                    if entry_old is not None:
                        del fmap_ns[rel_key]
                        deleted.append(rel_key)
                        if track_casing:
                            raw_old = index[old_winner][0][rel_key]
                            sl = raw_old.rfind("/")
                            if sl >= 0:
                                _casing_dir_removed(st, raw_old[:sl], pick_changes)
                    continue
                winner = stack if type(stack) is str else max(stack, key=pos.__getitem__)
                if winner == old_winner:
                    continue
                raw = index[winner][0][rel_key]
                if track_casing:
                    if old_winner is not None:
                        raw_old = index[old_winner][0][rel_key]
                        sl = raw_old.rfind("/")
                        if sl >= 0:
                            _casing_dir_removed(st, raw_old[:sl], pick_changes)
                    sl = raw.rfind("/")
                    if sl >= 0:
                        _casing_dir_added(st, raw[:sl], pick_changes)
                    rel_str = _normalize_one_rel_str(st, raw)
                elif force_strategy is not None:
                    rel_str = _force_case_one(raw, force_strategy)
                    if st.casing_pins:
                        rel_str = _pin_rel_str(rel_str, st.casing_pins)
                else:
                    rel_str = raw
                    if st.casing_pins:
                        rel_str = _pin_rel_str(rel_str, st.casing_pins)
                fmap_ns[rel_key] = (rel_str, winner)
                if entry_old is None:
                    inserted.append(rel_key)
                else:
                    replaced.append(rel_key)

        ins_n: list[str] = []
        del_n: list[str] = []
        rep_n: list[str] = []
        ins_r: list[str] = []
        del_r: list[str] = []
        rep_r: list[str] = []
        _apply_touched(st.providers, st.filemap, touched, ins_n, del_n, rep_n)
        _apply_touched(st.providers_root, st.filemap_root, touched_root,
                       ins_r, del_r, rep_r)
        perftrace.mark("incr: delta apply", time.perf_counter() - _delta_t0)

        # --- conflict aggregates, fresh objects every call --------------------
        # pair_counts holds the consecutive-pair relations (== the full
        # merge's (mod, prev-winner) events) maintained per-touched-path by
        # the brackets above. A reorder changes the relative provider order
        # everywhere, so only then are the refcounts rebuilt by a full sweep.
        _agg_t0 = time.perf_counter()
        if survivors_reordered:
            pc: dict[tuple[str, str], int] = {}
            ls: dict[str, int] = {}
            for prov_ns, cont_ns in ((st.providers, st.contested),
                                     (st.providers_root, st.contested_root)):
                for rel_key in cont_ns:
                    ss = sorted(prov_ns[rel_key], key=pos.__getitem__)
                    prev = ss[0]
                    for cur_m in ss[1:]:
                        k = (prev, cur_m)
                        pc[k] = pc.get(k, 0) + 1
                        ls[prev] = ls.get(prev, 0) + 1
                        prev = cur_m
            st.pair_counts = pc
            st.losses = ls
        overrides: dict[str, set[str]] = {s: set() for s in priority_order}
        overridden_by: dict[str, set[str]] = {s: set() for s in priority_order}
        for lo, hi in st.pair_counts:
            overrides[hi].add(lo)
            overridden_by[lo].add(hi)
        win_count = {m: c - st.losses.get(m, 0) for m, c in st.files_count.items()}
        conflict_map = _compute_conflict_status(
            priority_order, overrides, overridden_by, win_count,
            set(st.files_count),
        )
        perftrace.mark("incr: aggregates sweep", time.perf_counter() - _agg_t0)

        st.prev_priority_order = list(new)

        # --- update render caches by splice, then write ------------------------
        changed_norm = bool(ins_n or del_n or rep_n)
        changed_root = bool(ins_r or del_r or rep_r)
        if changed_norm:
            _splice_rendered(st.sorted_keys, st.lines, st.filemap,
                             ins_n, del_n, rep_n)
        if changed_root:
            _splice_rendered(st.sorted_keys_root, st.lines_root,
                             st.filemap_root, ins_r, del_r, rep_r)

        # --- canonical pick changes: re-normalize affected subtrees -----------
        # Every entry under a changed ctx shares the lowercase key prefix
        # parent+seg+"/", i.e. a contiguous range of the (post-splice) sorted
        # keys. Winners are unchanged - only rel_str casing is rewritten.
        if pick_changes:
            rep2_n: list[str] = []
            rep2_r: list[str] = []
            n_entries = 0
            for ctx in dict.fromkeys(pick_changes):
                prefix = ctx[0] + ctx[1] + "/"
                for keys_ns, fmap_ns, rep2 in (
                        (st.sorted_keys, st.filemap, rep2_n),
                        (st.sorted_keys_root, st.filemap_root, rep2_r)):
                    i = bisect_left(keys_ns, prefix)
                    while i < len(keys_ns) and keys_ns[i].startswith(prefix):
                        rk = keys_ns[i]
                        rs_old, w = fmap_ns[rk]
                        rs_new = _normalize_one_rel_str(st, index[w][0][rk])
                        if rs_new != rs_old:
                            fmap_ns[rk] = (rs_new, w)
                            rep2.append(rk)
                        n_entries += 1
                        i += 1
            if rep2_n:
                _splice_rendered(st.sorted_keys, st.lines, st.filemap,
                                 [], [], rep2_n)
                changed_norm = True
            if rep2_r:
                _splice_rendered(st.sorted_keys_root, st.lines_root,
                                 st.filemap_root, [], [], rep2_r)
                changed_root = True
            _incr_note(
                f"filemap: incremental pick-change rewrite "
                f"({len(dict.fromkeys(pick_changes))} ctx, "
                f"{len(rep2_n) + len(rep2_r)}/{n_entries} entries)")

        root_path = output_path.parent / "filemap_root.txt"
        need_norm_write = (changed_norm
                           or disabled_frozen != st.last_disabled_frozen
                           or not output_path.is_file())
        need_root_write = bool(st.filemap_root) and (
            changed_root or not root_path.is_file())

        if dry_run:
            norm_text = _join_filtered(st.sorted_keys, st.lines,
                                       st.filemap, disabled_lower)
            root_text = "".join(st.lines_root) if st.filemap_root else None
            return (norm_text.count("\n"), conflict_map, overrides,
                    overridden_by, (norm_text, root_text))

        if need_norm_write:
            _w_t0 = time.perf_counter()
            count = _join_and_write(output_path, st.sorted_keys, st.lines,
                                    st.filemap, disabled_lower)
            perftrace.mark("incr: join+write", time.perf_counter() - _w_t0)
        else:
            count = len(st.sorted_keys)
        if st.filemap_root:
            if need_root_write:
                _join_and_write(root_path, st.sorted_keys_root,
                                st.lines_root, st.filemap_root, {})
        elif root_path.is_file():
            root_path.unlink(missing_ok=True)
        st.last_disabled_frozen = disabled_frozen

        # Winner-cache coherence: a later full rebuild must never skip its
        # write against a snapshot that predates incremental writes.
        with _filemap_winner_cache_lock:
            _filemap_winner_cache.pop(output_key, None)

        _incr_stats["fast"] += 1
        return count, conflict_map, overrides, overridden_by, None
    except Exception as exc:
        if not dry_run:
            # State may be half-mutated - drop it; the full rebuild repopulates.
            _drop_incr_state(output_key)
        _incr_stats["fallback"] += 1
        _incr_note(f"filemap: incremental fallback ({exc.__class__.__name__}: {exc})")
        if log_fn is not None:
            log_fn(f"filemap: incremental fallback - {exc}")
        return None
    finally:
        if not dry_run:
            st0.lock.release()


def index_key_for_raw(raw_key: str,
                      strip_prefixes=None,
                      strip_path_prefixes: "list[str] | None" = None) -> str:
    """Index key for a mod-relative RAW path - the one translation between the
    tab's raw-keyed state and index space. Keep in step with _scan_dir."""
    rel = raw_key.replace("\\", "/")
    if strip_path_prefixes:
        rel_lower = rel.lower()
        for p in sorted(strip_path_prefixes, key=len, reverse=True):
            p_lower = p.lower()
            if rel_lower == p_lower or rel_lower.startswith(p_lower + "/"):
                rel = rel[len(p):].lstrip("/")
                break
    if strip_prefixes and "/" in rel:
        strips = {s.lower() for s in strip_prefixes}
        while "/" in rel:
            first_seg, remainder = rel.split("/", 1)
            if first_seg.lower() in strips:
                rel = remainder
            else:
                break
    return rel.lower()


def mod_strip_args(mod_name: str, strip_prefixes=None,
                   per_mod_strip_prefixes: dict | None = None,
                   root_folder_mods=None) -> tuple:
    """(strip_prefixes, strip_path_prefixes) for one mod - mirrors the
    _strip_for_mod / _path_prefixes_for_mod pair. Root mods get none."""
    if root_folder_mods and mod_name in root_folder_mods:
        return frozenset(), []
    base = frozenset(s.lower() for s in (strip_prefixes or ()))
    mod_strip = (per_mod_strip_prefixes or {}).get(mod_name)
    if not mod_strip:
        return base, []
    segments = frozenset(s.lower() for s in mod_strip if "/" not in s)
    return base | segments, [s for s in mod_strip if "/" in s]


def index_keys_for_mod(raw_keys, mod_name: str, strip_prefixes=None,
                       per_mod_strip_prefixes: dict | None = None,
                       root_folder_mods=None) -> set[str]:
    """Translate a mod's RAW per-file keys into index/filemap key space."""
    strips, paths = mod_strip_args(mod_name, strip_prefixes,
                                   per_mod_strip_prefixes, root_folder_mods)
    return {index_key_for_raw(k, strips, paths) for k in raw_keys}


def _scan_dir(
    source_name: str,
    source_dir: str,
    strip_prefixes: frozenset[str] = frozenset(),
    allowed_extensions: frozenset[str] = frozenset(),
    _unused_root_deploy_folders: frozenset[str] = frozenset(),
    strip_path_prefixes: list[str] | None = None,
    exclude_dirs: frozenset[str] = frozenset(),
) -> tuple[str, dict[str, str], dict[str, str], list[str]]:
    """Walk source_dir with os.scandir (fast, no Pathlib overhead).

    Returns (source_name, normal_files, {}, invalid_names) where normal_files
    is {rel_key_lower: rel_str_original} and invalid_names is a list of
    relative paths whose filenames contain non-UTF-8 bytes (surrogates).
    Pure function - no shared state, safe to call from any thread.

    strip_path_prefixes - full path prefixes to strip once (e.g. ["Tree", "Meshes/Architecture"]).
    Applied first, before strip_prefixes. Longest match wins. Case-insensitive.

    strip_prefixes - lowercase top-level folder names to remove from the
    start of each relative path before adding it to the result.  Only the
    first path segment is ever stripped, and only when it matches one of the
    listed names (case-insensitive).  e.g. strip_prefixes={"plugins"} turns
    "plugins/MyMod/MyMod.dll" into "MyMod/MyMod.dll".

    allowed_extensions - when non-empty, only files whose lowercase extension
    (including the leading dot) appears in this set are included.  e.g.
    allowed_extensions={".pak"} drops all non-.pak files from the result.

    exclude_dirs - lowercase directory names to skip entirely during the walk.
    Any directory whose name (case-insensitive) matches an entry here is never
    pushed onto the scan stack, so none of its files reach the filemap.
    e.g. exclude_dirs={"fomod"} prevents FOMOD installer metadata from being
    deployed to the game's data directory.

    _unused_root_deploy_folders - retained for call-site compatibility only;
    the root-deploy routing has been removed in favour of custom_routing_rules.
    """
    result: dict[str, str] = {}
    root_result: dict[str, str] = {}  # always empty; kept for tuple compat
    invalid_names: list[str] = []
    # Pre-sort once (longest match first) so we don't re-sort inside the per-file loop.
    # Each entry is (lowercase_prefix, len_of_original_prefix) for O(1) strip-by-length.
    sorted_path_prefixes: list[tuple[str, int]] = (
        sorted(((p.lower(), len(p)) for p in strip_path_prefixes), key=lambda t: -t[1])
        if strip_path_prefixes else []
    )
    # Iterative scandir stack - avoids rglob/Pathlib per-entry object cost.
    # Errors are handled at three levels so a transient failure on ONE entry
    # (stale NFS handle, permission blip, dying SD card) doesn't silently drop
    # every remaining entry of that directory: opening the directory and
    # advancing the iterator each have their own guard, and per-entry work has
    # its own try that counts the skip and continues.
    skipped_errors = 0
    stack = [("", source_dir)]
    while stack:
        prefix, current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            skipped_errors += 1
            continue
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError:
                    # readdir itself failed - remaining entries of this
                    # directory are unreadable; count once and move on.
                    skipped_errors += 1
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if exclude_dirs and entry.name.lower() in exclude_dirs:
                            continue
                        if _is_macos_junk(entry.name):
                            continue
                        # Isolated Proton prefixes created next to a mod's exe
                        # (see _get_tool_prefix_env in dialogs.py) are runtime
                        # state, not mod content - never include them in the
                        # filemap so they don't get deployed into the game.
                        if entry.name.startswith("prefix_"):
                            continue
                        # RE/Fluffy bundle option library - holds the original
                        # option folders; only the materialised selection at the
                        # mod root is deployed (see Utils/re_bundle.py).
                        if entry.name == ".mm_bundle":
                            continue
                        if not _is_utf8_safe(entry.name):
                            invalid_names.append(prefix + entry.name + "/")
                            continue
                        stack.append((
                            prefix + entry.name + "/",
                            entry.path,
                        ))
                    elif entry.is_file(follow_symlinks=False):
                        if entry.name in _EXCLUDE_NAMES:
                            continue
                        if _is_macos_junk(entry.name):
                            continue
                        if not _is_utf8_safe(entry.name):
                            invalid_names.append(prefix + entry.name)
                            continue
                        rel_str = prefix + entry.name
                        # Strip full path prefixes first (per-mod "ignore this folder" paths).
                        if sorted_path_prefixes:
                            rel_lower = rel_str.lower()
                            for p_lower, p_len in sorted_path_prefixes:
                                if rel_lower == p_lower or rel_lower.startswith(p_lower + "/"):
                                    rel_str = rel_str[p_len:].lstrip("/")
                                    break
                        # Strip leading wrapper folders declared by the game.
                        # Repeat until no more matching prefixes remain so that
                        # e.g. "bepinex/plugins/Mod/Mod.dll" → "Mod/Mod.dll"
                        # when strip_prefixes = {"bepinex", "plugins"}.
                        if strip_prefixes and "/" in rel_str:
                            while "/" in rel_str:
                                first_seg, remainder = rel_str.split("/", 1)
                                if first_seg.lower() in strip_prefixes:
                                    rel_str = remainder
                                else:
                                    break
                        # Extension filter - drop files not in the allowed set.
                        # Use suffix matching so multi-dot extensions like
                        # ".dekcns.json" are honoured (splitext only returns
                        # the last suffix).
                        if allowed_extensions:
                            name_lower = entry.name.lower()
                            if not any(
                                name_lower.endswith(e) and len(name_lower) > len(e)
                                for e in allowed_extensions
                            ):
                                continue
                        key = rel_str.lower()
                        if key in result:
                            # Two physical files map to the same case-insensitive path
                            # (e.g. Interface/ vs interface/).  Prefer the one whose
                            # folder segments have more uppercase characters.
                            existing = result[key]
                            ex_slash = existing.rfind("/")
                            new_slash = rel_str.rfind("/")
                            ex_folders = existing[:ex_slash] if ex_slash >= 0 else ""
                            new_folders = rel_str[:new_slash] if new_slash >= 0 else ""
                            if _upper_count(new_folders) > _upper_count(ex_folders):
                                result[key] = rel_str
                        else:
                            result[key] = rel_str
                except OSError:
                    skipped_errors += 1
    if skipped_errors:
        try:  # escaped name + guarded: logging must never abort the scan
            print(f"[filemap] WARN: scan of \"{_safe_log_str(source_name)}\" "
                  f"skipped {skipped_errors} entr(y/ies) due to filesystem "
                  f"errors", file=sys.stderr, flush=True)
        except Exception:
            pass
    return source_name, result, root_result, invalid_names


@lru_cache(maxsize=2048)
def _upper_count(s: str) -> int:
    return sum(1 for c in s if c.isupper())


def _is_utf8_safe(s: str) -> bool:
    """Return True if s can be encoded as UTF-8 (no lone surrogates)."""
    try:
        s.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def repair_nonutf8_names(root: "Path | str", log_fn=None) -> int:
    """Rename any file/dir under *root* whose name is not valid UTF-8.

    Mirrors mod_install._fix_nonutf8_names_extracted_tree but works on a live
    staging tree, so a Refresh can HEAL mods already on disk (installed before
    the extract-time repair existed). A non-UTF-8 name (legacy Windows code
    page byte written verbatim by the extractor) makes rebuild_mod_index skip
    the WHOLE mod - this restores it. Decodes the raw bytes as CP1252 then
    CP437 (else backslashreplace) to a valid UTF-8 name. Deepest-first; skips
    when the target name already exists. Returns entries renamed. Cheap when all
    names are already UTF-8 (the common case: one rglob, no renames).
    """
    root = Path(root)
    try:
        offenders = [p for p in root.rglob("*") if not _is_utf8_safe(p.name)]
    except OSError:
        return 0
    if not offenders:
        return 0
    renamed = 0
    for entry in sorted(offenders, key=lambda p: len(p.parts), reverse=True):
        try:
            raw = entry.name.encode("utf-8", "surrogateescape")
        except Exception:
            continue
        fixed = None
        for enc in ("cp1252", "cp437"):
            try:
                cand = raw.decode(enc)
                cand.encode("utf-8")
                fixed = cand
                break
            except (UnicodeDecodeError, UnicodeEncodeError):
                continue
        if fixed is None:
            fixed = raw.decode("utf-8", "backslashreplace")
        if fixed == entry.name:
            continue
        target = entry.parent / fixed
        try:
            if target.exists():
                continue
            os.rename(os.fsencode(str(entry)), os.fsencode(str(target)))
            renamed += 1
        except OSError:
            continue
    if renamed and log_fn is not None:
        log_fn(f"Repaired {renamed} non-UTF-8 file name(s) on disk (legacy "
               f"Windows code page) so affected mod(s) can be indexed.")
    return renamed


def _safe_log_str(s: str) -> str:
    """Make *s* safe to hand to any log sink (print/UTF-8 stdout, Qt widget).

    File names read from disk can contain surrogate-escaped non-UTF-8 bytes
    (e.g. Windows-1252 curly quotes in a mod's music files). Embedding those
    RAW in a log message makes the log call itself raise UnicodeEncodeError
    on a strict-UTF-8 stdout - which aborted rebuild_mod_index mid-rescan and
    left modindex.bin permanently stale ("Index rescan warning: 'utf-8' codec
    can't encode character '\\udc94' …"). Escape them instead (b"\\x94"-style)
    so warnings about bad names can never crash the operation reporting them.
    """
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", "backslashreplace").decode("utf-8", "replace")


# (path, stat_key) → mods with non-UTF-8 file names. See mod_index_utf8_unsafe.
_utf8_unsafe_cache: tuple[str, tuple, frozenset] | None = None


def mod_index_utf8_unsafe(index_path: Path) -> frozenset:
    """Mods in *index_path* owning a file with a non-UTF-8 (surrogate) name -
    a legacy-index-only condition (v4 indexes skip such files at scan time).

    Cached by (path, mtime) like read_mod_index: the sweep encodes every file
    name (~100 ms on a 100k-file index) and its result can't change without an
    index rewrite, so per-toggle filemap rebuilds shouldn't repeat it."""
    global _utf8_unsafe_cache
    try:
        stat_key = _index_stat_key(index_path)
    except OSError:
        return frozenset()
    key = str(index_path)
    c = _utf8_unsafe_cache
    if c is not None and c[0] == key and c[1] == stat_key:
        return c[2]
    index = read_mod_index(index_path) or {}
    bad = frozenset(
        name for name, (normal, _root) in index.items()
        if any(not _is_utf8_safe(rs) for rs in normal.values()))
    _utf8_unsafe_cache = (key, stat_key, bad)
    return bad


# Valid filemap casing strategies (game property `filemap_casing`).
FILEMAP_CASING_UPPER       = "upper"        # pick variant with most uppercase letters (default)
FILEMAP_CASING_LOWER       = "lower"        # pick variant with most lowercase letters
FILEMAP_CASING_FORCE_LOWER = "force_lower"  # lowercase every folder segment and filename
FILEMAP_CASING_FORCE_UPPER = "force_upper"  # uppercase every folder segment and filename stem (extension stays lower)
_VALID_FILEMAP_CASINGS = frozenset({
    FILEMAP_CASING_UPPER, FILEMAP_CASING_LOWER,
    FILEMAP_CASING_FORCE_LOWER, FILEMAP_CASING_FORCE_UPPER,
})


def _pick_canonical_segment(a: str, b: str, strategy: str = FILEMAP_CASING_UPPER) -> str:
    """Choose the folder name whose casing best matches *strategy*.

    strategy="upper" - prefer the variant with more uppercase characters.
    strategy="lower" - prefer the variant with more lowercase characters
                       (= fewer uppercase).
    On a tie, the first-seen variant (*a*) wins (stable choice).
    """
    if strategy == FILEMAP_CASING_LOWER:
        return a if _upper_count(a) <= _upper_count(b) else b
    return a if _upper_count(a) >= _upper_count(b) else b


def _collect_unique_dirs(
    *all_files_list: dict[str, dict[str, str]],
) -> dict[str, None]:
    """Collect the distinct original-cased parent-dir paths across all mods.

    All files sharing a parent directory share the exact same folder-segment
    chain, so the canonical-casing work is per *directory*, not per file.
    A large modlist has ~12x more files than unique directories, so we collect
    the distinct parent-dir paths first and walk segments once per directory.
    ``dir_str`` is the original-cased parent path (everything before the final
    "/"); loose top-level files (no folder to normalize) contribute nothing.
    """
    unique_dirs: dict[str, None] = {}
    for all_files in all_files_list:
        if not all_files:
            continue
        for files in all_files.values():
            for rel_str in files.values():
                slash = rel_str.rfind("/")
                if slash >= 0:
                    unique_dirs[rel_str[:slash]] = None
    return unique_dirs


def _collect_canonical(
    unique_dirs: "dict[str, None] | set[str]",
    strategy: str,
) -> dict[tuple[str, str], str]:
    """Pick the canonical casing per folder segment across *unique_dirs*.

    Keyed by the segment's full ancestor path so that identically-named
    segments at different tree locations are independent.  e.g.
    "textures/effects" vs "interface/photomode/overlays/effects" produce
    different keys, so Photo Mode's uppercase EFFECTS can never influence
    Particle Patch's lowercase effects.
    Key: (lowercase_parent_path, lowercase_segment) -> canonical segment str
    """
    canonical: dict[tuple[str, str], str] = {}
    for dir_str in unique_dirs:
        parent = ""
        for seg in dir_str.split("/"):
            ctx_key = (parent, seg.lower())
            cur = canonical.get(ctx_key)
            if cur is None:
                canonical[ctx_key] = seg
            else:
                canonical[ctx_key] = _pick_canonical_segment(cur, seg, strategy)
            parent = parent + seg.lower() + "/"
    return canonical


def _apply_canonical(
    canonical: dict[tuple[str, str], str],
    unique_dirs: "dict[str, None] | set[str]",
) -> dict[str, str | None]:
    """Resolve each unique directory path to its canonical form once.

    Directories whose casing already matches the canonical pick map to
    themselves (None marker) so the rewrite loop can skip them without
    re-walking segments.
    """
    dir_rewrite: dict[str, str | None] = {}
    for dir_str in unique_dirs:
        parent = ""
        new_parts: list[str] = []
        changed = False
        for seg in dir_str.split("/"):
            ctx_key = (parent, seg.lower())
            c = canonical.get(ctx_key, seg)
            if c != seg:
                changed = True
            new_parts.append(c)
            parent = parent + seg.lower() + "/"
        dir_rewrite[dir_str] = "/".join(new_parts) if changed else None
    return dir_rewrite


def _rewrite_rel_strs(
    dir_rewrite: dict[str, str | None],
    *all_files_list: dict[str, dict[str, str]],
) -> None:
    """Rewrite rel_str values for files whose parent directory's casing changed."""
    for all_files in all_files_list:
        if not all_files:
            continue
        for files in all_files.values():
            for rel_key in files:
                rel_str = files[rel_key]
                slash = rel_str.rfind("/")
                if slash < 0:
                    continue
                new_dir = dir_rewrite.get(rel_str[:slash])
                if new_dir is None:
                    continue
                files[rel_key] = new_dir + rel_str[slash:]


def _normalize_folder_cases(
    *all_files_list: dict[str, dict[str, str]],
    strategy: str = FILEMAP_CASING_UPPER,
) -> None:
    """Normalize folder name casing across all mods in-place.

    Folder names are case-insensitive on Windows (and in the game engine), so
    "Plugins" and "plugins" are the same folder.  When multiple mods use
    different casings we pick a single canonical variant according to
    *strategy* and rewrite every rel_str that uses a losing variant so the
    whole filemap is consistent.

    strategy:
      "upper" - prefer the variant with more uppercase letters (default).
      "lower" - prefer the variant with more lowercase letters.

    File *names* are left exactly as they are.  Use ``_apply_force_casing``
    for force-lower / force-upper modes which transform every segment.
    Accepts one or more dicts (e.g. normal and root) and builds canonical
    casing from all in one pass, then rewrites each in turn.
    """
    unique_dirs = _collect_unique_dirs(*all_files_list)
    if not unique_dirs:
        return
    canonical = _collect_canonical(unique_dirs, strategy)
    if not canonical:
        return
    dir_rewrite = _apply_canonical(canonical, unique_dirs)
    _rewrite_rel_strs(dir_rewrite, *all_files_list)


def _force_case_one(rel_str: str, strategy: str) -> str:
    """Force-case the folder segments of a single rel_str.

    Filenames (the final segment) are left exactly as the mod shipped them.
    Returns rel_str unchanged for non-force strategies or loose files.
    """
    if strategy == FILEMAP_CASING_FORCE_LOWER:
        _xform_seg = str.lower
    elif strategy == FILEMAP_CASING_FORCE_UPPER:
        _xform_seg = str.upper
    else:
        return rel_str
    if "/" not in rel_str:
        return rel_str  # no folder segments to rewrite
    parts = rel_str.split("/")
    new_parts = [_xform_seg(p) for p in parts[:-1]]
    new_parts.append(parts[-1])  # filename untouched
    return "/".join(new_parts)


def _apply_force_casing(
    *all_files_list: dict[str, dict[str, str]],
    strategy: str,
) -> None:
    """Force-rewrite every folder segment of rel_str in place.

    strategy="force_lower" - every folder segment is lowercased.
    strategy="force_upper" - every folder segment is uppercased.

    Filenames (the final segment) are left exactly as each mod shipped them.
    Used when the game engine prefers a uniform casing convention for
    directories regardless of what mod authors ship on disk.
    """
    if strategy not in (FILEMAP_CASING_FORCE_LOWER, FILEMAP_CASING_FORCE_UPPER):
        return
    for all_files in all_files_list:
        if not all_files:
            continue
        for files in all_files.values():
            for rel_key in files:
                rel_str = files[rel_key]
                if "/" not in rel_str:
                    continue  # no folder segments to rewrite
                files[rel_key] = _force_case_one(rel_str, strategy)


def _apply_casing_pins(
    *all_files_list: dict[str, dict[str, str]],
    pins: dict[str, str],
) -> None:
    """Force pinned path segments (folders AND filenames) to exact casing, in place.

    *pins* maps a lowercase segment name to the exact casing it must deploy as
    (``{"compassshoutmeterholder": "CompassShoutMeterHolder"}``, or with an
    extension for a file: ``{"compass.swf": "Compass.swf"}``).  Matches at any
    depth; only the named segment is touched.  Runs last so it wins over
    ``filemap_casing`` / ``_normalize_folder_cases``, and both the full-rebuild
    and incremental paths converge here so they can never disagree.
    """
    if not pins:
        return
    for all_files in all_files_list:
        if not all_files:
            continue
        for files in all_files.values():
            for rel_key in files:
                new = _pin_rel_str(files[rel_key], pins)
                if new != files[rel_key]:
                    files[rel_key] = new


def _pin_rel_str(rel_str: str, pins: dict[str, str],
                 include_filename: bool = True) -> str:
    """Return *rel_str* with any pinned segment (folder or filename) forced to its casing."""
    if "/" not in rel_str:
        if not include_filename:
            return rel_str
        pinned = pins.get(rel_str.lower())
        return pinned if pinned is not None else rel_str
    parts = rel_str.split("/")
    last = len(parts) if include_filename else len(parts) - 1
    changed = False
    for i in range(last):
        pinned = pins.get(parts[i].lower())
        if pinned is not None and pinned != parts[i]:
            parts[i] = pinned
            changed = True
    return "/".join(parts) if changed else rel_str


def canonicalize_dir_casing(
    rel_paths: "list[str]",
    strategy: str = FILEMAP_CASING_UPPER,
    pins: "dict[str, str] | None" = None,
) -> "dict[str, str]":
    """Map each rel path to the same path with case-variant FOLDER segments
    unified to one canonical casing. Filenames are never touched.

    Exposed so the installer can merge ONE mod's ``meshes/`` + ``Meshes/`` in
    staging using exactly the pick the filemap makes at deploy time - otherwise
    staging and the Data tab would disagree about which casing won.
    force_lower/force_upper collapse to their picking direction here: staging
    only merges variants, it never renames files.
    """
    if strategy == FILEMAP_CASING_FORCE_LOWER:
        strategy = FILEMAP_CASING_LOWER
    elif strategy == FILEMAP_CASING_FORCE_UPPER:
        strategy = FILEMAP_CASING_UPPER
    elif strategy not in _VALID_FILEMAP_CASINGS:
        strategy = FILEMAP_CASING_UPPER
    unique_dirs: dict[str, None] = {}
    for rel in rel_paths:
        slash = rel.rfind("/")
        if slash >= 0:
            unique_dirs[rel[:slash]] = None
    dir_rewrite = _apply_canonical(
        _collect_canonical(unique_dirs, strategy), unique_dirs)
    out: dict[str, str] = {}
    for rel in rel_paths:
        new = rel
        slash = rel.rfind("/")
        if slash >= 0:
            nd = dir_rewrite.get(rel[:slash])
            if nd is not None:
                new = nd + rel[slash:]
        # Folder pins only: staging merges case-variant folders but must never
        # rename a file on disk - deploy applies filename pins to the link name.
        out[rel] = _pin_rel_str(new, pins, False) if pins else new
    return out


def _apply_casing_pins_tuplemap(
    pins: dict[str, str],
    *tuple_maps: dict[str, tuple[str, str]],
) -> None:
    """Apply casing pins to ``{rel_key: (rel_str, mod_name)}`` maps, in place.

    Same pinning rule as :func:`_apply_casing_pins`, but for the winner-map
    shape used at write time (both the full-rebuild and incremental paths
    converge on this shape just before ``_write_filemap``).  Applying it at
    both write sites keeps a pinned folder identical regardless of which path
    produced the map - pins are deterministic and path-independent, so the two
    can never disagree.
    """
    if not pins:
        return
    for tmap in tuple_maps:
        if not tmap:
            continue
        for rel_key, (rel_str, mod_name) in tmap.items():
            new_rs = _pin_rel_str(rel_str, pins)
            if new_rs != rel_str:
                tmap[rel_key] = (new_rs, mod_name)


# ---------------------------------------------------------------------------
# Mod index - persistent cache of each mod's file list
# ---------------------------------------------------------------------------

def read_mod_index(
    index_path: Path,
) -> dict[str, tuple[dict[str, str], dict[str, str]]] | None:
    """Read modindex.bin and return {mod_name: (normal_files, root_files)}.

    Returns None if the index does not exist or has an unrecognised version
    (caller should fall back to a full disk scan).
    Paths in the returned dicts reflect raw on-disk casing per mod - folder
    case normalization across mods is applied at filemap-build time, not in
    the index.
    Results are cached in memory by (path, stat) so repeated calls within
    the same session are free.
    """
    path_str = str(index_path)
    with _index_cache_lock:
        try:
            stat_key = _index_stat_key(index_path)
        except OSError:
            return None
        cached = _index_cache.get(path_str)
        if cached is not None and cached[0] == stat_key:
            return cached[1]
    try:
        with perftrace.span("read_mod_index: COLD parse"):
            with index_path.open("rb") as f:
                data = msgpack.unpack(f, raw=False)
            if not isinstance(data, dict) or data.get("v") != _INDEX_VERSION:
                return None
            index: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
            for mod_name, files in data["mods"]:
                normal: dict[str, str] = {}
                root:   dict[str, str] = {}
                for rel_key, rel_str, kind in files:
                    # Self-heal older indexes built before macOS-junk filtering:
                    # drop ._* / .DS_Store / __MACOSX entries on read so they never
                    # reach the filemap or deploy.
                    if any(_is_macos_junk(seg) for seg in rel_str.split("/")):
                        continue
                    (root if kind == "r" else normal)[rel_key] = rel_str
                index[mod_name] = (normal, root)
    except Exception:
        return None
    with _index_cache_lock:
        _index_cache_store(path_str, stat_key, index)
    return index


def _drop_non_utf8_entries(
    index: dict[str, tuple[dict[str, str], dict[str, str]]],
) -> tuple[dict[str, tuple[dict[str, str], dict[str, str]]], list[str]]:
    """Filter out entries msgpack cannot UTF-8-encode.

    Returns (clean_index, dropped_descriptions). A mod whose NAME is bad is
    dropped whole; a mod with bad rel paths keeps its good files. Descriptions
    are backslash-escaped, safe for any log sink."""
    clean: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
    dropped: list[str] = []
    for mod_name, (normal, root) in index.items():
        if not _is_utf8_safe(mod_name):
            dropped.append(f"mod \"{_safe_log_str(mod_name)}\" (bad folder name)")
            continue
        bad_rels = [v for v in (*normal.values(), *root.values())
                    if not _is_utf8_safe(v)]
        if not bad_rels:
            clean[mod_name] = (normal, root)
            continue
        dropped.extend(f"{mod_name}: {_safe_log_str(v)}" for v in bad_rels)
        clean[mod_name] = (
            {k: v for k, v in normal.items() if _is_utf8_safe(v)},
            {k: v for k, v in root.items() if _is_utf8_safe(v)},
        )
    return clean, dropped


def _write_mod_index(
    index_path: Path,
    index: dict[str, tuple[dict[str, str], dict[str, str]]],
    normalize_folder_case: bool = True,
) -> None:
    """Write the full index atomically, then update the cache.

    The *normalize_folder_case* parameter is retained for API compatibility
    but is now a no-op: cross-mod folder-case normalization happens at
    filemap-build time, not in the index. The index always stores raw
    on-disk casing per mod.
    """
    del normalize_folder_case  # retained for back-compat; see docstring
    mods = []
    for mod_name, (normal, root) in index.items():
        files = [[k, v, "n"] for k, v in normal.items()]
        files += [[k, v, "r"] for k, v in root.items()]
        mods.append([mod_name, files])
    payload = {"v": _INDEX_VERSION, "mods": mods}
    # Per-process temp suffix: a second PROCESS (two app instances, CLI + GUI)
    # writing concurrently must not truncate/rename this writer's temp file -
    # a fixed ".tmp" let one writer rename the other's half-written file over
    # the real index. In-process writers are serialized by _index_write_lock.
    tmp_suffix = f".tmp-{os.getpid()}"
    with _index_write_lock:
        try:
            with atomic_writer(index_path, "wb", encoding=None,
                               suffix=tmp_suffix) as f:
                msgpack.pack(payload, f, use_bin_type=True)
        except UnicodeEncodeError:
            # A mod name or rel path carries surrogate-escaped non-UTF-8 bytes
            # msgpack cannot serialize. Scan-time filters catch files inside
            # mods, but e.g. a non-UTF-8 mod FOLDER name or a caller-supplied
            # rel can still reach here - and one bad entry must not block the
            # ENTIRE index write (the historical "index permanently stale"
            # failure). Drop the unencodable entries, warn, and write the rest;
            # a later Refresh (after repair_nonutf8_names) restores them.
            index, dropped = _drop_non_utf8_entries(index)
            try:
                print(f"[filemap] WARN: modindex.bin write dropped "
                      f"non-UTF-8 entr(y/ies): {', '.join(dropped)}",
                      file=sys.stderr, flush=True)
            except Exception:
                pass
            mods = []
            for mod_name, (normal, root) in index.items():
                files = [[k, v, "n"] for k, v in normal.items()]
                files += [[k, v, "r"] for k, v in root.items()]
                mods.append([mod_name, files])
            payload = {"v": _INDEX_VERSION, "mods": mods}
            with atomic_writer(index_path, "wb", encoding=None,
                               suffix=tmp_suffix) as f:
                msgpack.pack(payload, f, use_bin_type=True)
        # Sweep temp litter: legacy fixed-name ".tmp" strays and pid-suffixed
        # temps from crashed runs (this process's own temp was just renamed).
        try:
            for stray in index_path.parent.glob(index_path.name + ".tmp*"):
                stray.unlink()
        except OSError:
            pass
    # Update the in-memory index cache to match what was just written.
    with _index_cache_lock:
        try:
            _index_cache_store(str(index_path), _index_stat_key(index_path), index)
        except OSError:
            _index_cache.pop(str(index_path), None)
    # Invalidate the filemap skip-cache: the index changed so the next
    # build_filemap() must write a fresh filemap.txt regardless.
    profile_dir = str(index_path.parent)
    with _filemap_winner_cache_lock:
        for key in list(_filemap_winner_cache):
            if key.startswith(profile_dir):
                del _filemap_winner_cache[key]
    # The incremental state mirrors the index - drop it so the next
    # build_filemap() does a full merge and repopulates from fresh data.
    _drop_incr_states_under(profile_dir)


def update_mod_index(
    index_path: Path,
    mod_name: str,
    normal_files: dict[str, str],
    root_files: dict[str, str],
    normalize_folder_case: bool = True,
    log_fn: "Callable[[str], None] | None" = None,
) -> None:
    """Add or replace a single mod's entry in the index.

    Reads the existing index (if any), replaces the entry for mod_name,
    and writes the result atomically.  Call this after installing a mod.

    If an index file EXISTS but cannot be read (corrupt, truncated, or a
    future version), the update is SKIPPED: rewriting from a failed read
    would produce an index containing only *mod_name*, silently dropping
    every other mod until the next full Refresh. The bad file is left for
    rebuild_mod_index (Refresh) to replace wholesale.
    """
    with _index_write_lock:
        index = read_mod_index(index_path)
        if index is None and index_path.is_file():
            if log_fn is not None:
                try:
                    log_fn(f"WARN: modindex.bin exists but is unreadable - "
                           f"skipped index update for \"{_safe_log_str(mod_name)}\" "
                           f"(a full Refresh will rebuild the index).")
                except Exception:
                    pass
            return
        index = index or {}
        index[mod_name] = (normal_files, root_files)
        _write_mod_index(index_path, index, normalize_folder_case=normalize_folder_case)


def remove_from_mod_index(
    index_path: Path,
    mod_names: list[str],
    normalize_folder_case: bool = True,
) -> None:
    """Remove one or more mods from the index and rewrite it atomically.

    Call this after deleting mod folders from staging.
    No-op if the index does not exist or the mod is not in it.
    """
    if not index_path.is_file():
        return
    with _index_write_lock:
        index = read_mod_index(index_path)
        if not index:
            return
        changed = False
        for name in mod_names:
            if name in index:
                del index[name]
                changed = True
        if changed:
            _write_mod_index(index_path, index, normalize_folder_case=normalize_folder_case)


def rename_in_mod_index(
    index_path: Path,
    old_name: str,
    new_name: str,
    normalize_folder_case: bool = True,
) -> None:
    """Rename a mod's entry in the index from *old_name* to *new_name*.

    Call this after renaming a mod's staging folder so build_filemap() can
    still find its files (build_filemap keys the index by the modlist name).
    No-op if the index does not exist or the old name is not in it.
    """
    if not index_path.is_file() or old_name == new_name:
        return
    with _index_write_lock:
        index = read_mod_index(index_path)
        if not index or old_name not in index:
            return
        index[new_name] = index.pop(old_name)
        _write_mod_index(index_path, index, normalize_folder_case=normalize_folder_case)


def rebuild_mod_index(
    index_path: Path,
    staging_root: Path,
    strip_prefixes: set[str] | None = None,
    per_mod_strip_prefixes: dict[str, list[str]] | None = None,
    allowed_extensions: set[str] | None = None,
    root_deploy_folders: set[str] | None = None,  # unused, kept for call-site compat
    normalize_folder_case: bool = True,
    exclude_dirs: frozenset[str] | None = None,
    log_fn: "Callable[[str], None] | None" = None,
    root_folder_mods: set[str] | None = None,
    follow_toplevel_links_under: "Path | None" = None,
) -> None:
    """Scan every mod folder under staging_root and rewrite the full index.

    This is the slow path, triggered by the Refresh button.  Normal filemap
    rebuilds (enable/disable/reorder) use the cached index instead.

    The overwrite folder is also indexed under OVERWRITE_NAME.

    root_folder_mods - names of mods marked root_folder=True. These are deployed
    verbatim to the game root, so the global strip_prefixes (e.g. Bethesda's
    ``Data``) must NOT be applied: a SKSE-style mod ships ``Data/Scripts/...``
    plus loose ``.exe`` files at top level; stripping ``Data/`` would dump the
    Scripts subtree at the game root instead of inside ``<game>/Data/``.

    follow_toplevel_links_under - when set, a top-level symlinked mod dir is
    followed IF its resolved target lives under this root (callers pass the
    game's profiles/ dir so Profile Group links resolve); anything else keeps
    the historical skip+WARN. _scan_dir never follows nested symlinks, so a
    link smuggled inside a mod (malicious archive) stays un-indexed.
    """
    _strip = frozenset(s.lower() for s in strip_prefixes) if strip_prefixes else frozenset()
    _per_mod = per_mod_strip_prefixes or {}
    _root_mods = root_folder_mods or set()
    _exts  = frozenset(e.lower() for e in allowed_extensions) if allowed_extensions else frozenset()
    _root  = frozenset()  # root_deploy_folders routing removed; param kept for compat
    _excl_dirs = exclude_dirs if exclude_dirs is not None else frozenset()

    staging_str   = str(staging_root)
    overwrite_str = str(staging_root.parent / "overwrite")

    # Collect all mod folders that exist on disk
    scan_targets: list[tuple[str, str]] = []
    skipped_nondir: list[str] = []
    skipped_badname: list[str] = []
    try:
        with os.scandir(staging_str) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    # The folder name becomes the index KEY - a non-UTF-8
                    # (surrogate) name makes msgpack refuse to serialize the
                    # whole payload, blocking the ENTIRE index write. Skip it
                    # here (repair_nonutf8_names usually renames it on the
                    # next Refresh before this runs).
                    if not _is_utf8_safe(entry.name):
                        skipped_badname.append(entry.name)
                        continue
                    scan_targets.append((entry.name, entry.path))
                elif entry.is_dir(follow_symlinks=True):
                    # A SYMLINK pointing at a directory. Followed only when the
                    # caller passed follow_toplevel_links_under AND the resolved
                    # target is contained in it (Profile Group links into member
                    # profiles). Otherwise: the modlist sync adopts it (pathlib
                    # is_dir follows links) but this index scan skips it - so
                    # the mod appears in the list yet deploys nothing. Record +
                    # warn in that case.
                    followed = False
                    if follow_toplevel_links_under is not None:
                        if not _is_utf8_safe(entry.name):
                            skipped_badname.append(entry.name)
                            continue
                        try:
                            resolved = Path(entry.path).resolve(strict=True)
                            resolved.relative_to(
                                Path(follow_toplevel_links_under).resolve())
                        except (OSError, ValueError, RuntimeError):
                            pass
                        else:
                            scan_targets.append((entry.name, entry.path))
                            followed = True
                    if not followed:
                        skipped_nondir.append(entry.name)
    except OSError as scan_err:
        # Staging root unreadable (unmounted SD card, permission loss).
        # Writing an index from this state would WIPE every mod's entry -
        # abort without touching the existing index instead.
        if log_fn is not None:
            try:
                log_fn(f"WARN: staging folder could not be read "
                       f"({scan_err}) - modindex.bin left unchanged.")
            except Exception:
                pass
        return
    if skipped_nondir and log_fn is not None:
        # Names come raw from disk and may carry surrogate bytes - escape them
        # and never let a log-sink failure abort the rescan (same hardening as
        # the non-UTF-8 warning below; an unguarded sink crash here would
        # leave the index permanently stale).
        try:
            log_fn(f"WARN: {len(skipped_nondir)} staging entr(y/ies) are SYMLINKS to "
                   f"directories and were NOT indexed (they deploy nothing yet show "
                   f"in the modlist): "
                   f"{', '.join(_safe_log_str(n) for n in skipped_nondir[:10])}")
        except Exception:
            pass
    if skipped_badname and log_fn is not None:
        try:
            log_fn(f"WARN: {len(skipped_badname)} mod folder(s) skipped - folder "
                   f"NAME is not valid UTF-8 (rename to fix): "
                   f"{', '.join(_safe_log_str(n) for n in skipped_badname[:10])}")
        except Exception:
            pass
    scan_targets.append((OVERWRITE_NAME, overwrite_str))

    def _strip_for_mod(name: str) -> frozenset[str]:
        if name in _root_mods:
            return frozenset()
        mod_strip = _per_mod.get(name)
        if not mod_strip:
            return _strip
        segment_names = [s for s in mod_strip if "/" not in s]
        return _strip | frozenset(s.lower() for s in segment_names)

    def _path_prefixes_for_mod(name: str) -> list[str]:
        if name in _root_mods:
            return []
        mod_strip = _per_mod.get(name)
        if not mod_strip:
            return []
        return [s for s in mod_strip if "/" in s]

    futures = {
        _POOL.submit(
            _scan_dir, name, d, _strip_for_mod(name), _exts, _root,
            strip_path_prefixes=_path_prefixes_for_mod(name),
            exclude_dirs=_excl_dirs,
        ): name
        for name, d in scan_targets
    }

    index: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
    try:
        for fut in as_completed(futures, timeout=_SCAN_TIMEOUT):
            name, normal, root, invalid_names = fut.result()
            if invalid_names:
                if log_fn is not None:
                    # The names are KNOWN non-UTF-8 - escape them, and never let a
                    # log-sink failure abort the rescan (an unguarded print of the
                    # raw names crashed here and left the index permanently stale).
                    try:
                        log_fn(
                            f"WARN: Mod \"{_safe_log_str(name)}\" skipped - contains "
                            f"file(s) with non-UTF-8 name(s): "
                            f"{', '.join(_safe_log_str(n) for n in invalid_names)}"
                        )
                    except Exception:
                        pass
                continue  # skip the entire mod
            index[name] = (normal, root)
    except _FuturesTimeoutError:
        # A scan worker is stuck (hung mount / dying disk). Writing what we
        # have would silently drop the unscanned mods - keep the old index.
        stuck = [futures[f] for f in futures if not f.done()]
        if log_fn is not None:
            try:
                log_fn(f"WARN: index rescan timed out after {_SCAN_TIMEOUT:.0f}s "
                       f"waiting on: "
                       f"{', '.join(_safe_log_str(n) for n in stuck[:10])} - "
                       f"modindex.bin left unchanged (is the staging drive "
                       f"responding?).")
            except Exception:
                pass
        return

    _write_mod_index(index_path, index, normalize_folder_case=normalize_folder_case)


def rescan_mods_in_index(
    index_path: Path,
    staging_root: Path,
    mod_names: list[str],
    strip_prefixes: set[str] | None = None,
    per_mod_strip_prefixes: dict[str, list[str]] | None = None,
    allowed_extensions: set[str] | None = None,
    normalize_folder_case: bool = True,
    exclude_dirs: frozenset[str] | None = None,
    log_fn: "Callable[[str], None] | None" = None,
    root_folder_mods: set[str] | None = None,
) -> None:
    """Re-scan a specific subset of mods and update their entries in the index.

    Used when a mod's root_folder flag is toggled: a root-flagged mod must not
    have ``strip_prefixes`` (e.g. Bethesda's ``Data``) applied to its files,
    otherwise the cached paths point to the wrong deploy location once the
    flag flips.  Rescanning just the affected mods is far cheaper than a full
    Refresh.
    """
    if not mod_names:
        return
    _strip = frozenset(s.lower() for s in strip_prefixes) if strip_prefixes else frozenset()
    _per_mod = per_mod_strip_prefixes or {}
    _root_mods = root_folder_mods or set()
    _exts  = frozenset(e.lower() for e in allowed_extensions) if allowed_extensions else frozenset()
    _excl_dirs = exclude_dirs if exclude_dirs is not None else frozenset()

    def _warn_unreadable() -> None:
        # An existing-but-unreadable index means a subset rewrite would WIPE
        # every other mod's entry - skip and leave it for a full Refresh.
        if log_fn is not None:
            try:
                log_fn("WARN: modindex.bin exists but is unreadable - skipped "
                       "partial index update (a full Refresh will rebuild it).")
            except Exception:
                pass

    if read_mod_index(index_path) is None and index_path.is_file():
        _warn_unreadable()   # advisory early-out; re-checked under the lock
        return

    def _strip_for_mod(name: str) -> frozenset[str]:
        if name in _root_mods:
            return frozenset()
        mod_strip = _per_mod.get(name)
        if not mod_strip:
            return _strip
        segment_names = [s for s in mod_strip if "/" not in s]
        return _strip | frozenset(s.lower() for s in segment_names)

    def _path_prefixes_for_mod(name: str) -> list[str]:
        if name in _root_mods:
            return []
        mod_strip = _per_mod.get(name)
        if not mod_strip:
            return []
        return [s for s in mod_strip if "/" in s]

    targets: list[tuple[str, str]] = []
    for name in mod_names:
        mod_dir = staging_root / name
        if mod_dir.is_dir():
            targets.append((name, str(mod_dir)))
    if not targets:
        return

    futures = {
        _POOL.submit(
            _scan_dir, name, d, _strip_for_mod(name), _exts, frozenset(),
            strip_path_prefixes=_path_prefixes_for_mod(name),
            exclude_dirs=_excl_dirs,
        ): name
        for name, d in targets
    }
    scanned: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
    try:
        for fut in as_completed(futures, timeout=_SCAN_TIMEOUT):
            name, normal, root, invalid_names = fut.result()
            if invalid_names:
                if log_fn is not None:
                    try:  # escaped names + guarded: logging must never abort the write
                        log_fn(
                            f"WARN: Mod \"{_safe_log_str(name)}\" skipped - contains "
                            f"file(s) with non-UTF-8 name(s): "
                            f"{', '.join(_safe_log_str(n) for n in invalid_names)}"
                        )
                    except Exception:
                        pass
                continue
            scanned[name] = (normal, root)
    except _FuturesTimeoutError:
        stuck = [futures[f] for f in futures if not f.done()]
        if log_fn is not None:
            try:
                log_fn(f"WARN: index rescan timed out after {_SCAN_TIMEOUT:.0f}s "
                       f"waiting on: "
                       f"{', '.join(_safe_log_str(n) for n in stuck[:10])} - "
                       f"modindex.bin left unchanged (is the staging drive "
                       f"responding?).")
            except Exception:
                pass
        return
    if not scanned:
        return

    # Merge under the write lock against a FRESH read so a concurrent
    # writer's changes (install finishing, overwrite update) aren't lost.
    with _index_write_lock:
        index = read_mod_index(index_path)
        if index is None and index_path.is_file():
            _warn_unreadable()
            return
        index = index or {}
        index.update(scanned)
        _write_mod_index(index_path, index, normalize_folder_case=normalize_folder_case)


def _compute_conflict_status(
    priority_order: list[str],
    overrides: dict[str, set[str]],
    overridden_by: dict[str, set[str]],
    win_count: dict[str, int],
    mods_with_files: set[str],
) -> dict[str, int]:
    """Classify each mod's conflict status based on override relationships."""
    conflict_map: dict[str, int] = {}
    for name in priority_order:
        has_wins  = bool(overrides[name])
        has_loses = bool(overridden_by[name])
        if name not in mods_with_files or (not has_wins and not has_loses):
            conflict_map[name] = CONFLICT_NONE
        elif has_loses and win_count.get(name, 0) <= 0:
            conflict_map[name] = CONFLICT_FULL
        elif has_wins and not has_loses:
            conflict_map[name] = CONFLICT_WINS
        elif has_loses and not has_wins:
            conflict_map[name] = CONFLICT_LOSES
        else:
            conflict_map[name] = CONFLICT_PARTIAL
    return conflict_map


def _render_filemap(
    filemap: dict[str, tuple[str, str]],
) -> tuple[list[str], list[str]]:
    """Sort the filemap and render one output line per entry.

    Returns (sorted_keys, lines) - parallel lists, UNfiltered by
    disabled_plugins (that filter is applied at join/write time so the
    rendered cache stays valid when the disabled set changes).
    """
    sorted_keys = sorted(filemap)
    lines: list[str] = []
    for rel_key in sorted_keys:
        rel_str, mod_name = filemap[rel_key]
        lines.append(f"{rel_str}\t{mod_name}\n")
    return sorted_keys, lines


def _join_filtered(
    sorted_keys: list[str],
    lines: list[str],
    filemap: dict[str, tuple[str, str]],
    disabled_lower: dict[str, frozenset[str]],
) -> str:
    """Join rendered lines, skipping disabled root-level plugin lines."""
    if disabled_lower:
        parts: list[str] = []
        for rel_key, line in zip(sorted_keys, lines):
            # Skip root-level files that the user has disabled for this mod
            if "/" not in rel_key:
                mod_name = filemap[rel_key][1]
                if mod_name in disabled_lower and rel_key in disabled_lower[mod_name]:
                    continue
            parts.append(line)
        return "".join(parts)
    return "".join(lines)


def _join_and_write(
    output_path: Path,
    sorted_keys: list[str],
    lines: list[str],
    filemap: dict[str, tuple[str, str]],
    disabled_lower: dict[str, frozenset[str]],
) -> int:
    """Join rendered lines (skipping disabled root-level plugins) and write.

    Atomic (write-temp → rename) - concurrent readers (plugin resolution,
    Data tab, deploy) must never observe a partially-written filemap.
    Returns the number of lines written.
    """
    output = _join_filtered(sorted_keys, lines, filemap, disabled_lower)
    # surrogateescape: deployed relative paths may carry non-UTF-8 filename
    # bytes (surfaced as surrogate code points by the filesystem decode).
    write_atomic_text(output_path, output, errors="surrogateescape")
    return output.count("\n")


def _write_filemap(
    output_path: Path,
    filemap: dict[str, tuple[str, str]],
    disabled_lower: dict[str, frozenset[str]],
) -> int:
    """Sort and write filemap.txt, returning the number of lines written."""
    sorted_keys, lines = _render_filemap(filemap)
    return _join_and_write(output_path, sorted_keys, lines, filemap, disabled_lower)


class _PathFilters:
    """Compiled per-file acceptance filters for the merge.

    Built once per build_filemap() call and shared by the full merge loop and
    the incremental fast path - both must apply EXACTLY the same logic (same
    compiled regex objects, same check order).
    """
    __slots__ = ("ignore_re", "loose_excl_re", "allowed_top", "excluded",
                 "folder_ignore_re", "_dir_cache")

    def __init__(
        self,
        ignore_re: "re.Pattern[str] | None",
        loose_excl_re: "re.Pattern[str] | None",
        allowed_top: "set[str] | None",
        excluded: dict[str, set[str]],
        folder_ignore_re: "re.Pattern[str] | None" = None,
    ):
        self.ignore_re = ignore_re
        self.loose_excl_re = loose_excl_re
        self.allowed_top = allowed_top
        self.excluded = excluded
        self.folder_ignore_re = folder_ignore_re
        self._dir_cache: dict[str, bool] = {}

    def dir_ignored(self, rel_key: str) -> bool:
        """True when any directory segment of rel_key matches folder_ignore_re."""
        if self.folder_ignore_re is None or "/" not in rel_key:
            return False
        dirpath = rel_key.rsplit("/", 1)[0]
        hit = self._dir_cache.get(dirpath)
        if hit is None:
            match = self.folder_ignore_re.match
            hit = any(match(seg) for seg in dirpath.split("/"))
            self._dir_cache[dirpath] = hit
        return hit

    def accepts(self, mod: str, rel_key: str) -> bool:
        """Mirror of the merge-loop filter chain (same order, same semantics)."""
        exc = self.excluded.get(mod)
        if exc and rel_key in exc:
            return False
        if (self.loose_excl_re is not None and "/" not in rel_key
                and self.loose_excl_re.match(rel_key)):
            return False
        if self.allowed_top is not None:
            slash = rel_key.find("/")
            if slash != -1 and rel_key[:slash] not in self.allowed_top:
                return False
        if (self.ignore_re is not None
                and self.ignore_re.match(rel_key.rsplit("/", 1)[-1])):
            return False
        if self.dir_ignored(rel_key):
            return False
        return True


def _build_path_filters(
    conflict_ignore_filenames: "set[str] | None",
    excluded_loose_filenames: "set[str] | None",
    allowed_top_level_folders: "set[str] | None",
    excluded_mod_files: "dict[str, set[str]] | None",
    conflict_ignore_foldernames: "set[str] | None" = None,
) -> _PathFilters:
    """Compile the per-file filter inputs into a shared _PathFilters object."""
    # Pre-compile ignore patterns once into a single regex for O(1) matching.
    # `<name>.*` is expanded to also match the extensionless `<name>` so users
    # can ignore e.g. both `LICENCE` and `LICENCE.txt` with one pattern.
    _ignore_re: "re.Pattern[str] | None" = None
    if conflict_ignore_filenames:
        parts: list[str] = []
        for p in conflict_ignore_filenames:
            pl = p.lower()
            parts.append(fnmatch.translate(pl))
            if pl.endswith(".*") and "*" not in pl[:-2] and "?" not in pl[:-2]:
                parts.append(fnmatch.translate(pl[:-2]))
        _ignore_re = re.compile("|".join(parts))

    # Folder-name ignore patterns: a match on any directory segment drops the
    # whole subtree from the filemap (no `<name>.*` expansion - file-specific).
    _folder_ignore_re: "re.Pattern[str] | None" = None
    if conflict_ignore_foldernames:
        _folder_ignore_re = re.compile(
            "|".join(fnmatch.translate(p.lower())
                     for p in conflict_ignore_foldernames)
        )

    # Pre-compile loose-filename exclusion patterns.  Matches drop the file
    # from the filemap entirely, but only when the file is loose (no "/" in
    # its rel_key, i.e. it sits at the mod's top level).
    _loose_excl_re: "re.Pattern[str] | None" = None
    if excluded_loose_filenames:
        _loose_excl_re = re.compile(
            "|".join(fnmatch.translate(p.lower()) for p in excluded_loose_filenames)
        )

    # Lowercase allowed top-level folder names.  When set, any foldered entry
    # whose first path segment is not in this set is dropped.  Loose top-level
    # files (no "/") are intentionally left for the routing rules / the loose
    # exclusion above to handle, so game-specific loose routing still works.
    _allowed_top: "set[str] | None" = (
        {f.lower() for f in allowed_top_level_folders}
        if allowed_top_level_folders else None
    )

    return _PathFilters(_ignore_re, _loose_excl_re, _allowed_top,
                        excluded_mod_files or {}, _folder_ignore_re)


# ---------------------------------------------------------------------------
# Main filemap builder
# ---------------------------------------------------------------------------

def build_filemap(
    modlist_path: Path,
    staging_root: Path,
    output_path: Path,
    strip_prefixes: set[str] | None = None,
    per_mod_strip_prefixes: dict[str, list[str]] | None = None,
    allowed_extensions: set[str] | None = None,
    root_deploy_folders: set[str] | None = None,  # unused, kept for call-site compat
    disabled_plugins: dict[str, list[str]] | None = None,
    conflict_ignore_filenames: set[str] | None = None,
    conflict_ignore_foldernames: set[str] | None = None,
    excluded_loose_filenames: set[str] | None = None,
    allowed_top_level_folders: set[str] | None = None,
    excluded_mod_files: dict[str, set[str]] | None = None,
    normalize_folder_case: bool = True,
    filemap_casing: str = FILEMAP_CASING_UPPER,
    filemap_casing_pins: dict[str, str] | None = None,
    conflict_key_fn: "Callable[[str, str], str] | None" = None,
    exclude_dirs: frozenset[str] | None = None,
    log_fn: "Callable[[str], None] | None" = None,
    root_folder_mods: set[str] | None = None,
    root_mod_files: dict[str, set[str]] | None = None,
    follow_toplevel_links_under: "Path | None" = None,
    identity_ck_prefix: str | None = None,
    conflict_extras: dict | None = None,
) -> tuple[int, dict[str, int], dict[str, set[str]], dict[str, set[str]]]:
    """
    Build filemap.txt from the current modlist.

    Reads file lists from modindex.bin (fast path) when available.
    Falls back to a full disk scan if the index is missing or corrupt,
    and writes a fresh index as a side-effect of that scan.

    per_mod_strip_prefixes - optional dict mapping mod name to a list of
    top-level folder names to strip for that mod only (contents move up one
    level during deployment).  Merged with strip_prefixes when scanning.

    allowed_extensions - when non-empty, only files with a matching lowercase
    extension (e.g. {".pak"}) are included in the filemap.  Pass None or an
    empty set to include all files (default behaviour).

    root_deploy_folders - no longer used; kept for call-site compatibility.
    Previously wrote a ``filemap_root.txt``; routing is now done via
    ``custom_routing_rules`` at deploy time.

    conflict_ignore_filenames - lowercase filename glob patterns (not paths);
    matching files are dropped from the filemap entirely - never deployed and
    never conflict-tracked.  Pass None or an empty set to disable.

    conflict_ignore_foldernames - lowercase folder-name glob patterns; a
    folder whose name matches (at any depth) is dropped from the filemap along
    with its entire subtree - never deployed and never conflict-tracked.
    Pass None or an empty set to disable.

    excluded_loose_filenames - lowercase glob patterns; matching files are
    dropped from the filemap entirely, but only when the file is loose (no
    parent folder).  Same-named files nested in folders are unaffected.

    allowed_top_level_folders - when non-empty, any foldered entry whose first
    path segment is not in this set is dropped from the filemap.  Loose
    top-level files (no folder) are not affected by this rule.

    excluded_mod_files - dict mapping mod name to a set of lowercase rel_key
    paths that should be excluded from the filemap for that mod.  Excluded
    files are treated as if the mod does not have them, so the next
    lower-priority mod that has the same file wins instead.

    root_mod_files - per-mod index keys routed to the game-root namespace
    (filemap_root.txt) instead of the Data one.  Exclusions win over root tags;
    whole-mod root flags make per-file tags redundant.

    Returns:
        (count, conflict_map, overrides, overridden_by)
    """
    # Normalize pin keys to lowercase once (segment-name match is case-insensitive).
    _pins: dict[str, str] = (
        {k.lower(): v for k, v in filemap_casing_pins.items()}
        if filemap_casing_pins else {}
    )

    # Per-mod root-tagged rel_keys - frozen once for O(1) merge-loop membership.
    _root_files: dict[str, frozenset[str]] = {
        m: frozenset(v) for m, v in (root_mod_files or {}).items() if v
    }

    entries = read_modlist(modlist_path)

    # Only enabled, non-separator mods
    enabled = [e for e in entries if not e.is_separator and e.enabled]

    # Walk lowest-priority → highest-priority so higher-priority mods win
    # (modlist index 0 = highest priority, last index = lowest priority)
    enabled_low_to_high = list(reversed(enabled))

    priority_order = [e.name for e in enabled_low_to_high if e.name != ROOT_FOLDER_NAME] + [OVERWRITE_NAME]

    index_path = output_path.parent / "modindex.bin"
    index = read_mod_index(index_path)

    if index is None:
        # Index missing or corrupt - fall back to full disk scan and rebuild it.
        rebuild_mod_index(
            index_path, staging_root,
            strip_prefixes=strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip_prefixes,
            allowed_extensions=allowed_extensions,
            normalize_folder_case=normalize_folder_case,
            exclude_dirs=exclude_dirs,
            log_fn=log_fn,
            root_folder_mods=root_folder_mods,
            follow_toplevel_links_under=follow_toplevel_links_under,
        )
        index = read_mod_index(index_path) or {}

    # Mods with legacy surrogate-encoded file names (skipped below). The sweep
    # is mtime-cached - per-toggle rebuilds otherwise re-encode ~100k names.
    _utf8_bad = mod_index_utf8_unsafe(index_path)

    # Incremental fast-path bookkeeping. UE5 conflict_key_fn builds are never
    # tracked (retroactive-erasure semantics don't fit provider stacks); the
    # state mirrors modindex.bin, so its mtime anchors validity.
    _output_key = str(output_path)
    _incr_on = _incremental_enabled() and conflict_key_fn is None
    _incr_fp: tuple = ()
    if _incr_on:
        try:
            _index_mtime = index_path.stat().st_mtime_ns
        except OSError:
            _incr_on = False
        else:
            _incr_fp = _build_incr_fingerprint(
                index_path, _index_mtime, modlist_path, staging_root,
                strip_prefixes, per_mod_strip_prefixes, allowed_extensions,
                exclude_dirs, conflict_ignore_filenames,
                conflict_ignore_foldernames,
                excluded_loose_filenames, allowed_top_level_folders,
                excluded_mod_files, normalize_folder_case, filemap_casing,
                _pins, conflict_key_fn, root_folder_mods, _root_files,
                _utf8_bad,
            )
    if not _incr_on:
        _drop_incr_state(_output_key)

    # Compile the per-file acceptance filters once; the same object drives
    # both the full merge loop below and the incremental fast path.
    _pf = _build_path_filters(
        conflict_ignore_filenames, excluded_loose_filenames,
        allowed_top_level_folders, excluded_mod_files,
        conflict_ignore_foldernames,
    )
    _ignore_re = _pf.ignore_re
    _loose_excl_re = _pf.loose_excl_re
    _allowed_top = _pf.allowed_top
    _excluded = _pf.excluded

    def _is_ignored(rel_key: str) -> bool:
        if _ignore_re is None:
            return False
        return bool(_ignore_re.match(rel_key.rsplit("/", 1)[-1]))

    def _is_excluded_loose(rel_key: str) -> bool:
        if _loose_excl_re is None or "/" in rel_key:
            return False
        return bool(_loose_excl_re.match(rel_key))

    def _is_unknown_top_level(rel_key: str) -> bool:
        if _allowed_top is None:
            return False
        slash = rel_key.find("/")
        if slash == -1:
            return False
        return rel_key[:slash] not in _allowed_top

    # Per-mod disabled-plugin sets (lowercase filenames, root-level only) -
    # write-time filter, needed by both the fast path and the full path.
    _disabled_lower = _get_disabled_lower(disabled_plugins) if disabled_plugins else {}
    _disabled_frozen = (
        frozenset(_disabled_lower.items())
        if _disabled_lower else frozenset()
    )

    # --- incremental fast path -------------------------------------------
    # In verify mode the delta runs non-destructively on a clone and the full
    # rebuild below stays authoritative; its outputs are compared at the end.
    _verify = os.environ.get("AMM_FILEMAP_VERIFY") == "1"
    _verify_fast = None
    if _incr_on:
        with perftrace.span("filemap: incremental fast path"):
            _fast = _try_incremental(
                _output_key, _incr_fp, priority_order, index, _pf, _utf8_bad,
                root_folder_mods, _root_files, _disabled_lower, _disabled_frozen,
                output_path, normalize_folder_case, filemap_casing,
                log_fn, dry_run=_verify,
            )
        if _fast is not None:
            if _verify:
                _verify_fast = _fast
            else:
                _count, _cmap, _ov, _ob, _ = _fast
                return _count, _cmap, _ov, _ob

    # Single-pass merge: priority order (low→high) so later mods overwrite earlier ones.
    # Root-flagged mods get their own independent winner namespace (they deploy to
    # the game root, not Data/, so they should conflict only among themselves).
    # Both namespaces share priority_order, overrides/overridden_by, and win_count
    # so the UI still shows conflicts for root-flagged mods.
    filemap_winner: dict[str, str] = {}
    filemap: dict[str, tuple[str, str]] = {}
    filemap_root_winner: dict[str, str] = {}
    filemap_root: dict[str, tuple[str, str]] = {}
    # Incremental provider stacks (populated only when the fast path is on):
    # rel_key → provider mod name (str) or [providers] in low→high order.
    _prov: dict[str, "str | list[str]"] = {}
    _prov_root: dict[str, "str | list[str]"] = {}
    _contested: set[str] = set()
    _contested_root: set[str] = set()
    _files_count: dict[str, int] = {}
    _pair_counts: dict[tuple[str, str], int] = {}
    _losses: dict[str, int] = {}
    overrides:     dict[str, set[str]] = {s: set() for s in priority_order}
    overridden_by: dict[str, set[str]] = {s: set() for s in priority_order}
    win_count: dict[str, int] = {}
    mods_with_files: set[str] = set()
    # Effective-deploy-path winner dict (only used for normal/Data/ namespace).
    # When conflict_key_fn is provided (e.g. UE5 routing), two staged paths that land
    # at the same game location are treated as conflicting even if their staged keys differ.
    conflict_winner: dict[str, str] = {}
    # Parallel index ck → staged rel_key for the current winner. Avoids an O(n)
    # scan of filemap_winner per conflicting file in UE5 builds.
    conflict_staged: dict[str, str] = {}
    # Identity-key bookkeeping (BG3 pak UUIDs). _path_pairs holds every
    # (loser, winner) relation that came from a real path collision, so the
    # identity-only ones can be subtracted exactly - a pair that conflicts BOTH
    # ways keeps its loose-file status.
    # Identity keys (BG3 pak UUIDs): _path_pairs records every (loser, winner)
    # from a REAL path collision so identity-only ones can be subtracted exactly
    # - a pair conflicting both ways keeps its loose-file status.
    _ident_on = bool(identity_ck_prefix) and conflict_key_fn is not None
    _ident_pfx = identity_ck_prefix or ""
    _path_pairs: set[tuple[str, str]] = set()
    _ident_pops: dict[str, int] = {}

    # Hoist feature-flags out of the per-file hot loop. When a feature is
    # unused (the common case) we skip its function call entirely on each of
    # the ~100k+ files rather than calling a helper that immediately returns.
    _has_excluded_loose = _loose_excl_re is not None
    _has_unknown_top    = _allowed_top is not None
    _has_ignore         = _ignore_re is not None
    _has_folder_ignore  = _pf.folder_ignore_re is not None
    _dir_ignored        = _pf.dir_ignored

    _merge_t0 = time.perf_counter()
    for name in priority_order:
        entry = index.get(name)
        if not entry:
            # An enabled mod in the modlist has NO index entry. Expected for the
            # synthetic [Overwrite] boundary when the overwrite folder is empty;
            # for a real mod it means the mod folder exists on disk (it's in the
            # modlist) but rebuild_mod_index never indexed it (e.g. a symlinked
            # folder skipped by follow_symlinks=False, an unreadable dir, or a
            # non-UTF-8 filename that dropped the whole mod). Such a mod deploys
            # nothing and contributes no plugins/conflicts - the exact "copied
            # mod is invisible" signature - so surface it instead of skipping
            # silently. Only warn once we know the index isn't simply empty.
            if (log_fn is not None and name != OVERWRITE_NAME
                    and name not in _utf8_bad and index):
                # Try to explain WHY the lookup missed. The most subtle cause is
                # a NAME MISMATCH: the modlist entry and the on-disk folder /
                # index key differ by case, trailing whitespace, or a Unicode
                # normalization variant (common when the copy was made by a
                # different file manager / OS than the one that wrote the
                # modlist). Surface the near-match so it's obvious.
                _nm = name.strip().casefold()
                _near = [k for k in index
                         if k != OVERWRITE_NAME and k.strip().casefold() == _nm]
                if _near and _near != [name]:
                    log_fn(f"WARN: enabled mod \"{name}\" has NO index entry, but "
                           f"the index has a NEAR-MATCH key {_near!r} - the "
                           f"modlist name and the on-disk folder differ by case / "
                           f"whitespace / Unicode form. This mod deploys nothing "
                           f"until the names match (rename the folder or the "
                           f"modlist entry).")
                else:
                    log_fn(f"WARN: enabled mod \"{name}\" has NO index entry - it "
                           f"will deploy no files (not scanned into modindex.bin; "
                           f"check for a symlinked/unreadable folder or run "
                           f"Refresh). Index has {len(index)} mod(s).")
            continue
        normal, _ = entry
        if not normal:
            if (log_fn is not None and name != OVERWRITE_NAME
                    and name not in _utf8_bad):
                log_fn(f"WARN: enabled mod \"{name}\" indexed with ZERO files - "
                       f"it will deploy nothing")
            continue
        # Guard against surrogate-encoded filenames left in an old modindex.bin
        # (pre surrogate-skip-fix). v4 indexes are written after that fix, so
        # this is a legacy-only condition. The per-file sweep lives in
        # mod_index_utf8_unsafe (mtime-cached); only the rare flagged mod pays
        # for the bad-name list here (for the log).
        if name in _utf8_bad:
            bad_names = [rs for rs in normal.values() if not _is_utf8_safe(rs)]
            if log_fn is not None:
                try:  # escaped + guarded (see rebuild_mod_index)
                    log_fn(
                        f"WARN: Mod \"{_safe_log_str(name)}\" skipped - contains "
                        f"file(s) with non-UTF-8 name(s): "
                        f"{', '.join(_safe_log_str(n) for n in bad_names[:5])}"
                    )
                except Exception:
                    pass
            continue
        exc = _excluded.get(name)
        had_file = False
        _acc = 0
        _is_root_mod = bool(root_folder_mods and name in root_folder_mods)
        _rf = None if _is_root_mod else _root_files.get(name)
        # Pick which namespace this mod writes into.
        _winner_ns = filemap_root_winner if _is_root_mod else filemap_winner
        _map_ns    = filemap_root        if _is_root_mod else filemap
        _prov_ns   = _prov_root      if _is_root_mod else _prov
        _cont_ns   = _contested_root if _is_root_mod else _contested
        for rel_key, rel_str in normal.items():
            if exc and rel_key in exc:
                continue
            if _has_excluded_loose and _is_excluded_loose(rel_key):
                continue
            if _has_unknown_top and _is_unknown_top_level(rel_key):
                continue
            if _has_ignore and _is_ignored(rel_key):
                continue
            if _has_folder_ignore and _dir_ignored(rel_key):
                continue
            had_file = True
            if _rf is not None:
                # Per-file root tags: rebind the namespaces per entry (same
                # switch as whole-mod root flags). Only mods with tags pay.
                if rel_key in _rf:
                    _winner_ns, _map_ns = filemap_root_winner, filemap_root
                    _prov_ns, _cont_ns = _prov_root, _contested_root
                else:
                    _winner_ns, _map_ns = filemap_winner, filemap
                    _prov_ns, _cont_ns = _prov, _contested
            if _incr_on:
                _stack = _prov_ns.get(rel_key)
                if _stack is None:
                    _prov_ns[rel_key] = name
                elif type(_stack) is str:
                    _prov_ns[rel_key] = [_stack, name]
                    _cont_ns.add(rel_key)
                else:
                    _stack.append(name)
                _acc += 1
            prev = _winner_ns.get(rel_key)
            if prev is not None:
                win_count[prev] = win_count.get(prev, 0) - 1
                overrides[name].add(prev)
                overridden_by[prev].add(name)
                if _ident_on:
                    _path_pairs.add((prev, name))
                if _incr_on:
                    # Each overwrite event IS one consecutive pair - the same
                    # relation the incremental pair refcounts maintain.
                    _pk = (prev, name)
                    _pair_counts[_pk] = _pair_counts.get(_pk, 0) + 1
                    _losses[prev] = _losses.get(prev, 0) + 1
            _winner_ns[rel_key] = name
            _map_ns[rel_key] = (rel_str, name)
            win_count[name] = win_count.get(name, 0) + 1
            # Effective-deploy-path conflict detection only applies to normal mods.
            # Root-flagged mods and root-tagged files deploy verbatim to
            # game_root, no conflict_key_fn transform.
            if (not _is_root_mod and conflict_key_fn is not None
                    and (_rf is None or rel_key not in _rf)):
                ck = conflict_key_fn(name, rel_key).lower()
                prev_ck = conflict_winner.get(ck)
                _is_ident = (prev_ck is not None and _ident_on
                             and ck.startswith(_ident_pfx))
                # An identity key drops the earlier file even when the SAME mod
                # owns it (two paks of one module in a mod folder - or in the
                # overwrite folder, which is one pseudo-mod). Path keys keep the
                # historical mod != mod rule.
                if prev_ck is not None and (prev_ck != name or _is_ident):
                    prev_staged = conflict_staged.get(ck)
                    if (prev_staged is not None
                            and prev_staged != rel_key
                            and filemap_winner.get(prev_staged) == prev_ck):
                        filemap_winner.pop(prev_staged, None)
                        filemap.pop(prev_staged, None)
                        win_count[prev_ck] = win_count.get(prev_ck, 0) - 1
                        if _is_ident:
                            # Losing an identity contest isn't a file conflict -
                            # give the win back when scoring path-only status.
                            _ident_pops[prev_ck] = _ident_pops.get(prev_ck, 0) + 1
                    if prev_ck != name:
                        overrides[name].add(prev_ck)
                        overridden_by[prev_ck].add(name)
                        if _ident_on and not _is_ident:
                            _path_pairs.add((prev_ck, name))
                conflict_winner[ck] = name
                conflict_staged[ck] = rel_key
        if had_file:
            mods_with_files.add(name)
            if _acc:
                _files_count[name] = _acc
    perftrace.mark("filemap: priority merge loop", time.perf_counter() - _merge_t0)

    with perftrace.span("filemap: _compute_conflict_status"):
        conflict_map = _compute_conflict_status(
            priority_order, overrides, overridden_by, win_count, mods_with_files,
        )
        if _ident_on and conflict_extras is not None:
            # Same classification minus identity-only relations (and the wins
            # they cost) - the caller paints THIS as the loose-file icon, so an
            # identity clash doesn't light up two icons. overrides/overridden_by
            # stay whole, so cross-panel highlights still link the mods.
            _p_over = {n: {o for o in s if (o, n) in _path_pairs}
                       for n, s in overrides.items()}
            _p_overby = {n: {o for o in s if (n, o) in _path_pairs}
                         for n, s in overridden_by.items()}
            _p_wins = dict(win_count)
            for _m, _c in _ident_pops.items():
                _p_wins[_m] = _p_wins.get(_m, 0) + _c
            conflict_extras["path_conflict_map"] = _compute_conflict_status(
                priority_order, _p_over, _p_overby, _p_wins, mods_with_files,
            )

    # Normalize folder casing across the merged filemap so that two mods which
    # ship the same logical path with different casings (e.g. "archive/pc/Mod"
    # vs "Archive/PC/Mod") produce a single canonical path in filemap.txt.
    # This runs on the output dicts only - the index stays a faithful mirror
    # of each mod's on-disk casing, which is what _resolve_source needs.
    #
    # The picking strategy comes from the game's `filemap_casing` property:
    #   "upper"        - pick variant with more uppercase letters (default)
    #   "lower"        - pick variant with more lowercase letters
    #   "force_lower"  - every folder/filename forced lowercase
    #   "force_upper"  - every folder/filename-stem forced uppercase (extension stays lower)
    _norm_t0 = time.perf_counter()
    # Casing state for the incremental fast path (upper/lower strategies only).
    _cas_strategy: "str | None" = None
    _cas_refcount: Counter = Counter()
    _cas_ctxv: dict[tuple[str, str], Counter] = {}
    _cas_canon: dict[tuple[str, str], str] = {}
    _cas_drw: dict[str, "str | None"] = {}
    _cas_ties = False
    if normalize_folder_case and (filemap or filemap_root):
        _strategy = filemap_casing if filemap_casing in _VALID_FILEMAP_CASINGS else FILEMAP_CASING_UPPER
        _norm_normal: dict[str, dict[str, str]] = {}
        _norm_root: dict[str, dict[str, str]] = {}
        for _rk, (_rs, _mn) in filemap.items():
            _norm_normal.setdefault(_mn, {})[_rk] = _rs
        for _rk, (_rs, _mn) in filemap_root.items():
            _norm_root.setdefault(_mn, {})[_rk] = _rs
        if _strategy in (FILEMAP_CASING_FORCE_LOWER, FILEMAP_CASING_FORCE_UPPER):
            _apply_force_casing(_norm_normal, _norm_root, strategy=_strategy)
        else:
            # Inline composition of _normalize_folder_cases so the
            # intermediates (unique dirs, canonical picks) stay capturable
            # for the incremental state.
            _udirs = _collect_unique_dirs(_norm_normal, _norm_root)
            if _incr_on:
                _cas_strategy = _strategy
                # Raw-dir refcount over WINNING entries (both namespaces);
                # must be taken before the rel_str rewrite below. Casing
                # canonicalization pools winners only - losers' variants
                # never influence picks (mirrors _norm_* regrouping).
                for _m2 in (filemap, filemap_root):
                    for _rs2, _mn2 in _m2.values():
                        _sl2 = _rs2.rfind("/")
                        if _sl2 >= 0:
                            _cas_refcount[_rs2[:_sl2]] += 1
                _cas_ctxv = _build_ctx_variants(_udirs)
                _cas_ties = _ctx_ties_present(_cas_ctxv, _strategy)
            if _udirs:
                _canon = _collect_canonical(_udirs, _strategy)
                if _canon:
                    _drw = _apply_canonical(_canon, _udirs)
                    _rewrite_rel_strs(_drw, _norm_normal, _norm_root)
                    if _incr_on:
                        _cas_canon = _canon
                        _cas_drw = _drw
        for _mn, _files in _norm_normal.items():
            for _rk, _rs in _files.items():
                filemap[_rk] = (_rs, _mn)
        for _mn, _files in _norm_root.items():
            for _rk, _rs in _files.items():
                filemap_root[_rk] = (_rs, _mn)
    perftrace.mark("filemap: normalize folder casing", time.perf_counter() - _norm_t0)

    # Casing pins win over the strategy above - applied last, and regardless of
    # normalize_folder_case, so a mod that reads its own data folder by a
    # hardcoded case-sensitive path always sees the casing it shipped.  The
    # incremental fast path applies the same pins in _normalize_one_rel_str, so
    # both paths produce identical output for a pinned folder.
    if _pins:
        _apply_casing_pins_tuplemap(_pins, filemap, filemap_root)

    # Skip-if-unchanged: fingerprint the winner map + disabled state.
    # If identical to the last write for this output path, skip the expensive
    # sort + string build + disk write (and post_build_filemap re-read).
    # disabled_plugins is rare but must be included since it affects written lines.
    # Snapshot the full normal-namespace map (rel_key → rel_str+mod), not just
    # the winner (rel_key → mod): folder-casing changes - from the strategy or a
    # casing pin - alter the written rel_str without changing the winner, and
    # the skip-if-unchanged check must still detect them.
    _winner_snapshot = (frozenset(filemap.items()), _disabled_frozen, frozenset(filemap_root.items()))
    with _filemap_winner_cache_lock:
        _unchanged = _filemap_winner_cache.get(_output_key) == _winner_snapshot

    def _store_state(skeys, lines, skeys_r, lines_r) -> None:
        """Store the incremental state built from this full merge."""
        _put_incr_state(_output_key, _make_incr_state(
            _incr_fp, index_path, _index_mtime, priority_order,
            _prov, _prov_root, _contested, _contested_root, _files_count,
            _pair_counts, _losses,
            filemap, skeys, lines,
            filemap_root, skeys_r, lines_r,
            _disabled_frozen,
            _cas_strategy, _cas_refcount, _cas_ctxv, _cas_canon, _cas_drw,
            _cas_ties, _pins,
        ))
        _incr_stats["full"] += 1
        if _cas_ties and log_fn is not None:
            log_fn("filemap: casing-variant tie present - incremental fast "
                   "path disabled for this profile")

    def _verify_check() -> None:
        """Verify mode: compare the dry-run fast result vs the full rebuild."""
        if _verify_fast is None:
            return
        _log = log_fn if log_fn is not None else print
        try:
            _, _vcmap, _vov, _vob, _vtexts = _verify_fast
            mismatch = []
            if _vcmap != conflict_map:
                mismatch.append("conflict_map")
            if _vov != overrides:
                mismatch.append("overrides")
            if _vob != overridden_by:
                mismatch.append("overridden_by")
            _disk = (output_path.read_text(encoding="utf-8", errors="surrogateescape")
                     if output_path.is_file() else "")
            if _vtexts[0] != _disk:
                mismatch.append("filemap.txt")
            _rp = output_path.parent / "filemap_root.txt"
            _rdisk = _rp.read_text(encoding="utf-8", errors="surrogateescape") if _rp.is_file() else ""
            if (_vtexts[1] or "") != _rdisk:
                mismatch.append("filemap_root.txt")
            if mismatch:
                _incr_stats["verify_mismatch"] = _incr_stats.get("verify_mismatch", 0) + 1
                _log(f"FILEMAP VERIFY MISMATCH: {', '.join(mismatch)}")
                if "filemap.txt" in mismatch:
                    _va = _vtexts[0].splitlines()
                    _vb = _disk.splitlines()
                    _diff = [
                        f"  fast={a!r} full={b!r}"
                        for a, b in zip(_va, _vb) if a != b
                    ][:10]
                    if len(_va) != len(_vb):
                        _diff.append(f"  line counts: fast={len(_va)} full={len(_vb)}")
                    for _d in _diff:
                        _log(_d)
            else:
                _incr_stats["verify_ok"] = _incr_stats.get("verify_ok", 0) + 1
        except Exception as exc:  # verify must never break the build
            _log(f"filemap: verify comparison failed - {exc}")

    if _unchanged and output_path.is_file():
        # Conflict data is still valid; file on disk is already correct.
        count = sum(1 for _ in filemap_winner)  # approx - disabled_plugins may trim a few
        if _incr_on:
            _skeys, _lines = _render_filemap(filemap)
            _skeys_r, _lines_r = _render_filemap(filemap_root)
            _store_state(_skeys, _lines, _skeys_r, _lines_r)
        _verify_check()
        return count, conflict_map, overrides, overridden_by

    with perftrace.span("filemap: _write_filemap (sort+disk)"):
        _skeys, _lines = _render_filemap(filemap)
        count = _join_and_write(output_path, _skeys, _lines,
                                filemap, _disabled_lower)

    # Write filemap_root.txt for root-flagged mods.
    _root_filemap_path = output_path.parent / "filemap_root.txt"
    _skeys_r, _lines_r = _render_filemap(filemap_root)
    if filemap_root:
        _join_and_write(_root_filemap_path, _skeys_r, _lines_r,
                        filemap_root, {})
    elif _root_filemap_path.is_file():
        _root_filemap_path.unlink(missing_ok=True)

    with _filemap_winner_cache_lock:
        _filemap_winner_cache[_output_key] = _winner_snapshot

    if _incr_on:
        _store_state(_skeys, _lines, _skeys_r, _lines_r)

    _verify_check()
    return count, conflict_map, overrides, overridden_by
