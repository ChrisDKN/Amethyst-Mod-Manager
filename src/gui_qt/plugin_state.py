"""Plugin list loading for the Qt Plugins tab.

Produces the ordered, flagged plugin list for the active game/profile by reusing
the backend: Utils.plugins (read_plugins / read_loadorder / write_plugins) and
Utils.plugin_parser (ESL / master header flags). Vanilla plugins are pinned to
the top, then mods follow saved loadorder.txt order.

v1 scope: list + order + enable-toggle + ESL/master flags. The deeper Tk logic
(orphan detection, Data_Core pruning, LOOT messages, bash tags, missing-master
checks) is deferred - the Flags column is structured to receive them later.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from Utils.app_log import app_log
# Crash-proof diagnostic prints (Flatpak stdout can raise BrokenPipeError and
# kill worker threads). See Utils.app_log.safe_print.
from Utils.app_log import safe_print as print  # noqa: A004
from Utils.perftrace import span
from Utils.plugins import (
    read_plugins, read_loadorder, write_plugins, write_loadorder, PluginEntry,
    enforce_primary_plugin_order,
)

# Verbose plugin-panel diagnostics. Set AMM_PLUGIN_DIAG=1 to log every stage of
# load_plugins (plugins.txt / filemap recovery / resolver / prune) - used to
# chase "an enabled mod's plugins don't appear in the panel" reports where the
# filemap is correct but the panel comes up empty. The always-on WARN lines
# below fire regardless, to catch the silent-drop cases in normal use.
_PLUGIN_DIAG = os.environ.get("AMM_PLUGIN_DIAG") == "1"


def _diag(msg: str) -> None:
    if _PLUGIN_DIAG:
        app_log(f"[plugin-diag] {msg}")

# Most plugins the phantom-prune may remove from plugins.txt/loadorder.txt in
# one pass. Pruning exists to clean up after a REMOVED mod (a handful of
# plugins); anything bigger is treated as a broken resolution, not real data.
_PRUNE_MAX = 10

# Flag bits for the plugin Flags column (drawn left→right in this order).
PF_MISSING = 1 << 0    # missing masters (red warning)
PF_LATE = 1 << 1       # master loads after a dependent (late master)
PF_VMM = 1 << 2        # version-mismatched master
PF_ESL = 1 << 3        # ESL / light-flagged
PF_LOOT = 1 << 4       # LOOT masterlist messages/requirements/incompatibilities
PF_DIRTY = 1 << 5      # dirty edits (needs cleaning)
PF_TAGS = 1 << 6       # bash tags
PF_MASTER = 1 << 7     # master (.esm or master-flagged)
PF_USERLIST = 1 << 8   # managed in userlist.yaml (white dot)
PF_UL_CYCLE = 1 << 9   # userlist rules form a broken cycle (dot turns red)
PF_ESL_SAFE = 1 << 10  # .esp/.esm eligible for the ESL flag (libloot verdict)
PF_ESL_UNSAFE = 1 << 11  # .esp/.esm too many records for ESL (libloot verdict)
PF_BLUEPRINT = 1 << 12   # Starfield Blueprint (0x800) - loads after everything

# Bump when check_esl_eligible() changes its verdict criteria so cached
# eligibility results are invalidated (mirrors Tk _ESL_ELIG_CACHE_VERSION).
_ESL_ELIG_CACHE_VERSION = 2

# Process-wide caches keyed by (path, mtime_ns, size[, game_type, version]) so
# the expensive per-plugin record scan / flag read only runs when a plugin file
# is actually rewritten - mirrors Tk _esl_flag_cache / _esl_eligible_cache.
_ESL_FLAG_CACHE: dict = {}
_ESL_ELIG_CACHE: dict = {}

# BOS/SkyPatcher scan cache: (total_staging_mtime, staging_str) -> result dict.
_BOS_SP_CACHE: dict = {}
# Serializes BOS/SP scans: overlapping plugin reloads (e.g. the post-conflicts
# pass racing an auto-deploy's reload) would otherwise each run the scan.
_BOS_SP_LOCK = threading.Lock()


@dataclass
class PluginRow:
    name: str
    enabled: bool
    flags: int = 0
    vanilla: bool = False
    # Per-flag detail captured while computing the flag bits, so the Flags-column
    # tooltip can show the same content the Tk app shows (Tk parity).
    missing_masters: list[str] | None = None
    late_masters: list[str] | None = None
    vmm_masters: list[str] | None = None
    loot_info: dict | None = None
    # BOS/SkyPatcher patch kind: "" (none), "bos", "sp", or "both".
    bos_sp: str = ""
    # Lowercase MAST dependencies; None = unreadable, so no move constraints.
    masters: list[str] | None = None


_EXT_ORDER = {".esm": 0, ".esp": 1, ".esl": 2}

_OVERWRITE_NAME = "[Overwrite]"


def compute_game_indexes(rows: list[PluginRow]) -> list[str]:
    """Return the game's load index for each row, aligned to *rows* order.

    - Disabled            → "" (no index).
    - Light / ESL-flagged → "FE:xxx" (all share slot FE, sub-index rolls to
      FF after 4096).
    - Normal              → "%02X" of the running normal-plugin counter.

    Medium / ESH (slot FD) is not handled - the model has no medium flag today
    (matches current game support). TODO medium/ESH when a game needs it.
    """
    out: list[str] = []
    num_esl = 0
    num_skipped = 0
    for pos, row in enumerate(rows):
        if not row.enabled:
            out.append("")
            num_skipped += 1
            continue
        if row.flags & PF_ESL:
            esl_pos = 254 + (num_esl // 4096)
            out.append(f"{esl_pos:02X}:{num_esl % 4096:03X}")
            num_esl += 1
        else:
            out.append(f"{pos - num_esl - num_skipped:02X}")
    return out


def master_block_enabled(game) -> bool:
    """Whether *game*'s engine loads the master block before all non-masters."""
    return bool(getattr(game, "plugins_master_block", False))


def is_master_group(row: PluginRow) -> bool:
    """Master-flagged, .esm or .esl - the block the engine loads first."""
    # NOT keyed on PF_ESL: that's also set on an ESL-flagged .esp (an ESPFE),
    # which is a normal plugin loaded after the master block.
    return bool(row.flags & PF_MASTER) or row.name.lower().endswith(".esl")


def is_blueprint(row: PluginRow) -> bool:
    """Starfield Blueprint plugin - loads after every non-blueprint plugin."""
    return bool(row.flags & PF_BLUEPRINT)


def plugin_rank(row: PluginRow) -> int:
    """Load-order region, lowest first: master, normal, bp master, bp normal."""
    return (2 if is_blueprint(row) else 0) + (0 if is_master_group(row) else 1)


def master_boundary(rows: list[PluginRow]) -> int:
    """Index of the first non-master row in an already-partitioned *rows*."""
    return sum(1 for r in rows if is_master_group(r))


def master_flags_resolved(rows: list[PluginRow],
                          resolved: dict[str, Path]) -> bool:
    """Whether every row's master-ness is known from a real file on disk."""
    # _to_row falls back to extension-only detection for unresolved plugins, so
    # a master-flagged .esp would look normal and get demoted out of the master
    # block. .esm/.esl need no header read and never block the check.
    for r in rows:
        low = r.name.lower()
        if low.endswith((".esm", ".esl")):
            continue
        path = resolved.get(low)
        if path is None or not Path(path).is_file():
            return False
    return True


def enforce_master_block(rows: list[PluginRow]
                         ) -> "tuple[list[PluginRow], bool]":
    """Stable-partition *rows* by rank -> (new_rows, changed); MO2 parity."""
    # Row objects are reused, never copied, so callers can remap selection by id.
    if not rows:
        return rows, False
    new_rows = sorted(rows, key=plugin_rank)
    if all(a is b for a, b in zip(new_rows, rows)):
        return rows, False
    return new_rows, True


