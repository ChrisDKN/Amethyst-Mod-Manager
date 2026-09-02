"""
LOOT userlist.yaml backend - parsing, writing, cycle analysis, and mutations.

Neutral (GUI-free) port of the static/instance helpers from
gui/plugin_panel_userlist_cycle.py so the Qt GUI (and tests) can share them.
The file lives in the active profile dir: <profile>/userlist.yaml.

Parsed data shape (both directions):
  {"plugins": [{"name": str, "after": [str], "before": [str], "group": str}, ...],
   "groups":  [{"name": str, "after": [str]}, ...]}
(list/group fields are optional per entry.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from Utils.atomic_write import write_atomic_text

DEFAULT_GROUP = "default"


# ---------------------------------------------------------------------------
# Parse / write
# ---------------------------------------------------------------------------

def parse_userlist(path: Path) -> dict:
    """Parse a minimal LOOT userlist.yaml into {'plugins': [...], 'groups': [...]}."""
    result: dict = {"plugins": [], "groups": []}
    if not path.is_file():
        return result
    text = path.read_text(encoding="utf-8")
    # Split into top-level sections; collect raw block per plugin/group entry
    current_section: str | None = None
    current_block: list[str] = []

    def _flush_block(section, block):
        if not block:
            return
        entry: dict = {}
        # name - first line is "  - name: 'Foo.esp'" or "- name: 'Foo.esp'"
        m = re.match(r"^[\s\-]*name:\s*['\"]?(.*?)['\"]?\s*$", block[0])
        if m:
            entry["name"] = m.group(1)
        # scalar fields: group
        for line in block:
            mg = re.match(r"^\s*group:\s*['\"]?(.*?)['\"]?\s*$", line)
            if mg:
                entry["group"] = mg.group(1)
        # list fields: before, after
        for fld in ("before", "after"):
            pat = re.compile(r"^\s*" + fld + r":\s*$")
            inline = re.compile(r"^\s*" + fld + r":\s*\[(.+)\]\s*$")
            items: list[str] = []
            in_list = False
            for line in block:
                if inline.match(line):
                    raw_items = inline.match(line).group(1)
                    items = [i.strip().strip("'\"") for i in raw_items.split(",") if i.strip()]
                    break
                if pat.match(line):
                    in_list = True
                    continue
                if in_list:
                    if re.match(r"^\s+\w[\w_]*\s*:", line):
                        # A new key at the same or lower indent - end of this list
                        in_list = False
                    else:
                        item_m = re.match(r"^\s*-\s*['\"]?(.*?)['\"]?\s*$", line)
                        if item_m:
                            items.append(item_m.group(1))
            if items:
                # Deduplicate while preserving order
                seen_items: list[str] = []
                for item in items:
                    if item.lower() not in {s.lower() for s in seen_items}:
                        seen_items.append(item)
                entry[fld] = seen_items
        if entry.get("name"):
            result[section].append(entry)

    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "plugins:":
            if current_section:
                _flush_block(current_section, current_block)
            current_section = "plugins"
            current_block = []
        elif stripped == "groups:":
            if current_section:
                _flush_block(current_section, current_block)
            current_section = "groups"
            current_block = []
        elif stripped.startswith("- name:") and current_section:
            if current_block:
                _flush_block(current_section, current_block)
            current_block = [line]
        elif current_section and (line.startswith("  ") or line.startswith("\t")):
            current_block.append(line)

    if current_section and current_block:
        _flush_block(current_section, current_block)

    return result


def write_userlist(path: Path, data: dict) -> None:
    """Write a userlist dict back to YAML format (atomic). Deletes the file when
    the data serialises to nothing so libloot doesn't choke on empty YAML."""
    lines = []

    def _quote(s: str) -> str:
        if "'" in s:
            escaped = s.replace('"', '\\"')
            return f'"{escaped}"'
        return f"'{s}'"

    plugins = data.get("plugins", [])
    groups = data.get("groups", [])

    if plugins:
        lines.append("plugins:")
        for entry in plugins:
            lines.append(f"  - name: {_quote(entry['name'])}")
            for fld in ("before", "after"):
                items = entry.get(fld, [])
                if items:
                    lines.append(f"    {fld}:")
                    for item in items:
                        lines.append(f"      - {_quote(item)}")
            if entry.get("group"):
                lines.append(f"    group: {_quote(entry['group'])}")

    if groups:
        if lines:
            lines.append("")
        lines.append("groups:")
        for entry in groups:
            lines.append(f"  - name: {_quote(entry['name'])}")
            after_items = entry.get("after", [])
            if after_items:
                lines.append("    after:")
                for item in after_items:
                    lines.append(f"      - {_quote(item)}")

    if lines:
        write_atomic_text(path, "\n".join(lines) + "\n")
    else:
        # Nothing left - remove the file so libloot doesn't choke on an empty document
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Cycle analysis (Tarjan SCC)
# ---------------------------------------------------------------------------

