from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .jobs import JobError


ACTIONS = {
    "create": ({"mods"}, {"leader", "title", "collapsed"}),
    "add": ({"group", "mods"}, {"position"}),
    "ungroup": ({"mods"}, set()),
    "promote": ({"group", "leader"}, set()),
    "rename": ({"group", "title"}, set()),
    "collapse": ({"collapsed"}, {"group"}),
    "dissolve": ({"group"}, set()),
}


def _snapshot(modlist_path, raw, entries, original):
    from Utils.mods.groups import group_state_revision, normalize_groups

    return {"profile": modlist_path.parent.name,
            "groups": normalize_groups(original, entries),
            "revision": group_state_revision(raw, original)}


def get_groups(profile: Path):
    from Utils.mods.groups import group_edit_state

    path = profile / "modlist.txt"
    with group_edit_state(path) as (raw, state, entries, original):
        return _snapshot(path, raw, entries, original)


def _selection(entries, groups, names, *, unlocked=False):
    from Utils.mods.groups import expand_leaders

    moving = expand_leaders(names, groups)
    for name in moving:
        matches = [entry for entry in entries if entry.name == name]
        if len(matches) != 1:
            raise JobError("mod_not_found" if not matches else "ambiguous_mod",
                           "A selected mod or group is not unique in the profile.",
                           {"mod": name, "matches": len(matches)})
        entry = matches[0]
        if entry.is_separator:
            raise JobError("mod_not_groupable", "Separators cannot belong to mod groups.",
                           {"mod": name})
        if unlocked and entry.locked and not entry.is_group_header:
            raise JobError("mod_locked", "A selected mod is locked.", {"mod": name})
    return moving


def _edit(entries, groups, options):
    from Utils.mods import groups as grouping

    action = options["action"]
    selected = deepcopy(groups)
    group = options.get("group")
    if group is not None and group not in groups:
        raise JobError("mod_group_not_found", "The selected mod group is unavailable.",
                       {"group": group})
    if action == "create":
        moving = _selection(entries, groups, options["mods"], unlocked=True)
        if "leader" in options:
            group = options["leader"]
            _selection(entries, groups, [group])
            if group in groups or group in grouping.owners(groups):
                raise JobError("mod_already_grouped", "The requested leader already belongs to a group.",
                               {"mod": group})
            if group in moving:
                raise JobError("invalid_mod_group", "The leader cannot also be a selected member.")
            updated, selected = grouping.group_with(entries, groups, options["mods"], group)
        else:
            real = [entry for entry in entries if not entry.is_group_header]
            block = [entry for entry in real if entry.name in moving]
            at = next(index for index, entry in enumerate(real) if entry.name in moving)
            updated = [entry for entry in real if entry.name not in moving]
            updated[at:at] = block
            selected = grouping.detach(groups, moving)
            group = f"@group:{uuid4().hex}"
            selected[group] = {"title": options["title"],
                               "members": [entry.name for entry in block]}
        selected[group]["collapsed"] = options.get("collapsed", True)
    elif action == "add":
        moving = _selection(entries, groups, options["mods"], unlocked=True)
        if group in moving:
            raise JobError("invalid_mod_group", "A group cannot be added to itself.")
        targets = {group, *groups[group]["members"]}
        positions = [index for index, entry in enumerate(entries) if entry.name in targets]
        position = options.get("position")
        if position is None:
            slot = max(positions) + 1
        else:
            placement, anchor = next(iter(position.items()))
            if anchor not in targets or anchor in moving:
                raise JobError("invalid_group_position", "The anchor must be an unselected entry in the target group.",
                               {"anchor": anchor, "group": group})
            if placement == "before" and anchor == group:
                raise JobError("invalid_group_position", "Group members must follow their leader or header.",
                               {"anchor": anchor, "group": group})
            slot = next(index for index, entry in enumerate(entries) if entry.name == anchor)
            slot += placement == "after"
        updated, selected = grouping.move_into_group(entries, groups, options["mods"], group, slot)
    elif action == "ungroup":
        _selection(entries, groups, options["mods"])
        updated, selected = grouping.ungroup(entries, groups, options["mods"])
        old = {entry.name: index for index, entry in enumerate(entries)}
        for index, entry in enumerate(updated):
            if entry.name in options["mods"] and entry.locked and old[entry.name] != index:
                raise JobError("mod_locked", "Ungrouping would move a locked mod.", {"mod": entry.name})
    elif action == "promote":
        leader = options["leader"]
        if leader not in groups[group]["members"]:
            raise JobError("invalid_mod_group_leader", "The new leader must be a member of the group.",
                           {"group": group, "mod": leader})
        _selection(entries, {}, [leader], unlocked=True)
        selected = grouping.promote(groups, group, leader)
        updated = entries
        group = leader
    elif action == "rename":
        if "title" not in groups[group]:
            raise JobError("mod_group_not_cosmetic", "Only a named header can be renamed.",
                           {"group": group})
        selected[group]["title"] = options["title"]
        updated = entries
    elif action == "collapse":
        for key in ([group] if group is not None else groups):
            selected[key]["collapsed"] = options["collapsed"]
        updated = entries
    else:
        selected.pop(group)
        updated = entries
    return updated, selected, group


def edit_groups(game, profile: Path, options, owner, log, progress):
    from Utils.mods import groups as grouping
    from Utils.mods.state import _reconcile_filegraph
    from Utils.operations import acquire_game_operation

    with acquire_game_operation(game, "mods.groups.edit", owner=owner):
        if not profile.is_dir() or profile.is_symlink():
            raise JobError("profile_not_found", "The selected profile is unavailable.")
        game.set_active_profile_dir(profile)
        game.load_paths()
        path = profile / "modlist.txt"
        progress(0, 2, "Editing mod groups")
        with grouping.group_edit_state(path) as (raw, state, entries, original):
            current = _snapshot(path, raw, entries, original)
            if current["revision"] != options["revision"]:
                raise JobError("stale_mod_groups", "Mod order or grouping changed; read the groups again.",
                               current)
            groups = current["groups"]
            grouped = grouping.with_group_headers(entries, groups)
            updated, selected, group = _edit(grouped, groups, options)
            updated, selected, order_changed, groups_changed = grouping.save_group_edit(
                path, raw, state, updated, selected)
            result = _snapshot(path, path.read_bytes() if path.is_file() else b"", updated, selected)
        old_positions = {entry.name: index for index, entry in enumerate(entries)}
        moved = [entry.name for index, entry in enumerate(updated)
                 if not entry.is_separator and old_positions.get(entry.name) != index]
        reconciled, generation = False, None
        if order_changed:
            progress(1, 2, "Reconciling mod order")
            try:
                reconciled, generation = _reconcile_filegraph(
                    game, profile, kind="move", mod_names=moved, log=log, progress=progress)
            except Exception as exc:
                raise JobError("mod_group_order_sync_failed",
                               "The group edit was saved, but Filegraph reconciliation failed.",
                               {**result, "state_persisted": True, "moved": moved,
                                "reason": str(exc)}) from exc
        progress(2, 2, "Mod groups updated")
        changed = order_changed or groups_changed
        log("Saved mod group edit." if changed else "Mod group state already matched the request.")
        return {**result, "ok": True, "changed": changed, "group": group,
                "order_changed": order_changed, "moved": moved,
                "filegraph_reconciled": reconciled, "filegraph_generation": generation}
