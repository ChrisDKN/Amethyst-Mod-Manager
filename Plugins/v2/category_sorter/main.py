from __future__ import annotations

import re

from amethyst_plugins import Client, PluginError


def category(mod):
    nexus = mod.get("sources", {}).get("nexus") or {}
    return (nexus.get("category") or "").strip() or "Uncategorized"


def build_layout(inventory, relationships):
    rows = inventory["mods"]
    mods = {row["name"]: row for row in rows if row["kind"] == "mod"}
    order = list(mods)
    positions = {name: index for index, name in enumerate(order)}
    if len({row["name"] for row in rows}) != len(rows):
        raise PluginError("The mod list contains duplicate names.")

    parent = {name: name for name in mods}

    def root(name):
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def join(a, b):
        parent[root(a)] = root(b)

    edges = set()
    for relation in relationships:
        a, b = relation["winner"], relation["loser"]
        if a in mods and b in mods and a != b:
            edges.add(tuple(sorted((a, b), key=positions.__getitem__)))
            join(a, b)

    units = {name: [name] for name in mods}
    groups = inventory.get("groups", {})
    for leader, group in groups.items():
        names = list(group["members"])
        if "title" not in group:
            names.append(leader)
        names.sort(key=positions.__getitem__)
        for name in names:
            units[name] = names
            join(names[0], name)

    protected = set()
    frozen_block = False
    previous_routed = False
    for row in rows:
        name = row["name"]
        if row["kind"] == "separator":
            settings = row["separator_state"]
            routed = bool(settings.get("deploy_path"))
            frozen_block = bool(settings["locked"] or routed)
            if frozen_block or previous_routed:
                protected.add(name)
            previous_routed = routed
        elif frozen_block or row["locked"]:
            protected.update(units[name])

    sections, pending = [], []
    seen = set()
    for row in rows:
        name = row["name"]
        if name in protected:
            if pending:
                sections.append(pending)
                pending = []
            sections.append(name)
        elif row["kind"] == "mod" and name not in seen:
            unit = units[name]
            pending.append(unit)
            seen.update(unit)
    if pending:
        sections.append(pending)

    categories = {name: category(mods[units[name][0]]) for name in mods}
    canonical = {}
    for name in order:
        value = categories[name]
        canonical.setdefault(value.casefold(), value)
    categories = {name: canonical[value.casefold()] for name, value in categories.items()}

    def sorted_units(section):
        return sorted(section, key=lambda unit: (categories[unit[0]] == "Uncategorized",
                                                 categories[unit[0]].casefold()))

    candidate = []
    for section in sections:
        if isinstance(section, str):
            if section in mods:
                candidate.append(section)
        else:
            candidate.extend(name for unit in sorted_units(section) for name in unit)
    proposed = {name: index for index, name in enumerate(candidate)}
    conflict_roots = {root(a) for a, b in edges if proposed[a] > proposed[b]}
    conflicts = {name for name in mods if root(name) in conflict_roots}

    separators = {row["name"]: row for row in rows if row["kind"] == "separator"}
    existing_names = {row["name"].casefold(): row["name"] for row in rows}
    occupied = {row["display_name"].casefold() for row in rows
                if row["kind"] == "mod" or row["name"] in protected}
    internals = {row["name"].casefold() for row in rows
                 if row["kind"] == "mod" or row["name"] in protected}

    def separator(label):
        base = re.sub(r'[<>:"/\\|?*\x00-\x1f\ud800-\udfff]', " - ", label).strip(" .")
        base = base[:120].strip() or "Uncategorized"
        name, suffix = base, 2
        while (name.casefold() in occupied or (name + "_separator").casefold() in internals
               or existing_names.get((name + "_separator").casefold(), name + "_separator")
               != name + "_separator"):
            name = f"{base} ({suffix})"
            suffix += 1
        occupied.add(name.casefold())
        internals.add((name + "_separator").casefold())
        existing = separators.get(name + "_separator")
        if existing:
            return existing["name"]
        return {"separator": name}

    entries = []
    for section in sections:
        if isinstance(section, str):
            entries.append(section)
            continue
        affected = [name for unit in section for name in unit if name in conflicts]
        if affected:
            marker = separator("Conflicts")
            entries.extend([marker, *affected])
        current_category = None
        for unit in sorted_units(section):
            if unit[0] in conflicts:
                continue
            value = categories[unit[0]]
            if value != current_category:
                entries.append(separator(value))
                current_category = value
            entries.extend(unit)

    final = [entry for entry in entries if isinstance(entry, str) and entry in mods]
    final_positions = {name: index for index, name in enumerate(final)}
    if set(final) != set(mods) or len(final) != len(mods):
        raise PluginError("The category layout did not retain every mod exactly once.")
    if any(final_positions[a] > final_positions[b] for a, b in edges):
        raise PluginError("The category layout could not preserve conflict order.")
    return entries, conflicts - protected, len(protected & mods.keys())


