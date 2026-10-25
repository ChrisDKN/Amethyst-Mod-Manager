from __future__ import annotations

import hashlib
import math
import os
import re
from pathlib import Path

from Utils.ui import config as uc

from .jobs import JobError


def _text(value, limit=4096):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError("Supply a string without null characters.")
    return value


def _strings(value, choices=None):
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError("Supply an array of at most 256 strings.")
    result = [_text(item) for item in value]
    if len(set(result)) != len(result):
        raise ValueError("Array entries must be distinct.")
    if choices is not None and not set(result) <= set(choices):
        raise ValueError("Select only supported values.")
    return result


def _languages():
    from Utils.config_paths import get_config_dir
    roots = [Path(__file__).resolve().parents[1] / "translations",
             get_config_dir() / "languages"]
    return ["", "en", *sorted({path.stem.removeprefix("amethyst_")
                              for root in roots for path in root.glob("amethyst_*.qm")}
                             - {"en"})]


def _scale():
    raw = uc._read_ini(uc.get_ui_config_path()).get("ui", "scale", fallback="auto")
    if raw == "auto":
        return raw
    try:
        value = float(raw)
        return value if math.isfinite(value) and 0.5 <= value <= 3 else "auto"
    except ValueError:
        return "auto"


def _save_scale(value):
    uc._write_scale_ini(uc.get_ui_config_path(), str(value))


def _restore_whitelist(value):
    if not isinstance(value, dict) or set(value) != {"files", "folders"}:
        raise ValueError("Supply files and folders arrays.")
    result = {key: _strings(value[key]) for key in ("files", "folders")}
    if any(not item.strip() or item != item.strip() for items in result.values() for item in items):
        raise ValueError("Whitelist patterns cannot be empty or padded with whitespace.")
    return result


def _patterns(value):
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError("Supply at most 256 install-name rules.")
    result = []
    for rule in value:
        if (not isinstance(rule, dict) or not {"search", "replace"} <= set(rule)
                or set(rule) - {"search", "replace", "enabled", "id", "label"}):
            raise ValueError("Each rule needs search and replace, with optional enabled, id and label.")
        search, replacement = _text(rule["search"]), _text(rule["replace"])
        if not search.strip() or not isinstance(rule.get("enabled", True), bool):
            raise ValueError("Rules need a nonempty search and a boolean enabled flag.")
        try:
            re.compile(search).sub(replacement, "")
        except (re.error, IndexError) as exc:
            raise ValueError("Invalid rule expression or replacement.") from exc
        item = {"search": search, "replace": replacement, "enabled": rule.get("enabled", True)}
        for key in ("id", "label"):
            if key in rule:
                item[key] = _text(rule[key], 256)
        result.append(item)
    return result


def _environment(value):
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError("Supply at most 256 environment variables.")
    result, seen = [], set()
    for entry in value:
        if (not isinstance(entry, dict) or not {"name", "value"} <= set(entry)
                or set(entry) - {"name", "value", "enabled"}):
            raise ValueError("Each variable needs name and value, with an optional enabled flag.")
        name = _text(entry["name"], 256)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name in seen:
            raise ValueError("Environment variable names must be valid and distinct.")
        if not isinstance(entry.get("enabled", True), bool):
            raise ValueError("The enabled flag must be boolean.")
        seen.add(name)
        result.append({"name": name, "value": _text(entry["value"], 16384),
                       "enabled": entry.get("enabled", True)})
    return result


def _bindings(value):
    from Utils.ui.shortcut_bindings import shortcut_defaults, plugin_shortcut_id, binding_identity, normalise_binding
    defaults = shortcut_defaults()
    if not isinstance(value, dict) or any(key not in defaults and not plugin_shortcut_id(key) for key in value):
        raise ValueError("Supply bindings keyed by known shortcut action IDs.")
    result = {}
    for action, raw in value.items():
        normalized = normalise_binding(_text(raw, 128))
        if not normalized:
            raise ValueError("Supply a valid single key or mouse binding.")
        result[action] = normalized
    effective = {**defaults, **{key: item for key, item in result.items() if key in defaults}}
    identities = [identity for binding in effective.values() if (identity := binding_identity(binding)) is not None]
    if len(set(identities)) != len(identities):
        raise ValueError("Shortcut bindings must not conflict, including with defaults.")
    return {key: item for key, item in result.items() if item != defaults.get(key, "")}


