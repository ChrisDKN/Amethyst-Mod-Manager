"""Witcher 3 Script Merger inventory preservation.

Script Merger records every merge it creates in MergeInventory.xml next to
its own exe (Applications/ScriptMerger/).  That file is the merger's ONLY
record of existing merges - the merged files in the game folder are just
output, never rescanned.  On every launch the merger validates each entry
and offers to delete entries whose merged file or source-mod files are
missing from the deployed game folder, so running it while the source mods
are disabled wipes the records even though the manager preserves the merged
files themselves (the Merged_Mods staging mod).

This module pairs the inventory with the Merged_Mods staging mod:

- ``snapshot_inventory``: after a merger run (once ``game.restore()`` has
  rescued the merged files), record the app-dir inventory into the
  profile's Merged_Mods folder as ``.mm_merge_inventory.xml`` (excluded
  from deploy).  Merged, not copied verbatim: a run whose source mods were
  disabled wipes the app-dir records as collateral, so prior snapshot
  entries are kept as long as their merged output still exists in staging.
- ``restore_inventory``: before a merger launch, copy the profile's
  snapshot back into the app dir.  This also makes merges per-profile -
  the app dir is shared across profiles, the snapshot is not.
- ``missing_merge_sources``: parse the inventory and report merges whose
  source mods (or the merged mod itself) are not currently deployed, so
  the wizard can warn before the merger deletes those records.
"""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Games.base_game import BaseGame

MERGER_DIR = "ScriptMerger"
INVENTORY_NAME = "MergeInventory.xml"
SNAPSHOT_NAME = ".mm_merge_inventory.xml"


def app_inventory_path(game: "BaseGame") -> Path:
    """MergeInventory.xml next to WitcherScriptMerger.exe."""
    from Utils.bethesda.xedit import applications_dir
    return applications_dir(game, MERGER_DIR) / INVENTORY_NAME


def snapshot_path(game: "BaseGame") -> Path | None:
    """Snapshot location inside the Merged_Mods staging mod, or None if the
    game does not preserve merged files (only Witcher 3 does)."""
    getter = getattr(game, "merged_mods_staging_dir", None)
    if getter is None:
        return None
    return getter() / SNAPSHOT_NAME


def _merge_key(merge: ET.Element) -> tuple[str, str]:
    return ((merge.findtext("RelativePath") or "").strip().lower(),
            (merge.findtext("BundleName") or "").strip().lower())


def _staged_merged_file(merged_mods_dir: Path, merge: ET.Element) -> Path:
    """Where this merge's output sits inside the staged Merged_Mods mod.

    Mirrors the merger's Merge.GetMergedFile()/GetMergedBundle() layout
    (scripts under content/scripts/, XML at the mod root, bundle text as a
    packed bundle under content/), rebased from the game's mods dir onto
    the staging folder that game.restore() rescues into.
    """
    merged_name = (merge.findtext("MergedModName") or "").strip()
    base = merged_mods_dir / "mods" / merged_name
    bundle = (merge.findtext("BundleName") or "").strip()
    if bundle:
        return base / "content" / bundle.replace("\\", "/")
    rel = (merge.findtext("RelativePath") or "").strip().replace("\\", "/")
    if rel.lower().endswith(".ws"):
        return base / "content" / "scripts" / rel
    return base / rel


def collateral_keys(game: "BaseGame") -> set[tuple[str, str]]:
    """Merge keys whose source mods are not currently deployed.

    Called BEFORE launching the merger (on the restored inventory): these
    are the merges the merger will drop as collateral because it can't see
    their sources, so ``snapshot_inventory`` must keep them rather than
    treat the drop as a user deletion.
    """
    inv = app_inventory_path(game)
    if not inv.is_file():
        return set()
    try:
        root = ET.parse(inv).getroot()
    except (ET.ParseError, OSError):
        return set()
    try:
        from Utils.vfs import effective_tool_game_root
        game_path = effective_tool_game_root(game)
    except RuntimeError:
        return set()
    deployed: set[str] = set()
    for dirname in ("mods", "Mods"):
        d = game_path / dirname
        if d.is_dir():
            deployed.update(e.name.lower() for e in d.iterdir() if e.is_dir())

    keys: set[tuple[str, str]] = set()
    for merge in root.findall("Merge"):
        needed = [(m.text or "").strip() for m in merge.findall("IncludedMod")]
        needed.append((merge.findtext("MergedModName") or "").strip())
        if any(n and n.lower() not in deployed for n in needed):
            keys.add(_merge_key(merge))
    return keys


