"""Profile-local mod groups; modlist order remains authoritative."""

from __future__ import annotations

from copy import deepcopy


def normalize_groups(raw, entries=None) -> dict[str, dict]:
    groups = {}
    if not isinstance(raw, dict):
        return groups
    positions = ({e.name: i for i, e in enumerate(entries)
                  if not e.is_separator} if entries is not None else None)
    claimed = set()
    for leader, value in raw.items():
        if not isinstance(leader, str) or not leader or not isinstance(value, dict):
            continue
        members = value.get("members")
        if not isinstance(members, list):
            continue
        members = list(dict.fromkeys(n for n in members
                                     if isinstance(n, str) and n and n != leader))
        if positions is not None:
            if leader not in positions:
                continue
            members = [n for n in members if n in positions]
            members.sort(key=positions.__getitem__)
        names = {leader, *members}
        if not members or names & claimed:
            continue
        if positions is not None:
            slots = [positions[n] for n in names]
            if max(slots) - min(slots) + 1 != len(slots):
                continue
        groups[leader] = {"members": members,
                          "collapsed": value.get("collapsed") is not False}
        claimed.update(names)
    return groups


def owners(groups) -> dict[str, str]:
    return {name: leader for leader, data in groups.items()
            for name in (leader, *data["members"])}


def blocks(entries, groups):
    membership = owners(groups)
    result = []
    for entry in entries:
        leader = membership.get(entry.name)
        if (leader and result
                and membership.get(result[-1][0].name) == leader):
            result[-1].append(entry)
        else:
            result.append([entry])
    return result


def prioritize_leaders(entries, groups):
    membership = owners(groups)
    result = []
    for block in blocks(entries, groups):
        leader = membership.get(block[0].name)
        if leader:
            result.extend(e for e in block if e.name == leader)
            result.extend(e for e in block if e.name != leader)
        else:
            result.extend(block)
    return result


def load_grouped_modlist(modlist_path):
    from Utils.mods.modlist import modlist_lock, read_modlist, write_modlist
    from Utils.profiles.state import read_mod_groups, write_mod_groups
    with modlist_lock(modlist_path):
        entries = read_modlist(modlist_path)
        original = read_mod_groups(modlist_path.parent)
        groups = normalize_groups(original, entries)
        ordered = prioritize_leaders(entries, groups)
        if ordered != entries:
            write_modlist(modlist_path, ordered)
        try:
            if groups != original:
                write_mod_groups(modlist_path.parent, groups)
        except Exception:
            if ordered != entries:
                write_modlist(modlist_path, entries)
            raise
        return ordered, groups


def expand_leaders(names, groups) -> set[str]:
    names = set(names)
    return names | {n for leader in names if leader in groups
                    for n in groups[leader]["members"]}


def detach(groups, names) -> dict[str, dict]:
    names = set(names)
    result = deepcopy(groups)
    for leader in list(result):
        if leader in names:
            del result[leader]
        else:
            result[leader]["members"] = [n for n in result[leader]["members"]
                                          if n not in names]
    return normalize_groups(result)


def rename_group_mod(groups, old, new) -> dict[str, dict]:
    if old == new:
        return deepcopy(groups)
    result = detach(groups, [new])
    if old in result:
        result[new] = result.pop(old)
    for data in result.values():
        data["members"] = [new if n == old else n for n in data["members"]]
    return normalize_groups(result)


def boundary_slot(entries, groups, slot, *, after=False) -> int:
    slot = max(0, min(len(entries), slot))
    membership = owners(groups)
    if 0 < slot < len(entries):
        leader = membership.get(entries[slot].name)
        if leader and membership.get(entries[slot - 1].name) == leader:
            step = 1 if after else -1
            while 0 < slot < len(entries):
                name = entries[slot if after else slot - 1].name
                if membership.get(name) != leader:
                    break
                slot += step
    return slot


def group_with(entries, groups, names, leader, reverse=False):
    membership = owners(groups)
    moving = expand_leaders(names, groups)
    if leader in moving or membership.get(leader, leader) != leader:
        return entries, groups
    available = {e.name: e for e in entries if not e.is_separator}
    if leader not in available or not moving or not moving <= available.keys():
        return entries, groups
    if any(available[n].locked for n in moving):
        return entries, groups
    moved = [e for e in entries if e.name in moving]
    rest = [e for e in entries if e.name not in moving]
    result = detach(groups, moving)
    target = {leader, *result.get(leader, {}).get("members", [])}
    slots = [i for i, e in enumerate(rest) if e.name in target]
    at = min(slots) if reverse else max(slots) + 1
    rest[at:at] = moved
    result[leader] = {"members": list((target | moving) - {leader}),
                      "collapsed": True}
    return rest, normalize_groups(result, rest)


