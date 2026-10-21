from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

from .jobs import JobError


_MODES = ("auto", "steam", "heroic", "lutris", "faugus", "none")
_PROVIDER_FIELDS = {"arguments", "launch_options", "wayland", "lsfg", "mangohud",
                    "runner", "prefix_path", "plain_wine"}


def _selected_game(game, profile: Path):
    if profile.is_symlink() or not profile.is_dir():
        raise JobError("profile_not_found", "The selected profile is unavailable.")
    selected = copy.copy(game)
    selected.set_active_profile_dir(profile)
    selected.load_paths(read_only=True)
    return selected


def _read(path: Path):
    if path.is_symlink():
        raise JobError("unsafe_launch_settings", "A launch settings file is a symbolic link.")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    except OSError as exc:
        raise JobError("launch_settings_unavailable",
                       "Could not read launch settings.", {"path": str(path)}) from exc
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise JobError("invalid_launch_settings",
                       "A launch settings file is invalid.", {"path": str(path)}) from exc
    return raw, data


def _text(value, field):
    if not isinstance(value, str) or "\x00" in value or len(value) > 32768:
        raise JobError("invalid_launch_settings",
                       f"'{field}' must be text without null characters, at most 32768 characters.")
    return value


def _snapshot(selected, profile: Path):
    from Utils.executables import launch as shared
    from Utils.config_paths import get_exe_args_path

    key = shared.game_exe_key(selected)
    if not key:
        raise JobError("launch_executable_unavailable",
                       "The game has no launch executable settings key.")
    mode_path = shared._launch_mode_path(selected)
    args_path = shared.exe_args_file(selected)
    global_args = get_exe_args_path()
    files = {path: _read(path) for path in {mode_path, args_path, global_args}}
    mode = files[mode_path][1]
    local_args = files[args_path][1]
    fallback_args = files[global_args][1]
    toggles = list(getattr(selected, "launch_toggles", ()) or ())
    settings = {
        "mode": mode.get(key, "auto") if mode.get(key, "auto") in _MODES else "auto",
        "deploy_before_launch": bool(mode.get("__deploy_before_launch", True)),
        "arguments": _text(local_args.get(key, fallback_args.get(key, "")), "arguments"),
        "launch_options": _text(mode.get(f"__launch_options_{key}", ""), "launch_options"),
        "wayland": bool(mode.get("__launch_with_wayland", False)),
        "lsfg": shared._normalize_lsfg_settings(mode.get("__lsfg_vk", {})),
        "mangohud": shared._normalize_mangohud_settings(mode.get("__mangohud", {})),
        "toggles": {item.key: bool(mode.get(f"__toggle_{item.key}", item.default))
                    for item in toggles},
    }
    result = {
        "profile": profile.name, "executable_key": key, "settings": settings,
        "scope": {"launch_settings": "game",
                  "arguments": "global" if args_path == global_args else "profile"},
        "available_toggles": [{"key": item.key, "label": item.label,
                               "hint": item.hint, "default": item.default}
                              for item in toggles],
    }
    from amethyst_plugins.launchers import ProviderError, selected_entry
    try:
        entry = selected_entry(selected)
    except ProviderError as exc:
        raise JobError("launcher_provider_error", str(exc)) from exc
    if entry is not None:
        settings.update({field: entry[field] for field in _PROVIDER_FIELDS})
        settings["mode"] = "auto"
        result.update(provider_id=entry["provider_id"], launcher_id=entry["id"],
                      provider_revision=entry["revision"], provider_writable=entry["writable"])
        result["scope"].update(launch_settings="provider", arguments="provider")
    digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode())
    digest.update(str(profile).encode())
    for path in sorted(files, key=str):
        digest.update(str(path).encode())
        raw = files[path][0]
        digest.update(b"<missing>" if raw is None else raw)
    result["revision"] = digest.hexdigest()
    return result, files, mode_path, args_path


def get_settings(game, profile: Path):
    from Utils.executables.launch import launch_settings_edit

    selected = _selected_game(game, profile)
    with launch_settings_edit(selected):
        return _snapshot(selected, profile)[0]


def _valid_scalar(value, current):
    if isinstance(current, bool):
        return isinstance(value, bool)
    if isinstance(current, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(current, float):
        try:
            return (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value))
        except OverflowError:
            return False
    return isinstance(value, str)


