from __future__ import annotations

import configparser
import json
from io import StringIO
from pathlib import Path

from Utils.mods.metadata import meta_file_lock
from Utils.mods.state import ModStateError


FIELDS = frozenset(("version_label", "root_folder", "ignore_updates"))
UPDATE_SOURCES = frozenset(("nexus", "thunderstore", "plugin"))


def validate_changes(changes):
    if not isinstance(changes, dict) or not changes or changes.keys() - FIELDS:
        raise ModStateError("invalid_mod_properties", "Supply supported mod property changes.")
    if "version_label" in changes:
        value = changes["version_label"]
        if value is not None and (not isinstance(value, str) or len(value) > 512
                                  or any(c in value for c in "\x00\r\n")
                                  or any(0xd800 <= ord(c) <= 0xdfff for c in value)):
            raise ModStateError("invalid_mod_properties", "A version label must be a single line of at most 512 characters or null.")
    if "root_folder" in changes and not isinstance(changes["root_folder"], bool):
        raise ModStateError("invalid_mod_properties", "The root-folder flag must be a boolean.")
    if "ignore_updates" in changes:
        values = changes["ignore_updates"]
        if (not isinstance(values, dict) or not values or values.keys() - UPDATE_SOURCES
                or any(not isinstance(value, bool) for value in values.values())):
            raise ModStateError("invalid_mod_properties", "Update ignores must map supported sources to booleans.")


def _load(path):
    if path.is_symlink():
        raise ModStateError("unsafe_mod_path", "The mod metadata is a symbolic link.")
    raw = path.read_bytes() if path.exists() else None
    cp = configparser.ConfigParser(allow_no_value=True, strict=False)
    if raw is not None:
        try:
            cp.read_string(raw.decode("utf-8"))
        except (UnicodeError, configparser.Error) as exc:
            raise ModStateError("invalid_mod_metadata", "The mod metadata is invalid and was left unchanged.") from exc
    return cp, raw


def _summary(cp, *, is_bg3=False):
    def value(section, key, *, raw=False):
        return cp.get(section, key, fallback="", raw=raw) or ""
    def modio_value(key, legacy):
        return value("modio", key, raw=True) if cp.has_option("modio", key) else value("General", legacy, raw=True)
    def flag(section, key):
        return value(section, key).strip().lower() in ("true", "1", "yes")
    def number(section, key):
        try:
            return int(value(section, key) or "0")
        except ValueError:
            return 0
    try:
        label = value("General", "version")
        ts_version = value("thunderstore", "version")
        if value("thunderstore", "fullName") and not (
                value("thunderstore", "namespace") and value("thunderstore", "name")):
            from Thunderstore.thunderstore_meta import parse_full_name
            ts_version = ts_version or parse_full_name(value("thunderstore", "fullName"))[2]
        modio_version = modio_value("version", "modioVersion")
        plugin_metadata = json.loads(value("ManagerPluginSource", "metadata", raw=True) or "{}")
        plugin_version = plugin_metadata.get("version", "") if isinstance(plugin_metadata, dict) else ""
        if not isinstance(plugin_version, str):
            plugin_version = ""
        try:
            modio_id = int(modio_value("modId", "modioModId") or "0")
        except ValueError:
            modio_id = 0
        version, source = label, "general" if label else None
        if not version and is_bg3 and modio_id > 0 and modio_version:
            version, source = modio_version, "modio"
        if not version and ts_version:
            version, source = ts_version, "thunderstore"
        if not version and plugin_version:
            version, source = plugin_version, "plugin"
        sources = {}
        if number("General", "modId") > 0:
            sources["nexus"] = "General"
        if ((value("thunderstore", "namespace") and value("thunderstore", "name"))
                or value("thunderstore", "fullName")):
            sources["thunderstore"] = "thunderstore"
        if value("ManagerPluginSource", "plugin_id", raw=True):
            sources["plugin"] = "ManagerPluginUpdate"
        updates = {}
        for name, section in sources.items():
            ignored = flag(section, "ignoreUpdate")
            latest, pinned = value(section, "latestVersion"), value(section, "ignoredVersion")
            effective = ignored or bool(latest and pinned == latest)
            has_update = flag(section, "hasUpdate")
            if name == "plugin":
                state = json.loads(value(section, "state", raw=True) or "{}")
                has_update = bool(state.get("has_update"))
                latest = (state.get("candidate") or {}).get("version", "")
            updates[name] = {"ignore_update": ignored, "ignored_version": pinned,
                             "ignored": effective, "latest_version": latest,
                             "has_update": has_update and not effective}
        return {"version_label": label, "version": version or None, "version_source": source,
                "root_folder": flag("General", "rootFolder"), "updates": updates,
                "source_versions": {"nexus": label if "nexus" in sources else None,
                                    "thunderstore": ts_version or None,
                                    "plugin": plugin_version or None,
                                    "modio": modio_version or None}}
    except (configparser.Error, ValueError, AttributeError) as exc:
        raise ModStateError("invalid_mod_metadata", "The mod metadata contains invalid interpolated values.") from exc


