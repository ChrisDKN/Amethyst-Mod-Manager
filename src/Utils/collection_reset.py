"""Toolkit-neutral collection load-order reset.

Re-applies a Nexus collection's intended order (from its ``collection.json``
manifest) to a profile's ``modlist.txt`` / ``plugins.txt`` / ``loadorder.txt`` /
``userlist.yaml``. This is the logic behind the "Reset load order" action,
extracted VERBATIM from the Tk ``gui/collections_dialog.py`` (the priority
resolver + userlist writer + the reset body) so the Qt app can call it without
importing the Tk module (which would drag in tkinter).

Pure I/O — no GUI imports.
"""

from __future__ import annotations

import re
from pathlib import Path

from Utils.atomic_write import write_atomic_text
from Utils.modlist import write_modlist, ModEntry


# ---------------------------------------------------------------------------
# Priority resolution (moved verbatim from collections_dialog.py)
# ---------------------------------------------------------------------------

def _topo_sort_collection(schema_mods: list[dict], mod_rules: list[dict]) -> dict[int, int]:
    """Return file_id → priority-position dict respecting modRules before/after constraints.

    Position 0 = highest priority (wins conflicts), higher number = lower priority.
    Falls back to the mods-array order for any mod not constrained by rules.
    Cycles are broken by ignoring the offending edge (Kahn's algorithm skips them naturally).
    """
    # Build logical_name → file_id map from the mods array
    logical_to_fid: dict[str, int] = {}
    fid_order: list[int] = []  # original mods-array order, used as topo fallback
    for m in schema_mods:
        src = m.get("source") or {}
        fid = src.get("fileId")
        if fid is None:
            continue
        fid = int(fid)
        logical = (src.get("logicalFilename") or m.get("name") or "").strip()
        if logical:
            logical_to_fid[logical] = fid
        if fid not in fid_order:
            fid_order.append(fid)

    # Reverse so that mods[-1] (last installed = highest priority in collection.json)
    # gets position 0 → top of modlist.txt (highest priority in the manager).
    # Without this, mods[0] (lowest priority) would incorrectly end up at the top.
    fid_order = list(reversed(fid_order))

    all_fids: set[int] = set(fid_order)

    # edges: higher_priority_fid → {lower_priority_fids}
    # "source after reference"  → source loads last, so SOURCE has the higher
    #                             priority (it overwrites reference)
    # "source before reference" → reference loads last, so it wins instead
    higher_than: dict[int, set[int]] = {f: set() for f in all_fids}  # fid → fids it beats
    in_degree: dict[int, int] = {f: 0 for f in all_fids}

    def _resolve(name: str) -> "int | None":
        return logical_to_fid.get(name)

    for rule in mod_rules:
        rtype = rule.get("type")
        if rtype not in ("before", "after"):
            continue
        ref_name = (rule.get("reference") or {}).get("logicalFileName", "")
        src_name = (rule.get("source") or {}).get("logicalFileName", "")
        ref_fid = _resolve(ref_name)
        src_fid = _resolve(src_name)
        if ref_fid is None or src_fid is None or ref_fid == src_fid:
            continue

        if rtype == "after":
            # source loads after reference → source wins (loads on top of reference)
            winner, loser = src_fid, ref_fid
        else:  # "before"
            # source loads before reference → reference wins
            winner, loser = ref_fid, src_fid

        if loser not in higher_than[winner]:
            higher_than[winner].add(loser)
            in_degree[loser] += 1

    # Kahn's topological sort — highest priority first
    from collections import deque
    queue = deque(f for f in fid_order if in_degree[f] == 0)
    sorted_fids: list[int] = []
    remaining = set(fid_order)

    while queue:
        fid = queue.popleft()
        if fid not in remaining:
            continue
        remaining.discard(fid)
        sorted_fids.append(fid)
        # Process dependents in original-array order for determinism
        for dep in sorted(higher_than[fid], key=lambda f: fid_order.index(f) if f in fid_order else 999999):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    # Append any fids not reached (cycle members) in original order
    for fid in fid_order:
        if fid in remaining:
            sorted_fids.append(fid)

    # sorted_fids[0] = highest priority → position 0
    return {fid: pos for pos, fid in enumerate(sorted_fids)}


