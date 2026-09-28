from __future__ import annotations

import copy
import hashlib
import json
from urllib.parse import urlsplit

from .jobs import JobError


_RELATED_METHODS = {
    "wizards.reshade.ReShadeWizard": (
        "tools.reshade.options.get", "tools.reshade.dll.acquire.start"),
    "wizards.modio_settings.ModioSettingsWizard": (
        "modio.credentials.get", "modio.credentials.set.start", "modio.credentials.clear.start"),
    "wizards.bethesda_register_game_path.RegisterGamePathWizard": (
        "wine.prefix.health.start", "wine.prefix.repair.start"),
    "wizards.curated_profile.CuratedProfileWizard": (
        "collections.import.start", "profiles.share.inspect"),
}
_SCRIPT_EXTENDER = "wizards.script_extender.ScriptExtenderWizard"


def _source(option):
    url = option.get("download_url") or ""
    file_id = option.get("nexus_file_id") or 0
    if file_id:
        parsed = urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        if (parsed.hostname not in ("nexusmods.com", "www.nexusmods.com")
                or len(parts) != 3 or parts[1] != "mods" or not parts[2].isdigit()
                or int(parts[2]) <= 0 or isinstance(file_id, bool)
                or not isinstance(file_id, int) or file_id <= 0):
            raise JobError("invalid_tool_catalogue", "A tool's pinned Nexus source is invalid.")
        return {"kind": "nexus", "url": url, "domain": parts[0], "mod_id": int(parts[2]),
                "file_id": file_id, "download_method": "nexus.download.start"}
    if option.get("github_api_url"):
        return {"kind": "github_release", "url": option["github_api_url"], "manual_url": url or None}
    if option.get("direct_download_url"):
        return {"kind": "direct", "url": option["direct_download_url"], "manual_url": url or None}
    return {"kind": "manual", "url": url or None}


def _script_extender(tool):
    options = tool.extra.get("versions") or [tool.extra]
    versions = []
    for index, option in enumerate(options):
        versions.append({
            "index": index, "label": option.get("label") or tool.label,
            "description": option.get("description") or "",
            "archive_keywords": [str(value).lower() for value in
                                 (option.get("archive_keywords") or tool.extra.get("archive_keywords") or [])],
            "source": _source(option),
        })
    return {"version_required": bool(tool.extra.get("versions")),
            "install_modes": ["game", "root", "mod"], "versions": versions}


def _label(template, args):
    return template.format(*args) if args else template


def _catalogue(game, profile):
    from Utils.wizards.catalog import group_by_category, infer_category
    from Utils.wizards.plugins import _tool_exe_names, get_all_wizard_tools

    selected = copy.copy(game)
    selected._game_path = None
    selected._staging_path = None
    selected.set_active_profile_dir(profile)
    selected.load_paths(read_only=True, prefix_fallback=False)
    tools = get_all_wizard_tools(selected, include_plugins=False)
    tools.sort(key=lambda tool: (_label(tool.label, tool.label_args).casefold(), tool.id))
    if len({tool.id for tool in tools}) != len(tools):
        raise JobError("invalid_tool_catalogue", "The game's tool identifiers are not unique.")
    rows = []
    for tool in tools:
        row = {
            "id": tool.id, "label": _label(tool.label, tool.label_args),
            "description": _label(tool.description, tool.description_args),
            "category": infer_category(tool), "executables": sorted(_tool_exe_names(tool)),
            "headless": {
                "workflow_available": tool.dialog_class_path == "wizards.modio_settings.ModioSettingsWizard",
                "related_methods": list(_RELATED_METHODS.get(tool.dialog_class_path, ())),
            },
        }
        if tool.dialog_class_path == _SCRIPT_EXTENDER:
            row["script_extender"] = _script_extender(tool)
            row["headless"]["workflow_available"] = True
            row["headless"]["related_methods"] = sorted({
                version["source"]["download_method"] for version in row["script_extender"]["versions"]
                if "download_method" in version["source"]} | {
                    "tools.script_extender.acquire.start",
                    "tools.script_extender.inspect.start",
                    "tools.script_extender.install.start"})
        rows.append(row)
    result = {
        "game": {"id": str(selected.game_id), "name": str(selected.name)},
        "profile": profile.name if profile else None, "scope": "built_in",
        "external_plugins_loaded": False, "tools": rows,
        "categories": [{"category": category, "tools": [tool.id for tool in values]}
                       for category, values in group_by_category(tools)],
    }
    result["revision"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def catalogue(game, profile=None):
    from Utils.profiles.state import read_only_profile_state

    with read_only_profile_state():
        return _catalogue(game, profile)
