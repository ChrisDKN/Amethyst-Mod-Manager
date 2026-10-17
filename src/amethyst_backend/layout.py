from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .jobs import JobError


def _name(value):
    from Utils.mods.install import validate_mod_folder_name
    if not isinstance(value, str) or not value or value != value.strip() or any(ord(c) < 32 for c in value):
        raise JobError("invalid_layout", "Layout names must be non-empty safe names.")
    try:
        validate_mod_folder_name(value)
    except ValueError as exc:
        raise JobError("invalid_layout", str(exc)) from exc
    return value


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - {"collapsed", "locked", "color", "deploy_path"}:
        raise JobError("invalid_layout", "Unsupported separator settings.")
    result = dict(value)
    for key in ("collapsed", "locked"):
        if key in result and type(result[key]) is not bool:
            raise JobError("invalid_layout", f"'{key}' must be boolean.")
    color = result.get("color")
    if color is not None:
        if not isinstance(color, str) or len(color) != 7 or color[0] != "#" or any(c not in "0123456789abcdefABCDEF" for c in color[1:]):
            raise JobError("invalid_layout", "Separator color must be #rrggbb or null.")
        result["color"] = color.lower()
    deploy = result.get("deploy_path")
    if deploy is not None:
        if (not isinstance(deploy, dict) or set(deploy) != {"path", "raw", "mode", "merge"}
                or type(deploy["raw"]) is not bool or type(deploy["merge"]) is not bool
                or not isinstance(deploy["mode"], str) or deploy["mode"] not in ("", "hardlink", "symlink")
                or not isinstance(deploy["path"], str)):
            raise JobError("invalid_layout", "Deploy settings require path, raw, mode and merge.")
        path = deploy["path"]
        if (path and not Path(path).is_absolute()) or any(ord(c) < 32 or 0xd800 <= ord(c) <= 0xdfff for c in path):
            raise JobError("invalid_layout", "A deploy path must be absolute or empty.")
        result["deploy_path"] = dict(deploy) if any(deploy.values()) else None
    return result


def _routing(entries, settings):
    current = None
    result = {}
    for entry in entries:
        if entry.is_separator:
            current = settings[entry.name].get("deploy_path")
        else:
            result[entry.name] = current
    return result