def movable_bounds(rest: list[PluginRow], rank: int,
                   enabled: bool) -> "tuple[int, int]":
    """Legal (lo, hi) insert range in *rest* (block removed) for a *rank*."""
    # Scanned, not counted, like MO2's setPluginPriority: a counted boundary is
    # only meaningful on an already-partitioned list. lo = after the last row
    # that must load first, hi = before the first that must load after; if they
    # cross on an unpartitioned list, lo wins.
    n = len(rest)
    lo = 0
    hi = n
    if enabled:
        for i, r in enumerate(rest):
            rr = plugin_rank(r)
            if rr < rank:
                lo = i + 1
            elif rr > rank:
                hi = i
                break
        if hi < lo:
            hi = lo
    # Pinned vanilla rows sit at the head of their region and never move, so a
    # block may not be inserted above them.
    while lo < hi and rest[lo].vanilla:
        lo += 1
    return lo, hi


def dependency_bounds(rest: list[PluginRow], block: list[PluginRow],
                      moving_up: bool) -> "tuple[int, int]":
    """Legal (lo, hi) insert range in *rest* honouring master/child links."""
    # MO2 pluginlist.cpp 1983-2013: moving up, a plugin can't go above its own
    # masters; moving down, a master can't go below a plugin that needs it.
    # Same blueprint side only. masters=None (unreadable) imposes nothing.
    lo, hi = 0, len(rest)
    block_names = {r.name.lower() for r in block}
    block_bp = {is_blueprint(r) for r in block}
    if moving_up:
        needed = set()
        for r in block:
            for m in (r.masters or ()):
                if m not in block_names:
                    needed.add(m)
        if needed:
            for i, r in enumerate(rest):
                if r.name.lower() in needed and is_blueprint(r) in block_bp:
                    lo = max(lo, i + 1)
    else:
        for i, r in enumerate(rest):
            if r.masters and is_blueprint(r) in block_bp and (
                    block_names & set(r.masters)):
                hi = min(hi, i)
    if hi < lo:
        hi = lo
    return lo, hi


def plugins_path(game, profile: str) -> Path | None:
    if game is None or not profile:
        return None
    return game.get_profile_root() / "profiles" / profile / "plugins.txt"


def _find_plugin_in_mod_dir(mod_dir: Path, filename: str) -> Path | None:
    """Search *mod_dir* one level deep for *filename* (case-insensitive). Used
    when the filemap strips a prefix (e.g. 'Data Files') so the staging file
    lives in a subdir not reflected in the rel path. Ported from
    gui/plugin_panel.py:_find_plugin_in_mod_dir (pure Path logic)."""
    name_lower = filename.lower()
    if not mod_dir.is_dir():
        return None
    try:
        for entry in mod_dir.iterdir():
            if entry.is_file() and entry.name.lower() == name_lower:
                return entry
            if entry.is_dir():
                candidate = entry / filename
                if candidate.is_file():
                    return candidate
                for sub in entry.iterdir():
                    if sub.is_file() and sub.name.lower() == name_lower:
                        return sub
    except OSError:
        return None
    return None


def _resolve_plugin_paths(staging_dir: Path | None, data_dir: Path | None,
                          filemap_path: Path | None,
                          plugin_exts: tuple[str, ...],
                          root_filemap_path: Path | None = None,
                          root_prefix: str = "",
                          routing_ctx=None, snapshot=None) -> dict[str, Path]:
    """Map plugin filename (lowercase) → its on-disk path, from THREE sources in
    priority order (Tk parity: gui/plugin_panel.py:_check_all_masters).

    Mod plugins live in staging / overwrite (resolved via filemap.txt), NOT in
    the vanilla Data dir, so reading a plugin header needs this resolver - using
    only data_dir misses every mod-added (incl. ESL-flagged) plugin.
    """
    paths: dict[str, Path] = {}
    exts = tuple(e.lower() for e in plugin_exts)

    # 1. Filegraph winners → exact staged source paths.
    overwrite_dir = staging_dir.parent / "overwrite" if staging_dir else None
    del filemap_path, root_filemap_path, root_prefix, routing_ctx
    if snapshot is not None and staging_dir is not None:
        for plugin, winner in snapshot.plugin_winners().items():
            if not plugin.lower().endswith(exts):
                continue
            relative = winner.source_rel.decode("utf-8", "surrogateescape")
            source_root = (overwrite_dir if winner.mod_name == _OVERWRITE_NAME
                           else staging_dir / winner.mod_name)
            if source_root is not None:
                paths[plugin.lower()] = source_root / relative

    # 2. overwrite/ + overwrite/Data/ direct scan (plugins not yet in filemap).
    if overwrite_dir is not None and overwrite_dir.is_dir():
        for scan in (overwrite_dir, overwrite_dir / "Data"):
            if not scan.is_dir():
                continue
            try:
                for entry in scan.iterdir():
                    if entry.is_file() and entry.name.lower().endswith(exts):
                        paths.setdefault(entry.name.lower(), entry)
            except OSError:
                pass

    # 3. vanilla Data dir (or <data>_Core if present) via setdefault.
    if data_dir is not None and data_dir.is_dir():
        vanilla_dir = data_dir.parent / (data_dir.name + "_Core")
        scan_dir = vanilla_dir if vanilla_dir.is_dir() else data_dir
        try:
            for entry in scan_dir.iterdir():
                if entry.is_file() and entry.name.lower().endswith(exts):
                    paths.setdefault(entry.name.lower(), entry)
        except OSError:
            pass

    return paths


def resolve_plugin_paths_for_game(game, data_dir: Path | None = None,
                                  snapshot=None
                                  ) -> dict[str, Path]:
    """Map each plugin filename (lowercase) → its REAL on-disk path (staging mod
    / overwrite / vanilla Data), using the same resolver load_plugins uses. Used
    by the Flags column and the plugins context menu (ESL toggle needs the path
    of the file to edit). Returns {} on any failure."""
    if data_dir is None:
        data_dir = (game.get_vanilla_plugins_path()
                    if hasattr(game, "get_vanilla_plugins_path") else None)
    try:
        staging = (game.get_effective_mod_staging_path()
                   if hasattr(game, "get_effective_mod_staging_path") else None)
        exts = tuple(x.lower() for x in (getattr(game, "plugin_extensions", []) or ())) \
            or (".esp", ".esm", ".esl")
        return _resolve_plugin_paths(
            staging, data_dir, None, exts, snapshot=snapshot)
    except Exception:
        return {}


def _filemap_deployed_plugins(game, plugin_exts: tuple[str, ...],
                              enabled_mods_lower: "set[str] | None" = None,
                              snapshot=None) -> dict[str, str]:
    """Plugin winners from one pinned graph generation."""
    del game
    if snapshot is None:
        return {}
    exts = tuple(extension.lower() for extension in plugin_exts)
    found = {
        name.lower(): name
        for name, winner in snapshot.plugin_winners().items()
        if name.lower().endswith(exts)
        and (enabled_mods_lower is None
             or winner.mod_name == _OVERWRITE_NAME
             or winner.mod_name.lower() in enabled_mods_lower)
    }
    _diag(f"filegraph deploys {len(found)} plugin winner(s)")
    return found


def _staged_top_level_plugins(game, staging: "Path | None",
                              exts: tuple[str, ...], snapshot=None
                              ) -> "set[str] | None":
    """Plugin identities across all catalogued mods in the selected variants."""
    del game, staging
    if snapshot is None:
        return None
    extensions = tuple(extension.lower() for extension in exts)
    return {
        name for name in snapshot.staged_plugins()
        if name.lower().endswith(extensions)
    }