def _resolve_collection_priorities(collection_schema: dict) -> dict[int, int]:
    """Return file_id → priority position (0 = highest priority, top of modlist.txt).

    Prefers the manifest's `loadOrder` array when present (FBLO games like
    BG3 ship the curator's exact ordered list — `loadOrder[0]` is the first
    mod to load, which is bottom of modlist.txt / position N-1 in this
    manager). When `loadOrder` is present, `modRules` before/after constraints
    are layered on top as a stable post-pass: rules already satisfied by the
    LO are no-ops; only conflicting rules cause reordering, with the LO order
    used as the tie-breaker. Falls back to pure topo-sorting `mods` +
    `modRules` for collections that don't ship a load order block.
    """
    schema_mods = collection_schema.get("mods", [])
    mod_rules = collection_schema.get("modRules", [])

    lo = collection_schema.get("loadOrder")
    if not (isinstance(lo, list) and lo and any(e.get("fileId") for e in lo)):
        return _topo_sort_collection(schema_mods, mod_rules)

    # Reverse: loadOrder[0] = first to load = lowest priority = bottom of modlist
    ordered_fids: list[int] = []
    seen: set[int] = set()
    for entry in reversed(lo):
        fid = entry.get("fileId")
        if fid is None:
            continue
        fid = int(fid)
        if fid in seen:
            continue
        seen.add(fid)
        ordered_fids.append(fid)

    # Append any mods-array entries missing from loadOrder (FOMOD-only mods
    # with no .pak) at the bottom, preserving mods-array order.
    for m in schema_mods:
        fid = (m.get("source") or {}).get("fileId")
        if fid is None:
            continue
        fid = int(fid)
        if fid not in seen:
            seen.add(fid)
            ordered_fids.append(fid)

    # Build name → fileId resolver for rule references (rules use logicalFileName)
    logical_to_fid: dict[str, int] = {}
    for m in schema_mods:
        src = m.get("source") or {}
        fid = src.get("fileId")
        if fid is None:
            continue
        fid = int(fid)
        for n in (
            (src.get("logicalFilename") or "").strip(),
            (m.get("name") or "").strip(),
        ):
            if n and n not in logical_to_fid:
                logical_to_fid[n] = fid

    # Layer modRules on top via Kahn's topological sort, using the LO order
    # as the tie-breaker. Rules already satisfied by LO are free; conflicting
    # rules cause minimal swaps. Cycles are skipped (rules ignored).
    base_pos: dict[int, int] = {fid: pos for pos, fid in enumerate(ordered_fids)}
    higher_than: dict[int, set[int]] = {f: set() for f in ordered_fids}
    in_degree: dict[int, int] = {f: 0 for f in ordered_fids}

    for rule in mod_rules:
        rtype = rule.get("type")
        if rtype not in ("before", "after"):
            continue
        ref_name = (rule.get("reference") or {}).get("logicalFileName", "")
        src_name = (rule.get("source") or {}).get("logicalFileName", "")
        ref_fid = logical_to_fid.get(ref_name)
        src_fid = logical_to_fid.get(src_name)
        if ref_fid is None or src_fid is None or ref_fid == src_fid:
            continue
        if ref_fid not in base_pos or src_fid not in base_pos:
            continue

        # "source after reference"  → source loads later → source is higher priority
        # "source before reference" → source loads earlier → reference is higher priority
        if rtype == "after":
            winner, loser = src_fid, ref_fid
        else:
            winner, loser = ref_fid, src_fid

        if loser not in higher_than[winner]:
            higher_than[winner].add(loser)
            in_degree[loser] += 1

    # Stable topo sort using a min-heap keyed by base LO position. Whenever
    # multiple nodes are eligible, the one closest to its base-LO slot wins —
    # so rules already satisfied by the LO are no-ops, and only conflicting
    # rules cause the minimum reordering needed to satisfy them.
    import heapq
    heap: list[tuple[int, int]] = [
        (base_pos[f], f) for f in ordered_fids if in_degree[f] == 0
    ]
    heapq.heapify(heap)
    sorted_fids: list[int] = []
    remaining = set(ordered_fids)

    while heap:
        _, fid = heapq.heappop(heap)
        if fid not in remaining:
            continue
        remaining.discard(fid)
        sorted_fids.append(fid)
        for dep in higher_than[fid]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                heapq.heappush(heap, (base_pos[dep], dep))

    # Cycle members: append in base order
    for fid in ordered_fids:
        if fid in remaining:
            sorted_fids.append(fid)

    return {fid: pos for pos, fid in enumerate(sorted_fids)}


