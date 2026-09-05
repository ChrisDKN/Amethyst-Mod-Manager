"""Toolkit-neutral full mod removal - delete a mod's deployed files + staging
folder + Filegraph rows + its plugins from plugins.txt/loadorder.txt.

Ported from the Tk ModListPanel._remove_mod / _remove_plugins_for_mods so the Qt
remove does the SAME complete cleanup (not just dropping the modlist line, which
leaves the files on disk → the mod still reads as installed in the Downloads tab,
and its files stay deployed). Pure stdlib + Utils.* - no GUI toolkit.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def remove_mods(game, profile_dir: Path, mod_names: list[str], log_fn=None, *,
                staging_root: Path | None = None) -> None:
    """Fully remove *mod_names* for *game* / *profile_dir*:

      1. undeploy their files from the game dir (before deleting staging, so
         leftover hardlinks/copies aren't misclassified as runtime files),
      2. drop their plugins from plugins.txt + loadorder.txt,
      3. delete the staging folders,
      4. drop them from the Filegraph catalog and deployed-state table.

    Does NOT touch modlist.txt - the caller removes the rows from the model
    (which saves the modlist). Mirrors Tk _remove_mod.
    """
    log = log_fn or (lambda _m: None)
    if game is None or not mod_names:
        return
    try:
        staging_root = (Path(staging_root) if staging_root is not None
                        else game.get_effective_mod_staging_path())
    except Exception:
        return
    from Utils.filegraph.service import FileGraphService
    library = FileGraphService.open_library(game, profile_dir, log_fn=log)
    profile = library.open_profile(profile_dir)

    # 1. Undeploy deployed files first - but only when a deployment is
    #    actually active: after a restore the game folder holds the REAL
    #    game files, and a mod that shadows vanilla names (e.g. a patched
    #    FalloutNV.esm) would otherwise delete them. Ownership is verified per
    #    file against the committed deployed-state row and staged source.
    try:
        deploy_active = bool(game.get_deploy_active())
    except Exception:
        deploy_active = True
    if deploy_active:
        try:
            undeploy_catalog_mods(
                game, profile, Path(staging_root), mod_names, log_fn=log)
        except Exception as exc:
            log(f"undeploy during remove failed: {exc}")
    else:
        log("no deployment is active - skipping undeploy of removed mod(s).")

    # 2. Remove the mods' plugins from plugins.txt / loadorder.txt.
    try:
        _remove_plugins_for_mods(game, profile_dir, staging_root, mod_names, log)
    except Exception as exc:
        log(f"plugin cleanup during remove failed: {exc}")

    # 3. Delete staging folders. A symlinked mod dir (Profile Group link
    #    farm) is unlinked, never rmtree'd - rmtree on a dir symlink raises,
    #    and following it would delete the member profile's real files.
    for name in mod_names:
        folder = staging_root / name
        if folder.is_symlink():
            try:
                folder.unlink()
            except OSError as exc:
                log(f"could not remove staging link for '{name}': {exc}")
        elif folder.is_dir():
            try:
                shutil.rmtree(folder)
            except OSError as exc:
                log(f"could not delete staging folder for '{name}': {exc}")

    # 4. Drop targeted catalog/deployed-state rows.
    try:
        profile.forget_deployed_mods(mod_names)
        for name in mod_names:
            library.remove_mod(name)
    except Exception as exc:
        log(f"catalog cleanup during remove failed: {exc}")


def undeploy_catalog_mods(game, profile, staging_root: Path,
                          mod_names: list[str], log_fn=None) -> int:
    """Safely unlink committed deployment entries owned by *mod_names*.

    The source tree must still exist. Copies are accepted only when size and
    mtime match; links must resolve to the exact staged source.
    """
    from Utils.filegraph.deploy import absolute_destination
    log = log_fn or (lambda _message: None)
    remove_keys = {name.lower() for name in mod_names}
    removed = 0
    for entry in profile.deployed_entries():
        if entry.mod_key not in remove_keys:
            continue
        destination = absolute_destination(game, entry)
        if destination is None:
            continue
        source = staging_root / entry.mod_name / entry.source_display
        try:
            safe = False
            if destination.is_symlink():
                safe = destination.resolve() == source.resolve()
            elif destination.is_file() and source.is_file():
                dst_stat = destination.stat()
                src_stat = source.stat()
                safe = (dst_stat.st_ino == src_stat.st_ino or (
                    dst_stat.st_size == src_stat.st_size
                    and dst_stat.st_mtime_ns == src_stat.st_mtime_ns))
            if safe:
                destination.unlink()
                removed += 1
        except OSError as exc:
            log(f"could not undeploy {destination}: {exc}")
    if removed:
        log(f"undeployed {removed} file(s) owned by removed mod(s).")
    return removed


def _remove_plugins_for_mods(game, profile_dir: Path, staging_root: Path,
                             mod_names: list[str], log) -> None:
    """Drop the mods' plugin files from plugins.txt + loadorder.txt."""
    plugin_exts = {e.lower() for e in (getattr(game, "plugin_extensions", []) or [])}
    if not plugin_exts:
        return
    plugins_path = profile_dir / "plugins.txt"
    if not plugins_path.is_file():
        return
    to_remove: set[str] = set()
    for name in mod_names:
        folder = staging_root / name
        if folder.is_dir():
            for f in folder.iterdir():
                if f.is_file() and f.suffix.lower() in plugin_exts:
                    to_remove.add(f.name.lower())
            # Rule-routing games (Oblivion Remastered): nested plugins that
            # deploy to the top of the data dir were synced into plugins.txt
            # too - strip those names as well.
            try:
                from Utils.games.registry import routed_mod_plugin_names
                for n in routed_mod_plugin_names(game, folder):
                    to_remove.add(n.lower())
            except Exception:
                pass
    if not to_remove:
        return
    from Utils.plugins import (
        read_plugins, write_plugins, read_loadorder, write_loadorder, PluginEntry,
    )
    star = bool(getattr(game, "plugins_use_star_prefix", True))
    existing = read_plugins(plugins_path, star_prefix=star)
    new_entries = [e for e in existing if e.name.lower() not in to_remove]
    if len(new_entries) < len(existing):
        write_plugins(plugins_path, new_entries, star_prefix=star)
    loadorder_path = profile_dir / "loadorder.txt"
    loadorder = read_loadorder(loadorder_path)
    new_lo = [n for n in loadorder if n.lower() not in to_remove]
    if len(new_lo) < len(loadorder):
        write_loadorder(loadorder_path,
                        [PluginEntry(name=n, enabled=True) for n in new_lo])
