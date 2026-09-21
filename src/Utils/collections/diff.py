"""Collection revision candidates; deletion requires the installed ownership gate."""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class CollectionDiff:
    to_remove: list[str] = field(default_factory=list)          # mod folder names
    to_update_old: list[str] = field(default_factory=list)      # mod folder names (old versions)
    to_update_new_fids: list[int] = field(default_factory=list) # new file_ids for to_update
    to_install_fids: list[int] = field(default_factory=list)    # new file_ids needing download
    orphans: list[str] = field(default_factory=list)            # mod folder names

    @property
    def removals(self) -> list[str]:
        """Candidates requiring ownership checks before removal."""
        # Preserve order while de-duplicating.
        seen: set[str] = set()
        out: list[str] = []
        for name in (*self.to_remove, *self.to_update_old, *self.orphans):
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out

    @property
    def download_fids(self) -> list[int]:
        """Every file_id that needs to be downloaded: new installs + updates."""
        seen: set[int] = set()
        out: list[int] = []
        for fid in (*self.to_install_fids, *self.to_update_new_fids):
            if fid <= 0 or fid in seen:
                continue
            seen.add(fid)
            out.append(fid)
        return out

    @property
    def is_empty(self) -> bool:
        return (
            not self.to_remove
            and not self.to_update_old
            and not self.to_install_fids
            and not self.orphans
        )


def _old_manifest_file_ids(old_manifest: dict) -> set[int]:
    """Extract the set of file_ids from a cached collection.json manifest."""
    out: set[int] = set()
    mods = old_manifest.get("mods") if isinstance(old_manifest, dict) else None
    if not isinstance(mods, list):
        return out
    for entry in mods:
        if not isinstance(entry, dict):
            continue
        src = entry.get("source")
        if not isinstance(src, dict):
            continue
        fid = src.get("fileId")
        try:
            fid_int = int(fid)
        except (TypeError, ValueError):
            continue
        if fid_int > 0:
            out.add(fid_int)
    return out


def _read_installed_mods(
    staging_path: Path,
    installed_names_lower: set[str],
) -> list[tuple[str, int, int, int, str]]:
    """Return installed folder identity, including the authored collection fileId.

    The authored id differs from ``file_id`` when an update policy resolved a
    replacement file.

    Return tuples are ``(folder, mod_id, file_id, source_file_id, collection)`` for every
    folder whose lowercased name is in *installed_names_lower*. Missing meta.ini
    is treated as zero/empty identity."""
    out: list[tuple[str, int, int, int, str]] = []
    if not staging_path.is_dir():
        return out
    for mod_dir in staging_path.iterdir():
        if not mod_dir.is_dir():
            continue
        if mod_dir.name.lower() not in installed_names_lower:
            continue
        mod_id = 0
        file_id = 0
        source_file_id = 0
        from_collection = ""
        meta_ini = mod_dir / "meta.ini"
        if meta_ini.is_file():
            cp = configparser.ConfigParser()
            try:
                cp.read(str(meta_ini), encoding="utf-8")
                if cp.has_section("General"):
                    try:
                        mod_id = int(cp.get("General", "modid", fallback="0") or "0")
                    except ValueError:
                        pass
                    try:
                        file_id = int(cp.get("General", "fileid", fallback="0") or "0")
                    except ValueError:
                        pass
                    try:
                        source_file_id = int(cp.get(
                            "General", "collectionSourceFileId", fallback="0") or "0")
                    except ValueError:
                        pass
                    from_collection = cp.get(
                        "General", "fromCollection", fallback=""
                    ).strip()
            except Exception:
                pass
        out.append((mod_dir.name, mod_id, file_id, source_file_id, from_collection))
    return out


def diff_collection(
    *,
    old_manifest: dict,
    new_mods: Iterable,          # iterable of NexusCollectionMod
    staging_path: Path,
    installed_names_lower: set[str],
    collection_slug: str,
    collection_install_id: str = "",
    new_manifest: dict | None = None,
) -> CollectionDiff:
    """Compute candidates; callers must apply the collection ownership removal gate.

    ``old_manifest`` is the cached ``<profile>/collection.json`` dict (may be
    empty if unavailable - then the fallback classifier can't rescue legacy
    un-tagged mods).

    ``new_mods`` is the new revision's mod list from ``get_collection_detail``.

    ``staging_path`` is where per-mod folders + meta.ini live.

    ``installed_names_lower`` is the set of lowercased mod folder names that are
    currently listed in the profile's modlist.txt.
    """
    old_fids = _old_manifest_file_ids(old_manifest)
    new_fids_to_mod: dict[int, object] = {}
    for m in new_mods:
        fid = getattr(m, "file_id", 0) or 0
        if fid > 0:
            new_fids_to_mod[fid] = m
    new_fids = set(new_fids_to_mod.keys())
    target_bundles = {
        str(source.get("fileExpression") or "").casefold()
        for entry in (new_manifest or {}).get("mods", [])
        if isinstance(entry, dict)
        for source in (entry.get("source") or {},)
        if source.get("type", "").lower() == "bundle"
        and source.get("fileExpression")
    }

    installed = _read_installed_mods(staging_path, installed_names_lower)

    diff = CollectionDiff()

    installed_by_mod_id: dict[int, tuple[str, int]] = {}
    collection_owned_folders: set[str] = set()

    for folder, mod_id, file_id, source_file_id, origin in installed:
        member_file_id = source_file_id or file_id
        from Utils.collections.ownership import read_ownership
        memberships = read_ownership(staging_path / folder / "meta.ini").get("installations", [])
        is_owned_by_slug = (collection_install_id in memberships if collection_install_id
                            else bool(origin) and origin == collection_slug)
        fallback_owned = (
            not collection_install_id and not origin
            and member_file_id > 0 and member_file_id in old_fids
        )
        if not (is_owned_by_slug or fallback_owned):
            continue
        collection_owned_folders.add(folder)

        if mod_id <= 0 or file_id <= 0:
            if target_bundles:
                from Nexus.nexus_meta import read_meta
                meta = read_meta(staging_path / folder / "meta.ini")
                if str(meta.installation_file or "").casefold() in target_bundles:
                    continue
            diff.orphans.append(folder)
            continue

        if mod_id > 0:
            installed_by_mod_id[mod_id] = (folder, file_id)

    for folder, mod_id, file_id, source_file_id, origin in installed:
        if folder not in collection_owned_folders:
            continue
        if mod_id <= 0 or file_id <= 0:
            continue
        target_mod = new_fids_to_mod.get(file_id)
        if target_mod is not None and getattr(target_mod, "mod_id", 0) in (0, mod_id):
            continue
        matched_new_fid = None
        for nfid, nmod in new_fids_to_mod.items():
            if getattr(nmod, "mod_id", 0) == mod_id:
                matched_new_fid = nfid
                break
        if matched_new_fid is not None:
            diff.to_update_old.append(folder)
            diff.to_update_new_fids.append(matched_new_fid)
        else:
            diff.to_remove.append(folder)

    installed_fids = {
        existing for _, _, fid, source_fid, _ in installed
        for existing in (fid, source_fid) if existing > 0}
    update_new_set = set(diff.to_update_new_fids)
    for fid in new_fids:
        if fid in installed_fids:
            continue
        if fid in update_new_set:
            continue
        diff.to_install_fids.append(fid)

    return diff
