from __future__ import annotations

from pathlib import Path

from .downloads import resolve
from .repositories import callback, installed, lookup, requests


def graph(game, roots, log, control=None):
    ordered, complete, visiting = [], set(), set()
    def visit(row, depth=0):
        key = tuple(row[key] for key in ("plugin_id", "provider_id", "source"))
        if key in visiting:
            raise ValueError("Provider dependencies contain a cycle.")
        if key in complete:
            return
        if depth > 32 or len(complete) + len(visiting) >= 256:
            raise ValueError("Provider dependency graphs are limited to 256 artifacts and 32 levels.")
        visiting.add(key)
        plugin, provider = lookup(game.game_id, row["plugin_id"], row["provider_id"])
        children = callback(game, plugin, provider, "get_dependencies", {"source": row["source"]}, log, control, optional=True)
        for child in requests([] if children is None else children, game):
            visit(child, depth + 1)
        visiting.remove(key)
        complete.add(key)
        ordered.append(row)
    for root in requests(roots, game):
        visit(root)
    return ordered


def missing(game, profile, dependencies, log):
    from Utils.mods.modlist import read_modlist
    from Utils.mods.copy import resolve_target_staging
    staging = Path(resolve_target_staging(game, profile))
    identities = set()
    for entry in read_modlist(Path(profile) / "modlist.txt"):
        if entry.enabled and not entry.is_separator and not entry.is_group_header and Path(entry.name).name == entry.name:
            record = installed(staging / entry.name / "meta.ini")
            if record:
                identities.add((record["plugin_id"], record["provider_id"], record["identity"]))
    absent = []
    for row in graph(game, dependencies, log):
        plan = resolve(game, **row, log=log, control=None)
        if (row["plugin_id"], row["provider_id"], plan.descriptor["identity"]) not in identities:
            absent.append({**row, "artifact": plan.public()})
    return absent
