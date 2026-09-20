from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

from .jobs import JobError


def describe_choices(prepared):
    from Utils.re_engine.bundle import layout_to_spec

    spec = layout_to_spec(prepared.bundle_layout)
    return {
        "bundle_name": prepared.bundle_layout.bundle_name,
        "defaults": [option.folder for group in spec.groups
                     for option in group.options if option.selected],
        "groups": [{
            "name": group.name,
            "select_one": group.select_one,
            "options": [{
                "folder": option.folder,
                "label": option.label,
                "is_label": option.is_label,
                "default_selected": option.selected,
            } for option in group.options],
        } for group in spec.groups],
    }


def validate_choices(prepared, selected):
    from Utils.re_engine.bundle import layout_to_spec

    spec = layout_to_spec(prepared.bundle_layout)
    validate_spec_choices(spec, selected)


def validate_spec_choices(spec, selected):
    choices = set(selected)
    available = {option.folder for group in spec.groups
                 for option in group.options if not option.is_label}
    unknown = sorted(choices - available)
    if unknown or len(choices) != len(selected):
        raise JobError("installer_selection_invalid",
                       "The bundle selection contains unknown or repeated options.",
                       {"unknown": unknown})
    for group in spec.groups:
        if group.select_one:
            real = {option.folder for option in group.options
                    if not option.is_label}
            if real and len(choices & real) != 1:
                raise JobError("installer_selection_invalid",
                               "Exactly one option is required for this bundle group.",
                               {"group": group.name, "options": sorted(real)})


def read_installed(game, requested_profile: Path, mod_name: str):
    from Utils.mods.copy import resolve_target_staging
    from Utils.mods.modlist import read_modlist
    from Utils.profiles.groups import entry_owner_profile, is_group
    from Utils.re_engine.bundle import read_bundle_spec

    profiles_root = Path(game.get_profile_root()) / "profiles"
    if (requested_profile.parent != profiles_root or not requested_profile.is_dir()
            or requested_profile.is_symlink()):
        raise JobError("unsafe_profile_path", "The selected profile is unavailable or unsafe.")
    try:
        entries = [entry for entry in read_modlist(requested_profile / "modlist.txt")
                   if entry.name == mod_name and not entry.is_separator]
    except OSError as exc:
        raise JobError("resource_unavailable", "Could not read the profile mod list.",
                       {"reason": str(exc)}) from exc
    if not entries:
        raise JobError("mod_not_found", f"Mod '{mod_name}' is not in the profile.",
                       {"mod": mod_name})
    if len(entries) > 1:
        raise JobError("ambiguous_mod", f"Mod '{mod_name}' appears more than once.",
                       {"mod": mod_name, "matches": len(entries)})
    owner_profile, owner_name = requested_profile, mod_name
    if is_group(requested_profile):
        owner = entry_owner_profile(requested_profile, mod_name)
        if owner is None:
            raise JobError("profile_group_owner_missing",
                           "The profile group has no owner for this mod.",
                           {"mod": mod_name})
        owner_profile, owner_name = owner
        if (owner_profile.parent != profiles_root or owner_profile.is_symlink()
                or not owner_profile.is_dir()):
            raise JobError("unsafe_profile_path", "The owner profile is unsafe.")
        if owner_profile != requested_profile:
            try:
                owner_entries = read_modlist(owner_profile / "modlist.txt")
            except OSError as exc:
                raise JobError("resource_unavailable",
                               "Could not read the owner profile mod list.",
                               {"reason": str(exc)}) from exc
            if not any(not entry.is_separator and entry.name == owner_name
                       for entry in owner_entries):
                raise JobError("profile_group_owner_stale",
                               "The owner profile no longer lists this mod.",
                               {"mod": mod_name, "owner_profile": owner_profile.name})
    staging = Path(resolve_target_staging(game, owner_profile))
    destination = staging / owner_name
    if destination.is_symlink() or not destination.is_dir():
        raise JobError("mod_files_missing", "The staged mod folder is unavailable.",
                       {"mod": mod_name})
    meta = destination / "meta.ini"
    if meta.is_symlink():
        raise JobError("unsafe_mod_path", "The mod metadata is a symbolic link.",
                       {"mod": mod_name})
    spec = read_bundle_spec(meta)
    if spec is None:
        raise JobError("bundle_not_found", "The mod has no saved bundle options.",
                       {"mod": mod_name})
    return owner_profile, owner_name, destination, spec


def installed_choices(game, requested_profile: Path, mod_name: str) -> dict:
    owner_profile, owner_name, destination, spec = read_installed(
        game, requested_profile, mod_name)
    return {
        "profile": requested_profile.name,
        "owner_profile": owner_profile.name,
        "mod": mod_name,
        "owner_mod": owner_name,
        "revision": _revision(spec),
        "recovery_pending": (destination / ".mm_bundle" / ".selection-pending").exists(),
        "selected": [option.folder for group in spec.groups
                     for option in group.options if option.selected],
        "groups": [{
            "name": group.name,
            "select_one": group.select_one,
            "flat": group.flat,
            "options": [option.to_dict() for option in group.options],
        } for group in spec.groups],
    }


def _revision(spec) -> str:
    return hashlib.sha256(spec.to_json().encode("utf-8")).hexdigest()