# ---------------------------------------------------------------------------
# LOOT groups / plugin rules → userlist.yaml (moved verbatim)
# ---------------------------------------------------------------------------

def _apply_collection_groups(profile_dir: Path, collection_schema: dict, log_fn) -> None:
    """Merge LOOT groups, group ordering rules, and plugin rules from collection.json
    into userlist.yaml.

    Writes:
    - Group definitions (name + after ordering) from schema["groups"]
    - Per-plugin rules: after, before, group from schema["plugins"]

    Existing entries are overwritten with the collection's values so that
    re-running (e.g. Reset Load Order) always reflects the author's intent.
    """
    plugin_rules_block: dict = collection_schema.get("pluginRules", {})
    schema_groups: list[dict] = plugin_rules_block.get("groups", [])
    schema_plugins: list[dict] = plugin_rules_block.get("plugins", [])

    # Build lookup: lower(plugin_name) -> {group, after, before} from schema
    plugin_rules: dict[str, dict] = {}
    for p in schema_plugins:
        name = p.get("name", "")
        if not name:
            continue
        entry: dict = {"name": name}
        if p.get("group"):
            entry["group"] = p["group"]
        if p.get("after"):
            entry["after"] = list(p["after"])
        if p.get("before"):
            entry["before"] = list(p["before"])
        if len(entry) > 1:  # has something beyond just name
            plugin_rules[name.lower()] = entry

    # Nothing to do if the collection defines no groups or plugin rules
    if not schema_groups and not plugin_rules:
        return

    ul_path = profile_dir / "userlist.yaml"

    # Minimal parse/write inline (mirrors PluginPanel._parse_userlist / _write_userlist)
    def _parse(path: Path) -> dict:
        result: dict = {"plugins": [], "groups": []}
        if not path.is_file():
            return result
        text = path.read_text(encoding="utf-8")
        current_section: "str | None" = None
        current_block: list[str] = []

        def _flush(section, block):
            if not block:
                return
            entry: dict = {}
            m = re.match(r"^[\s\-]*name:\s*['\"]?(.*?)['\"]?\s*$", block[0])
            if m:
                entry["name"] = m.group(1)
            for line in block:
                mg = re.match(r"^\s*group:\s*['\"]?(.*?)['\"]?\s*$", line)
                if mg:
                    entry["group"] = mg.group(1)
            for field in ("before", "after"):
                pat = re.compile(r"^\s*" + field + r":\s*$")
                inline = re.compile(r"^\s*" + field + r":\s*\[(.+)\]\s*$")
                items: list[str] = []
                in_list = False
                for line in block:
                    inline_m = inline.match(line)
                    if inline_m:
                        raw_items = inline_m.group(1)
                        items = [i.strip().strip("'\"") for i in raw_items.split(",") if i.strip()]
                        break
                    if pat.match(line):
                        in_list = True
                        continue
                    if in_list:
                        if re.match(r"^\s+\w[\w_]*\s*:", line):
                            in_list = False
                        else:
                            item_m = re.match(r"^\s*-\s*['\"]?(.*?)['\"]?\s*$", line)
                            if item_m:
                                items.append(item_m.group(1))
                if items:
                    entry[field] = items
            if entry.get("name"):
                result[section].append(entry)

        for line in text.splitlines():
            stripped = line.strip()
            if stripped == "plugins:":
                if current_section:
                    _flush(current_section, current_block)
                current_section = "plugins"
                current_block = []
            elif stripped == "groups:":
                if current_section:
                    _flush(current_section, current_block)
                current_section = "groups"
                current_block = []
            elif stripped.startswith("- name:") and current_section:
                if current_block:
                    _flush(current_section, current_block)
                current_block = [line]
            elif current_section and (line.startswith("  ") or line.startswith("\t")):
                current_block.append(line)
        if current_section and current_block:
            _flush(current_section, current_block)
        return result

    def _write(path: Path, data: dict) -> None:
        def _q(s: str) -> str:
            # Use double quotes if the value contains a single quote
            if "'" in s:
                escaped = s.replace('"', '\\"')
                return f'"{escaped}"'
            return f"'{s}'"
        lines: list[str] = []
        plugins = data.get("plugins", [])
        groups = data.get("groups", [])
        if plugins:
            lines.append("plugins:")
            for entry in plugins:
                lines.append(f"  - name: {_q(entry['name'])}")
                for field in ("before", "after"):
                    items = entry.get(field, [])
                    if items:
                        lines.append(f"    {field}:")
                        for item in items:
                            lines.append(f"      - {_q(item)}")
                if entry.get("group"):
                    lines.append(f"    group: {_q(entry['group'])}")
        if groups:
            if lines:
                lines.append("")
            lines.append("groups:")
            for entry in groups:
                lines.append(f"  - name: {_q(entry['name'])}")
                after_items = entry.get("after", [])
                if after_items:
                    lines.append("    after:")
                    for item in after_items:
                        lines.append(f"      - {_q(item)}")
        if lines:
            write_atomic_text(path, "\n".join(lines) + "\n")

    try:
        data = _parse(ul_path)

        # Merge groups — add any that don't already exist
        existing_group_names = {g["name"].lower() for g in data["groups"]}
        added_groups = 0
        for sg in schema_groups:
            gname = sg.get("name", "")
            if not gname or gname.lower() in existing_group_names:
                continue
            g_entry: dict = {"name": gname}
            after = sg.get("after", [])
            if after:
                g_entry["after"] = list(after)
            data["groups"].append(g_entry)
            existing_group_names.add(gname.lower())
            added_groups += 1

        # Auto-create groups referenced by plugins but missing from the groups section
        for rule in plugin_rules.values():
            gname = rule.get("group", "")
            if gname and gname.lower() not in existing_group_names:
                data["groups"].append({"name": gname})
                existing_group_names.add(gname.lower())
                added_groups += 1

        # Merge plugin rules — overwrite existing entries for collection plugins
        existing_plugins: dict[str, dict] = {e["name"].lower(): e for e in data["plugins"]}
        for plugin_lower, rule in plugin_rules.items():
            if plugin_lower in existing_plugins:
                existing_plugins[plugin_lower].update(rule)
            else:
                new_entry = dict(rule)
                data["plugins"].append(new_entry)
                existing_plugins[plugin_lower] = new_entry

        ul_path.parent.mkdir(parents=True, exist_ok=True)
        _write(ul_path, data)
        log_fn(
            f"Collection: wrote {added_groups} group(s) and "
            f"{len(plugin_rules)} plugin rule(s) to userlist.yaml."
        )
    except Exception as exc:
        log_fn(f"Collection: failed to write groups/rules to userlist.yaml: {exc}")


