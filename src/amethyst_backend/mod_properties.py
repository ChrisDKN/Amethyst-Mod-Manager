from __future__ import annotations

import copy
from contextlib import contextmanager, ExitStack
import hashlib
import json
from pathlib import Path

from .jobs import JobError


def _raw(path):
    if path.is_symlink():
        raise JobError("unsafe_profile_path", "A profile state file is a symbolic link.",
                       {"path": str(path)})
    return path.read_bytes() if path.exists() else None


def _record(path):
    from Utils.mods.modlist import parse_modlist_text

    raw = _raw(path / "modlist.txt") or b""
    state_raw = _raw(path / "profile_state.json")
    try:
        state = json.loads(state_raw) if state_raw is not None else {}
        if not isinstance(state, dict):
            raise ValueError()
        legacy = None
        if "profile_settings" in state:
            settings = state["profile_settings"]
        else:
            legacy = _raw(path / "profile_settings.json")
            settings = json.loads(legacy) if legacy is not None else {}
        if not isinstance(settings, dict):
            raise ValueError()
        if any(key in settings and not isinstance(settings[key], bool)
               for key in ("profile_specific_mods", "is_group")):
            raise ValueError()
        if settings.get("is_group") and (
                not settings.get("profile_specific_mods")
                or not isinstance(settings.get("group_members"), list)
                or any(not isinstance(name, str) for name in settings["group_members"])):
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise JobError("invalid_profile_state", "The saved profile configuration is invalid.",
                       {"profile": path.name}) from exc
    entries = parse_modlist_text(raw.decode("utf-8", errors="surrogateescape"))
    return {"path": path, "raw": raw, "state": state, "settings": settings,
            "entries": entries, "inputs": [raw.hex(), (state_raw or b"").hex(), (legacy or b"").hex()]}


@contextmanager
def _profiles(game, requested):
    from Utils.mods.modlist import modlist_lock
    from Utils.profiles.state import _lock_for

    root = Path(game.get_profile_root()) / "profiles"
    if requested.parent != root or requested.is_symlink() or not requested.is_dir():
        raise JobError("profile_not_found", "The selected profile is unavailable.")
    paths = sorted((path for path in root.iterdir() if path.is_dir() and not path.is_symlink()), key=str)
    records, errors = {}, {}
    with ExitStack() as locks:
        for path in paths:
            locks.enter_context(modlist_lock(path / "modlist.txt"))
        for path in paths:
            locks.enter_context(_lock_for(path))
        for path in paths:
            try:
                records[path.name] = _record(path)
            except (OSError, JobError) as exc:
                errors[path.name] = {"name": getattr(exc, "name", "resource_unavailable"),
                                     "message": str(exc)}
        if requested.name not in records:
            error = errors.get(requested.name, {})
            raise JobError(error.get("name", "profile_not_found"),
                           error.get("message", "The selected profile is unavailable."),
                           {"profile": requested.name})
        yield records, errors


def _entry(record, name):
    matches = [entry for entry in record["entries"] if entry.name == name]
    if len(matches) != 1:
        raise JobError("ambiguous_mod" if matches else "mod_not_found",
                       "The mod must appear exactly once in its profile.",
                       {"profile": record["path"].name, "mod": name, "matches": len(matches)})
    if matches[0].is_separator or matches[0].is_group_header or name.startswith("@group:"):
        raise JobError("mod_properties_unavailable", "Properties apply to real installed mods.")
    return matches[0]


def _staging(game, record):
    if record["settings"].get("profile_specific_mods", False):
        root = record["path"] / "mods"
        if root.is_symlink():
            raise JobError("unsafe_mod_path", "A private staging root is a symbolic link.")
        return root
    return Path(game.get_mod_staging_path())


