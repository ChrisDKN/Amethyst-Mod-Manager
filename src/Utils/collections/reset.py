"""Toolkit-neutral collection load-order reset.

Re-applies a Nexus collection's intended order (from its ``collection.json``
manifest) to a profile's ``modlist.txt`` / ``plugins.txt`` / ``loadorder.txt`` /
``userlist.yaml``. This is the logic behind the "Reset load order" action,
shared by desktop and headless callers without importing GUI modules.

Pure I/O - no GUI imports.
"""

from __future__ import annotations

import re
from pathlib import Path

from Utils.atomic_write import write_atomic_text
from Utils.mods.modlist import write_modlist, ModEntry


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

    # Kahn's topological sort - highest priority first
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
    BG3 ship the curator's exact ordered list - `loadOrder[0]` is the first
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
    # multiple nodes are eligible, the one closest to its base-LO slot wins -
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
# LOOT groups / plugin rules → userlist.yaml
# ---------------------------------------------------------------------------


def _apply_collection_groups(profile_dir: Path, collection_schema: dict, log_fn, *, error_sink=None) -> None:
    from copy import deepcopy
    from Utils.plugins.userlist import (
        _plain_data, parse_userlist, parse_userlist_text, serialize_userlist, write_userlist)

    block = collection_schema.get("pluginRules", {})
    if not block:
        return
    path = profile_dir / "userlist.yaml"
    try:
        incoming = parse_userlist_text(serialize_userlist({
            "plugins": block.get("plugins", []), "groups": block.get("groups", [])}))
        rules = [entry for entry in incoming["plugins"]
                 if any(field in entry for field in ("group", "after", "before"))]
        if not incoming["groups"] and not rules:
            return
        if path.is_symlink():
            raise ValueError("The profile userlist cannot be a symlink.")
        data = parse_userlist(path)
        for section in ("plugins", "groups"):
            for values in (data[section], incoming[section]):
                keys = [entry["name"].lower() if section == "plugins" else entry["name"]
                        for entry in values]
                if len(set(keys)) != len(keys):
                    raise ValueError(f"Ambiguous {section} entries in LOOT user rules.")
        groups = {entry["name"]: entry for entry in data["groups"]}
        added_groups = 0
        for definition in incoming["groups"]:
            name = definition["name"]
            if name not in groups:
                groups[name] = {"name": name}
                data["groups"].append(groups[name])
                added_groups += 1
            entry = groups[name]
            if definition.get("after"):
                entry["after"] = deepcopy(definition["after"])
            else:
                entry.pop("after", None)
            if "description" in definition:
                entry["description"] = definition["description"]

        plugins = {entry["name"].lower(): entry for entry in data["plugins"]}
        for rule in rules:
            name = rule["name"].lower()
            if name not in plugins:
                plugins[name] = {"name": rule["name"]}
                data["plugins"].append(plugins[name])
            entry = plugins[name]
            entry["name"] = rule["name"]
            for field in ("before", "after"):
                if rule.get(field):
                    entry[field] = deepcopy(rule[field])
                else:
                    entry.pop(field, None)
            if "group" in rule:
                entry["group"] = rule["group"]
            group = entry.get("group")
            if group and group != "default" and group not in groups:
                groups[group] = {"name": group}
                data["groups"].append(groups[group])
                added_groups += 1

        verified = parse_userlist_text(serialize_userlist(data))
        if _plain_data(verified) != _plain_data(data):
            raise ValueError("Collection user rules could not round-trip.")
        write_userlist(path, data)
        log_fn(f"Collection: wrote {added_groups} group(s) and "
               f"{len(rules)} plugin rule(s) to userlist.yaml.")
    except Exception as exc:
        if error_sink is not None:
            error_sink.append(f"Collection plugin rules: {exc}")
        log_fn(f"Collection: failed to write groups/rules to userlist.yaml: {exc}")


# ---------------------------------------------------------------------------
# Reset load order (neutral body of Tk _run_reset_load_order)
# ---------------------------------------------------------------------------

def resolve_collection_mod_order(profile_dir: Path, manifest: dict,
                                 amethyst_state=None, *, staging_path=None,
                                 collection_record=None) -> list[str]:
    from Nexus.nexus_meta import read_meta
    from Utils.mods.modlist import parse_modlist_text, read_modlist
    from Utils.collections.ownership import read_ownership
    entries = [e for e in read_modlist(profile_dir / "modlist.txt") if not e.is_separator]
    installed = {e.name.lower(): e.name for e in entries}
    staging_path = Path(staging_path) if staging_path is not None else profile_dir / "mods"
    if collection_record is not None:
        install_id = collection_record.get("install_id")
        def exclusively_owned(entry):
            ownership = read_ownership(staging_path / entry.name / "meta.ini")
            return bool(install_id and ownership.get("introduced")
                        and ownership.get("installations") == [install_id])
        entries = [entry for entry in entries if exclusively_owned(entry)]
        installed = {e.name.lower(): e.name for e in entries}
    if amethyst_state and amethyst_state.get("modlist_text"):
        bundles = amethyst_state.get("bundles") or {}
        names = []
        for entry in parse_modlist_text(amethyst_state["modlist_text"]):
            if entry.is_separator:
                continue
            name = installed.get(entry.name.lower())
            if name is None and entry.name in bundles:
                for candidate in (bundles.get(entry.name), entry.name):
                    if candidate:
                        clean = re.sub(r"[^\w\s-]", "", str(candidate)).strip().replace(" ", "_") or str(candidate)
                        name = installed.get(clean.lower())
                        if name:
                            break
            if name and name not in names:
                names.append(name)
        return names
    priorities = _resolve_collection_priorities(manifest)
    name_to_fid = {}
    for mod in manifest.get("mods", []):
        source = mod.get("source") or {}
        fid = int(source.get("fileId") or 0)
        for name in (mod.get("name"), source.get("logicalFilename")):
            if name and fid:
                name_to_fid[name.lower()] = fid
    ordered = []
    for entry in entries:
        folder = staging_path / entry.name
        if not folder.is_dir():
            continue
        meta = read_meta(folder / "meta.ini")
        fid = next((fid for fid in (meta.collection_source_file_id, meta.file_id,
                                    name_to_fid.get(entry.name.lower(), 0)) if fid in priorities), 0)
        if fid in priorities:
            ordered.append((priorities[fid], entry.name))
    return [name for _priority, name in sorted(ordered, key=lambda item: item[0])]


