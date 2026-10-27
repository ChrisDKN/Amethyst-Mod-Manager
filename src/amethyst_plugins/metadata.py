from __future__ import annotations

import hashlib
from Utils import json_limits as json
import math
import re
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path

from Utils.mods.state import ModStateError


KEY = "plugin_metadata"
MAX_RECORD = 65536
MAX_NAMESPACE = 1048576
MAX_TOTAL = 8388608


def plugin_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", value):
        raise ModStateError("invalid_plugin_metadata", "Invalid plugin namespace ID.")
    return value


def _json(value, depth=0):
    if depth > 16:
        raise ValueError("Metadata nesting exceeds 16 levels.")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError as exc:
            raise ValueError("Metadata text must be valid Unicode.") from exc
        return
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list) and len(value) <= 4096:
        for item in value:
            _json(item, depth + 1)
        return
    if isinstance(value, dict) and len(value) <= 4096 and all(isinstance(k, str) for k in value):
        for key in value:
            _json(key, depth + 1)
        for item in value.values():
            _json(item, depth + 1)
        return
    raise ValueError("Metadata must contain finite JSON values.")


def _encoded(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ModStateError("invalid_plugin_metadata", "Metadata is not valid finite JSON.") from exc


def record(value):
    if (not isinstance(value, dict) or set(value) - {"value", "portable"}
            or not isinstance(value.get("value"), dict) or type(value.get("portable", False)) is not bool):
        raise ModStateError("invalid_plugin_metadata", "Metadata records require an object value and optional boolean portable flag.")
    try:
        _json(value["value"])
    except (ValueError, RecursionError) as exc:
        raise ModStateError("invalid_plugin_metadata", str(exc)) from exc
    result = {"value": deepcopy(value["value"]), "portable": value.get("portable", False)}
    if len(_encoded(result)) > MAX_RECORD:
        raise ModStateError("invalid_plugin_metadata", "A metadata record cannot exceed 64 KiB.")
    return result


def validate_tree(value):
    if not isinstance(value, dict):
        raise ModStateError("invalid_plugin_metadata_state", "Saved plugin metadata is invalid; it was left unchanged.")
    for pid, namespace in value.items():
        plugin_id(pid)
        if not isinstance(namespace, dict) or set(namespace) - {"profile", "mods"} or not isinstance(namespace.get("mods", {}), dict):
            raise ModStateError("invalid_plugin_metadata_state", "Saved plugin metadata namespace is invalid.")
        if namespace.get("profile") is not None:
            record(namespace["profile"])
        for name, item in namespace.get("mods", {}).items():
            if not isinstance(name, str) or not name:
                raise ModStateError("invalid_plugin_metadata_state", "Saved plugin metadata mod name is invalid.")
            record(item)
        if len(_encoded(namespace)) > MAX_NAMESPACE:
            raise ModStateError("invalid_plugin_metadata", "A plugin metadata namespace cannot exceed 1 MiB.")
    if len(_encoded(value)) > MAX_TOTAL:
        raise ModStateError("invalid_plugin_metadata", "Profile plugin metadata cannot exceed 8 MiB.")
    return value


def _state(profile):
    path = Path(profile) / "profile_state.json"
    if path.is_symlink():
        raise ModStateError("unsafe_profile_path", "The profile state is a symbolic link.")
    if not path.exists():
        return {}
    try:
        value = json.read_json(path, max_bytes=16 * 1024 * 1024)
        if not isinstance(value, dict):
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise ModStateError("invalid_plugin_metadata_state", "The profile state is invalid; it was left unchanged.") from exc
    validate_tree(value.get(KEY, {}))
    return value


def read_all(profile):
    return deepcopy(_state(profile).get(KEY, {}))


def _write_state(profile, state):
    from Utils.profiles.state import read_profile_state, write_profile_state
    if not (Path(profile) / "profile_state.json").is_file():
        migrated = read_profile_state(profile)
        migrated.update(state)
        state = migrated
    write_profile_state(profile, state)


@contextmanager
def _locked(profile):
    from Utils.mods.modlist import modlist_lock, parse_modlist_text
    from Utils.operations import acquire_resource_operation
    from Utils.profiles.state import _lock_for
    profile = Path(profile)
    if profile.is_symlink() or not profile.is_dir():
        raise ModStateError("profile_not_found", "The selected profile is unavailable.")
    with acquire_resource_operation(f"plugin-metadata:{profile.resolve()}", "Plugin metadata"):
        with modlist_lock(profile / "modlist.txt"), _lock_for(profile):
            if profile.is_symlink() or not profile.is_dir() or (profile / "modlist.txt").is_symlink():
                raise ModStateError("unsafe_profile_path", "The profile or mod list is unsafe.")
            path = profile / "modlist.txt"
            raw = path.read_bytes() if path.exists() else b""
            entries = parse_modlist_text(raw.decode("utf-8", errors="surrogateescape"))
            yield profile, entries, _state(profile)


def _inventory(profile, pid, entries, state, names=None):
    by_name = {}
    for entry in entries:
        by_name.setdefault(entry.name, []).append(entry)
    if names is None:
        names = [entry.name for entry in entries if not entry.is_separator and not entry.is_group_header
                 and not entry.name.startswith("@group:")]
    if len(set(names)) != len(names):
        raise ModStateError("ambiguous_mod", "The metadata selection contains duplicate names.")
    for name in names:
        matches = by_name.get(name, [])
        if len(matches) != 1:
            raise ModStateError("ambiguous_mod" if matches else "mod_not_found", "A requested metadata mod is not unique in the profile.", {"mod": name})
        if matches[0].is_separator or matches[0].is_group_header or name.startswith("@group:"):
            raise ModStateError("invalid_plugin_metadata", "Metadata applies to real mods, not separators or group headers.", {"mod": name})
    known = [entry.name for entry in entries if not entry.is_separator and not entry.is_group_header]
    namespace = state.get(KEY, {}).get(pid, {})
    digest = hashlib.sha256(_encoded([str(profile.resolve()), pid, known, namespace])).hexdigest()
    return {"profile": profile.name, "plugin_id": pid, "revision": digest,
            "metadata": deepcopy(namespace.get("profile")),
            "mods": {name: deepcopy(namespace.get("mods", {}).get(name)) for name in names}}


def get_metadata(profile, pid, mods=None):
    plugin_id(pid)
    if mods is not None and (not isinstance(mods, list) or len(mods) > 4096 or any(not isinstance(n, str) for n in mods)):
        raise ModStateError("invalid_plugin_metadata", "mods must be an array of at most 4096 names.")
    with _locked(profile) as (profile, entries, state):
        return _inventory(profile, pid, entries, state, mods)


def validate_changes(changes):
    if (not isinstance(changes, dict) or not changes or set(changes) - {"profile", "mods"}
            or "mods" in changes and (not isinstance(changes["mods"], dict) or len(changes["mods"]) > 4096)):
        raise ModStateError("invalid_plugin_metadata", "Supply profile metadata and/or a mods record mapping.")
    result = {}
    if "profile" in changes:
        result["profile"] = None if changes["profile"] is None else record(changes["profile"])
    if "mods" in changes:
        if any(not isinstance(name, str) or not name for name in changes["mods"]):
            raise ModStateError("invalid_plugin_metadata", "Mod metadata must use nonempty mod names.")
        result["mods"] = {name: None if value is None else record(value) for name, value in changes["mods"].items()}
    if len(_encoded(result)) > MAX_NAMESPACE:
        raise ModStateError("invalid_plugin_metadata", "The metadata request cannot exceed 1 MiB.")
    return result


def edit_metadata(profile, pid, changes, revision):
    plugin_id(pid)
    changes = validate_changes(changes)
    with _locked(profile) as (profile, entries, state):
        names = list(changes.get("mods", {}))
        current = _inventory(profile, pid, entries, state, names)
        if current["revision"] != revision:
            raise ModStateError("stale_plugin_metadata", "The mod inventory or plugin metadata changed; read it again.", {"revision": current["revision"]})
        original = deepcopy(state.get(KEY, {}).get(pid, {}))
        namespace = deepcopy(original)
        if "profile" in changes:
            if changes["profile"] is None:
                namespace.pop("profile", None)
            else:
                namespace["profile"] = changes["profile"]
        mods = namespace.setdefault("mods", {})
        for name, item in changes.get("mods", {}).items():
            if item is None:
                mods.pop(name, None)
            else:
                mods[name] = item
        if not mods:
            namespace.pop("mods", None)
        tree = state.setdefault(KEY, {})
        if namespace:
            tree[pid] = namespace
        else:
            tree.pop(pid, None)
        validate_tree(tree)
        changed = original != namespace
        if changed:
            if not tree:
                state.pop(KEY, None)
            _write_state(profile, state)
        return {"changed": changed, **_inventory(profile, pid, entries, state, names)}


def remap(tree, renames=None, drops=()):
    tree = deepcopy(validate_tree(tree))
    for namespace in tree.values():
        mods = namespace.get("mods", {})
        for old, new in (renames or {}).items():
            if old in mods:
                mods[new] = mods.pop(old)
        for name in drops:
            mods.pop(name, None)
        if not mods:
            namespace.pop("mods", None)
    return {pid: namespace for pid, namespace in tree.items() if namespace}


def update_mods(profile, *, renames=None, drops=()):
    from Utils.profiles.state import _lock_for
    with _lock_for(profile):
        state = _state(profile)
        original = state.get(KEY, {})
        updated = remap(original, renames, drops)
        if updated != original:
            if updated:
                state[KEY] = updated
            else:
                state.pop(KEY, None)
            _write_state(profile, state)


def copy_mod_metadata(source, target, source_name, target_name):
    from Utils.profiles.state import _lock_for
    with ExitStack() as stack:
        for path in sorted({Path(source).resolve(), Path(target).resolve()}, key=str):
            stack.enter_context(_lock_for(path))
        source_state, target_state = _state(source), _state(target)
        original = target_state.get(KEY, {})
        updated = remap(original, drops=[target_name])
        for pid, namespace in source_state.get(KEY, {}).items():
            item = namespace.get("mods", {}).get(source_name)
            if item is not None:
                updated.setdefault(pid, {}).setdefault("mods", {})[target_name] = deepcopy(item)
        validate_tree(updated)
        if updated != original:
            if updated:
                target_state[KEY] = updated
            else:
                target_state.pop(KEY, None)
            _write_state(target, target_state)


def portable(tree):
    result = {}
    for pid, namespace in validate_tree(tree).items():
        item = namespace.get("profile")
        kept = {"profile": deepcopy(item)} if item and item.get("portable") is True else {}
        mods = {name: deepcopy(value) for name, value in namespace.get("mods", {}).items() if value.get("portable") is True}
        if mods:
            kept["mods"] = mods
        if kept:
            result[pid] = kept
    return result


def adopt_group_metadata(group, profiles, renames, drops, adds):
    from Utils.profiles.state import _lock_for
    with _lock_for(group):
        state = _state(group)
        original = state.get(KEY, {})
        updated = remap(original, dict(renames), drops)
        members = {}
        for item in adds:
            member = Path(profiles) / item["member"]
            if not member.is_dir():
                continue
            if member not in members:
                members[member] = read_all(member)
            name = item["folder"]
            for pid, namespace in members[member].items():
                value = namespace.get("mods", {}).get(name)
                if value is not None:
                    updated.setdefault(pid, {}).setdefault("mods", {}).setdefault(name, deepcopy(value))
        validate_tree(updated)
        if updated != original:
            if updated:
                state[KEY] = updated
            else:
                state.pop(KEY, None)
            _write_state(group, state)


def import_portable(profile, incoming, renames, names):
    from Utils.profiles.state import _lock_for
    incoming = portable(incoming)
    known = {name.casefold(): name for name in names}
    with _lock_for(profile):
        state = _state(profile)
        original = state.get(KEY, {})
        updated = deepcopy(original)
        for pid, namespace in incoming.items():
            target = updated.setdefault(pid, {})
            if "profile" in namespace:
                target["profile"] = namespace["profile"]
            for name, item in namespace.get("mods", {}).items():
                name = known.get(renames.get(name, name).casefold())
                if name is not None:
                    target.setdefault("mods", {})[name] = item
        updated = {pid: value for pid, value in updated.items() if value}
        validate_tree(updated)
        if updated != original:
            state[KEY] = updated
            _write_state(profile, state)
        return updated != original


def provider_context(plugin, snapshot):
    context = deepcopy({key: value for key, value in snapshot.items()
                        if key not in {"_plugin_metadata", "_plugin_metadata_error"}})
    if "manager_plugins.metadata.get" not in plugin.available_methods:
        context["metadata"] = {}
        return context
    if snapshot.get("_plugin_metadata_error"):
        raise ModStateError("plugin_metadata_unavailable", snapshot["_plugin_metadata_error"])
    tree = snapshot.get("_plugin_metadata")
    if tree is None:
        profile = context.get("profile_path")
        tree = read_all(profile) if profile else {}
    context["metadata"] = deepcopy(tree.get(plugin.id, {}))
    return context