def analyze_userlist_cycles(data: dict) -> dict:
    """Analyze userlist.yaml data and return cycle information.

    Builds a directed graph from userlist plugin before/after rules and group
    after rules (every plugin in group G inherits "after X" for each X listed
    on G's after entry, meaning every plugin in group X must load before every
    plugin in G). Runs Tarjan's SCC; any node inside a non-trivial strongly-
    connected component (size ≥ 2, or with a self edge) participates in a cycle.

    Returns a dict:
      {
        "plugins":    set[str] - every plugin in any cycle (lowercased),
        "components": dict[str, frozenset[str]] - plugin → SCC membership,
        "edges":      dict[(u, v), list[dict]] - structured reasons.
      }
    Each edge reason is a dict like:
      {"kind": "plugin", "text": str,
       "owner": raw_name, "field": "after"|"before", "target": raw_name}
      {"kind": "group", "text": str}
    'kind=plugin' entries can be flipped (move target between the owner
    entry's after/before lists). Group reasons are informational.
    """
    plugins = data.get("plugins", []) or []
    groups = data.get("groups", []) or []

    empty = {"plugins": set(), "components": {}, "edges": {}}
    if not plugins and not groups:
        return empty

    # Edge u → v means "u must load before v". Reasons list per edge so the
    # cycle view can explain each cycle edge.
    adj: dict[str, set[str]] = {}
    edges: dict[tuple[str, str], list[dict]] = {}
    display: dict[str, str] = {}

    def _add_edge(u: str, v: str, reason: dict) -> None:
        if not u or not v:
            return
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set())
        edges.setdefault((u, v), []).append(reason)

    # Plugin-level rules. Track display casing from the entries.
    for entry in plugins:
        raw = entry.get("name") or ""
        name = raw.lower()
        if not name:
            continue
        display.setdefault(name, raw)
        adj.setdefault(name, set())
        for other in entry.get("after", []) or []:
            other_l = other.lower()
            display.setdefault(other_l, other)
            _add_edge(other_l, name, {
                "kind": "plugin",
                "text": f"plugin rule: {raw} 'after' {other}",
                "owner": raw,
                "field": "after",
                "target": other,
            })
        for other in entry.get("before", []) or []:
            other_l = other.lower()
            display.setdefault(other_l, other)
            _add_edge(name, other_l, {
                "kind": "plugin",
                "text": f"plugin rule: {raw} 'before' {other}",
                "owner": raw,
                "field": "before",
                "target": other,
            })

    # Group-level rules - expand each group-after into plugin-plugin edges.
    group_members: dict[str, list[str]] = {}
    for entry in plugins:
        name = (entry.get("name") or "").lower()
        grp = entry.get("group")
        if name and grp:
            group_members.setdefault(grp, []).append(name)
    for entry in groups:
        g_name = entry.get("name")
        if not g_name:
            continue
        dests = group_members.get(g_name, [])
        if not dests:
            continue
        for after_group in entry.get("after", []) or []:
            sources = group_members.get(after_group, [])
            for u in sources:
                for v in dests:
                    _add_edge(u, v, {
                        "kind": "group",
                        "text": (
                            f"group rule: '{g_name}' after '{after_group}' "
                            f"({display.get(u, u)} in '{after_group}' → "
                            f"{display.get(v, v)} in '{g_name}')"
                        ),
                    })

    if not adj:
        return empty

    # Tarjan's SCC, iterative.
    index = 0
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    cycle_plugins: set[str] = set()
    components: dict[str, frozenset[str]] = {}

    for start in list(adj.keys()):
        if start in indices:
            continue
        work: list[tuple[str, iter]] = [(start, iter(adj[start]))]
        indices[start] = low[start] = index
        index += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, it = work[-1]
            nxt = next(it, None)
            if nxt is None:
                if low[node] == indices[node]:
                    component: list[str] = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        component.append(w)
                        if w == node:
                            break
                    if len(component) > 1 or node in adj.get(node, ()):
                        frozen = frozenset(component)
                        cycle_plugins.update(component)
                        for w in component:
                            components[w] = frozen
                work.pop()
                if work:
                    parent = work[-1][0]
                    if low[node] < low[parent]:
                        low[parent] = low[node]
                continue
            if nxt not in indices:
                indices[nxt] = low[nxt] = index
                index += 1
                stack.append(nxt)
                on_stack.add(nxt)
                work.append((nxt, iter(adj[nxt])))
            elif nxt in on_stack:
                if indices[nxt] < low[node]:
                    low[node] = indices[nxt]

    # Only keep edges that are fully inside a cycle - irrelevant edges would
    # clutter the cycle view.
    cycle_edges: dict[tuple[str, str], list[dict]] = {}
    for (u, v), reasons in edges.items():
        if u in cycle_plugins and v in cycle_plugins and components.get(u) is components.get(v):
            cycle_edges[(u, v)] = reasons

    return {
        "plugins": cycle_plugins,
        "components": components,
        "edges": cycle_edges,
    }


