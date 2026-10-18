from __future__ import annotations

import copy
import shutil
from datetime import datetime, timezone

from .jobs import JobError


def validate_name(name):
    from Utils.filegraph.constants import OVERWRITE_NAME, ROOT_FOLDER_NAME
    from Utils.mods.names import sanitize_mod_folder_name

    suggested = sanitize_mod_folder_name(name)
    reserved = {OVERWRITE_NAME.casefold(), ROOT_FOLDER_NAME.casefold(), "__ungrouped_boundary__"}
    if (name != suggested or name.casefold().endswith("_separator")
            or name.casefold().startswith("@group:") or name.casefold() in reserved):
        raise JobError("invalid_mod_name", "Choose a safe, unreserved mod folder name.",
                       {"suggested": suggested})


def _slot(entries, groups, position):
    from Utils.mods.groups import owners, with_group_headers

    if position == "top":
        return 0
    if position == "bottom":
        return len(entries)
    placement, anchor = next(iter(position.items()))
    matches = [entry for entry in with_group_headers(entries, groups) if entry.name == anchor]
    if len(matches) != 1:
        raise JobError("mod_anchor_not_found" if not matches else "ambiguous_mod_anchor",
                       "Choose a unique mod, separator, or mod group anchor.",
                       {"anchor": anchor, "matches": len(matches)})
    leader = owners(groups).get(anchor)
    names = {leader, *groups[leader]["members"]} if leader else {anchor}
    indices = [index for index, entry in enumerate(entries) if entry.name in names]
    return min(indices) if placement == "before" else max(indices) + 1


def _refresh(game, profile, name, log, progress, *, materialize=False):
    from Utils.filegraph.service import FileGraphService
    from Utils.mods.state import _reconcile_filegraph

    selected = copy.copy(game)
    selected.set_active_profile_dir(profile)
    selected.load_paths(read_only=True)
    if materialize:
        from Utils.profiles.groups import materialize_group
        materialize_group(selected, profile, log_fn=log)
    library = FileGraphService.open_library(selected, profile, log_fn=log)
    library.refresh(profile, mod_names=[name])
    reconciled, generation = _reconcile_filegraph(
        selected, profile, kind="full", mod_names=[name], log=log, progress=progress)
    return {"profile": profile.name, "reconciled": reconciled, "generation": generation}


