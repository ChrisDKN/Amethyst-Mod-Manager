"""Plugin list loading for the Qt Plugins tab.

Produces the ordered, flagged plugin list for the active game/profile by reusing
the backend: Utils.plugins (read_plugins / read_loadorder / write_plugins) and
Utils.plugin_parser (ESL / master header flags). Vanilla plugins are pinned to
the top, then mods follow saved loadorder.txt order.

v1 scope: list + order + enable-toggle + ESL/master flags. The deeper Tk logic
(orphan detection, Data_Core pruning, LOOT messages, bash tags, missing-master
checks) is deferred — the Flags column is structured to receive them later.
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
)

# Verbose plugin-panel diagnostics. Set AMM_PLUGIN_DIAG=1 to log every stage of
# load_plugins (plugins.txt / filemap recovery / resolver / prune) — used to
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

# Bump when check_esl_eligible() changes its verdict criteria so cached
# eligibility results are invalidated (mirrors Tk _ESL_ELIG_CACHE_VERSION).
_ESL_ELIG_CACHE_VERSION = 2

# Process-wide caches keyed by (path, mtime_ns, size[, game_type, version]) so
# the expensive per-plugin record scan / flag read only runs when a plugin file
# is actually rewritten — mirrors Tk _esl_flag_cache / _esl_eligible_cache.
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


_EXT_ORDER = {".esm": 0, ".esp": 1, ".esl": 2}

_OVERWRITE_NAME = "[Overwrite]"


def compute_game_indexes(rows: list[PluginRow]) -> list[str]:
    """Return the game's load index for each row, aligned to *rows* order.

    - Disabled            → "" (no index).
    - Light / ESL-flagged → "FE:xxx" (all share slot FE, sub-index rolls to
      FF after 4096).
    - Normal              → "%02X" of the running normal-plugin counter.

    Medium / ESH (slot FD) is not handled — the model has no medium flag today
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
                          routing_ctx=None) -> dict[str, Path]:
    """Map plugin filename (lowercase) → its on-disk path, from THREE sources in
    priority order (Tk parity: gui/plugin_panel.py:_check_all_masters).

    Mod plugins live in staging / overwrite (resolved via filemap.txt), NOT in
    the vanilla Data dir, so reading a plugin header needs this resolver — using
    only data_dir misses every mod-added (incl. ESL-flagged) plugin.
    """
    paths: dict[str, Path] = {}
    exts = tuple(e.lower() for e in plugin_exts)

    # 1. filemap.txt → staging mods (and overwrite).
    overwrite_dir = staging_dir.parent / "overwrite" if staging_dir else None
    if filemap_path is not None and staging_dir is not None and filemap_path.is_file():
        try:
            for line in filemap_path.read_text(encoding="utf-8").splitlines():
                if "\t" not in line:
                    continue
                rel_path, mod_name = line.split("\t", 1)
                rel_path = rel_path.replace("\\", "/")
                if "/" in rel_path:
                    # Rule-routing games (Oblivion Remastered): a NESTED
                    # staged path can still deploy to the top of the plugins
                    # data dir — resolve it via the game's routing rules. The
                    # staged rel is the real on-disk path under the mod dir.
                    if routing_ctx is None:
                        continue
                    from Utils.game_helpers import routed_plugin_name
                    name = routed_plugin_name(routing_ctx, rel_path, exts)
                    if name is None:
                        continue
                    if mod_name == _OVERWRITE_NAME and overwrite_dir is not None:
                        paths[name.lower()] = overwrite_dir / rel_path
                    else:
                        paths[name.lower()] = staging_dir / mod_name / rel_path
                    continue
                if not rel_path.lower().endswith(exts):
                    continue
                low = rel_path.lower()
                if mod_name == _OVERWRITE_NAME and overwrite_dir is not None:
                    paths[low] = overwrite_dir / rel_path
                else:
                    direct = staging_dir / mod_name / rel_path
                    if direct.is_file():
                        paths[low] = direct
                    else:
                        found = _find_plugin_in_mod_dir(
                            staging_dir / mod_name, rel_path)
                        paths[low] = found or direct
        except OSError:
            pass

    # 1b. filemap_root.txt → root-flagged mods. Their entries deploy VERBATIM
    # to the game root, so '<root_prefix>/<plugin>' (Morrowind:
    # 'Data Files/XE Sky Variations.esp') lands at the top level of the deploy
    # dir. Rel paths are UNSTRIPPED, so staging/<mod>/<rel> resolves directly.
    # Root deploy runs after the main deploy and overwrites, so these win over
    # same-named filemap.txt entries.
    if (root_filemap_path is not None and staging_dir is not None
            and root_prefix and root_filemap_path.is_file()):
        prefix_low = root_prefix.lower() + "/"
        plen = len(root_prefix) + 1
        try:
            for line in root_filemap_path.read_text(encoding="utf-8").splitlines():
                if "\t" not in line:
                    continue
                rel_path, mod_name = line.split("\t", 1)
                rel_path = rel_path.replace("\\", "/")
                if not rel_path.lower().startswith(prefix_low):
                    continue
                name = rel_path[plen:]
                if "/" in name or not name.lower().endswith(exts):
                    continue   # nested below the data dir — not loadable
                src = staging_dir / mod_name / rel_path
                if not src.is_file():
                    found = _find_plugin_in_mod_dir(staging_dir / mod_name, name)
                    src = found or src
                paths[name.lower()] = src
        except OSError:
            pass

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


