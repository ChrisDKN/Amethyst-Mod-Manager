from __future__ import annotations

import copy
import hashlib
import json
import re
from contextlib import ExitStack
from pathlib import Path

from .jobs import JobError


_ORDERS = ("native,builtin", "builtin,native", "native", "builtin", "")
_NAME = re.compile(r"\*?[a-z0-9_][a-z0-9_.-]*")


def _selected_game(game, profile, *, prefix_fallback=True):
    if profile.is_symlink() or not profile.is_dir():
        raise JobError("profile_not_found", "The selected profile is unavailable.")
    selected = copy.copy(game)
    selected._game_path = None
    selected._staging_path = None
    selected.set_active_profile_dir(profile)
    options = {"prefix_fallback": False} if not prefix_fallback else {}
    if not selected.load_paths(read_only=True, **options):
        raise JobError("game_not_configured", "The game's saved paths are unavailable.")
    return selected


def _paths(selected):
    from Utils.wine.dll_config import _overrides_path
    from Utils.wine.registry import normalize_pfx

    prefix = selected.get_prefix_path()
    prefix = normalize_pfx(Path(prefix)) if prefix else None
    return _overrides_path(selected.name), prefix


def _read(path):
    if path is None:
        return None, None
    if path.is_symlink():
        raise JobError("unsafe_wine_overrides", "A Wine state file is a symbolic link.")
    try:
        info = path.stat()
        raw = path.read_bytes()
        latest = path.stat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise JobError("wine_overrides_unavailable", "Could not read Wine state.") from exc
    def token(value):
        return [value.st_dev, value.st_ino, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns]
    if token(info) != token(latest):
        raise JobError("wine_overrides_changed", "Wine state changed; read it again.")
    return raw, token(info)


def _snapshot(selected, profile, config_path, prefix):
    from Utils.executables.launch import resolve_game_exe
    from Utils.wine import dll_config as shared

    registry_path = prefix / "user.reg" if prefix is not None else None
    files = {path: _read(path) for path in (config_path, registry_path) if path is not None}
    raw = files[config_path][0]
    try:
        data = json.loads(raw) if raw is not None else {}
        if (not isinstance(data, dict)
                or ("overrides" in data and not isinstance(data["overrides"], dict))
                or not isinstance(data.get("removed", []), list)
                or not isinstance(data.get("reset", []), list)
                or any(not isinstance(item, str) for item in
                       [*data.get("removed", []), *data.get("reset", [])])):
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise JobError("invalid_wine_overrides", "The saved DLL override state is invalid.") from exc
    stored = shared.load_wine_dll_overrides(selected.name)
    removed = shared.load_removed_wine_dll_overrides(selected.name) - stored.keys()
    reset = shared.load_reset_wine_dll_overrides(selected.name) - stored.keys() - removed
    handler = shared._merge_overrides(dict(getattr(selected, "wine_dll_overrides", {}) or {}))
    discovered = shared.discover_adjacent_dll_overrides(resolve_game_exe(selected))
    configured = {name: mode for name, mode in shared._merge_overrides(
        discovered, handler, stored).items() if name not in removed}
    registry_raw = files[registry_path][0] if registry_path is not None else None
    registry = {}
    registry_error = None
    if registry_raw is not None:
        try:
            text = registry_raw.decode("utf-8")
            if (not text or text.splitlines()[0] not in
                    ("WINE REGISTRY Version 1", "WINE REGISTRY Version 2")):
                raise ValueError()
        except (UnicodeError, ValueError):
            registry_error = "invalid_wine_registry"
        else:
            registry = shared.read_prefix_wine_dll_overrides(prefix, registry_text=text)
    visible = shared._merge_overrides(configured, registry)
    result = {
        "profile": profile.name, "scope": "game",
        "prefix_path": str(prefix) if prefix is not None else None,
        "registry_available": registry_raw is not None and registry_error is None,
        "registry_present": registry_raw is not None, "registry_error": registry_error,
        "stored_overrides": stored, "removed": sorted(removed), "reset": sorted(reset),
        "handler_overrides": handler, "discovered_overrides": discovered,
        "configured_overrides": configured, "prefix_overrides": registry,
        "overrides": visible, "load_orders": list(_ORDERS),
    }
    digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode())
    digest.update(str(profile).encode())
    for path in sorted(files, key=str):
        raw, token = files[path]
        digest.update(str(path).encode())
        digest.update(json.dumps(token).encode())
        digest.update(b"<missing>" if raw is None else raw)
    result["revision"] = digest.hexdigest()
    return result, files, registry_path


def _locks(config_path, prefix):
    from Utils.wine.registry import wine_state_file_edit

    stack = ExitStack()
    try:
        stack.enter_context(wine_state_file_edit(config_path))
        if prefix is not None:
            stack.enter_context(wine_state_file_edit(prefix / "user.reg"))
    except BaseException:
        stack.close()
        raise
    return stack


def get_overrides(game, profile):
    selected = _selected_game(game, profile)
    config_path, prefix = _paths(selected)
    with _locks(config_path, prefix):
        return _snapshot(selected, profile, config_path, prefix)[0]


def _name(raw, known):
    if not isinstance(raw, str):
        raise JobError("invalid_wine_overrides", "DLL names must be strings.")
    name = raw.strip().lower()
    if name.endswith(".dll") and name not in known:
        name = name[:-4]
    if len(name) > 255 or not _NAME.fullmatch(name):
        raise JobError("invalid_wine_overrides", "A DLL name is invalid.")
    return name