def read_conflicts(client):
    relations = []
    offset, revision = 0, None
    while True:
        params = {**client.target, "offset": offset, "limit": 5000}
        if revision is not None:
            params["revision"] = revision
        page = client.wait(client.call("mods.conflicts.overview.start", params))
        revision = page["revision"]
        relations.extend(page["relationships"])
        offset = page["next_offset"]
        if offset is None:
            return relations


def main():
    client = Client()
    inventory = client.call("mods.list", client.target)
    if not any(row["kind"] == "mod" for row in inventory["mods"]):
        client.log("This profile has no mods to sort.")
        return
    client.log("Reading conflicts and categories. No layout changes have been made.")
    relationships = read_conflicts(client)
    entries, conflicts, protected = build_layout(inventory, relationships)
    latest = client.call("mods.list", client.target)
    if latest["mods"] != inventory["mods"] or latest.get("groups") != inventory.get("groups"):
        raise PluginError("The profile changed while reading it. Run Category Sorter again.")
    request = {**client.target, "entries": entries, "preserve_conflicts": True}
    plan = client.wait(client.call("mods.layout.plan.start", request))
    if not plan["changed"]:
        client.log("The profile is already sorted by category with conflict order preserved.")
        return
    old_separators = {row["name"] for row in inventory["mods"] if row["kind"] == "separator"}
    removed = sorted(old_separators - set(plan["separators"]))
    details = ["Proposed layout (highest priority first):", ""]
    for name in plan["entries"]:
        if name in plan["separators"]:
            details.append(f"[{name.removesuffix('_separator')}]")
        else:
            details.append(f"  {name}")
    details.extend(["", f"{len(conflicts)} mods placed in conflict separators in their original order.",
                    f"{protected} locked or protected mods kept in place.",
                    "Mod groups stay together and use their first mod's category.",
                    "Mods without category metadata use Uncategorized.",
                    "Archive conflict order is also preserved conservatively."])
    if removed:
        details.extend(["", "Existing separators replaced:", *removed])
    backup_inventory = client.wait(client.call("profiles.backups.list.start", client.target))
    backup_response = client.apply(
        "profiles.backups.create.start",
        {**client.target, "revision": backup_inventory["revision"], "label": "Before Category Sorter"},
        title="Back up profile before sorting?",
        summary="Create a retained manual backup before changing the mod layout.",
        details="The backup will appear in Restore backup as Before Category Sorter. "
                "It includes the mod list, plugin load order and profile state. "
                "Sorting will only proceed after the backup succeeds and you approve the layout.\n\n"
                + "\n".join(details),
    )
    if backup_response is None:
        client.log("Backup cancelled. Category sorting was not applied.")
        return
    backup = client.wait(backup_response)
    if not backup.get("ok") or not backup.get("backup", {}).get("id"):
        raise PluginError("The backup was not confirmed. Category sorting was not applied.")
    backup_id = backup["backup"]["id"]
    client.log(f"Manual backup created: Before Category Sorter ({backup_id}).")
    details.extend(["", f"Retained manual backup: Before Category Sorter ({backup_id})."])
    response = client.apply(
        "mods.layout.apply.start",
        {**request, "plan_revision": plan["plan_revision"], "confirm": True},
        title="Sort mods by category?",
        summary=f"Move {len(plan['moved'])} mods; keep {len(conflicts)} mods in conflict separators. "
                f"Replace {len(removed)} existing separators.",
        details="\n".join(details),
    )
    if response is None:
        client.log("Category sorting cancelled. The layout was not changed; the manual backup was kept.")
        return
    client.wait(response)
    client.log("Mods sorted by category. Conflict order, enabled states and groups were preserved.")


if __name__ == "__main__":
    try:
        main()
    except PluginError as exc:
        import sys

        print(f"Category Sorter: {exc}", file=sys.stderr)
        if exc.details:
            print(exc.details, file=sys.stderr)
        raise SystemExit(1)