def load_plugins(game, profile: str,
                 cancelled=None, report: dict | None = None, snapshot=None
                 ) -> "list[PluginRow] | None":
    """Return the ordered plugin rows for *game*/*profile*, or [] if none.

    *cancelled* - optional zero-arg callable polled between the expensive
    phases (path resolution, per-plugin header reads, master checks, ESL
    eligibility, BOS/SP scan). When it returns True the load aborts and
    returns None: a superseded reload's result is dropped by the caller's
    generation check anyway, so finishing it just burns seconds of disk + GIL
    time that slow the reload that superseded it.

    *report* - optional dict filled with prune diagnostics for the caller:
    'prune_checked' (the phantom-prune actually ran, i.e. filemap_ok held)
    and 'mass_prune' (names SAFETY 3 refused to auto-prune - more unresolved
    entries than _PRUNE_MAX). An explicit Refresh uses this to offer the
    user a confirmed cleanup the automatic path must not do on its own."""
    if cancelled is None:
        cancelled = lambda: False
    p = plugins_path(game, profile)
    if p is None or not p.is_file():
        _diag(f"load_plugins: no plugins.txt (path={p}) → 0 rows")
        return []
    star = getattr(game, "plugins_use_star_prefix", True)
    entries = read_plugins(p, star_prefix=star)
    saved_order = read_loadorder(p.parent / "loadorder.txt")
    _diag(f"load_plugins: profile={profile!r} plugins.txt={len(entries)} "
          f"loadorder.txt={len(saved_order)} "
          f"active_dir={getattr(game, '_active_profile_dir', None)}")

    # Full vanilla set: base + DLC + Creation Club (.ccc), filtered to files
    # present in Data - same resolver the Tk app uses.
    try:
        from Utils.game_helpers import _vanilla_plugins_for_game
        with span("plugins.vanilla_resolve"):
            vanilla = _vanilla_plugins_for_game(game)
    except Exception:
        vanilla = {n.lower(): n for n in getattr(game, "vanilla_plugins", [])}

    # Recover plugins still deployed by an enabled mod (per the fresh filemap)
    # but missing from plugins.txt - see _filemap_deployed_plugins. The guard
    # is the listed-entry set below: a disabled patcher mod's toggle sync
    # (Utils/plugin_sync.py) strips its plugins from BOTH plugins.txt and
    # loadorder.txt, so a name absent from both that the filemap still deploys
    # is exactly the case to recover as enabled.
    exts = tuple(e.lower() for e in (getattr(game, "plugin_extensions", []) or [])) \
        or (".esp", ".esm", ".esl")
    listed_lower = {e.name.lower() for e in entries}
    # Legacy (non-star) games have no disabled syntax in plugins.txt - a
    # user-disabled plugin is OMITTED from the file and survives only in
    # loadorder.txt (see Utils/plugins.py). Reconstruct those names as
    # disabled entries BEFORE the filemap recovery below: the plugin's file is
    # still deployed by its (enabled) mod, so without this the recovery
    # re-added it as enabled - and persisted it back into plugins.txt - on
    # every reload. Deploy and LOOT sort both end in a reload, so "disable a
    # plugin, then deploy/sort" silently re-enabled it. Names whose mod was
    # since removed don't resolve to a file and are pruned further down.
    if not star:
        for name in saved_order:
            low = name.lower()
            if low in listed_lower or low in vanilla:
                continue
            entries.append(PluginEntry(name=name, enabled=False))
            listed_lower.add(low)
    # Enabled mods per the CURRENT modlist.txt - the recovery filter (see
    # _filemap_deployed_plugins). Missing/unreadable modlist → no filtering.
    enabled_mods: "set[str] | None" = None
    modlist_path = p.parent / "modlist.txt"
    if modlist_path.is_file():
        try:
            from Utils.modlist import read_modlist
            enabled_mods = {e.name.lower()
                            for e in read_modlist(modlist_path) if e.enabled}
        except Exception:
            enabled_mods = None
    with span("plugins.filemap_deployed"):
        deployed = _filemap_deployed_plugins(
            game, exts, enabled_mods, snapshot=snapshot)
    recovered: list[str] = []
    for low, orig in deployed.items():
        if low in listed_lower or low in vanilla:
            continue
        entries.append(PluginEntry(name=orig, enabled=True))
        listed_lower.add(low)
        recovered.append(orig)
    _diag(f"load_plugins: filemap deploys {len(deployed)} top-level plugin(s); "
          f"recovered {len(recovered)} not in plugins.txt: {recovered[:10]}")

    # Orphan plugins: files sitting in the game's Data/ folder that the user
    # installed manually - see _scan_orphan_plugins. Appended here so they show
    # in the panel and can be toggled / LOOT-sorted like any other plugin.
    data_dir = (game.get_vanilla_plugins_path()
                if hasattr(game, "get_vanilla_plugins_path") else None)
    with span("plugins.orphan_scan"):
        orphans = _scan_orphan_plugins(game, data_dir, exts, listed_lower,
                                       vanilla, saved_order)
    for orphan in orphans:
        entries.append(orphan)
        listed_lower.add(orphan.name.lower())
    if orphans:
        _diag(f"load_plugins: {len(orphans)} manual orphan plugin(s) found in "
              f"{data_dir}: {[o.name for o in orphans][:10]}")
    mod_map = {e.name.lower(): e for e in entries}

    ordered: list[PluginEntry] = []
    seen: set[str] = set()

    # Vanilla pinned first (in saved order where known, else ext-sorted).
    for name in saved_order:
        low = name.lower()
        if low in seen:
            continue
        if low in vanilla:
            ordered.append(PluginEntry(vanilla[low], True)); seen.add(low)
    for low, orig in sorted(vanilla.items(),
                            key=lambda kv: (_EXT_ORDER.get(Path(kv[0]).suffix, 9), kv[0])):
        if low not in seen:
            ordered.append(PluginEntry(orig, True)); seen.add(low)

    # Mods in saved loadorder order, then any leftovers from plugins.txt.
    for name in saved_order:
        low = name.lower()
        if low in seen:
            continue
        if low in mod_map:
            ordered.append(mod_map[low]); seen.add(low)
    for e in entries:
        if e.name.lower() not in seen:
            ordered.append(e); seen.add(e.name.lower())

    # MO2 parity (fixPrimaryPlugins): a game may define an engine-owned block
    # whose order wins over a stale profile, collection export, or LOOT result.
    ordered, primary_order_changed = enforce_primary_plugin_order(game, ordered)

    # Resolve each plugin's REAL path (staging mod / overwrite / Data) so header
    # flags (ESL, master, missing-master) work for mod plugins, not just vanilla.
    if cancelled():
        return None
    with span("plugins.resolve_paths"):
        resolved = resolve_plugin_paths_for_game(
            game, data_dir, snapshot=snapshot)
    _diag(f"load_plugins: ordered={len(ordered)} resolver mapped "
          f"{len(resolved)} plugin(s) to on-disk paths")

    # Prune phantom entries: a plugin listed in plugins.txt but not vanilla and
    # with NO on-disk file anywhere (staging mod / overwrite / Data, per the
    # resolver) is a stale leftover from a removed mod. It has no owning mod, so
    # it can't be marker-highlighted and LOOT can't sort it. resolve runs after
    # the fresh filemap (app._on_conflicts_ready), so an empty resolution is
    # authoritative. Persist the cleanup so the phantom drops out of the files.
    #
    # SAFETY: only prune when the resolver returned a healthy map (the filemap
    # exists and resolution produced paths). resolve_plugin_paths_for_game
    # returns {} on ANY failure - pruning on an empty map would wipe every
    # non-vanilla plugin from plugins.txt. Require the filemap to exist AND the
    # resolver to have found at least one path before trusting a miss.
    staging = (game.get_effective_mod_staging_path()
               if hasattr(game, "get_effective_mod_staging_path") else None)
    # The caller passes a snapshot only after its intent transaction is
    # accepted. An absent snapshot means a newer reconcile is pending, so
    # recovery/pruning remains read-only and conservative for this reload.
    filemap_fresh = snapshot is not None
    filemap_ok = snapshot is not None and bool(resolved)
    # SAFETY 2: never prune while the game object points at a DIFFERENT
    # profile than the one being loaded. Background workers (deploy pipeline,
    # collection install/cleanup) swap game._active_profile_dir and can leave
    # it stale/None; every path above then resolved against the WRONG
    # staging/filemap and an unresolved plugin means nothing. (2026-07-04
    # incident: a stale active dir made this prune wipe all 461 collection
    # plugins from plugins.txt + loadorder.txt.)
    active = getattr(game, "_active_profile_dir", None)
    _active_matches = (active is not None
                       and Path(active).resolve() == p.parent.resolve())
    if not _active_matches:
        # Stale/mismatched active dir → the resolver above read the WRONG
        # staging/filemap, so every path resolution is meaningless. This also
        # silently disables the prune (safe), but if it fires during a normal
        # reload it means load_plugins ran against a different profile than the
        # one on screen - a prime suspect for "plugins missing after a toggle".
        _diag(f"load_plugins: SAFETY-2 active-dir MISMATCH - "
              f"active={active} vs plugins.txt dir={p.parent} "
              f"(resolver ran against the wrong profile; prune skipped)")
        filemap_ok = False
    # A superseded load must never mutate the files: its plugins.txt read AND
    # its filemap resolution may both predate the reload that superseded it
    # (observed: a superseded gen's prune fired with stale data, then the
    # current gen's result was applied on top of the damaged files).
    if cancelled():
        return None
    if report is not None:
        report["prune_checked"] = filemap_ok
    if filemap_ok:
        pruned: list[str] = []
        for e in ordered:
            low = e.name.lower()
            if low in vanilla:
                continue
            rp = resolved.get(low)
            if rp is None or not rp.is_file():
                pruned.append(e.name)
        # Partition unresolved entries by staged ownership (modindex.bin):
        # a name that IS a top-level plugin of some staged mod can only be
        # unresolved because that mod is disabled - the toggle sync should
        # have removed it (missed scan, older version, externally edited
        # profile). Ownership is proven, so it is NOT the broken-resolution
        # case SAFETY 3 exists for and prunes uncapped (self-heals the
        # GH#318 state). plugins.txt only on star games - loadorder.txt
        # keeps the position for a re-enable. AMM_PRUNE_OWNED=0 kills it.
        staged = (_staged_top_level_plugins(
            game, staging, exts, snapshot=snapshot)
                  if pruned else None)
        if os.environ.get("AMM_PRUNE_OWNED") == "0":
            staged = None
        owned = [n for n in pruned
                 if staged is not None and n.lower() in staged]
        unowned = [n for n in pruned
                   if staged is None or n.lower() not in staged]
        pruned_now: set[str] = set()
        if owned:
            app_log(f"Plugins: removed {len(owned)} entr(y/ies) belonging to "
                    f"disabled mod(s): {', '.join(owned[:20])}"
                    f"{'…' if len(owned) > 20 else ''}")
            _prune_phantom_plugins(p, star, {n.lower() for n in owned},
                                   loadorder_too=not star)
            pruned_now.update(n.lower() for n in owned)
        # SAFETY 3: a genuine stale entry is one removed mod's worth of
        # plugins. A mass miss means the resolution itself is wrong (desync
        # not caught above, or filemap.txt read mid-rewrite) - keep the
        # entries and let a later healthy reload prune them one by one.
        # Reported to the caller so an EXPLICIT Refresh can offer the user a
        # confirmed mass cleanup (a genuinely polluted plugins.txt - e.g.
        # another profile's load order copied in - looks identical to a
        # broken resolution from here, so only the user can arbitrate).
        if unowned and len(unowned) > _PRUNE_MAX:
            app_log(f"Plugins: NOT pruning {len(unowned)} unresolved plugin(s) "
                    f"(> {_PRUNE_MAX}) - wrong-staging/partial-filemap "
                    f"resolution suspected; plugins.txt left untouched. "
                    f"Refresh Modlist offers a confirmed cleanup.")
            if report is not None:
                report["mass_prune"] = list(unowned)
        elif unowned:
            app_log(f"Plugins: pruned {len(unowned)} stale entr(y/ies) with no "
                    f"on-disk file: {', '.join(unowned)}")
            _prune_phantom_plugins(p, star, set(n.lower() for n in unowned))
            pruned_now.update(n.lower() for n in unowned)
        if pruned_now:
            ordered = [e for e in ordered if e.name.lower() not in pruned_now]

    # Persist manual orphans into plugins.txt so LOOT sort and deploy pick
    # them up even if the user never touches the panel. Same SAFETY-2 guard
    # as the prune: only write when the game object points at the profile
    # being loaded. Skip any the prune dropped above (shouldn't happen -
    # orphans resolve via the Data/Data_Core scan - but stay consistent).
    if orphans and _active_matches:
        still = {e.name.lower() for e in ordered}
        _append_orphans_to_plugins(
            p, star, [o for o in orphans if o.name.lower() in still])

    # Persist filemap-recovered plugins too (Tk parity: Tk's Data/ orphan scan
    # fed the same sync that wrote them back to plugins.txt). Without this, a
    # plugin that lost its plugins.txt entry - e.g. pruned by an earlier
    # stale-filemap reload - shows in the panel forever but never reaches
    # plugins.txt, so deploy's prefix plugins.txt omits it and the game never
    # loads it. filemap_fresh: only trust the recovery when the filemap is at
    # least as new as plugins.txt (see SAFETY 4).
    if recovered and _active_matches and filemap_fresh:
        still = {e.name.lower() for e in ordered}
        _append_orphans_to_plugins(
            p, star, [PluginEntry(name=n, enabled=True) for n in recovered
                      if n.lower() in still])

    if cancelled():
        return None
    with span("plugins.header_flags(to_row)"):
        _bp = bool(getattr(game, "plugins_have_blueprints", False))
        rows = [_to_row(e, vanilla, resolved, data_dir, _bp) for e in ordered]
    if cancelled():
        return None
    if primary_order_changed and _active_matches:
        save_plugins(game, profile, rows)
        app_log("Plugins: restored the game's fixed primary-plugin order.")
    # MO2 parity (fixPluginRelationships). Must run before the checks below -
    # late-master warnings are computed from row positions.
    if master_block_enabled(game) and master_flags_resolved(rows, resolved):
        with span("plugins.master_block"):
            hoisted_rows, hoisted = enforce_master_block(rows)
            if hoisted:
                moved = sum(1 for a, b in zip(rows, hoisted_rows) if a is not b)
                rows = hoisted_rows
                # Persist so the mtime-ordered games (Oblivion/FO3/FNV) stamp the
                # corrected order. Same SAFETY guard as the writes above.
                if _active_matches:
                    save_plugins(game, profile, rows)
                    app_log(f"Plugins: hoisted the master block above the "
                            f"non-masters ({moved} row(s) moved) - the engine "
                            f"loads masters first regardless of load order.")
    with span("plugins.master_checks"):
        _apply_master_checks(rows, resolved, data_dir)
    with span("plugins.loot_flags"):
        enabled_lower = {r.name.lower() for r in rows if r.enabled}
        resolver = RequirementResolver(
            game, p.parent, staging, enabled_lower, snapshot=snapshot)
        _apply_loot_flags(rows, p.parent, resolver)
    with span("plugins.userlist_flags"):
        _apply_userlist_flags(rows, p.parent)
    if cancelled():
        return None
    # ESL eligibility deliberately NOT computed here - see
    # compute_esl_eligibility (deferred to its own post-apply worker).
    with span("plugins.bos_sp"):
        _apply_bos_sp(rows, staging, snapshot=snapshot)
    return rows