def create_runner(game, profile, name, revision, position, enabled, root_folder):
    def run(log, progress):
        from Nexus.nexus_meta import NexusModMeta, read_meta, write_meta
        from Utils.atomic_write import write_atomic
        from Utils.deployment.locking import coordinated_game_mutation
        from Utils.mods.groups import group_edit_state, group_state_revision, normalize_groups
        from Utils.mods.modlist import ModEntry, read_modlist, write_modlist
        from Utils.mods.separators import modlist_revision
        from .mod_properties import _profiles, _raw, _staging

        validate_name(name)
        with coordinated_game_mutation(game, "mods.create"):
            selected = copy.copy(game)
            selected.set_active_profile_dir(profile)
            selected.load_paths(read_only=True)
            progress(0, 3, "Creating empty mod")
            with _profiles(selected, profile) as (records, errors):
                if errors:
                    raise JobError("mod_creation_routing_unavailable", "Could not inspect profile routing.",
                                   {"profiles": errors})
                record = records[profile.name]
                staging = _staging(selected, record)
                if not staging.is_absolute():
                    raise JobError("unsafe_mod_path", "The staging path must be absolute.")
                original = normalize_groups(record["state"].get("mod_groups"))
                current = group_state_revision(record["raw"], original)
                if current != revision:
                    raise JobError("stale_mod_groups", "Mod order or grouping changed; read the mods again.",
                                   {"revision": current})
                entries = record["entries"]
                groups = normalize_groups(original, entries)
                slot = _slot(entries, groups, position)
                related = [item for item in records.values()
                           if _staging(selected, item).resolve() == staging.resolve()]
                for item in related:
                    names = {value.casefold() for entry in item["entries"]
                             for value in (entry.name, entry.display_name)}
                    names.update(value["title"].casefold() for value in normalize_groups(
                        item["state"].get("mod_groups"), item["entries"]).values() if "title" in value)
                    if name.casefold() in names:
                        raise JobError("mod_already_exists", "A mod or separator already uses this name.",
                                       {"mod": name, "profile": item["path"].name})
                staging.mkdir(parents=True, exist_ok=True)
                if any(path.name.casefold() == name.casefold() for path in staging.iterdir()):
                    raise JobError("mod_already_exists", "A staged folder already uses this name.", {"mod": name})
                destination = staging / name
                path = profile / "modlist.txt"
                before = _raw(path)
                updated = list(entries)
                updated.insert(slot, ModEntry(name, enabled, False))
                created, attempted = None, False
                try:
                    destination.mkdir(exist_ok=False)
                    created = ()
                    stat = destination.stat()
                    created = (stat.st_dev, stat.st_ino)
                    installed = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                    write_meta(destination / "meta.ini", NexusModMeta(
                        mod_name=name, version="1.0", root_folder=root_folder,
                        installed=installed))
                    metadata = read_meta(destination / "meta.ini")
                    if (metadata.version != "1.0" or metadata.root_folder != root_folder
                            or metadata.installed != installed):
                        raise OSError("Could not verify the new mod metadata.")
                    if _raw(path) != before:
                        raise JobError("stale_mod_groups", "Mod order changed while creating the mod.")
                    attempted = True
                    write_modlist(path, updated)
                    if read_modlist(path) != updated:
                        raise OSError("Could not verify the new mod-list entry.")
                except Exception as exc:
                    failures = []
                    if attempted:
                        try:
                            _raw(path)
                            if before is None:
                                path.unlink(missing_ok=True)
                            else:
                                write_atomic(path, before)
                            if _raw(path) != before:
                                raise OSError("Could not verify restored mod order.")
                        except Exception as recovery:
                            failures.append({"stage": "modlist", "reason": str(recovery)})
                    if created is not None:
                        try:
                            stat = destination.lstat()
                            if destination.is_symlink() or (stat.st_dev, stat.st_ino) != created:
                                raise OSError("The new mod folder changed during recovery.")
                            shutil.rmtree(destination)
                        except Exception as recovery:
                            failures.append({"stage": "folder", "reason": str(recovery)})
                    if failures:
                        raise JobError("mod_creation_rollback_failed", "Empty-mod creation failed and recovery was incomplete.",
                                       {"mod": name, "path": str(destination), "state_persisted": True,
                                        "failures": failures}) from exc
                    if isinstance(exc, JobError):
                        exc.details = {**exc.details, "rolled_back": True}
                        raise
                    raise JobError("mod_creation_failed", "Could not create the empty mod; previous state was restored.",
                                   {"mod": name, "rolled_back": True, "reason": str(exc)}) from exc
                targets = [profile] + [item["path"] for item in records.values()
                                      if item["path"] != profile and item["settings"].get("is_group")
                                      and profile.name in item["settings"].get("group_members", [])]
                result = {"profile": profile.name, "mod": {"name": name, "path": str(destination),
                          "enabled": enabled, "locked": False, "version": "1.0", "root_folder": root_folder},
                          "scope": "profile" if record["settings"].get("profile_specific_mods") else "shared",
                          "affected_profiles": [target.name for target in targets]}
                is_group = record["settings"].get("is_group", False)
            progress(1, 3, "Indexing empty mod")
            synced, failures = [], []
            for target in targets:
                try:
                    synced.append(_refresh(selected, target, name, log, progress,
                                           materialize=target != profile or is_group))
                except Exception as exc:
                    failures.append({"profile": target.name, "reason": str(exc)})
            result["synced_profiles"] = synced
            with group_edit_state(path) as (raw, _state, _entries, current_groups):
                result["revision"] = modlist_revision(raw)
                result["group_revision"] = group_state_revision(raw, current_groups)
            if failures:
                raise JobError("mod_creation_sync_failed", "The mod was created, but some profiles could not refresh.",
                               {"state_persisted": True, "result": result, "failures": failures})
            progress(3, 3, "Empty mod created")
            return result
    return run
