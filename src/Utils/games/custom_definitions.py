from __future__ import annotations

import copy
import json
from pathlib import PureWindowsPath
from urllib.parse import urlsplit


TEXT_FIELDS = (
    "name", "game_id", "exe_name", "deploy_type", "mod_data_path", "steam_id",
    "nexus_game_domain", "thunderstore_community", "image_url", "mod_install_prefix",
    "filemap_casing",
)
LIST_FIELDS = (
    "exe_name_alts", "additional_nexus_domains", "auto_install_deps",
    "mod_folder_strip_prefixes", "conflict_ignore_filenames", "conflict_ignore_foldernames",
    "mod_folder_strip_prefixes_post", "mod_required_top_level_folders", "mod_required_file_types",
)
BOOL_FIELDS = (
    "mod_auto_strip_until_required", "mod_install_as_is_if_no_match",
    "restore_before_deploy", "normalize_folder_case", "editable",
)
MAP_FIELDS = ("wine_dll_overrides", "custom_frameworks", "filemap_casing_pins")


def definition_schema():
    from Utils.deployment.shared import CustomRule
    from Utils.games.routing_rules import rule_values
    routing = {key: {"type": "boolean" if isinstance(value, bool) else
                     "array" if isinstance(value, list) else "string"}
               for key, value in rule_values(CustomRule(dest="")).items()}
    for spec in routing.values():
        if spec["type"] == "array":
            spec.update(items={"type": "string"}, maxItems=512)
    routing["rule_id"] = {"type": "string", "minLength": 1}
    properties = {key: {"type": "string", "maxLength": 4096} for key in TEXT_FIELDS}
    properties.update({key: {"type": "array", "items": {"type": "string"}, "maxItems": 512}
                       for key in LIST_FIELDS})
    properties.update({key: {"type": "boolean"} for key in BOOL_FIELDS})
    properties.update({key: {"type": "object", "additionalProperties": {"type": "string"}}
                       for key in MAP_FIELDS})
    properties.update(
        deploy_type={"enum": ["standard", "root", "ue5"]},
        filemap_casing={"enum": ["upper", "lower", "force_upper", "force_lower", "preserve"]},
        custom_routing_rules={"type": "array", "maxItems": 512, "items": {
            "type": "object", "properties": routing, "additionalProperties": False}},
        restore_whitelist={"type": "array", "maxItems": 512, "items": {
            "type": "object", "additionalProperties": False, "properties": {
                "path": {"type": "string"}, **{key: {"type": "array", "items": {"type": "string"}}
                for key in ("extensions", "folders", "filenames")}}}},
        vortex_mod_types={"type": "object"},
        version={"type": ["string", "number"]},
    )
    return {"type": "object", "required": ["name", "exe_name"], "properties": properties}


def relative_path(value):
    value = value.replace("\\", "/").strip()
    if (value.startswith("/") or PureWindowsPath(value).drive
            or ".." in value.split("/") or any(c in value for c in "\x00\r\n")):
        raise ValueError("Paths in a definition must be relative and cannot contain '..'.")
    return value