def userlist_rule_component(data: dict, name_lower: str) -> frozenset[str]:
    """Return every plugin reachable from name_lower through userlist rules
    (treated as undirected). Plugin before/after rules and group-rule
    expansions both count. Includes the starting plugin itself even if it has
    no rules yet. Empty frozenset if the plugin isn't in the userlist at all.
    """
    neigh: dict[str, set[str]] = {}

    def _link(a: str, b: str):
        if not a or not b or a == b:
            return
        neigh.setdefault(a, set()).add(b)
        neigh.setdefault(b, set()).add(a)

    for entry in data.get("plugins", []):
        nm = (entry.get("name") or "").lower()
        if not nm:
            continue
        neigh.setdefault(nm, set())
        for o in entry.get("after", []) or []:
            _link(nm, o.lower())
        for o in entry.get("before", []) or []:
            _link(nm, o.lower())

    group_members: dict[str, list[str]] = {}
    for entry in data.get("plugins", []):
        nm = (entry.get("name") or "").lower()
        grp = entry.get("group")
        if nm and grp:
            group_members.setdefault(grp, []).append(nm)
    for entry in data.get("groups", []):
        g = entry.get("name")
        if not g:
            continue
        dests = group_members.get(g, [])
        for ag in entry.get("after", []) or []:
            sources = group_members.get(ag, [])
            for u in sources:
                for v in dests:
                    _link(u, v)

    if name_lower not in neigh:
        return frozenset()

    seen: set[str] = set()
    stack = [name_lower]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(neigh.get(n, ()))
    return frozenset(seen)