def reset_collection_load_order(profile_dir: Path, manifest: dict,
                                log_fn=None, game=None,
                                amethyst_state=None) -> dict:
    """Re-apply *manifest*'s intended order to the profile's modlist.txt +
    plugins.txt + loadorder.txt + userlist.yaml.

    Returns a summary dict ``{"ordered": n, "unordered": m}`` (or
    ``{"error": <reason>}``). Unmatched mods (bundled carry-overs / user-added
    patches) go at the TOP (= highest priority in this manager). Vanilla plugins
    already in loadorder.txt are preserved above the collection's plugins.

    When *game* is provided the plugin write reuses the install path's
    ``_write_collection_plugins`` (build filemap → apply the manifest's enabled
    state to collection-owned plugins → LOOT sort) so the reset matches a
    subsequent manual sort without enabling unselected plugin variants.

    *amethyst_state* (from ``Utils.collections.install.load_amethyst_reset_data``)
    switches the reset to the EXACT exported profile: the archived modlist.txt
    replaces the fileId/priority rebuild (unmatched mods go BELOW the authored
    block, matching the install), the archived plugins.txt/loadorder.txt order
    replaces the LOOT sort, and userlist.yaml lands verbatim. The result dict
    then carries ``"amethyst": True``.
    """
    log = log_fn or (lambda _m: None)
    if not manifest:
        return {"error": "no_manifest"}

    from Utils.profiles.backup import create_load_order_backup
    create_load_order_backup(profile_dir, log_fn=log)

    if amethyst_state and amethyst_state.get("modlist_text"):
        return _reset_from_amethyst(profile_dir, manifest, amethyst_state,
                                    game, log)

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
    if isinstance(manifest.get("plugins"), list) and game is not None:
        # Reuse the install path: build the filemap for the just-written modlist
        # so LOOT and collection plugin-state recovery see the true winners,
        # then LOOT-sort exactly as a fresh install / manual sort would. This
        # keeps the reset from dropping unlisted plugins or writing the raw
        # (unsorted) manifest order - see project_qt_collection_loot_sort.
        try:
            from Utils.collections.install import (
                _write_collection_plugins, _loot_available)
            if getattr(game, "loot_sort_enabled", False) and _loot_available():
                try:
                    from Utils.filegraph.service import FileGraphService
                    _library = FileGraphService.open_library(
                        game, profile_dir, log_fn=log)
                    _library.ensure_ready(profile_dir)
                    _library.open_profile(profile_dir).reconcile(
                        operation_hint={"kind": "collection_reset"})
                except Exception as exc:
                    log(f"Reset load order: Filegraph reconcile before LOOT failed: {exc}")
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


def _reset_from_amethyst(profile_dir: Path, manifest: dict, amethyst_state,
                         game, log) -> dict:
    """Reset to the EXACT exported profile carried in the collection archive's
    Amethyst/ folder: modlist (order/separators/enabled + portable state +
    userlist.yaml) via the install-path apply, plugins via the install path's
    exported-order write (LOOT skipped)."""
    from Utils.collections.install import (
        _apply_amethyst_profile_state, _write_collection_plugins)

    stats = _apply_amethyst_profile_state(
        profile_dir, profile_dir / "modlist.txt", amethyst_state, log,
        staging_root=(game.get_effective_mod_staging_path()
                      if game is not None else None))

    if isinstance(manifest.get("plugins"), list) and game is not None:
        try:
            # Filemap rebuild feeds the phantom-plugin filter and collection
            # member-state recovery (the LOOT sort itself is skipped by the
            # exported order).
            try:
                from Utils.filegraph.service import FileGraphService
                _library = FileGraphService.open_library(
                    game, profile_dir, log_fn=log)
                _library.ensure_ready(profile_dir)
                _library.open_profile(profile_dir).reconcile(
                    operation_hint={"kind": "collection_reset"})
            except Exception as exc:
                log(f"Reset load order: Filegraph reconcile failed: {exc}")
            _write_collection_plugins(
                game, profile_dir, profile_dir / "plugins.txt", manifest,
                overwrite_existing=None, _is_append_run=False,
                log=log, _set_status=lambda _m: None,
                amethyst_state=amethyst_state)
            from Utils.plugins import invalidate_plugins_cache
            invalidate_plugins_cache(profile_dir / "plugins.txt")
            invalidate_plugins_cache(profile_dir / "loadorder.txt")
        except Exception as exc:
            log(f"Reset load order: failed to write plugins.txt: {exc}")

    return {"ordered": stats.get("ordered", 0),
            "unordered": stats.get("leftovers", 0),
            "amethyst": True}
