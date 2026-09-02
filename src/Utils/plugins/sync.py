"""Sync plugins.txt / loadorder.txt when mods are enabled or disabled.

Disabling a mod removes its top-level plugins from plugins.txt (star games
keep the loadorder.txt entry as position memory); enabling adds missing ones
back at their remembered load-order slot. The whole batch is one
read-modify-write per file. Per-mod plugin spellings come from a compact
Filegraph snapshot query, with a targeted disk scan when no snapshot is ready."""

from __future__ import annotations

from pathlib import Path

from Utils.plugins import (
    read_plugins, write_plugins, read_loadorder, write_loadorder, PluginEntry,
    insert_by_loadorder,
)


def _plugin_data_subfolders(game) -> set[str]:
    try:
        from Utils.games.registry import game_data_subpath
        subfolder = game_data_subpath(game)
        strip_prefixes = {
            str(value).lower() for value in
            (getattr(game, "mod_folder_strip_prefixes", None) or ())
        }
        if (subfolder and "/" not in subfolder
                and subfolder.lower() in strip_prefixes):
            return {subfolder.lower()}
    except Exception:
        pass
    return set()


def _scan_mod_plugins(game, staging_root: Path, mod_name: str,
                      plugin_exts: set[str],
                      data_subfolders: set[str]) -> list[str]:
    mod_dir = staging_root / mod_name
    if not mod_dir.is_dir():
        return []
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        lowered = name.lower()
        if lowered not in seen:
            names.append(name)
            seen.add(lowered)

    try:
        nested: list[Path] = []
        for entry in mod_dir.iterdir():
            if entry.is_file() and entry.suffix.lower() in plugin_exts:
                add(entry.name)
            elif (data_subfolders and entry.is_dir()
                  and entry.name.lower() in data_subfolders):
                nested.append(entry)
        for folder in nested:
            for entry in folder.iterdir():
                if entry.is_file() and entry.suffix.lower() in plugin_exts:
                    add(entry.name)
    except OSError:
        pass

    try:
        from Utils.games.registry import routed_mod_plugin_names
        for name in routed_mod_plugin_names(game, mod_dir):
            add(name)
    except Exception:
        pass
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
    from Utils.diagnostics.performance import span
    from Utils.filegraph.service import FileGraphService
    with span("plugin_sync.current_snapshot"):
        library = FileGraphService.open_library(game, profile_dir, log_fn=log)
        snapshot = library.try_inventory_snapshot(profile_dir)
    plugins_path = profile_dir / "plugins.txt"
    # NB: do NOT bail when plugins.txt is missing. A game that has no plugins.txt
    # concept was already filtered out above (empty plugin_exts), so a missing
    # file here just means a fresh profile that has never had a plugin enabled.
    # Tk's _sync_plugins_for_toggle creates it via write_plugins in that case;
    # read_plugins returns [] and write_plugins creates the file (+ parents), so
    # the code below handles a missing file correctly. An earlier port bailed
    # here, which silently dropped a freshly-enabled mod's plugins on a new
    # profile (they never reached plugins.txt) - a Qt-vs-Tk regression.

    add: list[str] = []
    add_seen: set[str] = set()
    remove_lower: set[str] = set()
    with span("plugin_sync.mod_plugins"):
        if snapshot is not None:
            plugin_changes = [
                (mod_name, now_enabled, snapshot.mod_plugins(mod_name))
                for mod_name, now_enabled in changes
            ]
        else:
            data_subfolders = _plugin_data_subfolders(game)
            plugin_changes = [
                (mod_name, now_enabled, _scan_mod_plugins(
                    game, staging_root, mod_name, plugin_exts,
                    data_subfolders))
                for mod_name, now_enabled in changes
            ]
    for mod_name, now_enabled, found in plugin_changes:
        # The native path returns only the handful of plugin spellings instead
        # of materialising every rich file/conflict record for large outputs.
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
                    f"staging folder at {mdir} - nothing added to plugins.txt")
        for name in found:
            low = name.lower()
            if now_enabled:
                # Dedupe: two mods in one batch can provide the same plugin
                # (patcher outputs) - one plugins.txt line, not two.
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