def _specs():
    specs = {}

    def add(key, group, load, save, schema, validate=None, restart=False):
        specs[key] = {"load": load, "save": save, "validate": validate,
                      "schema": schema, "group": group, "restart_required": restart}

    for group, keys in {
        "appearance": "hide_bsa_conflicts hide_endorsed_flag show_summary_tooltips hide_kofi_button hide_endorse_button header_force_compact",
        "archives": "clear_archive_after_install keep_fomod_archives install_mods_disabled rename_mod_after_install",
        "downloads": "nexus_free_download_nxm download_only",
        "general": "normalize_folder_case restore_on_close",
        "updates": "allow_prerelease update_notifications",
    }.items():
        for key in keys.split():
            add(key, group, getattr(uc, "load_" + key), getattr(uc, "save_" + key),
                {"type": "boolean"})
    for key in ("default_staging_path download_cache_path custom_proton_path heroic_config_path "
                "lutris_data_path lutris_appimage_path faugus_data_path faugus_appimage_path "
                "bottles_data_path bottles_appimage_path steam_libraries_vdf_path").split():
        add(key, "paths", getattr(uc, "load_" + key), getattr(uc, "save_" + key),
            {"type": "string", "maxLength": 4096}, validate=_path)
    for key, choices in {
        "language": _languages(), "date_format": list(uc._DATE_FORMAT_PATTERNS),
        "default_priority_mode": ["default", "reversed"],
        "header_position": list(uc._HEADER_POSITIONS),
        "nexus_download_server": ["", *uc.NEXUS_DOWNLOAD_SERVERS],
    }.items():
        add(key, "downloads" if key == "nexus_download_server" else "appearance",
            getattr(uc, "load_" + key), getattr(uc, "save_" + key),
            {"type": "string", "enum": choices}, restart=key == "language")
    add("ui_scale", "appearance", _scale, _save_scale,
        {"oneOf": [{"const": "auto"}, {"type": "number", "minimum": 0.5, "maximum": 2}]},
        validate=_validate_scale, restart=True)
    for key, maximum in (("max_concurrent", uc._MAX_CONCURRENT_CEILING),
                         ("max_extract_workers", uc._MAX_EXTRACT_WORKERS_CEILING)):
        add(key, "downloads" if key == "max_concurrent" else "extraction",
            lambda key=key: uc.load_collection_settings()[key],
            None,
            {"type": "integer", "minimum": 1, "maximum": maximum})
    add("download_order", "downloads", lambda: uc.load_collection_settings()["download_order"],
        None,
        {"type": "string", "enum": ["balanced", "smallest", "largest"]})
    add("download_speed_limit", "downloads", uc.load_download_speed_limit, uc.save_download_speed_limit,
        {"type": "number", "minimum": 0, "maximum": 250})
    for key, schema in {"cpu_threads": {"type": "integer", "minimum": 0,
                                      "maximum": min(uc._EXTRACTION_THREADS_CEILING, os.cpu_count() or 8)},
                        "low_priority": {"type": "boolean"}}.items():
        add("extraction_" + key, "extraction", lambda key=key: uc.load_extraction_settings()[key],
            getattr(uc, "save_extraction_" + key), schema)
    add("hidden_header_buttons", "appearance", lambda: sorted(uc.load_hidden_header_buttons()),
        uc.save_hidden_header_buttons, {"type": "array", "items": {"enum": list(uc._HIDEABLE_HEADER_BUTTONS)},
                                      "uniqueItems": True, "maxItems": len(uc._HIDEABLE_HEADER_BUTTONS)},
        validate=lambda value: sorted(_strings(value, uc._HIDEABLE_HEADER_BUTTONS)))
    string_list = {"type": "array", "maxItems": 256, "uniqueItems": True,
                   "items": {"type": "string", "minLength": 1, "maxLength": 4096}}
    add("restore_whitelist", "advanced", lambda: dict(zip(("files", "folders"), uc.load_global_restore_whitelist())),
        lambda value: uc.save_global_restore_whitelist(**value),
        {"type": "object", "required": ["files", "folders"], "additionalProperties": False,
         "properties": {"files": string_list, "folders": string_list}}, validate=_restore_whitelist)
    add("install_name_patterns", "general", lambda: uc.load_install_name_patterns(persist=False),
        uc.save_install_name_patterns, {"type": "array", "maxItems": 256, "items": {
            "type": "object", "required": ["search", "replace"], "additionalProperties": False,
            "properties": {"search": {"type": "string", "minLength": 1, "maxLength": 4096},
                           "replace": {"type": "string", "maxLength": 4096}, "enabled": {"type": "boolean"},
                           "id": {"type": "string", "maxLength": 256}, "label": {"type": "string", "maxLength": 256}}}}, validate=_patterns)
    add("app_env_vars", "advanced", uc.load_app_env_vars, uc.save_app_env_vars,
        {"type": "array", "maxItems": 256, "items": {
            "type": "object", "required": ["name", "value"], "additionalProperties": False,
            "properties": {"name": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$", "maxLength": 256},
                           "value": {"type": "string", "maxLength": 16384}, "enabled": {"type": "boolean"}}}},
        validate=_environment, restart=True)
    from Utils.ui.shortcut_bindings import shortcut_defaults
    add("shortcut_overrides", "shortcuts", uc.load_shortcut_overrides, uc.save_shortcut_overrides,
        {"type": "object", "additionalProperties": False,
         "properties": {key: {"type": "string", "minLength": 1, "maxLength": 128} for key in shortcut_defaults()},
         "patternProperties": {r"^plugin/[a-z0-9][a-z0-9_.-]{0,99}/[a-z0-9][a-z0-9_.-]{0,99}$":
                               {"type": "string", "minLength": 1, "maxLength": 128}}},
        validate=_bindings)
    add("disabled_shortcut_inputs", "shortcuts", lambda: sorted(uc.load_disabled_shortcut_inputs()),
        lambda value: uc.save_disabled_shortcut_inputs(set(value)),
        {"type": "array", "items": {"enum": ["keyboard", "mouse"]}, "uniqueItems": True, "maxItems": 2},
        validate=lambda value: sorted(_strings(value, ["keyboard", "mouse"])))
    return specs