def relocate(entries, groups, names, slot, *, retain=None):
    moving = expand_leaders(names, groups)
    block = [e for e in entries if e.name in moving]
    if not block or any(e.locked and not e.is_separator for e in block):
        return entries, groups
    if retain is None:
        slot = boundary_slot(entries, groups, slot, after=True)
    result = deepcopy(groups)
    for leader, data in list(result.items()):
        if leader not in moving and leader != retain:
            data["members"] = [n for n in data["members"] if n not in moving]
    at = slot - sum(e.name in moving for e in entries[:slot])
    rest = [e for e in entries if e.name not in moving]
    rest[at:at] = block
    return rest, normalize_groups(result, rest)


def promote(groups, leader, new):
    if leader not in groups or new not in groups[leader]["members"]:
        return groups
    result = deepcopy(groups)
    data = result.pop(leader)
    data["members"] = [leader, *(n for n in data["members"] if n != new)]
    result[new] = data
    return result


def ungroup(entries, groups, names, reverse=False):
    names = set(names)
    result = detach(groups, names)
    rest = list(entries)
    for leader, data in groups.items():
        if leader in names:
            continue
        leaving = names & set(data["members"])
        if not leaving or leader not in result:
            continue
        moving = [e for e in rest if e.name in leaving]
        rest = [e for e in rest if e.name not in leaving]
        staying = {leader, *result[leader]["members"]}
        slots = [i for i, e in enumerate(rest) if e.name in staying]
        at = min(slots) if reverse else max(slots) + 1
        rest[at:at] = moving
    return rest, normalize_groups(result, rest)


def reconcile_profile_groups(profile_dir, entries, *, renames=()):
    from Utils.profiles.state import read_mod_groups, write_mod_groups
    original = read_mod_groups(profile_dir)
    groups = original
    for old, new in renames:
        groups = rename_group_mod(groups, old, new)
    groups = normalize_groups(groups, entries)
    if groups != original:
        write_mod_groups(profile_dir, groups)
    return groups


def copy_complete_groups(source_profile, target_profile, name_map, *, source_groups=None):
    from Utils.mods.modlist import modlist_lock, read_modlist, write_modlist
    from Utils.profiles.state import read_mod_groups, write_mod_groups
    if source_groups is None:
        source_groups = read_mod_groups(source_profile)
    preserved = set()
    modlist_path = target_profile / "modlist.txt"
    with modlist_lock(modlist_path):
        original_entries = read_modlist(modlist_path)
        entries = list(original_entries)
        original = read_mod_groups(target_profile)
        target = deepcopy(original)
        claimed = owners(original)
        by_name = {e.name: e for e in entries if not e.is_separator}
        for leader, data in source_groups.items():
            source_names = {leader, *data["members"]}
            if not source_names <= name_map.keys():
                continue
            mapped = {name_map[n] for n in source_names}
            new_leader = name_map[leader]
            if (len(mapped) != len(source_names) or not mapped <= by_name.keys()
                    or any(claimed.get(n) not in (None, new_leader) for n in mapped)):
                continue
            members = [name_map[n] for n in data["members"]]
            members.extend(n for n in target.get(new_leader, {}).get("members", [])
                           if n not in mapped and n in by_name)
            block_names = {new_leader, *members}
            block = [by_name[n] for n in (new_leader, *members)]
            at = next(i for i, e in enumerate(entries) if e.name in block_names)
            remaining = [e for e in entries if e.name not in block_names]
            remaining[at:at] = block
            old_positions = {e.name: i for i, e in enumerate(entries)}
            if any(e.locked and not e.is_separator
                   and old_positions[e.name] != i
                   for i, e in enumerate(remaining) if e.name in block_names):
                continue
            entries = remaining
            target[new_leader] = {
                "members": members,
                "collapsed": target.get(new_leader, data)["collapsed"],
            }
            claimed.update({n: new_leader for n in block_names})
            preserved.add(leader)
        target = normalize_groups(target, entries)
        if entries != original_entries:
            write_modlist(modlist_path, entries)
        try:
            if target != original:
                write_mod_groups(target_profile, target)
        except Exception:
            if entries != original_entries:
                write_modlist(modlist_path, original_entries)
            raise
    return preserved