def _context(game, profile, mod, records, errors):
    from Utils.profiles.groups import owner_of

    requested = records[profile.name]
    _entry(requested, mod)
    owner, owner_mod = requested, mod
    folder = _staging(game, requested) / mod
    if requested["settings"].get("is_group") and folder.is_symlink():
        identity = owner_of(profile, mod)
        if (identity is None or identity[0] not in requested["settings"].get("group_members", [])
                or identity[0] not in records):
            raise JobError("profile_group_owner_stale", "The Profile Group's source mod is unavailable.")
        owner, owner_mod = records[identity[0]], identity[1]
        if owner["settings"].get("is_group") or not owner["settings"].get("profile_specific_mods"):
            raise JobError("profile_group_owner_stale", "The Profile Group's source is not a private member profile.")
        _entry(owner, owner_mod)
        source = _staging(game, owner) / owner_mod
        if folder.resolve() != source.resolve():
            raise JobError("unsafe_mod_path", "The Profile Group link no longer points at its source mod.")
        folder = source
    if folder.is_symlink() or not folder.is_dir():
        raise JobError("mod_files_missing", "The staged mod folder is unavailable.",
                       {"mod": mod, "path": str(folder)})
    aliases = []
    routing_errors = dict(errors)
    for name, record in records.items():
        try:
            staging = _staging(game, record)
            if staging.resolve() == folder.parent.resolve():
                names = [entry.name for entry in record["entries"]
                         if not entry.is_separator and entry.name == owner_mod]
            elif record["settings"].get("is_group") and not owner["settings"].get("is_group"):
                names = [entry.name for entry in record["entries"] if not entry.is_separator
                         and owner["path"].name in record["settings"].get("group_members", [])
                         and owner_of(record["path"], entry.name) == (owner["path"].name, owner_mod)]
            else:
                names = []
            if len(names) != len(set(names)):
                raise JobError("ambiguous_mod", "An affected profile has duplicate mod entries.")
            if names:
                aliases.append({"profile": name, "mods": names})
        except (OSError, JobError) as exc:
            routing_errors[name] = {"name": getattr(exc, "name", "resource_unavailable"), "message": str(exc)}
    return owner, owner_mod, folder, aliases, routing_errors


def _snapshot(game, profile, mod, records, errors, *, ignore_download_history=False):
    from Utils.mods.metadata import meta_file_identity
    from Utils.mods.properties import read_properties

    owner, owner_mod, folder, aliases, routing_errors = _context(game, profile, mod, records, errors)
    path = folder / "meta.ini"
    if path.is_symlink():
        raise JobError("unsafe_mod_path", "The mod metadata is a symbolic link.")
    token = meta_file_identity(path) if path.exists() else None
    raw = path.read_bytes() if token is not None else None
    values = read_properties(path, is_bg3=getattr(game, "game_id", "") == "baldurs_gate_3")
    latest = meta_file_identity(path) if path.exists() else None
    if token != latest:
        raise JobError("stale_mod_properties", "Mod metadata changed; read its properties again.")
    result = {"profile": profile.name, "mod": mod, "owner_profile": owner["path"].name,
              "owner_mod": owner_mod, "path": str(path),
              "scope": "profile" if owner["settings"].get("profile_specific_mods") else "shared",
              "affected_profiles": aliases, "routing_scan_errors": routing_errors, **values}
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=True).encode())
    inputs = {name: list(record["inputs"]) for name, record in records.items()}
    if ignore_download_history:
        for name, record in records.items():
            inputs[name][1] = json.dumps({key: value for key, value in record["state"].items()
                                         if key != "download_install_history"}, sort_keys=True)
    digest.update(json.dumps(inputs,
                             sort_keys=True).encode())
    digest.update(json.dumps(token).encode())
    digest.update(b"<missing>" if raw is None else raw)
    result["revision"] = digest.hexdigest()
    return result, path


def get_properties(game, profile, mod, *, ignore_download_history=False):
    from Utils.mods.metadata import meta_file_lock

    with _profiles(game, profile) as (records, errors):
        _, _, folder, _, _ = _context(game, profile, mod, records, errors)
        with meta_file_lock(folder / "meta.ini"):
            return _snapshot(game, profile, mod, records, errors,
                             ignore_download_history=ignore_download_history)[0]