def _validated_changes(current: dict, changes: dict):
    from Utils.executables import launch as shared

    if not changes or set(changes) - set(current):
        raise JobError("invalid_launch_settings", "Supply at least one known launch settings field.")
    proposed = copy.deepcopy(current)
    for field, value in changes.items():
        if field == "mode":
            if value not in _MODES:
                raise JobError("invalid_launch_settings", "Unknown launch mode.")
        elif field in ("deploy_before_launch", "wayland", "plain_wine"):
            if not isinstance(value, bool):
                raise JobError("invalid_launch_settings", f"'{field}' must be a boolean.")
        elif field in ("arguments", "launch_options"):
            value = _text(value, field)
        elif field == "prefix_path":
            value = _text(value, field)
            if not Path(value).is_absolute():
                raise JobError("invalid_launch_settings", "prefix_path must be absolute.")
        elif field == "runner":
            if not isinstance(value, dict):
                raise JobError("invalid_launch_settings", "runner must be an object.")
        elif field in ("lsfg", "mangohud", "toggles"):
            if not isinstance(value, dict) or set(value) - set(current[field]):
                raise JobError("invalid_launch_settings", f"Unknown or invalid '{field}' controls.")
            for key, item in value.items():
                if not _valid_scalar(item, current[field][key]):
                    raise JobError("invalid_launch_settings", f"Invalid '{field}.{key}' value.")
                if isinstance(item, str):
                    _text(item, f"{field}.{key}")
            value = {**current[field], **value}
            normalized = (shared._normalize_lsfg_settings(value) if field == "lsfg" else
                          shared._normalize_mangohud_settings(value) if field == "mangohud" else value)
            for key in changes[field]:
                if value[key] != normalized[key] and not isinstance(value[key], str):
                    raise JobError("invalid_launch_settings", f"'{field}.{key}' is outside its supported range.")
                if isinstance(value[key], str) and value[key].strip() != normalized[key]:
                    raise JobError("invalid_launch_settings", f"Unknown '{field}.{key}' choice.")
            value = normalized
        proposed[field] = value
    return proposed


def set_settings(game, profile: Path, revision: str, changes: dict, log):
    from Utils.atomic_write import write_atomic, write_atomic_text
    from Utils.deployment.locking import coordinated_game_mutation
    from Utils.executables import launch as shared

    current = get_settings(game, profile)
    if current.get("provider_id") and set(changes) & _PROVIDER_FIELDS:
        if current["revision"] != revision:
            raise JobError("launch_settings_changed", "Launch settings changed; read them again.")
        settings = _validated_changes(current["settings"], changes)
        if "mode" in changes and changes["mode"] != "auto":
            raise JobError("invalid_launch_settings", "Configure another installation to change launcher ownership.")
        provider_changes = {field: settings[field] for field in changes
                            if field in _PROVIDER_FIELDS and settings[field] != current["settings"][field]}
        from amethyst_plugins.launchers import update_entry
        changed = None
        if provider_changes:
            changed = update_entry(current["provider_id"], current["launcher_id"],
                                   provider_changes, current["provider_revision"], log=log)
        try:
            local = {field: value for field, value in changes.items() if field not in _PROVIDER_FIELDS}
            fresh = get_settings(game, profile)
            if local:
                return set_settings(game, profile, fresh["revision"], local, log)
            return {"changed": changed is not None, "launch_settings": fresh}
        except Exception:
            if changed is not None:
                update_entry(current["provider_id"], current["launcher_id"],
                             {field: current["settings"][field] for field in provider_changes},
                             changed["revision"], log=log)
            raise

    with coordinated_game_mutation(game, "headless.launch_settings"):
        selected = _selected_game(game, profile)
        with shared.launch_settings_edit(selected):
            current, files, mode_path, args_path = _snapshot(selected, profile)
            if current["revision"] != revision:
                raise JobError("launch_settings_changed", "Launch settings changed; read them again.")
            settings = _validated_changes(current["settings"], changes)
            if current.get("provider_id"):
                if "mode" in changes and changes["mode"] != "auto":
                    raise JobError("invalid_launch_settings", "Configure another installation to change launcher ownership.")
            key = current["executable_key"]
            mode = dict(files[mode_path][1])
            mapping = {"mode": key, "deploy_before_launch": "__deploy_before_launch",
                       "launch_options": f"__launch_options_{key}",
                       "wayland": "__launch_with_wayland", "lsfg": "__lsfg_vk",
                       "mangohud": "__mangohud"}
            for field, target in mapping.items():
                if field in changes:
                    if field in ("lsfg", "mangohud") and isinstance(mode.get(target), dict):
                        mode[target] = {**mode[target], **settings[field]}
                    else:
                        mode[target] = settings[field]
            if "toggles" in changes:
                for toggle in changes["toggles"]:
                    mode[f"__toggle_{toggle}"] = settings["toggles"][toggle]
            writes = []
            if mode != files[mode_path][1]:
                writes.append((mode_path, mode))
            if "arguments" in changes:
                args = {**files[args_path][1], key: settings["arguments"]}
                if args != files[args_path][1]:
                    writes.append((args_path, args))
            if not writes:
                return {"changed": False, "launch_settings": current}
            completed = []
            try:
                for path, data in writes:
                    write_atomic_text(path, json.dumps(data, indent=2))
                    completed.append(path)
                updated = _snapshot(selected, profile)[0]
            except (OSError, JobError) as exc:
                try:
                    for path in reversed(completed):
                        original = files[path][0]
                        if original is None:
                            path.unlink(missing_ok=True)
                        else:
                            write_atomic(path, original)
                except OSError as rollback:
                    raise JobError("launch_settings_rollback_failed",
                                   "Launch settings could not be restored after a failed save.") from rollback
                raise JobError("launch_settings_save_failed", "Could not save launch settings.") from exc
        handoff = None
        warning = None
        try:
            from Utils.launchers.handoff import refresh_launch_handoff_script
            handoff = refresh_launch_handoff_script(selected, log_fn=log)
        except Exception:
            warning = "launcher_handoff_refresh_failed"
            log("Launch settings saved; the launcher handoff could not be refreshed.")
        log("Launch settings saved.")
        return {"changed": True, "launch_settings": updated,
                "handoff_path": str(handoff) if handoff is not None else None,
                "warning": warning}