def would_flip_resolve(data: dict, reason: dict, scope: frozenset[str]) -> bool:
    """Return True iff flipping this plugin rule (in isolation) leaves no
    cycle inside `scope`. Uses an in-memory copy of the userlist data."""
    owner = reason.get("owner", "")
    target = reason.get("target", "")
    fld = reason.get("field", "")
    if fld not in ("after", "before") or not owner or not target:
        return False
    owner_l = owner.lower()
    target_l = target.lower()
    other = "before" if fld == "after" else "after"

    # Shallow copy the plugins list; deep copy only the owner entry we mutate.
    sim_plugins = []
    for entry in data.get("plugins", []):
        if (entry.get("name") or "").lower() == owner_l:
            e2 = dict(entry)
            e2[fld] = [t for t in (entry.get(fld, []) or []) if t.lower() != target_l]
            if not e2[fld]:
                e2.pop(fld, None)
            opp = list(entry.get(other, []) or [])
            if not any(t.lower() == target_l for t in opp):
                opp.append(target)
            e2[other] = opp
            sim_plugins.append(e2)
        else:
            sim_plugins.append(entry)
    sim_data = {"plugins": sim_plugins, "groups": data.get("groups", [])}
    sim_info = analyze_userlist_cycles(sim_data)
    return not any(p in sim_info["plugins"] for p in scope)


def flip_plugin_rule(data: dict, owner: str, field_name: str, target: str) -> bool:
    """Move `target` from the given field of `owner`'s userlist entry to the
    opposite field. Mutates `data` in place; returns True if a rule changed
    (caller is responsible for writing the file)."""
    if field_name not in ("after", "before"):
        return False
    owner_lower = owner.lower()
    target_lower = target.lower()
    other = "before" if field_name == "after" else "after"

    for entry in data.get("plugins", []):
        if (entry.get("name") or "").lower() != owner_lower:
            continue
        cur = entry.get(field_name, []) or []
        new_cur = [t for t in cur if t.lower() != target_lower]
        if len(new_cur) == len(cur):
            continue  # target not actually present - nothing to flip
        if new_cur:
            entry[field_name] = new_cur
        else:
            entry.pop(field_name, None)
        opposite = entry.get(other, []) or []
        if not any(t.lower() == target_lower for t in opposite):
            opposite.append(target)
            entry[other] = opposite
        return True
    return False