def set_properties(game, profile, mod, revision, changes, log, progress):
    from Utils.deployment.locking import coordinated_game_mutation
    from Utils.mods.metadata import meta_file_lock
    from Utils.mods.properties import edit_properties, validate_changes
    from Utils.mods.state import _reconcile_filegraph

    validate_changes(changes)
    with coordinated_game_mutation(game, "headless.mod_properties"):
        progress(0, 2, "Saving mod properties")
        with _profiles(game, profile) as (records, errors):
            _, _, folder, _, _ = _context(game, profile, mod, records, errors)
            with meta_file_lock(folder / "meta.ini"):
                current, path = _snapshot(game, profile, mod, records, errors)
                if current["revision"] != revision:
                    raise JobError("stale_mod_properties", "Mod properties or ownership changed; read them again.",
                                   {"revision": current["revision"]})
                if "root_folder" in changes and current["routing_scan_errors"]:
                    raise JobError("mod_properties_routing_unavailable", "Could not inspect every profile affected by routing.",
                                   {"profiles": current["routing_scan_errors"]})
                saved = edit_properties(path, changes, is_bg3=getattr(game, "game_id", "") == "baldurs_gate_3")
                result = _snapshot(game, profile, mod, records, errors)[0]
        synced, failures = [], []
        if "root_folder" in changes:
            progress(1, 2, "Refreshing root-folder routing")
            for affected in result["affected_profiles"]:
                target = Path(game.get_profile_root()) / "profiles" / affected["profile"]
                try:
                    if target.is_symlink() or not target.is_dir():
                        raise JobError("unsafe_profile_path", "An affected profile is unavailable.")
                    selected = copy.copy(game)
                    selected.set_active_profile_dir(target)
                    selected.load_paths(read_only=True)
                    reconciled, generation = _reconcile_filegraph(
                        selected, target, kind="full", mod_names=affected["mods"], log=log, progress=progress)
                    synced.append({**affected, "reconciled": reconciled, "generation": generation})
                except Exception as exc:
                    failures.append({"profile": target.name, "reason": str(exc)})
            if failures:
                raise JobError("mod_properties_sync_failed",
                               "The properties were saved, but some profile routing could not refresh.",
                               {"state_persisted": True, "properties": result,
                                "changed_fields": saved["changed_fields"], "synced_profiles": synced,
                                "failures": failures})
        progress(2, 2, "Mod properties saved")
        log("Saved mod properties." if saved["changed"] else "Mod properties already matched the request.")
        return {"ok": True, "changed": saved["changed"], "changed_fields": saved["changed_fields"],
                "properties": result, "synced_profiles": synced}


def get_properties_batch(game, profile, mods=None):
    from Utils.mods.metadata import meta_file_lock
    from Utils.mods.state import ModStateError
    from Utils.operations import acquire_game_operation
    from .batch import error_record

    items = []
    with acquire_game_operation(game, "mods.properties.get.batch"), _profiles(game, profile) as (records, errors):
        names = mods if mods is not None else [entry.name for entry in records[profile.name]["entries"]
                                                if not entry.is_separator and not entry.is_group_header]
        for mod in names:
            try:
                _, _, folder, _, _ = _context(game, profile, mod, records, errors)
                with meta_file_lock(folder / "meta.ini"):
                    value, _path = _snapshot(game, profile, mod, records, errors)
                items.append({"mod": mod, "state": "succeeded", "properties": value})
            except (OSError, JobError, ModStateError) as exc:
                items.append({"mod": mod, "state": "failed", "error": error_record(exc)})
    return {"ok": all(item["state"] == "succeeded" for item in items),
            "profile": profile.name, "items": items}