def read_properties(path, *, is_bg3=False):
    path = Path(path)
    with meta_file_lock(path):
        cp, raw = _load(path)
        return {**_summary(cp, is_bg3=is_bg3), "metadata_available": raw is not None}


def edit_properties(path, changes, *, is_bg3=False):
    from Utils.atomic_write import write_atomic

    validate_changes(changes)
    path = Path(path)
    if not path.parent.is_dir():
        raise ModStateError("mod_files_missing", "The staged mod folder is unavailable.")
    with meta_file_lock(path):
        cp, raw = _load(path)
        before = _summary(cp, is_bg3=is_bg3)
        changed = []
        def put(section, key, value):
            if not cp.has_section(section):
                cp.add_section(section)
            cp.set(section, key, str(value).replace("%", "%%"))
        if "version_label" in changes:
            label = (changes["version_label"] or "").strip()
            if label != before["version_label"] or (
                    not label and cp.has_option("General", "version") and "version" not in cp.defaults()):
                if label:
                    put("General", "version", label)
                elif "version" in cp.defaults():
                    put("General", "version", "")
                else:
                    cp.remove_option("General", "version")
                changed.append("version_label")
        if "root_folder" in changes and changes["root_folder"] != before["root_folder"]:
            put("General", "rootFolder", "true" if changes["root_folder"] else "false")
            changed.append("root_folder")
        for source, ignored in changes.get("ignore_updates", {}).items():
            current = before["updates"].get(source)
            if current is None:
                raise ModStateError("update_source_unavailable", "This mod has no saved identity for the requested update source.",
                                    {"source": source})
            section = "General" if source == "nexus" else "ManagerPluginUpdate" if source == "plugin" else "thunderstore"
            pinned = current["latest_version"] if source == "nexus" and ignored else (
                current["ignored_version"] if ignored else "")
            if current["ignore_update"] == ignored and current["ignored_version"] == pinned:
                continue
            put(section, "ignoreUpdate", "true" if ignored else "false")
            put(section, "ignoredVersion", pinned)
            available = False
            if not ignored:
                latest = current["latest_version"]
                installed = _summary(cp, is_bg3=is_bg3)["source_versions"][source] or ""
                known_update = (cp.get(section, "hasUpdate", fallback="") or "").strip().lower() in ("true", "1", "yes")
                if source == "nexus":
                    from Nexus.nexus_update_checker import _version_is_newer
                    try:
                        file_id = int(cp.get(section, "fileId", fallback="0") or "0")
                        latest_id = int(cp.get(section, "latestFileId", fallback="0") or "0")
                    except ValueError:
                        file_id = latest_id = 0
                    available = bool(known_update or (file_id > 0 and latest_id > 0 and file_id != latest_id)
                                     or (latest and installed and _version_is_newer(latest, installed)))
                else:
                    from Thunderstore.thunderstore_update_checker import _parse_version
                    old, new = _parse_version(installed), _parse_version(latest)
                    available = bool(known_update or (old is not None and new is not None and new > old))
            put(section, "hasUpdate", "true" if available else "false")
            changed.append(f"ignore_updates.{source}")
        expected = _summary(cp, is_bg3=is_bg3)
        if changed:
            text = StringIO()
            cp.write(text)
            payload = text.getvalue().encode("utf-8")
            try:
                write_atomic(path, payload)
                if (path.read_bytes() != payload
                        or read_properties(path, is_bg3=is_bg3) != {**expected, "metadata_available": True}):
                    raise OSError("The saved mod properties could not be verified.")
            except Exception as exc:
                try:
                    if raw is None:
                        path.unlink(missing_ok=True)
                        if path.exists() or path.is_symlink():
                            raise OSError("The new metadata file could not be removed.")
                    else:
                        write_atomic(path, raw)
                        if path.read_bytes() != raw:
                            raise OSError("The original metadata could not be verified.")
                except OSError as rollback:
                    raise ModStateError("mod_properties_rollback_failed", "Mod metadata could not be restored.") from rollback
                raise ModStateError("mod_properties_save_failed", "The mod properties could not be saved.") from exc
        return {**expected, "metadata_available": raw is not None or bool(changed),
                "changed": bool(changed), "changed_fields": changed}