def _scan_orphan_plugins(game, data_dir: Path | None,
                         exts: tuple[str, ...], listed_lower: set[str],
                         vanilla: dict[str, str],
                         saved_order: list[str]) -> list[PluginEntry]:
    """Plugin files sitting in the game's Data/ folder that the user installed
    manually - not in plugins.txt (*listed_lower*), not in loadorder.txt
    (*saved_order*), not vanilla, not deployed by another profile. Returned as
    enabled entries so the panel surfaces them (Tk parity:
    gui/plugin_panel.py:_refresh_plugins_tab orphan scan).

    When a deploy is active, Data/ contains mod hardlinks owned by the mod
    manager. Anything NOT also present in Data_Core/ (the vanilla snapshot)
    came from a mod deploy and must not be treated as a manual orphan -
    otherwise disabling a mod would leave its plugin in the panel, because the
    hardlink in Data/ outlives its plugins.txt entry until the next deploy."""
    if data_dir is None or not data_dir.is_dir():
        return []
    try:
        from Utils.game_helpers import foreign_deployed_plugin_basenames
        foreign = foreign_deployed_plugin_basenames(game)
    except Exception:
        foreign = set()
    saved_lower = {n.lower() for n in saved_order}
    core_dir = data_dir.parent / (data_dir.name + "_Core")
    core_names: set[str] | None = None
    if core_dir.is_dir():
        core_names = set()
        try:
            for centry in core_dir.iterdir():
                if centry.is_file() and centry.name.lower().endswith(exts):
                    core_names.add(centry.name.lower())
        except OSError:
            core_names = None
    orphans: list[PluginEntry] = []
    try:
        for entry in data_dir.iterdir():
            if not entry.is_file() or not entry.name.lower().endswith(exts):
                continue
            low = entry.name.lower()
            if (low in listed_lower or low in vanilla
                    or low in saved_lower or low in foreign):
                continue
            if core_names is not None and low not in core_names:
                continue
            orphans.append(PluginEntry(name=entry.name, enabled=True))
    except OSError:
        pass
    return orphans