# ---------------------------------------------------------------------------
# Reset load order (neutral body of Tk _run_reset_load_order)
# ---------------------------------------------------------------------------

def reset_collection_load_order(profile_dir: Path, manifest: dict,
                                log_fn=None, game=None) -> dict:
    """Re-apply *manifest*'s intended order to the profile's modlist.txt +
    plugins.txt + loadorder.txt + userlist.yaml.

    Returns a summary dict ``{"ordered": n, "unordered": m}`` (or
    ``{"error": <reason>}``). Unmatched mods (bundled carry-overs / user-added
    patches) go at the TOP (= highest priority in this manager). Vanilla plugins
    already in loadorder.txt are preserved above the collection's plugins.

    When *game* is provided the plugin write reuses the install path's
    ``_write_collection_plugins`` (build filemap → recover staged-but-unlisted
    plugins → LOOT sort) so the reset matches a subsequent manual sort instead of
    dropping unlisted plugins and writing the raw manifest order.
    """
    log = log_fn or (lambda _m: None)
    if not manifest:
        return {"error": "no_manifest"}

    fid_to_pos = _resolve_collection_priorities(manifest)

    # Fallback name → fileId map (mods without a readable meta.ini fileid).
    name_to_fid: dict[str, int] = {}
    for m in manifest.get("mods", []):
        src = m.get("source") or {}
        fid = src.get("fileId")
        if fid is None:
            continue
        fid = int(fid)
        for n in (
            (src.get("logicalFilename") or "").strip(),
            (m.get("name") or "").strip(),
        ):
            if n and n.lower() not in name_to_fid:
                name_to_fid[n.lower()] = fid

    staging_path = profile_dir / "mods"
    if not staging_path.is_dir():
        return {"error": "no_mods"}

    from Nexus.nexus_meta import read_meta

    ordered: list[tuple[int, str]] = []   # (position, folder_name)
    unordered: list[str] = []
    for folder in staging_path.iterdir():
        if not folder.is_dir():
            continue
        fid = 0
        try:
            fid = int(read_meta(folder / "meta.ini").file_id or 0)
        except Exception:
            fid = 0
        if not fid:
            fid = name_to_fid.get(folder.name.lower()) or 0
        if fid and fid in fid_to_pos:
            ordered.append((fid_to_pos[fid], folder.name))
        else:
            unordered.append(folder.name)

    ordered.sort(key=lambda x: x[0])   # position 0 = highest priority → top
    modlist_entries = [
        ModEntry(name=name, enabled=True, locked=False) for name in unordered
    ] + [
        ModEntry(name=name, enabled=True, locked=False) for _, name in ordered
    ]

    if modlist_entries:
        try:
            write_modlist(profile_dir / "modlist.txt", modlist_entries)
            log(f"Reset load order: wrote modlist.txt with "
                f"{len(modlist_entries)} entries")
        except Exception as exc:
            log(f"Reset load order: failed to write modlist.txt: {exc}")

    # Re-write plugins.txt and loadorder.txt.
    schema_plugins: list = manifest.get("plugins", [])
    if schema_plugins and game is not None:
        # Reuse the install path: build the filemap for the just-written modlist
        # so LOOT (and staged-plugin recovery) see the true conflict winners,
        # then LOOT-sort exactly as a fresh install / manual sort would. This
        # keeps the reset from dropping unlisted plugins or writing the raw
        # (unsorted) manifest order — see project_qt_collection_loot_sort.
        try:
            from Utils.collection_install import (
                _write_collection_plugins, _loot_available)
            if getattr(game, "loot_sort_enabled", False) and _loot_available():
                try:
                    from Utils.deploy_pipeline import _build_filemap_for_game
                    _build_filemap_for_game(game, profile_dir.name, log_fn=log)
                except Exception as exc:
                    log(f"Reset load order: filemap rebuild before LOOT failed: {exc}")
            _write_collection_plugins(
                game, profile_dir, profile_dir / "plugins.txt", manifest,
                overwrite_existing=None, _is_append_run=False,
                log=log, _set_status=lambda _m: None)
            from Utils.plugins import invalidate_plugins_cache
            invalidate_plugins_cache(profile_dir / "plugins.txt")
            invalidate_plugins_cache(profile_dir / "loadorder.txt")
        except Exception as exc:
            log(f"Reset load order: failed to write plugins.txt: {exc}")
    elif schema_plugins:
        # Fallback (no game handle): raw manifest write + group rules only.
        try:
            lines = []
            loadorder_lines = []
            for plugin in schema_plugins:
                name = plugin.get("name", "")
                enabled = plugin.get("enabled", True)
                lines.append(("*" if enabled else "") + name)
                loadorder_lines.append(name)
            plugins_path = profile_dir / "plugins.txt"
            from Utils.plugins import _plugins_txt_encoding
            text = "\n".join(lines) + "\n"
            plugins_path.write_text(text, encoding=_plugins_txt_encoding(text))
            log(f"Reset load order: wrote plugins.txt with {len(lines)} plugins")
            # Preserve vanilla plugins already in loadorder.txt (they stay on top)
            loadorder_path = profile_dir / "loadorder.txt"
            collection_lower = {n.lower() for n in loadorder_lines}
            vanilla_prefix: list[str] = []
            if loadorder_path.exists():
                for lo_line in loadorder_path.read_text(encoding="utf-8").splitlines():
                    lo_line = lo_line.strip()
                    if lo_line and lo_line.lower() not in collection_lower:
                        vanilla_prefix.append(lo_line)
            final_loadorder = vanilla_prefix + loadorder_lines
            loadorder_path.write_text("\n".join(final_loadorder) + "\n", encoding="utf-8")
            log(f"Reset load order: wrote loadorder.txt with "
                f"{len(final_loadorder)} plugins ({len(vanilla_prefix)} vanilla)")
            from Utils.plugins import invalidate_plugins_cache
            invalidate_plugins_cache(plugins_path)
            invalidate_plugins_cache(loadorder_path)
        except Exception as exc:
            log(f"Reset load order: failed to write plugins.txt: {exc}")

    # Re-apply LOOT groups and plugin-group assignments from the manifest.
    # (When game is set, _write_collection_plugins already applied them; the
    # reset fallback path and the modlist-only case still need this.)
    if game is None or not schema_plugins:
        _apply_collection_groups(profile_dir, manifest, log)

    return {"ordered": len(ordered), "unordered": len(unordered)}
