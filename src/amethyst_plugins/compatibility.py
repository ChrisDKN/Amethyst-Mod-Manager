from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Dependency:
    id: str
    min_version: str = ""
    max_version: str = ""
    optional: bool = False


def identifiers(values, field, own_id):
    if (not isinstance(values, list) or len(values) > 64
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", value)
                   or value == own_id for value in values) or len(set(values)) != len(values)):
        raise ValueError(f"{field} must contain up to 64 unique other plugin IDs.")
    return tuple(values)


def declarations(data, own_id):
    from .catalog import version_key
    rows = data.get("plugin_dependencies", [])
    if not isinstance(rows, list) or len(rows) > 64:
        raise ValueError("Expected at most 64 plugin dependencies.")
    output, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"id", "min_version", "max_version", "optional"}:
            raise ValueError("Invalid plugin dependency.")
        pid, = identifiers([row.get("id")], "plugin_dependencies", own_id)
        if pid in seen or type(row.get("optional", False)) is not bool:
            raise ValueError("Plugin dependencies must have unique IDs and boolean optional flags.")
        low, high = row.get("min_version", ""), row.get("max_version", "")
        if not isinstance(low, str) or not isinstance(high, str):
            raise ValueError("Dependency versions must be SemVer strings.")
        if low:
            version_key(low)
        if high:
            version_key(high)
        if low and high and version_key(low) > version_key(high):
            raise ValueError("Dependency minimum exceeds maximum.")
        output.append(Dependency(pid, low, high, row.get("optional", False)))
        seen.add(pid)
    return tuple(output)


def matches(dependency, plugin):
    from .catalog import version_key
    if dependency.min_version or dependency.max_version:
        if not plugin.version:
            return False
        key = version_key(plugin.version)
        return (not dependency.min_version or key >= version_key(dependency.min_version)) and (
            not dependency.max_version or key <= version_key(dependency.max_version))
    return True


def resolve(plugins):
    active = {plugin.id: plugin for plugin in plugins}
    errors, blocked = [], {}
    for plugin in plugins:
        for other in active.values():
            if other.id in plugin.plugin_conflicts or plugin.id in other.plugin_conflicts:
                blocked[plugin.id] = f"Conflicts with enabled plugin '{other.id}'."
    while True:
        for plugin in active.values():
            if plugin.id in blocked:
                continue
            for dependency in plugin.plugin_dependencies:
                other = active.get(dependency.id)
                if not dependency.optional and (other is None or other.id in blocked or not matches(dependency, other)):
                    version = f" ({dependency.min_version or '*'} to {dependency.max_version or '*'})"
                    blocked[plugin.id] = f"Required plugin '{dependency.id}'{version} is missing, disabled or incompatible."
                    break
        rejected = set(active) & set(blocked)
        if not rejected:
            break
        for pid in rejected:
            active.pop(pid)
    errors.extend(f"{pid}: {message}" for pid, message in sorted(blocked.items()))
    edges = {}
    for plugin in active.values():
        edges[plugin.id] = {d.id for d in plugin.plugin_dependencies if d.id in active and matches(d, active[d.id])}
        edges[plugin.id].update(pid for pid in plugin.load_after if pid in active)
    ordered = []
    while edges:
        ready = sorted((pid for pid, dependencies in edges.items() if not dependencies),
                       key=lambda pid: (active[pid].name.casefold(), pid))
        if not ready:
            errors.extend(f"{pid}: Plugin dependency or load_after ordering contains a cycle or depends on one."
                          for pid in sorted(edges))
            break
        for pid in ready:
            ordered.append(active[pid])
            edges.pop(pid)
        for dependencies in edges.values():
            dependencies.difference_update(ready)
    return ordered, errors
