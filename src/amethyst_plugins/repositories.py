from __future__ import annotations

import configparser
from copy import deepcopy
import hashlib
import importlib.util
import io
from Utils import json_limits as json
from pathlib import Path
import re
import sys
import uuid

from .catalog import discover, load_settings, plugin_directory
from .downloads import _check, _metadata, _signature, matches, source_uri


def source_key(record):
    return f"plugin:{record['plugin_id']}/{record['provider_id']}"


def portable_source(record):
    value = (record or {}).get("metadata", {}).get("source")
    if isinstance(value, str):
        try:
            source_uri(value)
            return value
        except ValueError:
            pass
    return ""


def lookup(game_id, plugin_id, provider_id):
    plugins, _ = discover(game_id)
    plugin = next((item for item in plugins if item.id == plugin_id), None)
    provider = next((item for item in plugin.download_providers if item.id == provider_id), None) if plugin else None
    if provider is None or load_settings(plugin).get("enabled") is False:
        raise ValueError("The plugin provider is unavailable.")
    return plugin, provider


def installed(path):
    path = Path(path)
    if not path.is_file():
        return None
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    if not parser.has_section("ManagerPluginSource"):
        return None
    record = dict(parser["ManagerPluginSource"])
    for key in ("plugin_id", "provider_id"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", record.get(key, "")):
            raise ValueError("Invalid installed provider identity.")
    if not record.get("identity") or not re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", "")):
        raise ValueError("Invalid installed artifact identity.")
    record["size_bytes"] = int(record["size_bytes"])
    record["metadata"] = _metadata(json.loads(record.get("metadata", "{}")))
    record["version"] = 1
    return record


def callback(game, plugin, provider, method, fields, log, control=None, *, optional=False):
    if provider.repository_entrypoint is None:
        if optional:
            return None
        raise ValueError("This provider has no repository callbacks.")
    signature = _signature(plugin, provider)
    name = f"_amethyst_repository_{uuid.uuid4().hex}"
    try:
        _check(control)
        path = provider.repository_entrypoint
        spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        function = getattr(module, method, None)
        if not callable(function):
            if optional:
                return None
            raise ValueError(f"The repository must export {method}(context).")
        context = {"api_version": 1, "plugin_id": plugin.id, "provider_id": provider.id,
                   "plugin_dir": str(plugin_directory(plugin)), "settings": deepcopy(signature[2]), "data": deepcopy(signature[3]),
                   "game_id": game.game_id, "game": game.name, **deepcopy(fields),
                   "log": lambda message: log(f"{plugin.name}: {message}"), "check_cancelled": lambda: _check(control)}
        result = function(context)
        raw = json.dumps(result, max_bytes=1024 * 1024, ensure_ascii=True)
        _check(control)
        if signature != _signature(plugin, provider) or plugin not in discover(game.game_id)[0]:
            raise ValueError("The provider changed while reading repository data.")
        return json.loads(raw)
    except Exception as exc:
        from Utils.operations import OperationCancelled
        if isinstance(exc, OperationCancelled):
            raise
        message = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+", "[source URL]", str(exc))
        raise ValueError(f"{plugin.name} repository {provider.label}: {message}") from exc
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


def requests(value, game):
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError("Dependencies must be an array of at most 256 provider source requests.")
    result = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"plugin_id", "provider_id", "source"}:
            raise ValueError("A dependency requires plugin_id, provider_id and source.")
        plugin, provider = lookup(game.game_id, row["plugin_id"], row["provider_id"])
        if not matches(provider, row["source"]):
            raise ValueError("The dependency source does not match its provider.")
        result.append(row)
    return result