def _prepare(game, profile, request, log, progress):
    from Utils.mods import groups as grouping
    from Utils.mods.modlist import ModEntry, parse_modlist_text
    from Utils.mods.separators import separator_settings
    from Utils.profiles import state as profile_state

    if not profile.is_dir() or profile.is_symlink():
        raise JobError("profile_not_found", "The selected profile is unavailable.")
    for name in ("modlist.txt", "profile_state.json"):
        if (profile / name).is_symlink():
            raise JobError("unsafe_profile_path", "A profile state file is a symbolic link.", {"path": str(profile / name)})
    raw = (profile / "modlist.txt").read_bytes() if (profile / "modlist.txt").is_file() else b""
    state_path = profile / "profile_state.json"
    try:
        state = json.loads(state_path.read_text()) if state_path.is_file() else {}
        if not isinstance(state, dict):
            raise ValueError("Expected a profile state object")
    except (ValueError, UnicodeError) as exc:
        raise JobError("invalid_profile_state", "The saved profile state is invalid.") from exc
    original = parse_modlist_text(raw.decode("utf-8", "surrogateescape"))
    by_name = {entry.name: entry for entry in original}
    if len(by_name) != len(original):
        raise JobError("ambiguous_mod", "The current layout contains duplicate names.")
    old_settings = {entry.name: separator_settings(profile, entry.name, state) for entry in original if entry.is_separator}
    old_groups = grouping.normalize_groups(profile_state.read_mod_groups(profile, state), original)
    updated, settings, sources = [], {}, {}
    for item in request["entries"]:
        if isinstance(item, str):
            if item not in by_name:
                raise JobError("layout_entry_not_found", "An existing layout entry is unavailable.", {"entry": item})
            entry = copy.copy(by_name[item])
            if entry.is_separator:
                settings[item] = old_settings[item]
                sources[item] = item
        elif isinstance(item, dict) and not set(item) - {"separator", "from", "settings"} and "separator" in item:
            name = _name(item["separator"])
            internal = _name(name + "_separator")
            source = item.get("from", internal if internal in old_settings else None)
            if source is not None:
                _name(source)
            if source is not None and source not in old_settings:
                raise JobError("separator_not_found", "The source separator is unavailable.", {"separator": source})
            if internal in by_name and (not by_name[internal].is_separator or internal != source):
                raise JobError("layout_name_collision", "A separator name collides with an existing entry.", {"separator": internal})
            entry = ModEntry(internal, True, True, True)
            defaults = old_settings[source] if source is not None else {"collapsed": False, "locked": False, "color": None, "deploy_path": None}
            settings[internal] = {**defaults, **validate_settings(item.get("settings", {}))}
            if source is not None:
                sources[internal] = source
        else:
            raise JobError("invalid_layout", "Each layout entry must be an existing name or a separator object.")
        updated.append(entry)
    names = [entry.name for entry in updated]
    if len(set(names)) != len(names) or len(set(sources.values())) != len(sources):
        raise JobError("invalid_layout", "Layout entries and separator sources must be unique.")
    displays = [entry.display_name.casefold() for entry in updated]
    if len(set(displays)) != len(displays):
        raise JobError("layout_name_collision", "Layout display names must be unique.")
    old_order = [entry.name for entry in original if not entry.is_separator]
    new_order = [entry.name for entry in updated if not entry.is_separator]
    if set(new_order) != set(old_order) or len(new_order) != len(old_order):
        raise JobError("invalid_layout", "Every existing real mod must appear exactly once.")
    old_positions = {name: index for index, name in enumerate(old_order)}
    new_positions = {name: index for index, name in enumerate(new_order)}
    for entry in original:
        if entry.locked and not entry.is_separator and old_positions[entry.name] != new_positions[entry.name]:
            raise JobError("mod_locked", "A locked mod cannot be moved.", {"mod": entry.name})
    old_prefix = 0
    for entry in original:
        if not entry.is_separator:
            old_prefix += 1
            continue
        if not old_settings[entry.name]["locked"]:
            continue
        destination = next((name for name, source in sources.items() if source == entry.name), None)
        new_prefix = sum(not e.is_separator for e in updated[:names.index(destination)]) if destination else -1
        if destination != entry.name or old_prefix != new_prefix:
            raise JobError("separator_locked", "A locked separator cannot be moved, renamed or removed.", {"separator": entry.name})
    groups = request.get("groups", old_groups)
    if not isinstance(groups, dict):
        raise JobError("invalid_layout", "'groups' must be a complete group object.")
    for key, value in groups.items():
        if (not isinstance(key, str) or len(key) > 256 or any(ord(c) < 32 for c in key)
                or not isinstance(value, dict)
                or set(value) - {"members", "collapsed", "title"}
                or not isinstance(value.get("members"), list)
                or not all(isinstance(member, str) for member in value["members"])
                or len(set(value["members"])) != len(value["members"])
                or ("collapsed" in value and type(value["collapsed"]) is not bool)
                or ("title" in value and (not isinstance(value["title"], str) or not value["title"].strip()
                                          or len(value["title"]) > 256 or any(ord(c) < 32 for c in value["title"])))):
            raise JobError("invalid_layout", "Invalid mod group definition.")
        if "title" in value and not key.startswith("@group:"):
            raise JobError("invalid_layout", "Named group headers require an @group: identifier.")
    normalized = grouping.normalize_groups(groups, updated)
    if set(normalized) != set(groups) or any(set(normalized[key]["members"]) != set(groups[key]["members"]) for key in normalized):
        raise JobError("layout_splits_mod_group", "Groups must contain unique real mods in contiguous blocks.")
    if [e.name for e in grouping.prioritize_leaders(updated, normalized)] != names:
        raise JobError("invalid_layout", "Each real group leader must precede its members.")
    old_routing, new_routing = _routing(original, old_settings), _routing(updated, settings)
    routing_changed = [name for name in old_order if old_routing[name] != new_routing[name]]
    conflicts_revision = None
    if request.get("preserve_conflicts", True):
        if routing_changed:
            raise JobError("layout_routing_changed", "Preserving conflicts requires unchanged separator routing for every mod.", {"mods": routing_changed})
        from .conflicts import overview, profile_snapshot, snapshot_revision
        with profile_snapshot(game, profile, log) as (selected, entries, snapshot):
            _items, relationships = overview(selected, entries, snapshot, progress=progress)
            conflicts_revision = snapshot_revision(snapshot)
        reversed_pairs = [row for row in relationships if row["winner"] in old_positions and row["loser"] in old_positions
                          and (old_positions[row["winner"]] < old_positions[row["loser"]]) != (new_positions[row["winner"]] < new_positions[row["loser"]])]
        if reversed_pairs:
            raise JobError("layout_conflict_order_changed", "The layout reverses existing conflicting mods.", {"relationships": reversed_pairs})
    next_state = copy.deepcopy(state)
    next_state["mod_groups"] = normalized
    next_state["collapsed_seps"] = sorted(name.removesuffix("_separator") for name, value in settings.items() if value["collapsed"])
    next_state["separator_locks"] = {name.removesuffix("_separator"): True for name, value in settings.items() if value["locked"]}
    next_state["separator_colors"] = {name: value["color"] for name, value in settings.items() if value["color"] is not None}
    next_state["separator_deploy_paths"] = {name: value["deploy_path"] for name, value in settings.items() if value["deploy_path"] is not None}
    inputs = {"raw": raw.hex(), "state": state, "groups": old_groups, "settings": old_settings,
              "request": request, "conflicts_revision": conflicts_revision}
    revision = hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    public = {"ok": True, "profile": profile.name, "plan_revision": revision, "entries": names,
              "groups": normalized, "separators": settings, "routing_changed": routing_changed,
              "preserve_conflicts": request.get("preserve_conflicts", True), "conflicts_revision": conflicts_revision,
              "moved": [name for name in old_order if old_positions[name] != new_positions[name]],
              "changed": updated != original or next_state != state}
    return public, updated, next_state, raw


