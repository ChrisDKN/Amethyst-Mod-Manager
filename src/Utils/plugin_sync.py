"""Sync plugins.txt / loadorder.txt when mods are enabled or disabled.

Disabling a mod removes its top-level plugins from plugins.txt (star games
keep the loadorder.txt entry as position memory); enabling adds missing ones
back at their remembered load-order slot. The whole batch is one
read-modify-write per file. Per-mod plugin sets come from modindex.bin, with
a staging disk scan as fallback for unindexed mods."""

from __future__ import annotations

from pathlib import Path

from Utils.plugins import (
    read_plugins, write_plugins, read_loadorder, write_loadorder, PluginEntry,
    insert_by_loadorder,
)


def _mod_plugins(staging_root: Path, mod_name: str,
                 plugin_exts: set[str],
                 data_subfolders: "set[str] | None" = None,
                 game=None) -> list[str]:
    """Top-level plugin filenames inside staging_root/<mod_name>/ (what Tk
    scans), plus the top level of any *data_subfolders* (lowercase names, e.g.
    {'data files'}). The latter covers mods whose plugins sit one level down in
    staging yet deploy to the top of the data dir: root-flagged mods (verbatim
    to game root → '<subfolder>/x.esp' lands in the data dir) and mods that
    retain a strip prefix on disk (the filemap strips it).

    For rule-routing games (*game* with a plugins_routing_ctx, e.g. Oblivion
    Remastered) plugins can additionally sit ANYWHERE inside the mod as long
    as the deploy rules route them to the top of the data dir — those are
    picked up via routed_mod_plugin_names."""
    mod_dir = staging_root / mod_name
    if not mod_dir.is_dir():
        return []
    names: list[str] = []
    subdirs: list[Path] = []
    try:
        for f in mod_dir.iterdir():
            if f.is_file() and f.suffix.lower() in plugin_exts:
                names.append(f.name)
            elif data_subfolders and f.is_dir() and f.name.lower() in data_subfolders:
                subdirs.append(f)
        for d in subdirs:
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in plugin_exts:
                    names.append(f.name)
    except OSError:
        pass
    if game is not None:
        try:
            from Utils.game_helpers import routed_mod_plugin_names
            seen = {n.lower() for n in names}
            for n in routed_mod_plugin_names(game, mod_dir):
                if n.lower() not in seen:
                    names.append(n)
                    seen.add(n.lower())
        except Exception:
            pass
    return names


def _mod_plugins_from_index(index, mod_name: str, exts: tuple[str, ...],
                            root_prefix: str, routing_ctx) -> "list[str] | None":
    """Top-level plugin names for *mod_name* from modindex.bin, or None if the
    mod is not indexed (caller falls back to the disk scan).

    Index rel paths are destination-relative (strip prefixes removed), so this
    matches exactly what the filemap deploys to the top of the data dir — the
    same universe the panel's filemap recovery adds plugins from. The disk
    scan misses layouts the filemap handles (e.g. non-data strip prefixes),
    which left stale plugins.txt entries on disable (GH#318 class)."""
    entry = index.get(mod_name)
    if entry is None:
        return None
    normal, root = entry
    names: list[str] = []
    seen: set[str] = set()

    def _add(n: str) -> None:
        if n.lower() not in seen:
            names.append(n)
            seen.add(n.lower())

    for rel_low, rel_orig in normal.items():
        if "/" not in rel_low:
            if rel_low.endswith(exts):
                _add(rel_orig)
        elif routing_ctx is not None:
            from Utils.game_helpers import routed_plugin_name
            n = routed_plugin_name(routing_ctx, rel_orig, exts)
            if n is not None:
                _add(n)
    # Root-flagged entries deploy verbatim to the game root, so
    # '<data subpath>/<plugin>' lands at the top of the data dir.
    if root_prefix and root:
        prefix_low = root_prefix.lower() + "/"
        plen = len(root_prefix) + 1
        for rel_low, rel_orig in root.items():
            if rel_low.startswith(prefix_low):
                name = rel_orig[plen:]
                if "/" not in name and name.lower().endswith(exts):
                    _add(name)
    return names