def build_cycle_scope_data(data: dict, scope: frozenset[str],
                           display: dict[str, str]) -> dict:
    """Assemble everything the cycle view needs for a pinned plugin scope.

    Port of the compute half of Tk _refresh_cycle_overlay_data: every rule
    between scope plugins (cyclic or not), which edges are still cyclic, and
    which plugin rules would - flipped in isolation - resolve every cycle.

    Returns {"scope_edges", "cyclic_edges", "fixable_reasons", "is_broken"}.
    """
    # Re-run analyzer on the fresh data to get cycle membership for the
    # banner + per-edge cyclic flag.
    info = analyze_userlist_cycles(data)
    cycle_plugins = info["plugins"]
    cycle_components = info["components"]

    # All plugin rules between scope plugins (cyclic or not).
    scope_edges: dict[tuple[str, str], list[dict]] = {}
    for entry in data.get("plugins", []):
        raw = entry.get("name") or ""
        name = raw.lower()
        if name not in scope:
            continue
        for other in entry.get("after", []) or []:
            ol = other.lower()
            if ol not in scope:
                continue
            scope_edges.setdefault((ol, name), []).append({
                "kind": "plugin",
                "text": f"plugin rule: {raw} 'after' {other}",
                "owner": raw,
                "field": "after",
                "target": other,
                "id": (name, "after", ol),
            })
        for other in entry.get("before", []) or []:
            ol = other.lower()
            if ol not in scope:
                continue
            scope_edges.setdefault((name, ol), []).append({
                "kind": "plugin",
                "text": f"plugin rule: {raw} 'before' {other}",
                "owner": raw,
                "field": "before",
                "target": other,
                "id": (name, "before", ol),
            })
    # Group rules - informational only. Any group→group rule where both ends
    # include at least one scope plugin shows up here.
    group_members: dict[str, list[str]] = {}
    for entry in data.get("plugins", []):
        name = (entry.get("name") or "").lower()
        grp = entry.get("group")
        if name and grp:
            group_members.setdefault(grp, []).append(name)
    for entry in data.get("groups", []):
        g_name = entry.get("name")
        if not g_name:
            continue
        dests = group_members.get(g_name, [])
        for after_group in entry.get("after", []) or []:
            sources = group_members.get(after_group, [])
            for u in sources:
                for v in dests:
                    if u in scope and v in scope:
                        scope_edges.setdefault((u, v), []).append({
                            "kind": "group",
                            "text": (
                                f"group rule: '{g_name}' after '{after_group}' "
                                f"({display.get(u, u)} in '{after_group}' → "
                                f"{display.get(v, v)} in '{g_name}')"
                            ),
                        })

    # Mark which edges still form part of a cycle so the view can annotate
    # them. Edge (u, v) is cyclic iff u and v share an SCC AND that SCC has
    # size ≥ 2 (or a self-loop - guaranteed by analyzer).
    cyclic_edges: set[tuple[str, str]] = set()
    for (u, v) in scope_edges:
        cu = cycle_components.get(u)
        cv = cycle_components.get(v)
        if cu is not None and cu is cv:
            cyclic_edges.add((u, v))

    is_broken = any(p in cycle_plugins for p in scope)

    # Compute which plugin rules, if flipped in isolation, would leave no
    # cycle inside the scope. These get highlighted as single-flip fixes.
    fixable_reasons: set[tuple[str, str, str]] = set()
    if is_broken:
        # Collect unique plugin rules from cyclic edges - flipping a
        # non-cyclic rule can't break a cycle that doesn't touch it.
        seen: set[tuple[str, str, str]] = set()
        for edge in cyclic_edges:
            for reason in scope_edges.get(edge, []):
                if reason.get("kind") != "plugin":
                    continue
                rid = reason.get("id")
                if rid is None or rid in seen:
                    continue
                seen.add(rid)
                if would_flip_resolve(data, reason, scope):
                    fixable_reasons.add(rid)

    return {
        "scope_edges": scope_edges,
        "cyclic_edges": cyclic_edges,
        "fixable_reasons": fixable_reasons,
        "is_broken": is_broken,
    }


# ---------------------------------------------------------------------------
# Mutations (mutate parsed data in place; caller writes)
# ---------------------------------------------------------------------------

def set_plugin_rules(data: dict, plugin_name: str,
                     after: list[str], before: list[str]) -> None:
    """Replace plugin_name's entry with the given before/after lists (inline
    'Add to userlist' panel semantics - Tk _ul_save). Keeps the existing
    group, defaulting to 'default'."""
    existing = next(
        (e for e in data["plugins"] if e.get("name", "").lower() == plugin_name.lower()),
        {},
    )
    data["plugins"] = [
        e for e in data["plugins"]
        if e.get("name", "").lower() != plugin_name.lower()
    ]
    entry: dict = {"name": plugin_name}
    if after:
        entry["after"] = list(after)
    if before:
        entry["before"] = list(before)
    entry["group"] = existing.get("group") or DEFAULT_GROUP
    data["plugins"].append(entry)


def set_plugin_group(data: dict, plugin_names: list[str], group: str) -> None:
    """Assign each plugin to `group`, preserving any other entry fields
    (Tk _grp_save)."""
    for plugin_name in plugin_names:
        existing = next(
            (e for e in data["plugins"] if e.get("name", "").lower() == plugin_name.lower()),
            None,
        )
        data["plugins"] = [
            e for e in data["plugins"]
            if e.get("name", "").lower() != plugin_name.lower()
        ]
        entry = dict(existing) if existing else {"name": plugin_name}
        entry["name"] = plugin_name
        entry["group"] = group
        data["plugins"].append(entry)


