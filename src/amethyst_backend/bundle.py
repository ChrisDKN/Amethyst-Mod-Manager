from __future__ import annotations

import hashlib
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
    owner_profile, owner_name, _, spec = read_installed(
        game, requested_profile, mod_name)
    return {
        "profile": requested_profile.name,
        "owner_profile": owner_profile.name,
        "mod": mod_name,
        "owner_mod": owner_name,
        "revision": hashlib.sha256(spec.to_json().encode("utf-8")).hexdigest(),
        "selected": [option.folder for group in spec.groups
                     for option in group.options if option.selected],
        "groups": [{
            "name": group.name,
            "select_one": group.select_one,
            "flat": group.flat,
            "options": [option.to_dict() for option in group.options],
        } for group in spec.groups],
    }