def _changes(current, changes, reset):
    from Utils.wine.dll_config import _merge_overrides

    known = (set(current["overrides"]) | set(current["stored_overrides"])
             | set(current["removed"]) | set(current["reset"]))
    edits = {}
    resets = set()
    for raw, mode in changes.items():
        name = _name(raw, known)
        if name in edits:
            raise JobError("invalid_wine_overrides", "DLL names must be unique after normalization.")
        if mode is not None:
            if not isinstance(mode, str):
                raise JobError("invalid_wine_overrides", "DLL load orders must be strings or null.")
            mode = _merge_overrides({name: mode})[name]
            if mode not in _ORDERS:
                raise JobError("invalid_wine_overrides", "A DLL load order is invalid.")
        edits[name] = mode
    for raw in reset:
        name = _name(raw, known)
        if name in resets or name in edits:
            raise JobError("invalid_wine_overrides", "Reset DLLs must be distinct from edited DLLs.")
        resets.add(name)
    return edits, resets


def set_overrides(game, profile, revision, changes, reset, apply, log):
    from Utils.atomic_write import write_atomic
    from Utils.deployment.locking import coordinated_game_mutation
    from Utils.deployment.wine_dll import apply_wine_dll_overrides, remove_wine_dll_overrides
    from Utils.wine import dll_config as shared

    with coordinated_game_mutation(game, "headless.wine_overrides"):
        selected = _selected_game(game, profile)
        config_path, prefix = _paths(selected)
        with _locks(config_path, prefix):
            current, files, registry_path = _snapshot(selected, profile, config_path, prefix)
            if current["revision"] != revision:
                raise JobError("wine_overrides_changed", "Wine DLL overrides changed; read them again.")
            edits, resets = _changes(current, changes, reset)
            stored = dict(current["stored_overrides"])
            removed = set(current["removed"])
            reset_defaults = set(current["reset"])
            for dll, mode in edits.items():
                reset_defaults.discard(dll)
                if mode is None:
                    stored.pop(dll, None)
                    removed.add(dll)
                else:
                    stored[dll] = mode
                    removed.discard(dll)
            for dll in resets:
                stored.pop(dll, None)
                removed.discard(dll)
                reset_defaults.add(dll)
            configured = {dll: mode for dll, mode in shared._merge_overrides(
                current["discovered_overrides"], current["handler_overrides"], stored).items()
                if dll not in removed}
            desired = shared._merge_overrides(configured, current["prefix_overrides"], stored,
                                             {dll: mode for dll, mode in edits.items() if mode is not None})
            for dll in reset_defaults:
                if dll in configured:
                    desired[dll] = configured[dll]
                else:
                    desired.pop(dll, None)
            desired = {dll: mode for dll, mode in desired.items() if dll not in removed}
            updates = {dll: mode for dll, mode in desired.items()
                       if current["prefix_overrides"].get(dll) != mode}
            deletions = (removed | (reset_defaults - configured.keys())) & current["prefix_overrides"].keys()
            available = current["registry_available"]
            if apply and current["registry_error"] is not None:
                raise JobError(current["registry_error"], "The prefix user.reg is not a valid Wine registry hive.")
            apply_now = apply and available
            if apply_now:
                for dll, mode in updates.items():
                    if len(dll) > 255 or not _NAME.fullmatch(dll) or mode not in _ORDERS:
                        raise JobError("invalid_wine_overrides",
                                       "An override to apply has an unsupported name or load order.")
                from Utils.processes.game import matching_pids, prefix_markers
                pids = matching_pids(prefix_markers(prefix))
                if pids is None:
                    raise JobError("wine_prefix_scan_unavailable", "Could not check whether the prefix is in use.")
                if pids:
                    raise JobError("wine_prefix_in_use", "Close the game and tools using this prefix first.",
                                   {"pids": sorted(pids)})
            config_changed = (stored != current["stored_overrides"]
                              or removed != set(current["removed"])
                              or reset_defaults != set(current["reset"]))
            registry_changed = apply_now and bool(updates or deletions)
            attempted = []
            try:
                if config_changed:
                    attempted.append(config_path)
                    shared.save_wine_dll_overrides(selected.name, stored,
                                                  removed=removed, reset=reset_defaults)
                    if (shared.load_wine_dll_overrides(selected.name) != stored
                            or shared.load_removed_wine_dll_overrides(selected.name) != removed
                            or shared.load_reset_wine_dll_overrides(selected.name) != reset_defaults):
                        raise JobError("wine_overrides_save_failed", "Saved DLL overrides could not be verified.")
                if registry_changed:
                    attempted.append(registry_path)
                    if deletions and not remove_wine_dll_overrides(prefix, deletions, log_fn=log):
                        raise JobError("wine_overrides_apply_failed", "Could not remove prefix DLL overrides.")
                    if updates and not apply_wine_dll_overrides(prefix, updates, log_fn=log):
                        raise JobError("wine_overrides_apply_failed", "Could not apply prefix DLL overrides.")
                updated = _snapshot(selected, profile, config_path, prefix)[0]
                if registry_changed and updated["prefix_overrides"] != desired:
                    raise JobError("wine_overrides_apply_failed", "Prefix DLL overrides could not be verified.")
            except (OSError, JobError) as exc:
                try:
                    for path in reversed(attempted):
                        raw = files[path][0]
                        if raw is None:
                            path.unlink(missing_ok=True)
                        else:
                            write_atomic(path, raw)
                except OSError as rollback:
                    raise JobError("wine_overrides_rollback_failed",
                                   "Wine state could not be restored after a failed save.") from rollback
                if isinstance(exc, JobError):
                    raise
                raise JobError("wine_overrides_save_failed", "Could not save Wine DLL overrides.") from exc
            log("Wine DLL override settings updated.")
            return {"changed": bool(config_changed or registry_changed), "saved": config_changed,
                    "applied": bool(apply_now), "apply_reason": None if apply_now else (
                        "deferred" if not apply else "prefix_unavailable"),
                    "wine_overrides": updated}