def _append_orphans_to_plugins(plugins_path: Path, star: bool,
                               orphans: list[PluginEntry]) -> None:
    """Append plugins missing from plugins.txt (manually-installed Data/
    orphans AND filemap-recovered entries from enabled mods) so they stay
    listed (and get deployed / LOOT-sorted) without requiring a panel edit.
    Best-effort and idempotent - names already listed are skipped, failures
    are swallowed (the plugin just re-surfaces from its scan next reload).
    Entries with a loadorder.txt position go back to that slot (plugins.txt
    file order is the engine load order); the rest append at the end."""
    try:
        from Utils.plugins import insert_by_loadorder
        entries = read_plugins(plugins_path, star_prefix=star)
        listed = {e.name.lower() for e in entries}
        new = [o for o in orphans if o.name.lower() not in listed]
        if new:
            lo = read_loadorder(plugins_path.parent / "loadorder.txt")
            lo_pos = {n.lower(): i for i, n in enumerate(lo)}
            for o in new:
                insert_by_loadorder(entries, o, lo_pos)
            write_plugins(plugins_path, entries, star_prefix=star)
            app_log(f"Plugins: added {len(new)} plugin(s) missing from "
                    f"plugins.txt: {', '.join(o.name for o in new)}")
    except Exception:
        pass


def _prune_phantom_plugins(plugins_path: Path, star: bool,
                           phantom_lower: set[str],
                           loadorder_too: bool = True) -> None:
    """Remove *phantom_lower* plugin names from plugins.txt (+ loadorder.txt
    unless *loadorder_too* is False - disabled-mod-owned prunes on star games
    keep the loadorder entry as position memory). Best-effort - failures are
    swallowed so a read-only profile still renders."""
    try:
        entries = read_plugins(plugins_path, star_prefix=star)
        new_entries = [e for e in entries if e.name.lower() not in phantom_lower]
        if len(new_entries) != len(entries):
            write_plugins(plugins_path, new_entries, star_prefix=star)
    except Exception:
        pass
    if not loadorder_too:
        return
    try:
        lo_path = plugins_path.parent / "loadorder.txt"
        lo = read_loadorder(lo_path)
        new_lo = [n for n in lo if n.lower() not in phantom_lower]
        if len(new_lo) != len(lo):
            write_loadorder(lo_path,
                            [PluginEntry(name=n, enabled=True) for n in new_lo])
    except Exception:
        pass


def prune_listed_plugins(game, profile: str, names: list[str]) -> None:
    """Remove *names* from the profile's plugins.txt + loadorder.txt.

    User-confirmed mass cleanup behind the Refresh Modlist flow: load_plugins'
    automatic prune refuses to drop more than _PRUNE_MAX unresolved entries
    (SAFETY 3 - a mass miss usually means a broken resolution, not a stale
    file), so removals above the cap require this explicit path."""
    p = plugins_path(game, profile)
    if p is None or not p.is_file() or not names:
        return
    star = getattr(game, "plugins_use_star_prefix", True)
    _prune_phantom_plugins(p, star, {n.lower() for n in names})
    app_log(f"Plugins: removed {len(names)} listed plugin(s) with no on-disk "
            f"file (user-confirmed Refresh cleanup): {', '.join(names)}")


def _to_row(e: PluginEntry, vanilla: dict, resolved: dict[str, Path],
            data_dir: Path | None, blueprints: bool = False) -> PluginRow:
    low = e.name.lower()
    flags = 0
    masters: list[str] | None = None
    path = resolved.get(low) or ((data_dir / e.name) if data_dir else None)
    if path and path.is_file():
        try:
            from Utils.plugin_parser import (
                is_esl_flagged, is_master_flagged, is_blueprint_flagged,
                read_masters)
            if is_esl_flagged(path) or low.endswith(".esl"):
                flags |= PF_ESL
            if is_master_flagged(path) or low.endswith(".esm"):
                flags |= PF_MASTER
            # Gated: 0x800 only means "blueprint" on Starfield.
            if blueprints and is_blueprint_flagged(path):
                flags |= PF_BLUEPRINT
            masters = [m.lower() for m in read_masters(path)]
        except Exception:
            if low.endswith(".esl"):
                flags |= PF_ESL
            if low.endswith(".esm"):
                flags |= PF_MASTER
    else:
        if low.endswith(".esl"):
            flags |= PF_ESL
        if low.endswith(".esm"):
            flags |= PF_MASTER
    return PluginRow(e.name, e.enabled, flags, low in vanilla, masters=masters)


def _apply_master_checks(rows: list[PluginRow], resolved: dict[str, Path],
                         data_dir: Path | None) -> None:
    """Flag missing / late / version-mismatched masters from each plugin's
    resolved on-disk path (staging/overwrite/Data), not just the Data dir - so
    the checks work for mod plugins on un-deployed profiles too. The check_*
    functions index plugin_paths by name.lower(), so key the dict that way."""
    names = [r.name for r in rows]
    # Lowercase-keyed paths: resolved path wins; fall back to data_dir/name.
    paths = {r.name.lower(): (resolved.get(r.name.lower())
                              or ((data_dir / r.name) if data_dir else None))
             for r in rows}
    paths = {k: v for k, v in paths.items() if v is not None}
    if not paths:
        return
    try:
        from Utils.plugin_parser import (
            check_missing_masters, check_late_masters,
            check_version_mismatched_masters)
        missing = check_missing_masters(names, paths)
        late = check_late_masters(names, paths)
        # vmm needs the vanilla Data dir for master sizes; skip if unavailable.
        vmm = (check_version_mismatched_masters(names, paths, data_dir)
               if data_dir is not None and data_dir.is_dir() else {})
    except Exception:
        return
    for r in rows:
        m = missing.get(r.name)
        if m:
            r.flags |= PF_MISSING
            r.missing_masters = list(m)
        lt = late.get(r.name)
        if lt:
            r.flags |= PF_LATE
            r.late_masters = list(lt)
        vm = vmm.get(r.name)
        if vm:
            r.flags |= PF_VMM
            r.vmm_masters = list(vm)


_FILENAME_REQ_RE = re.compile(r'^Filename\(["\'](.+?)["\']\)$')
_NEXUS_ID_RE = re.compile(r"nexusmods\.com/[^/\s)]+/mods/(\d+)")
_SE_LOADER_RE = re.compile(
    r"\b(sk?se(64|vr)?|f4se(vr)?|fose|nvse|obse|sfse|mwse)\b")