def runner(game, profile, request, plan_revision=None):
    def run(log, progress):
        from Utils.atomic_write import write_atomic
        from Utils.deployment.locking import coordinated_game_mutation
        from Utils.mods.modlist import modlist_lock, write_modlist
        from Utils.mods.state import _reconcile_filegraph
        from Utils.profiles import state as profile_state

        with coordinated_game_mutation(game, "mods.layout"):
            selected = copy.copy(game)
            selected.set_active_profile_dir(profile)
            selected.load_paths(read_only=True)
            path = profile / "modlist.txt"
            with modlist_lock(path), profile_state._lock_for(profile), profile_state.read_only_profile_state():
                plan, entries, state, raw = _prepare(selected, profile, request, log, progress)
                if plan_revision is None:
                    return plan
                if plan["plan_revision"] != plan_revision:
                    raise JobError("stale_layout", "The layout plan changed; preview it again.", {"plan": plan})
                if not plan["changed"]:
                    return {**plan, "applied": True, "filegraph_reconciled": False}
                state_path = profile / "profile_state.json"
                previous = state_path.read_bytes() if state_path.exists() else None
                existed = path.exists()
                try:
                    write_modlist(path, entries)
                    profile_state.write_profile_state(profile, state)
                except Exception as exc:
                    try:
                        if existed:
                            write_atomic(path, raw)
                        else:
                            path.unlink(missing_ok=True)
                        if previous is not None:
                            write_atomic(state_path, previous)
                        else:
                            state_path.unlink(missing_ok=True)
                    except Exception as rollback:
                        raise JobError("layout_rollback_failed", "Layout recovery failed.", {"state_persisted": True, "reason": str(rollback)}) from exc
                    raise JobError("layout_save_failed", "The layout could not be saved; prior state was restored.", {"rolled_back": True}) from exc
            try:
                reconciled, generation = _reconcile_filegraph(selected, profile, kind="full", mod_names=[], log=log, progress=progress)
            except Exception as exc:
                raise JobError("layout_sync_failed", "The layout was saved, but Filegraph reconciliation failed.", {"state_persisted": True, "plan": plan, "reason": str(exc)}) from exc
            return {**plan, "applied": True, "filegraph_reconciled": reconciled, "filegraph_generation": generation}
    return run