def _path(value):
    value = _text(value)
    if value and (value != value.strip() or not Path(value).expanduser().is_absolute()
                  or any(ord(char) < 32 for char in value)):
        raise ValueError("Supply an absolute path, or an empty string to use the default.")
    return str(Path(value).expanduser()) if value else ""


def _validate_scale(value):
    if value == "auto":
        return value
    if type(value) not in (int, float) or not math.isfinite(value) or not 0.5 <= value <= 2:
        raise ValueError("UI scale must be auto or a number between 0.5 and 2.")
    return value


def validate_changes(changes):
    specs = _specs()
    if not isinstance(changes, dict) or not changes or set(changes) - specs.keys():
        raise JobError("invalid_settings", "Supply a nonempty changes object containing supported settings.")
    validated = {}
    for key, value in changes.items():
        spec, schema = specs[key], specs[key]["schema"]
        try:
            if spec["validate"]:
                value = spec["validate"](value)
            else:
                kind = schema["type"]
                valid = {"boolean": type(value) is bool, "integer": type(value) is int,
                         "number": type(value) in (int, float), "string": isinstance(value, str)}[kind]
                if not valid:
                    raise ValueError("The setting has the wrong value type.")
                if "enum" in schema and value not in schema["enum"]:
                    raise ValueError("Select a supported value.")
                if kind in ("integer", "number") and (not math.isfinite(value)
                        or not schema["minimum"] <= value <= schema["maximum"]):
                    raise ValueError("The setting is outside its supported range.")
            validated[key] = value
        except (ValueError, TypeError, OverflowError) as exc:
            raise JobError("invalid_settings", str(exc), {"setting": key}) from None
    return validated