def snapshot_inventory(game: "BaseGame", log_fn=None,
                       keep_keys: "set[tuple[str, str]] | None" = None) -> bool:
    """Record the app-dir inventory into the Merged_Mods staging mod.

    Invariant: the snapshot holds exactly the records whose merged output
    exists in the staged Merged_Mods.  Records come from the app-dir
    inventory, plus prior snapshot entries the merger dropped whose source
    mods were not deployed this run (*keep_keys*, from
    :func:`collateral_keys` captured before launch - the merger drops these
    as collateral even though the merged files survive in staging).  A
    prior record the merger dropped that is NOT in *keep_keys* was deleted
    by the user; its orphaned staged file is removed so it stops deploying.
    Records without staged output are dropped either way (covers crashed
    runs: files deleted, records unsaved).
    """
    _log = log_fn or (lambda _: None)
    src = app_inventory_path(game)
    dest = snapshot_path(game)
    if dest is None or not src.is_file():
        return False
    try:
        tree = ET.parse(src)
    except (ET.ParseError, OSError):
        return False
    root = tree.getroot()
    keep_keys = keep_keys or set()

    # Bring over prior-snapshot records the merger no longer has, but only
    # collateral-wipe casualties (their source mods were not deployed this
    # run, so *keep_keys* lists them).  A prior record that IS absent from
    # keep_keys was deleted by the user in the merger - drop it, and remove
    # its now-orphaned merged file from staging so it stops deploying.
    # Dedup by path+bundle key; the app-dir entry wins (newer hashes).
    seen = {_merge_key(m) for m in root.findall("Merge")}
    pruned_files = 0
    if dest.is_file():
        try:
            prev = ET.parse(dest).getroot()
        except (ET.ParseError, OSError):
            prev = None
        if prev is not None:
            for merge in prev.findall("Merge"):
                key = _merge_key(merge)
                if key in seen:
                    continue
                if key in keep_keys:
                    root.append(merge)            # collateral - keep
                else:
                    staged = _staged_merged_file(dest.parent, merge)
                    if staged.is_file():
                        staged.unlink()           # user deleted this merge
                        pruned_files += 1

    # Enforce the invariant: no record without its staged merged output
    # (covers crashed-run half-states - files deleted, records unsaved).
    dropped = 0
    for merge in list(root.findall("Merge")):
        if not _staged_merged_file(dest.parent, merge).is_file():
            root.remove(merge)
            dropped += 1

    dest.parent.mkdir(parents=True, exist_ok=True)
    tree.write(dest, encoding="utf-8", xml_declaration=True)
    kept = len(root.findall("Merge"))
    _log(f"snapshotted {INVENTORY_NAME} to {dest} ({kept} merge record(s); "
         f"{dropped} without staged output dropped, "
         f"{pruned_files} deleted-merge file(s) pruned).")
    return True


def restore_inventory(game: "BaseGame", log_fn=None) -> bool:
    """Copy the profile's snapshot back into the Script Merger app dir.

    A missing snapshot is a no-op (first run migrates: the app-dir
    inventory is left alone and snapshotted after the run).
    """
    _log = log_fn or (lambda _: None)
    src = snapshot_path(game)
    if src is None or not src.is_file():
        return False
    dest = app_inventory_path(game)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    _log(f"restored {INVENTORY_NAME} snapshot from {src}.")
    return True


_EMPTY_INVENTORY = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<MergeInventory xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:xsd="http://www.w3.org/2001/XMLSchema" />'
)


def purge_merges(game: "BaseGame", log_fn=None) -> None:
    """Remove all merge state so Script Merger starts from a clean slate.

    Deletes the staged Merged_Mods mod (snapshot included), any deployed
    *_MergedFiles folders in the game dir, and empties the app-dir
    inventory.  Used when existing merges reference mods that are no
    longer deployed: letting the merger clean that state up itself makes
    it delete the merged files one by one in the game folder, which
    crashes it under Wine (unhandled IOException in DeleteEmptyDirs) and
    leaves half-deleted merges behind.
    """
    _log = log_fn or (lambda _: None)

    staged = snapshot_path(game)
    if staged is not None and staged.parent.is_dir():
        shutil.rmtree(staged.parent, ignore_errors=True)
        _log("removed staged Merged_Mods folder.")

    try:
        from Utils.vfs import effective_tool_game_root
        game_path = effective_tool_game_root(game)
    except RuntimeError:
        game_path = None
    if game_path is not None:
        for dirname in ("mods", "Mods"):
            mods_dir = game_path / dirname
            if not mods_dir.is_dir():
                continue
            for folder in mods_dir.iterdir():
                if folder.is_dir() and "_mergedfiles" in folder.name.lower():
                    shutil.rmtree(folder, ignore_errors=True)
                    _log(f"removed deployed {folder.name} folder.")

    inv = app_inventory_path(game)
    if inv.parent.is_dir():
        inv.write_text(_EMPTY_INVENTORY, encoding="utf-8")
        _log(f"reset {INVENTORY_NAME}.")


def missing_merge_sources(game: "BaseGame") -> list[tuple[str, list[str]]]:
    """Merges in the app-dir inventory whose mods are not deployed.

    Returns ``[(merge relative path, [missing mod folder names])]`` - each
    entry is a merge Script Merger would offer to delete on next launch.
    Mod names are checked as folder names under the game's mods dir,
    case-insensitively (the merger runs under Wine and matches either way).
    """
    inv = app_inventory_path(game)
    if not inv.is_file():
        return []
    try:
        root = ET.parse(inv).getroot()
    except (ET.ParseError, OSError):
        return []

    try:
        from Utils.vfs import effective_tool_game_root
        game_path = effective_tool_game_root(game)
    except RuntimeError:
        return []
    deployed: set[str] = set()
    for dirname in ("mods", "Mods"):
        d = game_path / dirname
        if d.is_dir():
            deployed.update(e.name.lower() for e in d.iterdir() if e.is_dir())

    out: list[tuple[str, list[str]]] = []
    for merge in root.findall("Merge"):
        rel = (merge.findtext("RelativePath") or "").strip()
        needed = [(m.text or "").strip() for m in merge.findall("IncludedMod")]
        needed.append((merge.findtext("MergedModName") or "").strip())
        missing = [n for n in needed if n and n.lower() not in deployed]
        if missing:
            out.append((rel, missing))
    return out
