"""Sync plugins.txt / loadorder.txt when mods are enabled or disabled.

Tkinter-free port of gui/modlist_panel.py `_sync_plugins_for_toggle` (8730-8776):
disabling a mod removes its top-level plugin files from plugins.txt and
loadorder.txt; enabling appends any that aren't already listed. Unlike Tk (which
re-reads/re-writes both files once per mod), the whole batch is applied in a
single read-modify-write so Enable/Disable-all doesn't do N I/O round-trips.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Mapping

from Utils.deploy_shared import resolve_mod_dir
from Utils.plugins import (
    read_plugins, write_plugins, read_loadorder, write_loadorder, PluginEntry,
)


def _mod_plugins(staging_root: "Path | Mapping[str, Path]", mod_name: str,
                 plugin_exts: set[str],
                 data_subfolders: "set[str] | None" = None) -> list[str]:
    """Top-level plugin filenames inside the mod's real folder (what Tk
    scans), plus the top level of any *data_subfolders* (lowercase names, e.g.
    {'data files'}). The latter covers mods whose plugins sit one level down in
    staging yet deploy to the top of the data dir: root-flagged mods (verbatim
    to game root → '<subfolder>/x.esp' lands in the data dir) and mods that
    retain a strip prefix on disk (the filemap strips it).

    staging_root is a plain Path for a regular profile or a
    {mod_name: real_mod_dir} mapping for a Profile Group — see
    resolve_mod_dir in deploy_shared.py."""
    mod_dir = resolve_mod_dir(staging_root, mod_name)
    if mod_dir is None or not mod_dir.is_dir():
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
    return names


def sync_plugins_for_mods(game, profile_dir: Path | None,
                          staging_root: "Path | Mapping[str, Path] | None",
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
    try:
        from Utils.game_helpers import game_data_subpath
        sub = game_data_subpath(game)
        strips = {s.lower() for s in
                  (getattr(game, "mod_folder_strip_prefixes", None) or ())}
        if sub and "/" not in sub and sub.lower() in strips:
            data_subs = {sub.lower()}
    except Exception:
        data_subs = None
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
    remove_lower: set[str] = set()
    for mod_name, now_enabled in changes:
        found = _mod_plugins(staging_root, mod_name, plugin_exts, data_subs)
        if now_enabled and not found:
            # Enabling a mod whose staging folder has NO top-level plugin files
            # for this game's extensions. This is completely normal for
            # content-only mods (textures/meshes/grass caches/etc.) and needs no
            # report. Only warn when the staging folder is actually missing,
            # which points at a real bug (never-staged / wrong-cased / symlinked
            # folder) rather than a plain content mod.
            mdir = resolve_mod_dir(staging_root, mod_name)
            if mdir is None or not mdir.is_dir():
                log(f"WARN plugin sync: enabled mod \"{mod_name}\" has no "
                    f"staging folder at {mdir} — nothing added to plugins.txt")
        for name in found:
            if now_enabled:
                add.append(name)
            else:
                remove_lower.add(name.lower())
    # Enable wins over disable within one batch.
    remove_lower -= {n.lower() for n in add}
    if not add and not remove_lower:
        return False

    star = getattr(game, "plugins_use_star_prefix", True)
    loadorder_path = profile_dir / "loadorder.txt"
    wrote = False

    entries = read_plugins(plugins_path, star_prefix=star)
    existing_lower = {e.name.lower() for e in entries}
    new_entries = [e for e in entries if e.name.lower() not in remove_lower]
    added = [n for n in add if n.lower() not in existing_lower]
    for name in added:
        new_entries.append(PluginEntry(name=name, enabled=True))
    if added or len(new_entries) != len(entries):
        write_plugins(plugins_path, new_entries, star_prefix=star)
        wrote = True

    loadorder = read_loadorder(loadorder_path)
    lo_lower = {n.lower() for n in loadorder}
    new_lo = [n for n in loadorder if n.lower() not in remove_lower]
    lo_added = [n for n in add if n.lower() not in lo_lower]
    new_lo.extend(lo_added)
    if lo_added or len(new_lo) != len(loadorder):
        write_loadorder(loadorder_path,
                        [PluginEntry(name=n, enabled=True) for n in new_lo])
        wrote = True

    if wrote:
        removed_ct = len(remove_lower & (existing_lower | lo_lower))
        log(f"Plugins synced: +{len(added)} / -{removed_ct} "
            f"for {len(changes)} mod(s).")
    return wrote