def _file_bytes():
    path = uc.get_ui_config_path()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise JobError("unsafe_settings", "The settings file must be a regular file.")
    try:
        data = path.read_bytes() if path.exists() else None
        if data is not None:
            if len(data) > 16 * 1024 * 1024:
                raise ValueError("Settings file is too large.")
            parser = uc._new_parser()
            parser.read_string(data.decode("utf-8"))
            if parser.get("meta", "version", fallback=str(uc._APP_INI_VERSION)) != str(uc._APP_INI_VERSION):
                raise JobError("unsupported_settings_version", "Migrate the application settings with the desktop manager before editing them headlessly.")
        return data
    except JobError:
        raise
    except Exception:
        raise JobError("invalid_settings_file", "The settings file could not be read.") from None


def snapshot():
    original = _file_bytes()
    specs = _specs()
    settings = {key: spec["load"]() for key, spec in specs.items()}
    from Utils.ui.shortcut_bindings import shortcut_defaults, binding_parts, load_shortcuts
    effective = {key: "" if (parts := binding_parts(value)) is not None and parts[0] in settings["disabled_shortcut_inputs"] else value
                 for key, value in load_shortcuts().items()}
    dismissed = uc.count_dismissed_notices()
    if _file_bytes() != original:
        raise JobError("settings_changed", "Settings changed; read them again.")
    return {"revision": hashlib.sha256(b"missing" if original is None else b"file:" + original).hexdigest(),
            "settings": settings, "definitions": {
                key: {**spec["schema"], "group": spec["group"],
                      "restart_required": spec["restart_required"]}
                for key, spec in specs.items()},
            "shortcut_defaults": shortcut_defaults(), "effective_shortcuts": effective,
            "dismissed_notices": dismissed}


def set_settings(revision, changes, log, *, reset_notices=False):
    from Utils.operations import OperationBusy
    changes = validate_changes(changes) if changes else {}
    try:
        with uc.edit_ui_settings():
            current = snapshot()
            if current["revision"] != revision:
                raise JobError("settings_changed", "Settings changed; read them again.")
            changed = {key: value for key, value in changes.items() if value != current["settings"][key]}
            specs = _specs()
            collection_keys = {"max_concurrent", "max_extract_workers", "download_order"}
            if collection_keys & changed.keys():
                collection = uc.load_collection_settings()
                collection.update({key: value for key, value in changed.items() if key in collection_keys})
                uc.save_collection_settings(**collection)
            for key, value in changed.items():
                if key not in collection_keys:
                    specs[key]["save"](value)
            cleared = uc.reset_dismissed_notices() if reset_notices else 0
    except (JobError, OperationBusy):
        raise
    except uc.UiSettingsChangedError:
        raise JobError("settings_changed", "Settings changed; read them again.") from None
    except Exception:
        raise JobError("settings_save_failed", "Could not save settings; no changes were published.") from None
    if "download_speed_limit" in changed:
        from Utils.downloads.bandwidth import set_limit_mbps
        set_limit_mbps(changed["download_speed_limit"])
    log("Saved application settings.")
    try:
        updated = snapshot()
    except JobError as exc:
        raise JobError(exc.name, "Settings were saved, but could not be read back.",
                       {"state_persisted": True}) from None
    return {"changed": updated["revision"] != current["revision"], "changed_keys": sorted(changed),
            "restart_required": sorted(key for key in changed if specs[key]["restart_required"]),
            "desktop_refresh_required": bool(set(changed) - {"download_speed_limit"}),
            "cleared_notices": cleared, "settings": updated}