def set_properties_batch(game, profile, items, log, progress):
    from Utils.atomic_write import write_atomic
    from Utils.deployment.locking import coordinated_game_mutation
    from Utils.mods.metadata import meta_file_lock
    from Utils.mods.properties import edit_properties, validate_changes
    from Utils.mods.state import _reconcile_filegraph

    for item in items:
        validate_changes(item["changes"])
    with coordinated_game_mutation(game, "headless.mod_properties.batch"):
        saved_items, affected = [], {}
        with _profiles(game, profile) as (records, errors):
            paths = {}
            for item in items:
                _, _, folder, _, _ = _context(game, profile, item["mod"], records, errors)
                paths[item["mod"]] = folder / "meta.ini"
            with ExitStack() as locks:
                for path in sorted(set(paths.values()), key=str):
                    locks.enter_context(meta_file_lock(path))
                snapshots = {}
                changes_by_path = {}
                for item in items:
                    mod, changes = item["mod"], item["changes"]
                    value, path = _snapshot(game, profile, mod, records, errors)
                    if value["revision"] != item["revision"]:
                        raise JobError("stale_mod_properties", "Mod properties or ownership changed; read them again.", {"mod": mod, "revision": value["revision"]})
                    if "root_folder" in changes and value["routing_scan_errors"]:
                        raise JobError("mod_properties_routing_unavailable", "Could not inspect every affected profile.", {"mod": mod, "profiles": value["routing_scan_errors"]})
                    if path in changes_by_path and changes_by_path[path] != changes:
                        raise JobError("batch_owner_collision", "Two selected aliases request different changes to the same mod.", {"mod": mod})
                    changes_by_path[path] = changes
                    snapshots[mod] = value
                previous = {path: _raw(path) for path in changes_by_path}
                written = []
                saved = {}
                try:
                    for index, (path, changes) in enumerate(changes_by_path.items()):
                        written.append(path)
                        saved[path] = edit_properties(path, changes, is_bg3=getattr(game, "game_id", "") == "baldurs_gate_3")
                        progress(index + 1, len(changes_by_path), "Saving mod properties")
                    for item in items:
                        mod = item["mod"]
                        result = saved[paths[mod]]
                        value, _path = _snapshot(game, profile, mod, records, errors)
                        saved_items.append({"mod": mod, "state": "succeeded", "properties": value,
                                            "changed": result["changed"], "changed_fields": result["changed_fields"]})
                        if "root_folder" in result["changed_fields"]:
                            for alias in snapshots[mod]["affected_profiles"]:
                                affected.setdefault(alias["profile"], set()).update(alias["mods"])
                except Exception as exc:
                    failures = []
                    for path in reversed(written):
                        try:
                            if previous[path] is None:
                                path.unlink(missing_ok=True)
                            else:
                                write_atomic(path, previous[path])
                        except OSError as rollback:
                            failures.append({"path": str(path), "reason": str(rollback)})
                    raise JobError("mod_properties_batch_save_failed", "The property batch could not be saved.",
                                   {"reason": str(exc), "rolled_back": not failures,
                                    "state_persisted": bool(failures), "rollback_failures": failures}) from exc
        synced, failures = [], []
        for name, names in sorted(affected.items()):
            target = Path(game.get_profile_root()) / "profiles" / name
            try:
                if not target.is_dir() or target.is_symlink():
                    raise JobError("unsafe_profile_path", "An affected profile is unavailable.")
                selected = copy.copy(game)
                selected.set_active_profile_dir(target)
                selected.load_paths(read_only=True)
                reconciled, generation = _reconcile_filegraph(selected, target, kind="full", mod_names=sorted(names), log=log, progress=progress)
                synced.append({"profile": name, "mods": sorted(names), "reconciled": reconciled, "generation": generation})
            except Exception as exc:
                failures.append({"profile": name, "reason": str(exc)})
        result = {"ok": not failures, "profile": profile.name, "items": saved_items, "synced_profiles": synced}
        if failures:
            raise JobError("mod_properties_sync_failed", "The properties were saved, but routing could not refresh.", {"state_persisted": True, **result, "failures": failures})
        return result