def resolve_plugin_paths_for_game(game, data_dir: Path | None = None
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
        filemap_path = (staging.parent / "filemap.txt") if staging else None
        root_fm = (staging.parent / "filemap_root.txt") if staging else None
        try:
            from Utils.game_helpers import game_data_subpath
            root_prefix = game_data_subpath(game)
        except Exception:
            root_prefix = ""
        try:
            from Utils.game_helpers import plugins_routing_ctx
            routing_ctx = plugins_routing_ctx(game)
        except Exception:
            routing_ctx = None
        exts = tuple(x.lower() for x in (getattr(game, "plugin_extensions", []) or ())) \
            or (".esp", ".esm", ".esl")
        return _resolve_plugin_paths(staging, data_dir, filemap_path, exts,
                                     root_filemap_path=root_fm,
                                     root_prefix=root_prefix,
                                     routing_ctx=routing_ctx)
    except Exception:
        return {}


def _filemap_deployed_plugins(game, plugin_exts: tuple[str, ...]) -> dict[str, str]:
    """Top-level plugin names that the CURRENT filemap.txt deploys — i.e. still
    provided by some enabled mod (or overwrite). Returns {lower: original_name}.

    A patcher mod (e.g. ESLifier Output) ships duplicate copies of plugins that
    other enabled mods also provide. Disabling it strips those names from
    plugins.txt (Tk parity: _sync_plugins_for_toggle removes a mod's own
    plugins unconditionally), but the other mods still deploy identically-named
    copies. Without this recovery those plugins vanish from the panel until a
    full re-sync. Tk recovers them in _refresh_plugins_tab via its Data/ orphan
    scan; we recover them from the freshly-rebuilt filemap instead.
    """
    staging = (game.get_effective_mod_staging_path()
               if hasattr(game, "get_effective_mod_staging_path") else None)
    if staging is None:
        _diag("_filemap_deployed_plugins: no staging path")
        return {}
    fm = staging.parent / "filemap.txt"
    if not fm.is_file():
        _diag(f"_filemap_deployed_plugins: filemap.txt MISSING at {fm}")
        return {}
    exts = tuple(e.lower() for e in plugin_exts)
    # Rule-routing games (Oblivion Remastered): a nested staged path can still
    # deploy to the top of the plugins data dir — treat those the same as
    # top-level entries. See Utils.game_helpers.plugins_routing_ctx.
    try:
        from Utils.game_helpers import plugins_routing_ctx, routed_plugin_name
        routing_ctx = plugins_routing_ctx(game)
    except Exception:
        routing_ctx = None
    found: dict[str, str] = {}
    total_lines = 0
    try:
        for line in fm.read_text(encoding="utf-8").splitlines():
            total_lines += 1
            if "\t" not in line:
                continue
            rel_path = line.split("\t", 1)[0].replace("\\", "/")
            if "/" in rel_path:
                # Top-level entries match the deploy layout directly; nested
                # ones only count when the routing rules land them in the
                # data dir.
                if routing_ctx is None:
                    continue
                name = routed_plugin_name(routing_ctx, rel_path, exts)
                if name is not None:
                    found.setdefault(name.lower(), name)
                continue
            low = rel_path.lower()
            if low.endswith(exts):
                found.setdefault(low, rel_path)
    except OSError as exc:
        _diag(f"_filemap_deployed_plugins: read error on {fm}: {exc}")
        return {}
    # Root-flagged mods (filemap_root.txt) deploy VERBATIM to the game root:
    # '<data subpath>/<plugin>' lands at the top level of the deploy dir, the
    # same place a top-level filemap.txt entry does — recover those too (e.g.
    # a root MGE XE install shipping 'Data Files/XE Sky Variations.esp').
    fm_root = staging.parent / "filemap_root.txt"
    if fm_root.is_file():
        try:
            from Utils.game_helpers import game_data_subpath
            prefix = game_data_subpath(game)
        except Exception:
            prefix = ""
        if prefix:
            prefix_low = prefix.lower() + "/"
            plen = len(prefix) + 1
            try:
                for line in fm_root.read_text(encoding="utf-8").splitlines():
                    if "\t" not in line:
                        continue
                    rel_path = line.split("\t", 1)[0].replace("\\", "/")
                    if not rel_path.lower().startswith(prefix_low):
                        continue
                    name = rel_path[plen:]
                    if "/" in name:
                        continue   # nested below the data dir — not loadable
                    low = name.lower()
                    if low.endswith(exts):
                        found.setdefault(low, name)
            except OSError as exc:
                _diag(f"_filemap_deployed_plugins: read error on {fm_root}: {exc}")
    _diag(f"_filemap_deployed_plugins: {fm} has {total_lines} line(s), "
          f"{len(found)} top-level plugin(s) with exts {exts}")
    return found


def load_plugins(game, profile: str,
                 cancelled=None, report: dict | None = None
                 ) -> "list[PluginRow] | None":
    """Return the ordered plugin rows for *game*/*profile*, or [] if none.

    *cancelled* — optional zero-arg callable polled between the expensive
    phases (path resolution, per-plugin header reads, master checks, ESL
    eligibility, BOS/SP scan). When it returns True the load aborts and
    returns None: a superseded reload's result is dropped by the caller's
    generation check anyway, so finishing it just burns seconds of disk + GIL
    time that slow the reload that superseded it.

    *report* — optional dict filled with prune diagnostics for the caller:
    'prune_checked' (the phantom-prune actually ran, i.e. filemap_ok held)
    and 'mass_prune' (names SAFETY 3 refused to auto-prune — more unresolved
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
    # present in Data — same resolver the Tk app uses.
    try:
        from Utils.game_helpers import _vanilla_plugins_for_game
        with span("plugins.vanilla_resolve"):
            vanilla = _vanilla_plugins_for_game(game)
    except Exception:
        vanilla = {n.lower(): n for n in getattr(game, "vanilla_plugins", [])}

    # Recover plugins still deployed by an enabled mod (per the fresh filemap)
    # but missing from plugins.txt — see _filemap_deployed_plugins. The guard
    # is the listed-entry set below: a disabled patcher mod's toggle sync
    # (Utils/plugin_sync.py) strips its plugins from BOTH plugins.txt and
    # loadorder.txt, so a name absent from both that the filemap still deploys
    # is exactly the case to recover as enabled.
    exts = tuple(e.lower() for e in (getattr(game, "plugin_extensions", []) or [])) \
        or (".esp", ".esm", ".esl")
    listed_lower = {e.name.lower() for e in entries}
    # Legacy (non-star) games have no disabled syntax in plugins.txt — a
    # user-disabled plugin is OMITTED from the file and survives only in
    # loadorder.txt (see Utils/plugins.py). Reconstruct those names as
    # disabled entries BEFORE the filemap recovery below: the plugin's file is
    # still deployed by its (enabled) mod, so without this the recovery
    # re-added it as enabled — and persisted it back into plugins.txt — on
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
    with span("plugins.filemap_deployed"):
        deployed = _filemap_deployed_plugins(game, exts)
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
    # installed manually — see _scan_orphan_plugins. Appended here so they show
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

    # Resolve each plugin's REAL path (staging mod / overwrite / Data) so header
    # flags (ESL, master, missing-master) work for mod plugins, not just vanilla.
    if cancelled():
        return None
    with span("plugins.resolve_paths"):
        resolved = resolve_plugin_paths_for_game(game, data_dir)
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
    # returns {} on ANY failure — pruning on an empty map would wipe every
    # non-vanilla plugin from plugins.txt. Require the filemap to exist AND the
    # resolver to have found at least one path before trusting a miss.
    staging = (game.get_effective_mod_staging_path()
               if hasattr(game, "get_effective_mod_staging_path") else None)
    fm_path = (staging.parent / "filemap.txt") if staging is not None else None
    # SAFETY 4 (freshness): plugins.txt newer than filemap.txt means entries
    # were appended AFTER the filemap was built — a just-committed install
    # (_add_plugins) or toggle sync whose conflict rebuild hasn't landed yet.
    # The resolver can't see those mods, so an unresolved name proves nothing.
    # Without this guard the prune deleted a freshly installed mod's plugin
    # from plugins.txt within the same second as its install (SkyUI_SE.esp,
    # 2026-07-17); later reloads then recovered it from the fresh filemap for
    # DISPLAY only, so it never returned to plugins.txt. Also gates the
    # recovered-entry persist below (a stale filemap could re-add a
    # just-disabled mod's plugins).
    filemap_fresh = True
    try:
        if fm_path is not None and fm_path.is_file():
            filemap_fresh = fm_path.stat().st_mtime >= p.stat().st_mtime
    except OSError:
        pass
    if not filemap_fresh:
        _diag(f"load_plugins: SAFETY-4 filemap OLDER than plugins.txt "
              f"({fm_path}) — prune + recovered-persist skipped this reload")
    filemap_ok = (fm_path is not None and fm_path.is_file()
                  and bool(resolved) and filemap_fresh)
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
        # one on screen — a prime suspect for "plugins missing after a toggle".
        _diag(f"load_plugins: SAFETY-2 active-dir MISMATCH — "
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
        kept: list[PluginEntry] = []
        pruned: list[str] = []
        for e in ordered:
            low = e.name.lower()
            if low in vanilla:
                kept.append(e); continue
            rp = resolved.get(low)
            if rp is not None and rp.is_file():
                kept.append(e)
            else:
                pruned.append(e.name)
        # SAFETY 3: a genuine stale entry is one removed mod's worth of
        # plugins. A mass miss means the resolution itself is wrong (desync
        # not caught above, or filemap.txt read mid-rewrite) — keep the
        # entries and let a later healthy reload prune them one by one.
        # Reported to the caller so an EXPLICIT Refresh can offer the user a
        # confirmed mass cleanup (a genuinely polluted plugins.txt — e.g.
        # another profile's load order copied in — looks identical to a
        # broken resolution from here, so only the user can arbitrate).
        if pruned and len(pruned) > _PRUNE_MAX:
            app_log(f"Plugins: NOT pruning {len(pruned)} unresolved plugin(s) "
                    f"(> {_PRUNE_MAX}) — wrong-staging/partial-filemap "
                    f"resolution suspected; plugins.txt left untouched. "
                    f"Refresh Modlist offers a confirmed cleanup.")
            if report is not None:
                report["mass_prune"] = list(pruned)
        elif pruned:
            app_log(f"Plugins: pruned {len(pruned)} stale entr(y/ies) with no "
                    f"on-disk file: {', '.join(pruned)}")
            _prune_phantom_plugins(p, star, set(n.lower() for n in pruned))
            ordered = kept

    # Persist manual orphans into plugins.txt so LOOT sort and deploy pick
    # them up even if the user never touches the panel. Same SAFETY-2 guard
    # as the prune: only write when the game object points at the profile
    # being loaded. Skip any the prune dropped above (shouldn't happen —
    # orphans resolve via the Data/Data_Core scan — but stay consistent).
    if orphans and _active_matches:
        still = {e.name.lower() for e in ordered}
        _append_orphans_to_plugins(
            p, star, [o for o in orphans if o.name.lower() in still])

    # Persist filemap-recovered plugins too (Tk parity: Tk's Data/ orphan scan
    # fed the same sync that wrote them back to plugins.txt). Without this, a
    # plugin that lost its plugins.txt entry — e.g. pruned by an earlier
    # stale-filemap reload — shows in the panel forever but never reaches
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
        rows = [_to_row(e, vanilla, resolved, data_dir) for e in ordered]
    if cancelled():
        return None
    with span("plugins.master_checks"):
        _apply_master_checks(rows, resolved, data_dir)
    with span("plugins.loot_flags"):
        enabled_lower = {r.name.lower() for r in rows if r.enabled}
        resolver = RequirementResolver(game, p.parent, staging, enabled_lower)
        _apply_loot_flags(rows, p.parent, resolver)
    with span("plugins.userlist_flags"):
        _apply_userlist_flags(rows, p.parent)
    if cancelled():
        return None
    # ESL eligibility deliberately NOT computed here — see
    # compute_esl_eligibility (deferred to its own post-apply worker).
    with span("plugins.bos_sp"):
        _apply_bos_sp(rows, staging)
    return rows


def _scan_orphan_plugins(game, data_dir: Path | None,
                         exts: tuple[str, ...], listed_lower: set[str],
                         vanilla: dict[str, str],
                         saved_order: list[str]) -> list[PluginEntry]:
    """Plugin files sitting in the game's Data/ folder that the user installed
    manually — not in plugins.txt (*listed_lower*), not in loadorder.txt
    (*saved_order*), not vanilla, not deployed by another profile. Returned as
    enabled entries so the panel surfaces them (Tk parity:
    gui/plugin_panel.py:_refresh_plugins_tab orphan scan).

    When a deploy is active, Data/ contains mod hardlinks owned by the mod
    manager. Anything NOT also present in Data_Core/ (the vanilla snapshot)
    came from a mod deploy and must not be treated as a manual orphan —
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
    Best-effort and idempotent — names already listed are skipped, failures
    are swallowed (the plugin just re-surfaces from its scan next reload)."""
    try:
        entries = read_plugins(plugins_path, star_prefix=star)
        listed = {e.name.lower() for e in entries}
        new = [o for o in orphans if o.name.lower() not in listed]
        if new:
            write_plugins(plugins_path, entries + new, star_prefix=star)
            app_log(f"Plugins: added {len(new)} plugin(s) missing from "
                    f"plugins.txt: {', '.join(o.name for o in new)}")
    except Exception:
        pass


def _prune_phantom_plugins(plugins_path: Path, star: bool,
                           phantom_lower: set[str]) -> None:
    """Remove *phantom_lower* plugin names from plugins.txt + loadorder.txt.

    Called when load_plugins finds a listed plugin with no on-disk file (removed
    mod). Mirrors Tk, which lets its plugins.txt sync write such entries out once
    their source plugin disappears. Best-effort — failures are swallowed so a
    read-only profile still renders (the phantom just re-prunes next reload)."""
    try:
        entries = read_plugins(plugins_path, star_prefix=star)
        new_entries = [e for e in entries if e.name.lower() not in phantom_lower]
        if len(new_entries) != len(entries):
            write_plugins(plugins_path, new_entries, star_prefix=star)
    except Exception:
        pass
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
    (SAFETY 3 — a mass miss usually means a broken resolution, not a stale
    file), so removals above the cap require this explicit path."""
    p = plugins_path(game, profile)
    if p is None or not p.is_file() or not names:
        return
    star = getattr(game, "plugins_use_star_prefix", True)
    _prune_phantom_plugins(p, star, {n.lower() for n in names})
    app_log(f"Plugins: removed {len(names)} listed plugin(s) with no on-disk "
            f"file (user-confirmed Refresh cleanup): {', '.join(names)}")


def _to_row(e: PluginEntry, vanilla: dict, resolved: dict[str, Path],
            data_dir: Path | None) -> PluginRow:
    low = e.name.lower()
    flags = 0
    path = resolved.get(low) or ((data_dir / e.name) if data_dir else None)
    if path and path.is_file():
        try:
            from Utils.plugin_parser import is_esl_flagged, is_master_flagged
            if is_esl_flagged(path) or low.endswith(".esl"):
                flags |= PF_ESL
            if is_master_flagged(path) or low.endswith(".esm"):
                flags |= PF_MASTER
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
    return PluginRow(e.name, e.enabled, flags, low in vanilla)


def _apply_master_checks(rows: list[PluginRow], resolved: dict[str, Path],
                         data_dir: Path | None) -> None:
    """Flag missing / late / version-mismatched masters from each plugin's
    resolved on-disk path (staging/overwrite/Data), not just the Data dir — so
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
    requirement that IS satisfied — but not by an *enabled plugin name* — is not
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
                 enabled_plugins_lower: set[str]):
        self._game = game
        self._profile_dir = profile_dir
        self._staging_root = staging_root
        self._enabled_lower = enabled_plugins_lower
        self._staged_paths: set[str] | None = None
        self._mod_ids: set[int] | None = None
        self._root_files: set[str] | None = None
        self._se_detected: bool | None = None

    # -- lazy context sets ------------------------------------------------
    def _staged(self) -> set[str]:
        if self._staged_paths is not None:
            return self._staged_paths
        paths: set[str] = set()
        fm = (self._staging_root.parent / "filemap.txt") \
            if self._staging_root is not None else None
        if fm is not None and fm.is_file():
            try:
                with fm.open(encoding="utf-8") as f:
                    for line in f:
                        if "\t" not in line:
                            continue
                        rel = line.split("\t", 1)[0].strip()
                        if rel:
                            paths.add(rel.replace("\\", "/").lower())
            except OSError:
                pass
        self._staged_paths = paths
        return paths

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
        staged = self._staged()
        for exe in se_exes:
            base = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if base in root_files:
                return True
            rel = exe.replace("\\", "/").lstrip("./").lstrip("../").lower()
            if rel in staged or any(p.rsplit("/", 1)[-1] == base for p in staged):
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
            if inner_lower in self._staged():
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
        # installed script extender — not just an enabled plugin name) doesn't
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
    .esp/.esm in *names* — libloot's is-this-safe-to-ESL-flag verdict,
    mirroring Tk _refresh_esl_flagged_set. Feeds the ESL-safe/unsafe filters.

    NOT called from load_plugins: a cold scan is seconds of libloot record
    parsing that does not release the GIL, which starves every other reload
    worker AND the UI thread. The window defers it to its own worker after
    the plugin rows are applied (app._start_esl_scan) and patches the bits in.

    Gated on the game's ``supports_esl_flag`` capability — no point scanning
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
        # .esl files are always light by extension — not eligibility-scanned.
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


def scan_bos_sp_patches(staging_root: Path | None) -> dict[str, str]:
    """Scan staging mods for BOS (Base Object Swapper) / SkyPatcher patches.

    Returns {plugin_name_lower: "bos" | "sp" | "both"} for every staged plugin
    a patch targets. Semantics ported from Tk _do_scan_bos_sp:

    * BOS: a mod ships ``<PluginStem>_SWAP.ini`` anywhere under it.
    * SP:  a SkyPatcher/SkyPatcher2 INI has a ``filterByFormID = Plugin.esp|..``
           line referencing the plugin. Patch mods target *other* mods' plugins,
           so every mod is scanned, not just the plugin's owner.

    Fast path: derive everything from modindex.bin (already an in-memory-cached
    parse) instead of walking the staging tree — a full rglob over hundreds of
    mods costs seconds, the index pass costs milliseconds. Only SkyPatcher INI
    contents are read from disk. Cached by the index's mtime, which only changes
    on install/remove/refresh — deploys touch staging dir mtimes but not the
    index, so the cache survives an auto-deploy (the old mtime-sum key didn't).
    Falls back to the original disk walk when the index is missing/unreadable.
    A lock serializes concurrent scans (overlapping plugin reloads) so the
    second waits and hits the first's cache instead of duplicating the work.
    Safe to call off the UI thread (pure filesystem)."""
    if staging_root is None:
        return {}
    index_path = staging_root.parent / "modindex.bin"
    try:
        idx_key = ("modindex", str(index_path), index_path.stat().st_mtime_ns)
    except OSError:
        idx_key = None
    with _BOS_SP_LOCK:
        if idx_key is not None:
            cached = _BOS_SP_CACHE.get(idx_key)
            if cached is not None:
                return cached
            from Utils.filemap import read_mod_index, OVERWRITE_NAME
            index = read_mod_index(index_path)
            if index:
                result = _scan_bos_sp_from_index(
                    index, staging_root, OVERWRITE_NAME)
                _BOS_SP_CACHE[idx_key] = result
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
    """Index-backed BOS/SP scan — see scan_bos_sp_patches. Index paths are
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
    """Original full-disk-walk BOS/SP scan — the fallback when modindex.bin is
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


def _apply_bos_sp(rows: list[PluginRow], staging_root: Path | None) -> None:
    """Tag each row with its BOS/SP patch kind (see scan_bos_sp_patches)."""
    kinds = scan_bos_sp_patches(staging_root)
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
    (staged files / enabled Nexus mod ids / script-extender detection — Tk parity)
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
                line += f" — clean with {um.group(1) if um else util}"
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
    # differ from the plugins.txt casing in *rows* — a case-sensitive miss
    # here silently re-enabled disabled plugins on every sort.
    name_to_enabled = {r.name.lower(): r.enabled for r in rows}
    total = len(rows)
    pre_unlocked = [r.name for i, r in enumerate(rows) if i not in locked_indices]
    if len(sorted_names) != len(pre_unlocked):
        # Set mismatch (shouldn't happen — LOOT preserves the input set). Bail
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

    plugins.txt — mod plugins only (vanilla excluded unless the game includes
    them); loadorder.txt — the FULL order incl. vanilla so LOOT-sorted positions
    survive a refresh. Mirrors plugin_panel._save_plugins (Tk parity)."""
    p = plugins_path(game, profile)
    if p is None:
        return
    star = getattr(game, "plugins_use_star_prefix", True)
    include_vanilla = bool(getattr(game, "plugins_include_vanilla", False))
    # The whole vanilla set — base masters, DLC AND .ccc-listed Creation Club
    # content (incl. _ResourcePack.esl, which ships inside Skyrim.ccc) — stays
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