def _validate_storage(destination: Path, spec) -> None:
    from Utils.re_engine.bundle import BUNDLE_LIB_DIR

    library = destination / BUNDLE_LIB_DIR
    if library.is_symlink() or not library.is_dir():
        raise JobError("unsafe_bundle_library", "The bundle option library is unavailable.")
    manifest = library / ".materialized"
    if manifest.is_symlink():
        raise JobError("unsafe_bundle_library", "The bundle manifest is a symbolic link.")
    if (library / ".selection-pending").is_symlink():
        raise JobError("unsafe_bundle_library", "The bundle recovery marker is a symbolic link.")
    try:
        recorded = manifest.read_text(encoding="utf-8").splitlines() if manifest.exists() else []
    except OSError as exc:
        raise JobError("resource_unavailable", "Could not read the bundle manifest.",
                       {"reason": str(exc)}) from exc

    def safe_target(relative: str) -> bool:
        path = Path(relative)
        if (not relative or any(char in relative for char in "\\\x00\r\n")
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
                or path.parts[0] == BUNDLE_LIB_DIR
                or path.parts == ("meta.ini",)):
            return False
        current = destination
        for part in path.parts[:-1]:
            current = current / part
            if current.is_symlink():
                return False
        return True

    if any(not safe_target(relative) for relative in recorded):
        raise JobError("unsafe_bundle_library", "The bundle manifest has unsafe paths.")
    for group in spec.groups:
        for option in group.options:
            folder = option.folder
            if (not folder or folder in {".", ".."}
                    or any(char in folder for char in "\\\x00\r\n")
                    or Path(folder).parts != (folder,)):
                raise JobError("unsafe_bundle_library", "A bundle option has an unsafe folder.")
            root = library / folder
            if root.is_symlink() or not root.is_dir():
                raise JobError("unsafe_bundle_library", "A bundle option folder is unavailable.",
                               {"folder": folder})
            for file in root.rglob("*"):
                if file.is_symlink() or (file.is_file() and not safe_target(
                        file.relative_to(root).as_posix())):
                    raise JobError("unsafe_bundle_library",
                                   "A bundle option contains an unsafe path.",
                                   {"folder": folder})


def set_installed_choices(game, requested_profile: Path, mod_name: str,
                          selected: list[str], expected_revision: str, *,
                          log_fn, progress_fn, operation_owner: str) -> dict:
    from Utils.operations import acquire_game_operation
    from Utils.profiles.groups import materialize_group, member_of_groups
    from Utils.re_engine.bundle import BUNDLE_LIB_DIR, materialize_selection, write_bundle_spec

    with acquire_game_operation(game, "bundle selection", owner=operation_owner):
        owner_profile, owner_name, destination, spec = read_installed(
            game, requested_profile, mod_name)
        revision = _revision(spec)
        if revision != expected_revision:
            raise JobError("bundle_revision_changed",
                           "The saved bundle choices changed; read them again.",
                           {"expected": expected_revision, "actual": revision})
        validate_spec_choices(spec, selected)
        _validate_storage(destination, spec)
        current = {option.folder for group in spec.groups
                   for option in group.options if option.selected}
        pending = destination / BUNDLE_LIB_DIR / ".selection-pending"
        recovering = pending.exists()
        changed = current != set(selected)
        if not changed and not recovering:
            return {"ok": True, "changed": False, "recovered": False,
                    "recovery_pending": False, "revision": revision,
                    "selected": list(selected), "owner_profile": owner_profile.name,
                    "owner_mod": owner_name}

        updated = deepcopy(spec)
        for group in updated.groups:
            for option in group.options:
                option.selected = not option.is_label and option.folder in selected
        progress_fn(0, 2, "materialize_bundle")
        meta = destination / "meta.ini"
        try:
            pending.write_text("1", encoding="utf-8")
        except OSError as exc:
            raise JobError("resource_unavailable",
                           "Could not record bundle selection recovery state.",
                           {"reason": str(exc)}) from exc
        try:
            materialize_selection(destination, updated)
            write_bundle_spec(meta, updated)
        except Exception as exc:
            try:
                materialize_selection(destination, spec)
                write_bundle_spec(meta, spec)
                pending.unlink(missing_ok=True)
                restored = True
            except Exception as rollback_exc:
                log_fn(f"Bundle selection rollback failed: {rollback_exc}")
                restored = False
            raise JobError("bundle_selection_failed",
                           "Could not apply the bundle selection.",
                           {"mod": mod_name, "reason": str(exc),
                            "disk_changed": not restored}) from exc
        progress_fn(1, 2, "index_bundle")
        try:
            from Utils.filegraph.service import FileGraphService
            library = FileGraphService.open_library(game, owner_profile, log_fn=log_fn)
            library.update_mod_from_disk(owner_profile, owner_name)
            for group_name in member_of_groups(game, owner_profile.name):
                materialize_group(
                    game, owner_profile.parent / group_name, log_fn=log_fn)
        except Exception as exc:
            raise JobError("bundle_selection_sync_failed",
                           "The bundle files changed, but indexing failed.",
                           {"mod": mod_name, "disk_changed": True,
                            "reason": str(exc)}) from exc
        progress_fn(2, 2, "index_bundle")
        try:
            pending.unlink(missing_ok=True)
        except OSError as exc:
            log_fn(f"Bundle recovery marker could not be cleared: {exc}")
        return {"ok": True, "changed": changed, "recovered": recovering,
                "recovery_pending": pending.exists(),
                "revision": _revision(updated),
                "selected": list(selected), "owner_profile": owner_profile.name,
                "owner_mod": owner_name}
