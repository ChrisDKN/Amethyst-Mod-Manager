from __future__ import annotations

import copy
import hashlib
import json
import stat
from contextlib import contextmanager
from pathlib import Path

from .jobs import JobError


ACTIONS = {
    "set_rules": ({"plugin", "after", "before"}, set()),
    "set_group": ({"plugins", "group"}, set()),
    "remove_plugins": ({"plugins"}, set()),
    "save_group": ({"group", "after"}, {"description"}),
    "remove_group": ({"group"}, set()),
    "flip_rule": ({"plugin", "field", "target"}, set()),
    "clear": ({"confirm"}, set()),
    "reset": ({"confirm"}, set()),
}
MAX_FILE_BYTES = 4 * 1024 * 1024


@contextmanager
def _mutation(game):
    from Utils.deployment.locking import DeploymentBusy, coordinated_game_mutation

    try:
        with coordinated_game_mutation(game, "plugins.userlist.edit"):
            yield
    except DeploymentBusy as exc:
        raise JobError("operation_busy", str(exc), {"game": game.game_id}) from exc


def _read(path):
    if path.is_symlink():
        raise JobError("unsafe_userlist_path", "A userlist path cannot be a symlink.",
                       {"path": str(path)})
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        raise JobError("userlist_unavailable", "The userlist must be a regular file of at most 4 MiB.",
                       {"path": str(path)})
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise JobError("userlist_unavailable", "The userlist exceeds 4 MiB.", {"path": str(path)})
    return raw


def _parse(raw, path):
    from Utils.plugins.userlist import parse_userlist_text

    try:
        data = parse_userlist_text(raw.decode("utf-8-sig") if raw is not None else "")
        for section in ("plugins", "groups"):
            seen = set()
            for entry in data[section]:
                name = entry["name"].lower() if section == "plugins" else entry["name"]
                if name in seen:
                    raise ValueError(f"Ambiguous {section} entry: {entry['name']}")
                seen.add(name)
        return data
    except Exception as exc:
        raise JobError("userlist_invalid", "Could not parse the LOOT userlist.",
                       {"path": str(path), "reason": str(exc)}) from exc


def _profile_identity(profile):
    if profile.is_symlink() or not profile.is_dir():
        raise JobError("profile_not_found", "The selected profile is unavailable.",
                       {"profile": profile.name})
    info = profile.stat()
    return [info.st_dev, info.st_ino]


def _diagnostics(data):
    from Utils.plugins.userlist import analyze_userlist_cycles

    info = analyze_userlist_cycles(data)
    names = {"default"}
    for entry in data["groups"]:
        names.add(entry["name"])
        names.update(entry.get("after", []))
    names = sorted(names)
    group_info = analyze_userlist_cycles({
        "plugins": [{"name": str(index), "group": name} for index, name in enumerate(names)],
        "groups": data["groups"],
    })
    components = sorted({tuple(sorted(value)) for value in info["components"].values()})
    group_components = sorted({
        tuple(sorted(names[int(index)] for index in value))
        for value in group_info["components"].values()
    })
    defined = {"default", *(entry["name"] for entry in data["groups"])}
    referenced = {entry["group"] for entry in data["plugins"] if entry.get("group")}
    referenced.update(target for entry in data["groups"] for target in entry.get("after", []))
    return {
        "plugin_cycles": sorted(info["plugins"]),
        "plugin_components": [list(value) for value in components],
        "plugin_cycle_edges": [
            {"from": source, "to": target, "reasons": reasons}
            for (source, target), reasons in sorted(info["edges"].items())
        ],
        "group_components": [list(value) for value in group_components],
        "groups_not_defined_in_userlist": sorted(referenced - defined),
        "conditional_rules_evaluated": False,
        "includes_masterlist": False,
    }


def _public(profile, path, source_path, source, raw, data, revision):
    def targets(entry, field):
        names = {}
        for item in entry.get(field, []):
            names.setdefault(item.lower(), str(item))
        return list(names.values())

    plugins = []
    for entry in data["plugins"]:
        conditional = []
        for field in ("after", "before"):
            for target in entry.get(field, []):
                metadata = getattr(target, "metadata", None)
                if metadata:
                    conditional.append({"field": field, "target": str(target),
                                        "condition": metadata.get("condition"),
                                        "has_file_metadata": True})
        plugins.append({
            "name": entry["name"], "group": entry.get("group", "default"),
            "after": targets(entry, "after"),
            "before": targets(entry, "before"),
            "file_metadata": conditional,
            "has_other_metadata": any(key not in ("name", "group", "after", "before")
                                      for key in entry),
        })
    groups = [{"name": entry["name"], "after": list(entry.get("after", [])),
               "description": entry.get("description", "")} for entry in data["groups"]]
    return {
        "profile": profile.name, "path": str(path), "exists": raw is not None,
        "source": source, "source_path": str(source_path) if source_path else None,
        "plugins": plugins, "groups": groups, "default_group": "default",
        "diagnostics": _diagnostics(data), "revision": revision,
    }