def sync_plugins_for_mods(game, profile_dir: Path | None,
                          staging_root: Path | None,
                          changes: list[tuple[str, bool]],
                          log_fn=None) -> bool:
    """Apply mod enable/disable *changes* (``[(mod_name, now_enabled), ...]``)
    to plugins.txt + loadorder.txt. Returns True if either file was rewritten.

    When the same plugin belongs to both an enabled and a disabled mod in the
    batch, enable wins (the file is still provided by the enabled mod).
    """
    log = log_fn or (lambda m: None)
    if game is None or profile_dir is None or staging_root is None or not changes:
        return False
    plugin_exts = {e.lower() for e in
                   (getattr(game, "plugin_extensions", []) or [])}
    if not plugin_exts:
        return False
    # Also scan the top level of the game's data subfolder inside each mod
    # ('Data Files/' for Morrowind) — see _mod_plugins. Gated on the subfolder
    # being a declared strip prefix so a folder that would deploy NESTED into
    # the data dir (never loadable) isn't scanned by mistake.
    data_subs: "set[str] | None" = None
    sub = ""
    try:
        from Utils.game_helpers import game_data_subpath
        sub = game_data_subpath(game)
        strips = {s.lower() for s in
                  (getattr(game, "mod_folder_strip_prefixes", None) or ())}
        if sub and "/" not in sub and sub.lower() in strips:
            data_subs = {sub.lower()}
    except Exception:
        data_subs = None
    # Primary per-mod plugin source: modindex.bin (in-memory cached, and its
    # destination-relative paths match what the filemap actually deploys).
    # Mods missing from the index (e.g. symlinked dirs) fall back to the
    # disk scan below.
    index = None
    try:
        from Utils.filemap import read_mod_index
        index = read_mod_index(staging_root.parent / "modindex.bin")
    except Exception:
        index = None
    routing_ctx = None
    try:
        from Utils.game_helpers import plugins_routing_ctx
        routing_ctx = plugins_routing_ctx(game)
    except Exception:
        routing_ctx = None
    exts_t = tuple(plugin_exts)
    plugins_path = profile_dir / "plugins.txt"
    # NB: do NOT bail when plugins.txt is missing. A game that has no plugins.txt
    # concept was already filtered out above (empty plugin_exts), so a missing
    # file here just means a fresh profile that has never had a plugin enabled.
    # Tk's _sync_plugins_for_toggle creates it via write_plugins in that case;
    # read_plugins returns [] and write_plugins creates the file (+ parents), so
    # the code below handles a missing file correctly. An earlier port bailed
    # here, which silently dropped a freshly-enabled mod's plugins on a new
    # profile (they never reached plugins.txt) — a Qt-vs-Tk regression.

    add: list[str] = []
    add_seen: set[str] = set()
    remove_lower: set[str] = set()
    for mod_name, now_enabled in changes:
        found = None
        if index is not None:
            found = _mod_plugins_from_index(index, mod_name, exts_t,
                                            sub, routing_ctx)
        if found is None:
            found = _mod_plugins(staging_root, mod_name, plugin_exts,
                                 data_subs, game=game)
        if now_enabled and not found:
            # Enabling a mod whose staging folder has NO top-level plugin files
            # for this game's extensions. This is completely normal for
            # content-only mods (textures/meshes/grass caches/etc.) and needs no
            # report. Only warn when the staging folder is actually missing,
            # which points at a real bug (never-staged / wrong-cased / symlinked
            # folder) rather than a plain content mod.
            mdir = staging_root / mod_name
            if not mdir.is_dir():
                log(f"WARN plugin sync: enabled mod \"{mod_name}\" has no "
                    f"staging folder at {mdir} — nothing added to plugins.txt")
        for name in found:
            low = name.lower()
            if now_enabled:
                # Dedupe: two mods in one batch can provide the same plugin
                # (patcher outputs) — one plugins.txt line, not two.
                if low not in add_seen:
                    add.append(name)
                    add_seen.add(low)
            else:
                remove_lower.add(low)
    # Enable wins over disable within one batch.
    remove_lower -= add_seen
    if not add and not remove_lower:
        return False

    star = getattr(game, "plugins_use_star_prefix", True)
    loadorder_path = profile_dir / "loadorder.txt"
    wrote = False

    loadorder = read_loadorder(loadorder_path)
    lo_lower = {n.lower() for n in loadorder}
    if star:
        # Keep disabled mods' names in loadorder.txt: it is the position
        # memory, so a disable→enable round-trip restores each plugin's slot
        # instead of appending at the end. Legacy (non-star) games encode
        # "user-disabled plugin" as in-loadorder-but-not-in-plugins.txt, so
        # there a disabled mod's names must leave the file or they would
        # resurface as disabled panel rows.
        new_lo = list(loadorder)
    else:
        new_lo = [n for n in loadorder if n.lower() not in remove_lower]
    lo_added = [n for n in add if n.lower() not in lo_lower]
    new_lo.extend(lo_added)
    if lo_added or len(new_lo) != len(loadorder):
        write_loadorder(loadorder_path,
                        [PluginEntry(name=n, enabled=True) for n in new_lo])
        wrote = True

    entries = read_plugins(plugins_path, star_prefix=star)
    existing_lower = {e.name.lower() for e in entries}
    new_entries = [e for e in entries if e.name.lower() not in remove_lower]
    added = [n for n in add if n.lower() not in existing_lower]
    if added:
        # plugins.txt file order IS the engine load order (the deploy copies
        # it verbatim into the prefix), so a re-added plugin must go back to
        # the slot loadorder.txt kept for it, not the end of the file.
        lo_pos = {n.lower(): i for i, n in enumerate(new_lo)}
        for name in added:
            insert_by_loadorder(new_entries, PluginEntry(name=name, enabled=True),
                                lo_pos)
    if added or len(new_entries) != len(entries):
        write_plugins(plugins_path, new_entries, star_prefix=star)
        wrote = True

    if wrote:
        removed_ct = len(remove_lower & (existing_lower | lo_lower))
        log(f"Plugins synced: +{len(added)} / -{removed_ct} "
            f"for {len(changes)} mod(s).")
    return wrote