class RequirementResolver:
    """Resolves LOOT "Requires" entries against the active profile so a
    requirement that IS satisfied - but not by an *enabled plugin name* - is not
    flagged as missing.

    Ported from Tk gui/plugin_panel_loot.py (`_is_requirement_satisfied` and its
    helpers). The Qt v1 tooltip only checked enabled plugin filenames, so an
    SKSE-plugin requirement like "PapyrusUtil SE" (a Nexus link / a bare
    `SKSE/Plugins/*.dll`, never a plugin file) always read as missing even when
    the mod was installed and enabled. This resolver adds the other three
    strategies the Tk app used:

      1. enabled plugin filename (Filename("foo.esp"))
      2. staged file path (filemap.txt) or a top-level game-root file
      3. an enabled mod's Nexus mod_id (from the requirement's Nexus URL)
      4. script-extender heuristic → an installed extender loader

    Built once per plugin load; all lookups are lazy + cached on the instance.
    """

    def __init__(self, game, profile_dir: Path, staging_root: Path | None,
                 enabled_plugins_lower: set[str], snapshot=None):
        self._game = game
        self._profile_dir = profile_dir
        self._staging_root = staging_root
        self._enabled_lower = enabled_plugins_lower
        self._snapshot = snapshot
        self._mod_ids: set[int] | None = None
        self._root_files: set[str] | None = None
        self._se_detected: bool | None = None

    # -- lazy context sets ------------------------------------------------
    def _staged_contains(self, path: str, *, basename: bool = False) -> bool:
        return bool(
            self._snapshot is not None
            and self._snapshot.has_deployed_path(path, basename=basename)
        )

    def _enabled_mod_ids(self) -> set[int]:
        if self._mod_ids is not None:
            return self._mod_ids
        ids: set[int] = set()
        modlist_path = self._profile_dir / "modlist.txt"
        if (self._staging_root is not None and self._staging_root.is_dir()
                and modlist_path.is_file()):
            try:
                from Utils.modlist import read_modlist
                from Nexus.nexus_meta import read_meta
                for e in read_modlist(modlist_path):
                    if not e.enabled:
                        continue
                    meta_path = self._staging_root / e.name / "meta.ini"
                    if not meta_path.is_file():
                        continue
                    try:
                        meta = read_meta(meta_path)
                        if meta.mod_id:
                            ids.add(int(meta.mod_id))
                    except Exception:
                        pass
            except Exception:
                pass
        self._mod_ids = ids
        return ids

    def _game_root_files(self) -> set[str]:
        if self._root_files is not None:
            return self._root_files
        names: set[str] = set()
        game = self._game
        root = None
        if game is not None and hasattr(game, "get_game_path"):
            try:
                root = game.get_game_path()
            except Exception:
                root = None
        if root is not None:
            root = Path(root)
            if root.is_dir():
                try:
                    for entry in root.iterdir():
                        if entry.is_file():
                            names.add(entry.name.lower())
                except OSError:
                    pass
        self._root_files = names
        return names

    def _script_extender_detected(self) -> bool:
        if self._se_detected is not None:
            return self._se_detected
        self._se_detected = self._detect_script_extender()
        return self._se_detected

    def _detect_script_extender(self) -> bool:
        """True if a script-extender loader is installed (game root / staged).

        Mirrors Tk _script_extender_detected: a loader counts if it sits in the
        game root or is present in the staged filemap. Root_Folder staging and
        disabled-mod detection from Tk are approximated by the staged-path check,
        which already includes Root_Folder deploys."""
        game = self._game
        if game is None:
            return False
        try:
            frameworks = game.frameworks or {}
        except Exception:
            frameworks = {}
        from Utils.framework_detect import framework_exe_candidates
        se_exes = [
            exe for label, value in frameworks.items()
            for exe in framework_exe_candidates(value)
            if "script extender" in label.lower()
            or _SE_LOADER_RE.search(exe.rsplit("/", 1)[-1].lower())
        ]
        if not se_exes:
            return False
        root_files = self._game_root_files()
        for exe in se_exes:
            base = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if base in root_files:
                return True
            rel = exe.replace("\\", "/").lstrip("./").lstrip("../").lower()
            if (self._staged_contains(rel)
                    or self._staged_contains(base, basename=True)):
                return True
        return False

    # -- resolution -------------------------------------------------------
    def is_satisfied(self, raw: str, display: str) -> bool:
        """True if a LOOT requirement entry is met in the current profile."""
        m = _FILENAME_REQ_RE.match(raw)
        if m:
            inner = m.group(1).replace("\\", "/").lstrip("./").lstrip("../")
            inner_lower = inner.lower()
            if inner_lower in self._enabled_lower:
                return True
            if self._staged_contains(inner_lower):
                return True
            base = inner_lower.rsplit("/", 1)[-1]
            if base and base in self._game_root_files():
                return True
        mod_id = _extract_nexus_mod_id(display) or _extract_nexus_mod_id(raw)
        if mod_id is not None and mod_id in self._enabled_mod_ids():
            return True
        text = f"{display} {raw}".lower()
        if "script extender" in text or _SE_LOADER_RE.search(text):
            if self._script_extender_detected():
                return True
        return False

    def unsatisfied_requirements(self, reqs: list) -> list:
        """Filter a requirements list down to the entries not satisfied."""
        out = []
        for r in reqs:
            raw = r.get("name", "")
            display = r.get("display_name") or raw
            if not self.is_satisfied(raw, display):
                out.append(r)
        return out