def _snapshot(game, profile):
    from Utils.config_paths import get_loot_game_dir

    identity = _profile_identity(profile)
    if profile.parent.is_symlink():
        raise JobError("unsafe_userlist_path", "The profile parent cannot be a symlink.")
    path = profile / "userlist.yaml"
    global_path = get_loot_game_dir(str(game.game_id)) / "userlist.yaml"
    raw, fallback = _read(path), _read(global_path)
    source = "profile" if raw is not None else "game" if fallback is not None else None
    source_path = path if raw is not None else global_path if fallback is not None else None
    data = _parse(raw if raw is not None else fallback, source_path or path)
    selected = copy.copy(game)
    selected.set_active_profile_dir(profile)
    if not selected.load_paths(read_only=True):
        raise JobError("game_not_configured", "The selected game's paths are unavailable.")
    configuration = [
        str(selected.get_game_path()), str(selected.get_prefix_path()),
        str(selected.get_effective_mod_staging_path()),
        str(selected.get_profile_root()), bool(selected.loot_sort_enabled),
    ]
    payload = [str(path), identity, configuration, raw.hex() if raw is not None else None,
               str(global_path), fallback.hex() if fallback is not None else None]
    revision = hashlib.sha256(json.dumps(payload, ensure_ascii=True).encode()).hexdigest()
    public = _public(profile, path, source_path, source, raw, data, revision)
    return public, data, raw, identity


def get_userlist(game, profile):
    try:
        return _snapshot(game, profile)[0]
    except JobError:
        raise
    except Exception as exc:
        raise JobError("userlist_unavailable", "Could not inspect the LOOT userlist.",
                       {"profile": profile.name, "reason": str(exc)}) from exc


def _edit(data, options):
    from Utils.plugins.userlist import (
        flip_plugin_rule, remove_group, remove_plugins, set_plugin_group, set_plugin_rules)

    action = options["action"]
    if action == "set_rules":
        set_plugin_rules(data, options["plugin"], options["after"], options["before"])
    elif action == "set_group":
        set_plugin_group(data, options["plugins"], options["group"])
    elif action == "remove_plugins":
        remove_plugins(data, options["plugins"])
    elif action == "save_group":
        name = options["group"]
        entry = next((item for item in data["groups"] if item["name"] == name), None)
        if entry is None:
            entry = {"name": name}
            data["groups"].append(entry)
        if options["after"]:
            entry["after"] = list(options["after"])
        else:
            entry.pop("after", None)
        if "description" in options:
            entry["description"] = options["description"]
    elif action == "remove_group":
        name = options["group"]
        if (not any(item["name"] == name for item in data["groups"])
                and not any(item.get("group") == name for item in data["plugins"])):
            raise JobError("loot_group_not_found", "The selected user group is unavailable.",
                           {"group": name})
        remove_group(data, name)
        data["groups"] = [item for item in data["groups"] if item["name"] != name]
        for entry in data["groups"]:
            if name in entry.get("after", []):
                entry["after"] = [target for target in entry["after"] if target != name]
                if not entry["after"]:
                    entry.pop("after", None)
    elif action == "flip_rule":
        if not flip_plugin_rule(data, options["plugin"], options["field"], options["target"]):
            raise JobError("plugin_rule_not_found", "The selected plugin rule is unavailable.",
                           {"plugin": options["plugin"], "field": options["field"],
                            "target": options["target"]})
    return data


def edit_runner(game, profile, revision, options):
    def run(log, progress):
        from Utils.atomic_write import write_atomic
        from Utils.plugins.userlist import _plain_data, serialize_userlist

        with _mutation(game):
            progress(0, 2, "Reading LOOT user rules")
            current, original, raw, identity = _snapshot(game, profile)
            if current["revision"] != revision:
                raise JobError("stale_plugin_userlist", "The userlist changed; read it again.",
                               {"revision": current["revision"]})
            action = options["action"]
            if action == "clear":
                data = {"plugins": [], "groups": []}
                after = b"plugins: []\ngroups: []\n"
            elif action == "reset":
                data, after = None, None
            else:
                data = _edit(copy.deepcopy(original), options)
                if _plain_data(data) == _plain_data(original):
                    return {"changed": False, "action": action, "userlist": current}
                text = serialize_userlist(data)
                after = (text or "plugins: []\ngroups: []\n").encode("utf-8")
            if after == raw:
                return {"changed": False, "action": action, "userlist": current}
            if after is not None:
                verified = _parse(after, profile / "userlist.yaml")
                if _plain_data(verified) != _plain_data(data):
                    raise JobError("userlist_invalid", "The edited userlist could not round-trip.")
            if _snapshot(game, profile)[0]["revision"] != revision:
                raise JobError("stale_plugin_userlist", "The userlist changed before saving.")
            path = profile / "userlist.yaml"
            progress(1, 2, "Saving LOOT user rules")
            attempted = False
            try:
                if _profile_identity(profile) != identity or _read(path) != raw:
                    raise JobError("stale_plugin_userlist", "The userlist changed before saving.")
                attempted = True
                if after is None:
                    path.unlink(missing_ok=True)
                else:
                    write_atomic(path, after)
                if _profile_identity(profile) != identity or _read(path) != after:
                    raise OSError("Could not verify the saved userlist.")
                result = get_userlist(game, profile)
            except Exception as exc:
                if not attempted:
                    raise
                try:
                    if _profile_identity(profile) != identity:
                        raise OSError("The profile directory changed during recovery.")
                    _read(path)
                    if raw is None:
                        path.unlink(missing_ok=True)
                    else:
                        write_atomic(path, raw)
                    if _read(path) != raw:
                        raise OSError("Could not verify the restored userlist.")
                except Exception as recovery:
                    raise JobError("plugin_userlist_rollback_failed",
                                   "Saving the userlist failed and recovery was incomplete.",
                                   {"profile": profile.name, "state_persisted": True,
                                    "reason": str(exc), "recovery_error": str(recovery)}) from exc
                raise JobError("plugin_userlist_write_failed",
                               "Could not save the userlist; previous state was restored.",
                               {"profile": profile.name, "rolled_back": True,
                                "reason": str(exc)}) from exc
            log(f"Saved LOOT user rules for {profile.name}.")
            progress(2, 2, "LOOT user rules saved")
            return {"changed": True, "action": action, "userlist": result}
    return run