def remove_plugins(data: dict, plugin_names: list[str]) -> None:
    """Drop the given plugins' entries entirely (Tk _remove_plugins_from_userlist)."""
    lower = {n.lower() for n in plugin_names}
    data["plugins"] = [e for e in data["plugins"]
                       if e.get("name", "").lower() not in lower]


def save_plugin_rules_merged(data: dict, plugin_name: str,
                             rules: list[list[str]]) -> None:
    """Replace plugin_name's before/after lists from [[rel, target], ...]
    (Plugin Rules view semantics - Tk LootPluginRulesOverlay._save_current).
    Merges into the existing entry to preserve extra fields; drops the entry
    entirely when nothing meaningful remains."""
    existing = next(
        (e for e in data["plugins"] if e.get("name", "").lower() == plugin_name.lower()),
        {},
    )
    data["plugins"] = [
        e for e in data["plugins"]
        if e.get("name", "").lower() != plugin_name.lower()
    ]

    after_list = [t for rel, t in rules if rel == "after"]
    before_list = [t for rel, t in rules if rel == "before"]

    if after_list or before_list or existing:
        # Merge into the existing entry to preserve extra fields (dirty, tag, etc.)
        entry = dict(existing)
        entry["name"] = plugin_name
        if not entry.get("group"):
            entry["group"] = DEFAULT_GROUP
        # Replace rule lists (clear old ones if now empty)
        if after_list:
            entry["after"] = after_list
        else:
            entry.pop("after", None)
        if before_list:
            entry["before"] = before_list
        else:
            entry.pop("before", None)
        # Only keep the entry if it has rules or a non-default group or extra fields
        has_content = (after_list or before_list
                       or entry.get("group", DEFAULT_GROUP) != DEFAULT_GROUP
                       or any(k not in ("name", "group", "after", "before") for k in entry))
        if has_content:
            data["plugins"].append(entry)


def remove_group(data: dict, group_name: str) -> bool:
    """Rewrite plugin entries after a group is removed (Tk Groups overlay
    _remove_group): plugins in the removed group keep their entry (moved to
    'default') if they have before/after rules, otherwise the entry is
    dropped. Returns True if any plugin entry was touched."""
    if not any(e.get("group", "") == group_name for e in data.get("plugins", [])):
        return False
    new_plugins = []
    for entry in data.get("plugins", []):
        if entry.get("group", "") == group_name:
            has_rules = entry.get("before") or entry.get("after")
            if has_rules:
                entry["group"] = DEFAULT_GROUP
                new_plugins.append(entry)
            # else: drop the entry entirely
        else:
            new_plugins.append(entry)
    data["plugins"] = new_plugins
    return True


# ---------------------------------------------------------------------------
# State snapshot (flag + menu predicates)
# ---------------------------------------------------------------------------

@dataclass
class UserlistState:
    """Everything the plugins panel needs from userlist.yaml in one read."""
    plugins: set[str] = field(default_factory=set)            # names (lower)
    group_map: dict[str, str] = field(default_factory=dict)   # name (lower) → non-default group
    cycle_plugins: set[str] = field(default_factory=set)      # names (lower) in a cycle
    cycle_components: dict[str, frozenset[str]] = field(default_factory=dict)


def read_userlist_state(path: Path | None) -> UserlistState:
    """Load the plugin/group/cycle sets from userlist.yaml (Tk
    _refresh_userlist_set). Missing file → empty state."""
    if path is None or not path.is_file():
        return UserlistState()
    data = parse_userlist(path)
    info = analyze_userlist_cycles(data)
    return UserlistState(
        plugins={e["name"].lower() for e in data["plugins"] if e.get("name")},
        group_map={
            e["name"].lower(): e["group"]
            for e in data["plugins"]
            if e.get("name") and e.get("group") and e["group"] != DEFAULT_GROUP
        },
        cycle_plugins=info["plugins"],
        cycle_components=info["components"],
    )