def catalogue(game, record, log, control=None):
    plugin, provider = lookup(game.game_id, record["plugin_id"], record["provider_id"])
    result = callback(game, plugin, provider, "get_versions", {"installed": record}, log, control)
    if not isinstance(result, dict) or set(result) - {"versions", "recommended", "url", "actions"}:
        raise ValueError("A repository catalogue requires versions and optional recommended, url and actions.")
    versions = result.get("versions")
    if not isinstance(versions, list) or len(versions) > 256:
        raise ValueError("The version catalogue exceeds 256 versions.")
    seen = set()
    for row in versions:
        if not isinstance(row, dict) or set(row) - {"id", "version", "source", "label", "dependencies"}:
            raise ValueError("Invalid repository version.")
        if any(not isinstance(row.get(key), str) or not 1 <= len(row[key]) <= 1000 for key in ("id", "version")):
            raise ValueError("Versions require a stable id and version label.")
        if row["id"] in seen or not matches(provider, row.get("source")):
            raise ValueError("Version IDs must be unique and sources must match the provider.")
        if "label" in row and (not isinstance(row["label"], str) or len(row["label"]) > 1000):
            raise ValueError("Invalid version label.")
        requests(row.get("dependencies", []), game)
        seen.add(row["id"])
    recommended = result.get("recommended")
    if recommended is not None and (not isinstance(recommended, str) or recommended not in seen):
        raise ValueError("The recommended version must identify a catalogue entry.")
    url = result.get("url", "")
    if not isinstance(url, str) or url and source_uri(url).scheme not in ("https", "http"):
        raise ValueError("Repository page URLs must use HTTP or HTTPS.")
    actions = result.get("actions", [])
    if not isinstance(actions, list) or len(actions) > 32:
        raise ValueError("A repository supports at most 32 actions.")
    ids = set()
    for action in actions:
        if (not isinstance(action, dict) or set(action) != {"id", "label"}
                or not isinstance(action["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", action["id"])
                or action["id"] in ids or not isinstance(action["label"], str) or not 1 <= len(action["label"]) <= 200):
            raise ValueError("Invalid repository action.")
        ids.add(action["id"])
    normalized = {"versions": versions, "recommended": recommended, "url": url, "actions": actions}
    revision = hashlib.sha256(json.dumps([_signature(plugin, provider), record, normalized], sort_keys=True).encode()).hexdigest()
    return {**normalized, "catalogue_revision": revision, "source": source_key(record), "installed": record,
            "provider_label": provider.label}


def update_state(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    if not parser.has_section("ManagerPluginUpdate"):
        return {}
    result = json.loads(parser.get("ManagerPluginUpdate", "state", fallback="{}"))
    record = installed(path)
    if not isinstance(result, dict) or record is None or result.get("identity") != record["identity"]:
        return {}
    ignored = parser.getboolean("ManagerPluginUpdate", "ignoreUpdate", fallback=False)
    return {**result, "ignore_update": ignored, "has_update": bool(result.get("has_update")) and not ignored}


def check_updates(game, profile, names=None, log=lambda message: None, control=None):
    from Utils.mods.copy import resolve_target_staging
    from Utils.mods.modlist import read_modlist
    from Utils.mods.metadata import meta_file_lock
    from Utils.atomic_write import write_atomic_text
    staging = Path(resolve_target_staging(game, profile))
    rows, errors = [], []
    for entry in read_modlist(Path(profile) / "modlist.txt"):
        if entry.is_separator or entry.is_group_header or names is not None and entry.name not in names:
            continue
        if Path(entry.name).name != entry.name or entry.name in (".", ".."):
            continue
        path = staging / entry.name / "meta.ini"
        try:
            _check(control)
            with meta_file_lock(path):
                record = installed(path)
                before = path.read_bytes() if record else None
            if record is None:
                continue
            _plugin, provider = lookup(game.game_id, record["plugin_id"], record["provider_id"])
            if provider.repository_entrypoint is None:
                continue
            catalog = catalogue(game, record, log, control)
            candidate = next((row for row in catalog["versions"] if row["id"] == catalog["recommended"]), None)
            state = {"identity": record["identity"], "has_update": candidate is not None and candidate["id"] != record["identity"],
                     "candidate": candidate, "catalogue_revision": catalog["catalogue_revision"]}
            with meta_file_lock(path):
                if path.is_symlink() or path.read_bytes() != before:
                    raise ValueError("The installed mod changed during its update check.")
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(before.decode("utf-8"))
                if not parser.has_section("ManagerPluginUpdate"):
                    parser.add_section("ManagerPluginUpdate")
                parser.set("ManagerPluginUpdate", "state", json.dumps(state, ensure_ascii=True))
                output = io.StringIO()
                parser.write(output)
                write_atomic_text(path, output.getvalue())
            rows.append({"mod": entry.name, "source": source_key(record), "provider_label": provider.label, **update_state(path)})
        except Exception as exc:
            from Utils.operations import OperationCancelled
            if isinstance(exc, OperationCancelled):
                raise
            errors.append({"mod": entry.name, "message": str(exc)})
            log(f"Provider update check for {entry.name}: {exc}")
    return {"items": rows, "errors": errors}
