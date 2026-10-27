from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from Utils.mods.state import ModStateError, _reconcile_filegraph


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _public(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def plan_mod_rename(game: Any, profile_dir: Path, mod: str,
                    new_name: str) -> dict[str, Any]:
    from Utils.collections.ownership import read_ownership
    from Utils.mods.modlist import read_modlist
    from Utils.mods.names import sanitize_mod_folder_name
    from Utils.profiles.groups import (
        _members_providing, get_members, is_group, list_groups, owner_of,
    )
    from Utils.profiles.state import (
        profile_uses_specific_mods, read_profile_settings,
        read_profile_state,
    )

    profile_dir = Path(profile_dir)
    profiles_root = Path(game.get_profile_root()) / "profiles"
    specific = profile_uses_specific_mods(profile_dir)
    staging = profile_dir / "mods" if specific else Path(game.get_mod_staging_path())
    folder = staging / mod
    destination = staging / new_name
    blockers: list[dict[str, Any]] = []

    def block(name: str, message: str, **details: Any) -> None:
        blockers.append({"name": name, "message": message, "details": details})

    if sanitize_mod_folder_name(new_name) != new_name or new_name.endswith("_separator"):
        block("invalid_mod_name", "The target is not a safe mod folder name.",
              suggested=sanitize_mod_folder_name(new_name))
    if new_name == mod:
        block("unchanged_mod_name", "The mod already has this name.")
    if profile_dir.is_symlink() or (specific and staging.is_symlink()):
        block("unsafe_profile_path", "The profile or private staging root is a symlink.")
    if folder.is_symlink() and is_group(profile_dir):
        owner = owner_of(profile_dir, mod)
        block("group_member_mod",
              "Rename a member-owned mod in its source profile.",
              owner_profile=owner[0] if owner else None,
              owner_mod=owner[1] if owner else None)
    elif folder.is_symlink() or not folder.is_dir():
        block("mod_folder_missing", "The source must be a real staged mod folder.",
              path=str(folder))
    try:
        siblings = list(staging.iterdir()) if staging.is_dir() else []
    except OSError as exc:
        raise ModStateError(
            "resource_unavailable", "Could not list the staging folder.",
            {"path": str(staging), "reason": str(exc)},
        ) from exc
    collisions = [path.name for path in siblings
                  if path.name != mod and path.name.casefold() == new_name.casefold()]
    if collisions or (destination.exists() and destination != folder
                      and destination.resolve() != folder.resolve()):
        block("mod_name_exists", "A staged folder already uses the target name.",
              collisions=collisions)
    try:
        if game.get_deploy_active():
            block("game_deployed", "Restore the game before renaming a staged mod.")
    except Exception as exc:
        block("deployment_state_unavailable",
              "The deployment state could not be read.", reason=str(exc))
    if folder.is_dir() and not folder.is_symlink():
        ownership = read_ownership(folder / "meta.ini")
        if ownership.get("installations"):
            block("collection_owned_mod",
                  "The mod belongs to an installed collection.",
                  installations=ownership["installations"])

    profile_dirs = sorted(
        (path for path in profiles_root.iterdir() if path.is_dir()),
        key=lambda path: path.name.casefold(),
    ) if profiles_root.is_dir() else []
    if not specific:
        shared_root = staging.resolve()
        for path in profile_dirs:
            if (not is_group(path) and profile_uses_specific_mods(path)
                    and (path / "mods").resolve() == shared_root):
                block("staging_alias",
                      "A private profile points at the shared staging folder.",
                      profile=path.name)
    affected = ([profile_dir] if specific else [
        path for path in profile_dirs
        if not is_group(path) and not profile_uses_specific_mods(path)
        and any(entry.name == mod and not entry.is_separator
                for entry in read_modlist(path / "modlist.txt"))
    ])
    if profile_dir not in affected:
        affected.append(profile_dir)
    affected.sort(key=lambda path: path.name.casefold())
    for path in affected:
        if path.is_symlink():
            block("unsafe_profile_path", "An affected profile is a symlink.",
                  profile=path.name)
        for filename in ("modlist.txt", "profile_state.json"):
            if (path / filename).is_symlink():
                block("unsafe_profile_path", "An affected profile file is a symlink.",
                      profile=path.name, filename=filename)
        entries = read_modlist(path / "modlist.txt")
        matches = [entry for entry in entries
                   if entry.name == mod and not entry.is_separator]
        if len(matches) != 1:
            block("ambiguous_mod" if matches else "mod_not_found",
                  "The source mod must appear exactly once in each affected profile.",
                  profile=path.name, matches=len(matches))
        elif matches[0].locked:
            block("mod_locked", "A locked mod cannot be renamed.",
                  profile=path.name)
        conflicts = [entry.name for entry in entries
                     if entry.name != mod
                     and entry.name.casefold() == new_name.casefold()]
        if conflicts:
            block("mod_name_exists", "The target name exists in a mod list.",
                  profile=path.name, collisions=conflicts)
        state = read_profile_state(path)
        if "plugin_metadata" in state:
            from amethyst_plugins.metadata import validate_tree
            for namespace in validate_tree(state["plugin_metadata"]).values():
                if any(name != mod and name.casefold() == new_name.casefold()
                       for name in namespace.get("mods", {})):
                    block("mod_state_name_exists", "The target already has saved plugin metadata.",
                          profile=path.name, state_key="plugin_metadata")
        settings = read_profile_settings(path, state)
        if settings.get("wabbajack_profile"):
            block("managed_profile", "A Wabbajack profile owns this mod list.",
                  profile=path.name)
        for key in ("mod_strip_prefixes", "disabled_plugins",
                    "excluded_mod_files", "root_mod_files", "mod_notes"):
            values = state.get(key)
            if isinstance(values, dict) and any(
                    name != mod and isinstance(name, str)
                    and name.casefold() == new_name.casefold()
                    for name in values):
                block("mod_state_name_exists",
                      "The target already has saved mod state.",
                      profile=path.name, state_key=key)
        mod_groups = state.get("mod_groups")
        if isinstance(mod_groups, dict) and (
                any(isinstance(name, str)
                    and name.casefold() == new_name.casefold()
                    for name in mod_groups) or any(
                    isinstance(member, str)
                    and member.casefold() == new_name.casefold()
                    for value in mod_groups.values()
                    if isinstance(value, dict)
                    and isinstance(value.get("members"), list)
                    for member in value["members"])):
            block("mod_state_name_exists",
                  "The target already has saved mod-group state.",
                  profile=path.name, state_key="mod_groups")

    groups: list[dict[str, Any]] = []
    if specific and not is_group(profile_dir):
        for group_name in list_groups(game):
            group_dir = profiles_root / group_name
            members = get_members(group_dir)
            if profile_dir.name not in members:
                continue
            if group_dir.is_symlink():
                block("unsafe_profile_path", "An affected group is a symlink.",
                      group=group_name)
            for filename in ("modlist.txt", "profile_state.json"):
                if (group_dir / filename).is_symlink():
                    block("unsafe_profile_path", "An affected group file is a symlink.",
                          group=group_name, filename=filename)
            group_entries = read_modlist(group_dir / "modlist.txt")
            owned = [entry.name for entry in group_entries if not entry.is_separator
                     and owner_of(group_dir, entry.name) == (profile_dir.name, mod)]
            if len(owned) > 1:
                block("ambiguous_group_mod",
                      "Several group entries link to the source mod.",
                      group=group_name, entries=owned)
            conflicts = [entry.name for entry in group_entries
                         if entry.name.casefold() == new_name.casefold()
                         and entry.name not in owned]
            if conflicts:
                block("group_name_exists",
                      "The target name already exists in a Profile Group.",
                      group=group_name, collisions=conflicts)
            for member in members:
                if member == profile_dir.name:
                    continue
                other_entries = read_modlist(
                    profiles_root / member / "modlist.txt")
                if any(entry.name.casefold() == new_name.casefold()
                       and not entry.is_separator for entry in other_entries):
                    block("group_member_name_exists",
                          "Another group member already uses the target name.",
                          group=group_name, member=member)
            preserve = [name for name in owned
                        if set(_members_providing(group_dir, name))
                        == {profile_dir.name}]
            groups.append({"name": group_name, "path": group_dir,
                           "preserve": preserve})

    try:
        stat = folder.stat() if folder.is_dir() else None
        fingerprint = {
            "profile": profile_dir.name, "mod": mod, "new_name": new_name,
            "specific": specific, "blockers": blockers,
            "folder": ([(stat.st_dev), stat.st_ino, stat.st_mtime_ns]
                       if stat is not None else None),
            "profiles": [
                [path.name, _digest(_bytes(path / "modlist.txt") or b""),
                 _digest(_bytes(path / "profile_state.json") or b"")]
                for path in affected
            ],
            "groups": [
                [group["name"], group["preserve"],
                 _digest(_bytes(group["path"] / "modlist.txt") or b""),
                 _digest(_bytes(group["path"] / "profile_state.json") or b"")]
                for group in groups
            ],
        }
    except OSError as exc:
        raise ModStateError(
            "resource_unavailable", "Could not inspect rename inputs.",
            {"reason": str(exc)},
        ) from exc
    revision = _digest(json.dumps(fingerprint, sort_keys=True,
                                   ensure_ascii=True).encode("ascii"))
    return {
        "profile": profile_dir.name,
        "mod": mod,
        "new_name": new_name,
        "scope": "profile" if specific else "shared_staging",
        "affected_profiles": [path.name for path in affected],
        "affected_groups": [group["name"] for group in groups],
        "can_rename": not blockers,
        "blockers": blockers,
        "revision": revision,
        "_affected": affected,
        "_groups": groups,
        "_staging": staging,
    }


def rename_profile_mod(
    game: Any, profile_dir: Path, mod: str, new_name: str,
    plan_revision: str, *,
    log_fn: Callable[[str], None] | None = None,
    progress_fn: Callable[[int, int, str | None], None] | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.atomic_write import write_atomic
    from Utils.mods.modlist import modlist_lock, read_modlist, write_modlist
    from Utils.mods.rename import migrate_mod_state_strict
    from Utils.operations import acquire_game_operation
    from Utils.profiles.groups import (
        _mod_identity_and_version, _read_identity_map,
        _write_identity_map, materialize_group,
    )

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    with acquire_game_operation(
        game, "mods.rename", owner=operation_owner or "mods.rename",
    ):
        plan = plan_mod_rename(game, profile_dir, mod, new_name)
        if plan["revision"] != plan_revision:
            raise ModStateError(
                "stale_mod_rename_plan", "The rename inputs changed; plan it again.",
                {"plan": _public(plan)},
            )
        if not plan["can_rename"]:
            raise ModStateError(
                "mod_rename_blocked", "The mod cannot be renamed safely.",
                {"blockers": plan["blockers"]},
            )

        affected = plan["_affected"]
        from amethyst_plugins.guards import validate_operation
        for target_profile in affected:
            validate_operation(game, target_profile, "mods.rename", mods=[mod], new_name=new_name)
        groups = plan["_groups"]
        staging = plan["_staging"]
        old_folder = staging / mod
        new_folder = staging / new_name
        paths = [path / filename for path in affected
                 for filename in ("modlist.txt", "profile_state.json")]
        snapshots = {path: _bytes(path) for path in paths}
        progress(0, 4, "Renaming staged mod")
        folder_renamed = False
        try:
            old_folder.rename(new_folder)
            folder_renamed = True
            for path in affected:
                modlist_path = path / "modlist.txt"
                with modlist_lock(modlist_path):
                    entries = read_modlist(modlist_path)
                    next(entry for entry in entries
                         if entry.name == mod and not entry.is_separator).name = new_name
                    write_modlist(modlist_path, entries)
                migrate_mod_state_strict(path, mod, new_name)
        except Exception as exc:
            rollback_errors: list[str] = []
            for path, original in snapshots.items():
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        write_atomic(path, original)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{path}: {rollback_exc}")
            try:
                if folder_renamed and new_folder.exists():
                    new_folder.rename(old_folder)
            except OSError as rollback_exc:
                rollback_errors.append(f"{new_folder}: {rollback_exc}")
            raise ModStateError(
                "mod_rename_failed",
                "The rename failed; its saved profile files were restored."
                if not rollback_errors else
                "The rename failed and rollback was incomplete.",
                {"reason": str(exc), "rollback_errors": rollback_errors},
            ) from exc

        progress(1, 4, "Refreshing Filegraph catalogs")
        sync_errors: list[str] = []
        original_profile = getattr(game, "_active_profile_dir", None)
        try:
            for path in affected:
                try:
                    from Utils.filegraph.service import FileGraphService
                    game.set_active_profile_dir(path)
                    game.load_paths()
                    library = FileGraphService.open_library(game, path, log_fn=log)
                    library.rename_mod(mod, new_name)
                    session = library.open_profile(path)
                    library.replace_mod_manifest(
                        session.adapter.build_manifest(new_name))
                    _reconcile_filegraph(
                        game, path, kind="rename", mod_names=[mod, new_name],
                        log=log, progress=progress,
                    )
                except Exception as exc:
                    sync_errors.append(f"{path.name}: {exc}")
            progress(2, 4, "Updating Profile Groups")
            for group in groups:
                try:
                    group_dir = group["path"]
                    game.set_active_profile_dir(group_dir)
                    game.load_paths()
                    if group["preserve"]:
                        identity = _mod_identity_and_version(staging, new_name)[0]
                        mapping = _read_identity_map(group_dir)
                        for entry_name in group["preserve"]:
                            mapping[entry_name] = identity
                        _write_identity_map(group_dir, mapping)
                    materialize_group(game, group_dir, log_fn=log)
                except Exception as exc:
                    sync_errors.append(f"group {group['name']}: {exc}")
        finally:
            try:
                game.set_active_profile_dir(original_profile)
                game.load_paths()
            except Exception as exc:
                sync_errors.append(f"active profile: {exc}")
        progress(3, 4, "Finishing mod rename")
        if sync_errors:
            raise ModStateError(
                "mod_rename_sync_failed",
                "The mod was renamed, but a derived index or group did not refresh.",
                {"renamed": True, "mod": mod, "new_name": new_name,
                 "affected_profiles": plan["affected_profiles"],
                 "affected_groups": plan["affected_groups"],
                 "sync_errors": sync_errors},
            )
        progress(4, 4, "Mod renamed")
        log(f"Renamed '{mod}' to '{new_name}' in {len(affected)} profile(s).")
        return {
            "ok": True, "mod": mod, "new_name": new_name,
            "affected_profiles": plan["affected_profiles"],
            "affected_groups": plan["affected_groups"],
        }
