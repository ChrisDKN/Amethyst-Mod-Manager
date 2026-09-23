from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .jobs import JobError


def _snapshot(group: Path) -> dict[str, Any]:
    from Utils.profiles.groups import (
        get_members, is_group, read_group_exclusions,
    )

    if group.is_symlink() or not group.is_dir() or not is_group(group):
        raise JobError("profile_group_not_found",
                       "The selected Profile Group is unavailable.",
                       {"profile": group.name})
    members = get_members(group)
    exclusions = read_group_exclusions(group)
    payload = json.dumps(
        {"members": members, "exclusions": exclusions},
        sort_keys=True, ensure_ascii=True).encode("ascii")
    return {"profile": group.name, "members": members,
            "exclusions": exclusions,
            "revision": hashlib.sha256(payload).hexdigest()}


def get_exclusions(group: Path) -> dict[str, Any]:
    from Utils.profiles.groups import group_build_lock

    with group_build_lock(group):
        return _snapshot(group)


def set_exclusions(game: Any, group: Path, selected: list[dict[str, str]],
                   revision: str, log, progress) -> dict[str, Any]:
    from Utils.mods.modlist import read_modlist
    from Utils.operations import acquire_game_operation
    from Utils.profiles.groups import (
        group_build_lock, group_exclusion_for_mod, materialize_group,
        write_group_exclusions,
    )
    from Utils.profiles.manage import ProfileError, _reject_deployed_use

    with acquire_game_operation(
            game, "profiles.groups.exclusions.set",
            owner="headless:profiles.groups.exclusions.set"):
        with group_build_lock(group):
            current = _snapshot(group)
            if current["revision"] != revision:
                raise JobError("profile_group_exclusions_changed",
                               "The group exclusions changed; inspect them again.",
                               current)
            try:
                _reject_deployed_use(game, group)
            except ProfileError as exc:
                raise JobError(exc.name, str(exc), exc.details) from exc

            exclusions = []
            seen = set()
            existing = {(item["member"], item["mod"]): item
                        for item in current["exclusions"]}
            for item in selected:
                member, mod = item["member"], item["mod"]
                if member not in current["members"]:
                    raise JobError("profile_group_member_changed",
                                   "An excluded member is no longer in the group.",
                                   {"member": member})
                member_dir = group.parent / member
                folder = member_dir / "mods" / mod
                if member_dir.is_symlink() or (member_dir / "mods").is_symlink():
                    raise JobError("unsafe_mod_path",
                                   "A group member path is a symbolic link.",
                                   {"member": member})
                if (member, mod) in seen:
                    raise JobError("duplicate_mod",
                                   "The same member mod was selected twice.",
                                   {"member": member, "mod": mod})
                seen.add((member, mod))
                available = (
                    member_dir.is_dir() and not folder.is_symlink()
                    and folder.is_dir()
                    and any(entry.name == mod and not entry.is_separator
                            for entry in read_modlist(
                                member_dir / "modlist.txt")))
                if available:
                    exclusions.append(group_exclusion_for_mod(
                        group, member, mod))
                elif (member, mod) in existing:
                    exclusions.append(existing[(member, mod)])
                else:
                    raise JobError("mod_not_found",
                                   "An excluded member mod is unavailable.",
                                   {"member": member, "mod": mod})

            progress(0, 1, "Updating Profile Group exclusions")
            write_group_exclusions(group, exclusions)
            game.set_active_profile_dir(group)
            game.load_paths()
            materialize_group(game, group, log_fn=log)
            progress(1, 1, "Updating Profile Group exclusions")
            return _snapshot(group)