def _extract_nexus_mod_id(text: str) -> int | None:
    """Pull a Nexus mod_id out of a URL or markdown link, if present."""
    if not text:
        return None
    m = _NEXUS_ID_RE.search(text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _apply_loot_flags(rows: list[PluginRow], profile_dir: Path,
                      resolver: "RequirementResolver | None" = None) -> None:
    """Flag LOOT messages / dirty edits / bash tags from the cached loot.json.

    *resolver* (when given) filters each plugin's LOOT "requirements" down to the
    entries not already satisfied by the active profile (enabled plugin, staged
    file, enabled Nexus mod, or installed script extender). The filtered list is
    what drives both the PF_LOOT flag icon and the rendered tooltip, so a
    requirement met by a non-plugin mod (e.g. PapyrusUtil SE) no longer shows as
    missing. Without a resolver the old enabled-plugin-only behaviour is used by
    the tooltip renderer."""
    try:
        from LOOT.loot_sorter import read_loot_info
        data = read_loot_info(profile_dir)
    except Exception:
        return
    plugins = data.get("plugins", {}) if isinstance(data, dict) else {}
    version = data.get("version", 1) if isinstance(data, dict) else 1
    info: dict[str, dict] = {}
    if version >= 2:
        info = {k.lower(): v for k, v in plugins.items() if isinstance(v, dict) and v}
    else:
        info = {k.lower(): {"messages": v} for k, v in plugins.items()
                if isinstance(v, list) and v}
    for r in rows:
        d = info.get(r.name.lower())
        if not d:
            continue
        # Resolve "requirements" against the active profile so a requirement
        # already satisfied (by an enabled mod's Nexus id, a staged file, or an
        # installed script extender - not just an enabled plugin name) doesn't
        # light the flag or show up as missing. Shallow-copy before overwriting
        # so the shared cached `info` dict is left intact for other consumers.
        reqs = d.get("requirements") or []
        if reqs and resolver is not None:
            unmet = resolver.unsatisfied_requirements(reqs)
            if len(unmet) != len(reqs):
                d = dict(d)
                d["requirements"] = unmet
                reqs = unmet
        matched = False
        if d.get("messages") or reqs or d.get("incompatibilities"):
            r.flags |= PF_LOOT
            matched = True
        if d.get("dirty"):
            r.flags |= PF_DIRTY
            matched = True
        if d.get("tags"):
            r.flags |= PF_TAGS
            matched = True
        if matched:
            # Keep the (requirement-filtered) per-plugin dict so the Flags
            # tooltip renders only the still-missing requirements.
            r.loot_info = d


def _apply_userlist_flags(rows: list[PluginRow], profile_dir: Path) -> None:
    """Flag plugins managed by <profile>/userlist.yaml (white dot; red when
    their rules form a cycle). Mirrors Tk _refresh_userlist_set + _predraw."""
    try:
        from Utils.userlist import read_userlist_state
        state = read_userlist_state(profile_dir / "userlist.yaml")
    except Exception:
        return
    if not state.plugins:
        return
    for r in rows:
        low = r.name.lower()
        if low in state.plugins:
            r.flags |= PF_USERLIST
            if low in state.cycle_plugins:
                r.flags |= PF_UL_CYCLE


def compute_esl_eligibility(names: list[str], resolved: dict[str, Path],
                            data_dir: Path | None, game) -> dict[str, int]:
    """Return {plugin_name_lower: PF_ESL_SAFE | PF_ESL_UNSAFE} for each
    .esp/.esm in *names* - libloot's is-this-safe-to-ESL-flag verdict,
    mirroring Tk _refresh_esl_flagged_set. Feeds the ESL-safe/unsafe filters.

    NOT called from load_plugins: a cold scan is seconds of libloot record
    parsing that does not release the GIL, which starves every other reload
    worker AND the UI thread. The window defers it to its own worker after
    the plugin rows are applied (app._start_esl_scan) and patches the bits in.

    Gated on the game's ``supports_esl_flag`` capability - no point scanning
    games without an ESL flag (Fallout 3 / Oblivion / Morrowind). ``.esl``
    files are always light by extension, so eligibility isn't computed for
    them. Results are cached by (path, mtime_ns, size, game_type, version) so
    the full-file record scan only runs when a plugin file is rewritten.
    """
    out: dict[str, int] = {}
    if not getattr(game, "supports_esl_flag", False):
        return out
    game_type_attr = getattr(game, "loot_game_type", "") or ""
    try:
        from Utils.plugin_parser import check_esl_eligible
    except Exception:
        return out
    for name in names:
        low = name.lower()
        # .esl files are always light by extension - not eligibility-scanned.
        if low.endswith(".esl") or not low.endswith((".esp", ".esm")):
            continue
        path = resolved.get(low) or ((data_dir / name) if data_dir else None)
        if path is None:
            continue
        try:
            st = os.stat(str(path))
        except OSError:
            continue
        elig_key = ((str(path), st.st_mtime_ns, st.st_size),
                    game_type_attr, _ESL_ELIG_CACHE_VERSION)
        cached = _ESL_ELIG_CACHE.get(elig_key)
        if cached is None:
            try:
                cached = bool(check_esl_eligible(path, game_type_attr))
            except Exception:
                cached = False
            _ESL_ELIG_CACHE[elig_key] = cached
        out[low] = PF_ESL_SAFE if cached else PF_ESL_UNSAFE
    return out


def scan_bos_sp_patches(staging_root: Path | None, snapshot=None) -> dict[str, str]:
    """Scan staging mods for BOS (Base Object Swapper) / SkyPatcher patches.

    Returns {plugin_name_lower: "bos" | "sp" | "both"} for every staged plugin
    a patch targets. Semantics ported from Tk _do_scan_bos_sp:

    * BOS: a mod ships ``<PluginStem>_SWAP.ini`` anywhere under it.
    * SP:  a SkyPatcher/SkyPatcher2 INI has a ``filterByFormID = Plugin.esp|..``
           line referencing the plugin. Patch mods target *other* mods' plugins,
           so every mod is scanned, not just the plugin's owner.

    The fast path requests only relevant raw rows from the pinned filegraph
    snapshot. Only SkyPatcher INI contents are read from disk. Before initial
    graph publication it falls back to the original disk walk.
    A lock serializes concurrent scans (overlapping plugin reloads) so the
    second waits and hits the first's cache instead of duplicating the work.
    Safe to call off the UI thread (pure filesystem)."""
    if staging_root is None:
        return {}
    with _BOS_SP_LOCK:
        if snapshot is not None:
            cache_key = (
                "filegraph", str(staging_root), snapshot.inventory_generation)
            cached = _BOS_SP_CACHE.get(cache_key)
            if cached is not None:
                return cached
            all_plugins: set[str] = set()
            bos_stems: set[str] = set()
            sp_plugins: set[str] = set()
            for mod_name, relative_bytes in snapshot.patch_files():
                if mod_name == _OVERWRITE_NAME:
                    continue
                relative = relative_bytes.decode("utf-8", "surrogateescape")
                lower = relative.replace("\\", "/").lower()
                basename = lower.rsplit("/", 1)[-1]
                if basename.endswith((".esp", ".esm", ".esl")):
                    all_plugins.add(basename)
                if basename.endswith("_swap.ini"):
                    bos_stems.add(basename[:-len("_swap.ini")])
                if (basename.endswith(".ini")
                        and ("/skse/plugins/skypatcher/" in "/" + lower
                             or "/skse/plugins/skypatcher2/" in "/" + lower)):
                    try:
                        _parse_sp_ini_text(
                            (staging_root / mod_name / relative).read_text(
                                encoding="utf-8", errors="ignore"),
                            sp_plugins)
                    except OSError:
                        pass
            result = _combine_bos_sp(all_plugins, bos_stems, sp_plugins)
            _BOS_SP_CACHE[cache_key] = result
            return result
        return _scan_bos_sp_disk(staging_root)


def _parse_sp_ini_text(text: str, sp_plugins: set[str]) -> None:
    """Collect the plugin names referenced by ``filterByFormID = Plugin.esp|…``
    lines of one SkyPatcher INI into *sp_plugins* (lowercase)."""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(";"):
            continue
        if s.lower().startswith("filterbyformid"):
            eq = s.find("=")
            if eq == -1:
                continue
            val = s[eq + 1:].strip()
            if "|" in val:
                ref = val.split("|")[0].strip().lower()
                if ref.endswith((".esp", ".esm", ".esl")):
                    sp_plugins.add(ref)


def _scan_bos_sp_from_index(index: dict, staging_root: Path,
                            overwrite_name: str) -> dict[str, str]:
    """Index-backed BOS/SP scan - see scan_bos_sp_patches. Index paths are
    destination-relative (the game's top-level strip prefix, e.g. ``Data``,
    already removed), which matches what the disk walk collected from the mod
    root + ``Data/``. Only SkyPatcher INIs are opened; the index key stripped
    any prefix, so both on-disk candidates are tried."""
    all_plugins: set[str] = set()
    bos_stems: set[str] = set()
    sp_plugins: set[str] = set()
    for mod_name, (normal, _root) in index.items():
        if mod_name == overwrite_name:   # disk-walk parity: staging mods only
            continue
        mod_dir = staging_root / mod_name
        for rel_low, rel_orig in normal.items():
            base = rel_low.rsplit("/", 1)[-1]
            if "/" not in rel_low and base.endswith((".esp", ".esm", ".esl")):
                all_plugins.add(base)
            if base.endswith("_swap.ini"):
                bos_stems.add(base[:-len("_swap.ini")])
            elif base.endswith(".ini") and rel_low.startswith(
                    ("skse/plugins/skypatcher/", "skse/plugins/skypatcher2/")):
                for cand in (mod_dir / rel_orig, mod_dir / "Data" / rel_orig):
                    try:
                        _parse_sp_ini_text(
                            cand.read_text(encoding="utf-8", errors="ignore"),
                            sp_plugins)
                        break
                    except (OSError, UnicodeDecodeError):
                        continue
    return _combine_bos_sp(all_plugins, bos_stems, sp_plugins)


def _combine_bos_sp(all_plugins: set[str], bos_stems: set[str],
                    sp_plugins: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for pname in all_plugins:
        is_bos = Path(pname).stem.lower() in bos_stems
        is_sp = pname in sp_plugins
        if is_bos and is_sp:
            result[pname] = "both"
        elif is_bos:
            result[pname] = "bos"
        elif is_sp:
            result[pname] = "sp"
    return result


def _scan_bos_sp_disk(staging_root: Path) -> dict[str, str]:
    """Original full-disk-walk BOS/SP scan - the fallback when modindex.bin is
    missing or unreadable. Cached by (total staging dir mtime, staging path)."""
    staging_str = str(staging_root)
    try:
        total_mtime = sum(
            d.stat().st_mtime
            for d in staging_root.iterdir()
            if d.is_dir()
        )
    except OSError:
        total_mtime = 0.0
    cache_key = (total_mtime, staging_str)
    cached = _BOS_SP_CACHE.get(cache_key)
    if cached is not None:
        return cached

    all_plugins: set[str] = set()   # all plugin basenames (lowercase) in staging
    bos_stems: set[str] = set()     # plugin stems (lowercase) with a _SWAP.ini
    sp_plugins: set[str] = set()    # plugin names (lowercase) in filterByFormID

    try:
        for mod_dir in staging_root.iterdir():
            if not mod_dir.is_dir():
                continue

            search_roots = [mod_dir]
            data_sub = mod_dir / "Data"
            if data_sub.is_dir():
                search_roots.append(data_sub)

            # Collect plugin basenames
            for root in search_roots:
                try:
                    for f in root.iterdir():
                        if f.is_file() and f.suffix.lower() in {".esp", ".esm", ".esl"}:
                            all_plugins.add(f.name.lower())
                except OSError:
                    pass

            # BOS: any <stem>_SWAP.ini anywhere under the mod
            for root in search_roots:
                try:
                    for f in root.rglob("*.ini"):
                        if f.is_file() and f.name.lower().endswith("_swap.ini"):
                            bos_stems.add(f.name.lower()[:-len("_swap.ini")])
                except OSError:
                    pass

            # SP: parse filterByFormID lines in SkyPatcher/SkyPatcher2 INIs.
            for sp_dir_name in ("SkyPatcher", "SkyPatcher2"):
                sp_dir = mod_dir / "SKSE" / "Plugins" / sp_dir_name
                if not sp_dir.is_dir():
                    continue
                try:
                    for ini in sp_dir.rglob("*.ini"):
                        if not ini.is_file():
                            continue
                        try:
                            _parse_sp_ini_text(
                                ini.read_text(encoding="utf-8",
                                              errors="ignore"),
                                sp_plugins)
                        except (OSError, UnicodeDecodeError):
                            pass
                except OSError:
                    pass
    except OSError:
        pass

    result = _combine_bos_sp(all_plugins, bos_stems, sp_plugins)
    _BOS_SP_CACHE[cache_key] = result
    return result


def _apply_bos_sp(rows: list[PluginRow], staging_root: Path | None,
                  snapshot=None) -> None:
    """Tag each row with its BOS/SP patch kind (see scan_bos_sp_patches)."""
    kinds = scan_bos_sp_patches(staging_root, snapshot=snapshot)
    if not kinds:
        return
    for r in rows:
        r.bos_sp = kinds.get(r.name.lower(), "")


_FILENAME_RE = re.compile(r'^Filename\(["\'](.+?)["\']\)$')


def format_loot_tooltip(info: dict, enabled_lower: set[str]) -> str:
    """Render a loot.json plugin-info dict into the multi-section tooltip string
    (messages / missing requirements / active incompatibilities / dirty edits /
    bash tags). Ported from Tk gui/plugin_panel_loot.py:_format_loot_tooltip.

    *enabled_lower* is the set of enabled plugin filenames (lowercase); it filters
    requirements to those not met by an enabled plugin, and incompatibilities to
    those whose conflicting plugin is currently enabled. The richer resolution
    (staged files / enabled Nexus mod ids / script-extender detection - Tk parity)
    is applied upstream by RequirementResolver in _apply_loot_flags, which stores
    only the still-unsatisfied requirements in `info["requirements"]`, so a
    non-plugin requirement like PapyrusUtil SE no longer reads as missing."""
    if not info:
        return ""
    sections: list[str] = []

    msgs = info.get("messages") or []
    if msgs:
        lines = []
        for m in msgs:
            prefix = {"error": "[!]", "warn": "[!]", "say": "[i]"}.get(
                m.get("type", "say"), "[i]")
            lines.append(f"{prefix} {m.get('text', '')}")
        sections.append("LOOT messages:\n" + "\n".join(lines))

    reqs = info.get("requirements") or []
    if reqs:
        lines = []
        for r in reqs:
            raw = r.get("name", "")
            display = r.get("display_name") or raw
            m = _FILENAME_RE.match(raw)
            fname = m.group(1) if m else raw
            fname_lower = fname.replace("\\", "/").lstrip("./").lstrip("../").lower()
            if fname_lower in enabled_lower:
                continue
            dm = _FILENAME_RE.match(display)
            if dm:
                display = dm.group(1)
            line = f"  - {display}"
            detail = r.get("detail", "")
            if detail:
                line += f" ({detail})"
            lines.append(line)
        if lines:
            sections.append("Requires (missing):\n" + "\n".join(lines))

    incs = info.get("incompatibilities") or []
    if incs:
        lines = []
        for i in incs:
            raw = i.get("name", "")
            display = i.get("display_name") or raw
            m = _FILENAME_RE.match(raw)
            fname = m.group(1) if m else raw
            fname_lower = fname.lower().lstrip("./").lstrip("../")
            if fname_lower not in enabled_lower:
                continue
            dm = _FILENAME_RE.match(display)
            if dm:
                display = dm.group(1)
            line = f"  - {display}"
            detail = i.get("detail", "")
            if detail:
                line += f" ({detail})"
            lines.append(line)
        if lines:
            sections.append("Incompatible with (currently active):\n" + "\n".join(lines))

    dirty = info.get("dirty") or []
    if dirty:
        lines = []
        for d in dirty:
            parts = []
            if d.get("itm"):
                parts.append(f"{d['itm']} ITM")
            if d.get("udr"):
                parts.append(f"{d['udr']} UDR")
            if d.get("nav"):
                parts.append(f"{d['nav']} deleted navmesh")
            counts = ", ".join(parts) if parts else "needs cleaning"
            line = f"  - {counts}"
            util = d.get("utility", "")
            if util:
                um = re.match(r'^\[(.+?)\]\(.+?\)$', util)
                line += f" - clean with {um.group(1) if um else util}"
            lines.append(line)
            detail = d.get("detail", "")
            if detail:
                lines.append(f"    {detail}")
        sections.append("Dirty edits:\n" + "\n".join(lines))

    tags = info.get("tags") or {}
    if tags:
        lines = []
        cur = tags.get("current") or []
        add = tags.get("add") or []
        rem = tags.get("remove") or []
        if cur:
            lines.append("  Current: " + ", ".join(cur))
        if add:
            lines.append("  Suggested (add): " + ", ".join(f"+{t}" for t in add))
        if rem:
            lines.append("  Suggested (remove): " + ", ".join(f"-{t}" for t in rem))
        if lines:
            sections.append("Bash Tags:\n" + "\n".join(lines))

    return "\n\n".join(sections)


def apply_loot_sort(rows: list[PluginRow], locked_indices: dict[int, PluginRow],
                    sorted_names: list[str],
                    include_vanilla: bool) -> "tuple[list[PluginRow], int]":
    """Re-interleave a LOOT sort result back into the row list.

    *rows* is the pre-sort order; *locked_indices* maps an index in *rows* → the
    locked PluginRow that must stay at that index; *sorted_names* is LOOT's order
    for the UNLOCKED plugins only. Returns (new_rows, visible_moved_count).

    Pure (no Qt) so it's unit-testable. Mirrors gui/plugin_panel_loot.py
    _apply_result (264-295).
    """
    vanilla_lower = {r.name.lower() for r in rows if r.vanilla}
    # Case-insensitive: LOOT returns names with on-disk casing, which can
    # differ from the plugins.txt casing in *rows* - a case-sensitive miss
    # here silently re-enabled disabled plugins on every sort.
    name_to_enabled = {r.name.lower(): r.enabled for r in rows}
    total = len(rows)
    pre_unlocked = [r.name for i, r in enumerate(rows) if i not in locked_indices]
    if len(sorted_names) != len(pre_unlocked):
        # Set mismatch (shouldn't happen - LOOT preserves the input set). Bail
        # to the original order rather than risk a bad interleave.
        return list(rows), 0
    it = iter(sorted_names)
    new_rows: list[PluginRow] = []
    for i in range(total):
        if i in locked_indices:
            new_rows.append(locked_indices[i])
        else:
            name = next(it)
            new_rows.append(PluginRow(
                name, name_to_enabled.get(name.lower(), True), 0,
                name.lower() in vanilla_lower))

    # Moved count over plugins the user actually sees (exclude hidden vanilla).
    def _visible(names):
        return [n for n in names
                if include_vanilla or n.lower() not in vanilla_lower]
    before = _visible([r.name for r in rows])
    after = _visible([r.name for r in new_rows])
    moved = sum(1 for i, n in enumerate(after)
                if i >= len(before) or before[i] != n)
    return new_rows, moved


def save_plugins(game, profile: str, rows: list[PluginRow]) -> None:
    """Write the plugin order + enable state back to disk.

    plugins.txt - mod plugins only (vanilla excluded unless the game includes
    them); loadorder.txt - the FULL order incl. vanilla so LOOT-sorted positions
    survive a refresh. Mirrors plugin_panel._save_plugins (Tk parity)."""
    p = plugins_path(game, profile)
    if p is None:
        return
    rows, _ = enforce_primary_plugin_order(game, rows)
    star = getattr(game, "plugins_use_star_prefix", True)
    include_vanilla = bool(getattr(game, "plugins_include_vanilla", False))
    # The whole vanilla set - base masters, DLC AND .ccc-listed Creation Club
    # content (incl. _ResourcePack.esl, which ships inside Skyrim.ccc) - stays
    # out of plugins.txt: the engine force-loads it at fixed early positions
    # before reading the file, ignores any entries for it, and strips them when
    # it rewrites plugins.txt on launch. MO2 (primaryPlugins skip), Vortex
    # (nativePlugins filter) and libloadorder/LOOT all exclude it the same way.
    mod_entries = [PluginEntry(r.name, r.enabled) for r in rows
                   if include_vanilla or not r.vanilla]
    write_plugins(p, mod_entries, star_prefix=star)
    full = [PluginEntry(r.name, True) for r in rows]
    write_loadorder(p.parent / "loadorder.txt", full)
    # Timestamp-ordered games (Oblivion/FO3/FNV) need deployed mtimes re-stamped.
    if game is not None and hasattr(game, "stamp_plugin_load_order"):
        try:
            game.stamp_plugin_load_order(profile)
        except Exception as exc:
            print(f"[gui_qt] stamp_plugin_load_order failed: {exc}", flush=True)