def validate_definition(value):
    from Games.Custom.custom_game import _make_game_id
    from Utils.games.routing_rules import definition_rule_ids, rule_values, validate_rule

    if not isinstance(value, dict) or any(not isinstance(key, str) or key.startswith("_") for key in value):
        raise ValueError("Supply a definition object without internal metadata keys.")
    if len(json.dumps(value, allow_nan=False).encode()) > 1024 * 1024:
        raise ValueError("The definition exceeds 1 MiB.")
    result = copy.deepcopy(value)
    for key in TEXT_FIELDS:
        if key in result:
            if not isinstance(result[key], str) or len(result[key]) > 4096 or "\x00" in result[key]:
                raise ValueError(f"{key} must be a string without null characters.")
            result[key] = result[key].strip()
    for key in ("name", "exe_name"):
        if not result.get(key):
            raise ValueError(f"{key} is required.")
    if len(result["name"]) > 120:
        raise ValueError("Game name is too long (maximum 120 characters).")
    result["game_id"] = result.get("game_id") or _make_game_id(result["name"])
    if ".." in result["game_id"]:
        raise ValueError("Invalid game_id.")
    for key in ("name", "game_id"):
        text = result[key]
        if text in (".", "..") or any(c in text for c in "/\\") or any(ord(c) < 32 for c in text):
            raise ValueError(f"Invalid {key}.")
    if result.get("deploy_type", "standard") not in ("standard", "root", "ue5"):
        raise ValueError("deploy_type must be standard, root, or ue5.")
    if result.get("filemap_casing", "upper") not in ("upper", "lower", "force_upper", "force_lower", "preserve"):
        raise ValueError("Invalid filemap_casing.")
    if "version" in result and (type(result["version"]) not in (str, int, float)):
        raise ValueError("version must be a string or number.")
    for key in BOOL_FIELDS:
        if key in result and type(result[key]) is not bool:
            raise ValueError(f"{key} must be a boolean.")
    for key in LIST_FIELDS:
        if key not in result:
            continue
        items = result[key]
        if (not isinstance(items, list) or len(items) > 512
                or any(not isinstance(item, str) or not item.strip() or
                       any(c in item for c in "\x00\r\n") for item in items)):
            raise ValueError(f"{key} must contain at most 512 non-empty strings.")
        result[key] = [item.strip() for item in items]
    for key in MAP_FIELDS:
        if key in result and (not isinstance(result[key], dict) or any(
                not isinstance(k, str) or not k or not isinstance(v, str)
                or any(c in k + v for c in "\x00\r\n") for k, v in result[key].items())):
            raise ValueError(f"{key} must map names to strings.")
    for key in ("exe_name", "mod_data_path", "mod_install_prefix"):
        if key in result:
            result[key] = relative_path(result[key])
    result["exe_name_alts"] = [relative_path(item) for item in result.get("exe_name_alts", [])]
    if "custom_frameworks" in result:
        result["custom_frameworks"] = {k: relative_path(v) for k, v in result["custom_frameworks"].items()}
    if "filemap_casing_pins" in result:
        result["filemap_casing_pins"] = {relative_path(k): relative_path(v)
                                       for k, v in result["filemap_casing_pins"].items()}
    url = result.get("image_url", "")
    if url and (urlsplit(url).scheme not in ("https", "http") or not urlsplit(url).netloc):
        raise ValueError("image_url must be an HTTP or HTTPS URL.")
    result["thunderstore_community"] = result.get("thunderstore_community", "").lower()
    rules = result.get("custom_routing_rules", [])
    if not isinstance(rules, list) or len(rules) > 512:
        raise ValueError("Supply at most 512 routing rules.")
    normalized = []
    for row in rules:
        if not isinstance(row, dict):
            raise ValueError("Each routing rule must be an object.")
        values = rule_values(validate_rule({k: v for k, v in row.items() if k != "rule_id"}))
        if "rule_id" in row:
            values["rule_id"] = row["rule_id"]
        normalized.append(values)
    definition_rule_ids(normalized)
    result["custom_routing_rules"] = normalized
    whitelist = result.get("restore_whitelist", [])
    if not isinstance(whitelist, list) or len(whitelist) > 512:
        raise ValueError("Supply at most 512 restore whitelist rules.")
    for row in whitelist:
        if not isinstance(row, dict) or set(row) - {"path", "extensions", "folders", "filenames"}:
            raise ValueError("Invalid restore whitelist rule.")
        if not isinstance(row.get("path", ""), str):
            raise ValueError("Restore whitelist paths must be strings.")
        row["path"] = relative_path(row.get("path", ""))
        validate_rule({k: v for k, v in row.items() if k != "path"})
    types = result.get("vortex_mod_types", {})
    if not isinstance(types, dict):
        raise ValueError("vortex_mod_types must be an object.")
    for spec in types.values():
        if not isinstance(spec, dict) or not isinstance(spec.get("dest", ""), str):
            raise ValueError("Each Vortex mod type must contain a relative dest.")
        spec["dest"] = relative_path(spec.get("dest", ""))
    return result
