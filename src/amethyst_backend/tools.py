from __future__ import annotations

import copy
import hashlib
import json
from urllib.parse import urlsplit

from .jobs import JobError


_RELATED_METHODS = {
    "wizards.bethesda_synthesis.SynthesisWizard": (
        "tools.synthesis.acquire.start", "tools.synthesis.inspect.start", "tools.synthesis.install.start",
        "tools.synthesis.prefix.plan.start", "tools.synthesis.prefix.setup.start"),
    "wizards.wrye_bash.WryeBashWizard": (
        "tools.wrye_bash.acquire.start", "tools.wrye_bash.inspect.start", "tools.wrye_bash.install.start",
        "tools.wrye_bash.run.plan.start", "tools.wrye_bash.run.start"),
    **{path: ("tools.lod.acquire.start", "tools.lod.inspect.start", "tools.lod.install.start",
              "tools.lod.run.plan.start", "tools.lod.run.start")
       for path in ("wizards.dyndolod.TexGenWizard", "wizards.dyndolod.DynDOLODWizard",
                    "wizards.dyndolod.xLODGenWizard")},
    "wizards.bsa_pack_candidates.BsaPackCandidatesWizard": (
        "tools.archive_packing.scan.start", "tools.archive_packing.plan.start", "tools.archive_packing.apply.start"),
    "wizards.sdv_smapi.SmapiWizard": (
        "tools.smapi.acquire.start", "tools.smapi.inspect.start", "tools.smapi.install.start"),
    "wizards.reshade.ReShadeWizard": (
        "tools.reshade.options.get", "tools.reshade.dll.acquire.start",
        "tools.reshade.shaders.acquire.start", "tools.reshade.inspect.start",
        "tools.reshade.install.start", "wine.dll_overrides.get", "wine.dll_overrides.set.start",
        "wine.dependencies.list.start", "wine.dependencies.install.start"),
    "wizards.modio_settings.ModioSettingsWizard": (
        "modio.credentials.get", "modio.credentials.set.start", "modio.credentials.clear.start"),
    "wizards.fnv_4gb_patch.Fnv4GbPatchWizard": (
        "tools.4gb_patch.inspect.start", "tools.4gb_patch.apply.start", "tools.4gb_patch.restore.start"),
    "wizards.oblivion_4gb_patch.Oblivion4GbPatchWizard": (
        "tools.4gb_patch.inspect.start", "tools.4gb_patch.apply.start", "tools.4gb_patch.restore.start"),
    "wizards.bethesda_register_game_path.RegisterGamePathWizard": (
        "wine.game_registry.inspect.start", "wine.game_registry.write.start",
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
        strip_top_dir = option.get("strip_top_dir", tool.extra.get("strip_top_dir", True))
        if not isinstance(strip_top_dir, bool):
            raise JobError("invalid_tool_catalogue", "A tool's archive folder policy is invalid.")
        versions.append({
            "index": index, "label": option.get("label") or tool.label,
            "description": option.get("description") or "",
            "archive_keywords": [str(value).lower() for value in
                                 (option.get("archive_keywords") or tool.extra.get("archive_keywords") or [])],
            "source": _source(option),
            "strip_top_dir": strip_top_dir,
        })
    return {"version_required": bool(tool.extra.get("versions")),
            "install_modes": ["game", "root", "mod"], "versions": versions}


def _label(template, args):
    return template.format(*args) if args else template


def _catalogue(game, profile):
    from Utils.bethesda.synthesis import WIZARD_CLASS as SYNTHESIS_CLASS, installation_options as synthesis_options
    from Utils.bethesda.wrye_bash import WIZARD_CLASS as WRYE_CLASS, options as wrye_options
    from Utils.bethesda.lod import WIZARD_CLASSES as LOD_CLASSES, tool_options as lod_options
    from Utils.bethesda.xedit_install import WIZARD_CLASSES, tool_options
    from Utils.wizards.payload import PayloadInstallError
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
                "workflow_available": tool.dialog_class_path in (
                    "wizards.modio_settings.ModioSettingsWizard", "wizards.reshade.ReShadeWizard",
                    "wizards.sdv_smapi.SmapiWizard",
                    "wizards.bethesda_register_game_path.RegisterGamePathWizard",
                    "wizards.bsa_pack_candidates.BsaPackCandidatesWizard",
                    "wizards.fnv_4gb_patch.Fnv4GbPatchWizard", "wizards.oblivion_4gb_patch.Oblivion4GbPatchWizard"),
                "related_methods": list(_RELATED_METHODS.get(tool.dialog_class_path, ())),
            },
        }
        if tool.dialog_class_path in WIZARD_CLASSES:
            try:
                row["xedit"] = tool_options(tool)
            except PayloadInstallError as exc:
                raise JobError("invalid_tool_catalogue", str(exc), exc.details) from exc
            row["headless"]["related_methods"] = [
                "tools.xedit.acquire.start", "tools.xedit.inspect.start", "tools.xedit.install.start",
                "tools.xedit.run.plan.start", "tools.xedit.run.start"]
            row["headless"]["workflow_available"] = True
        if tool.dialog_class_path in LOD_CLASSES:
            try:
                row["lod"] = lod_options(selected, tool)
            except PayloadInstallError as exc:
                raise JobError("invalid_tool_catalogue", str(exc), exc.details) from exc
            row["headless"]["workflow_available"] = True
        if tool.dialog_class_path == "wizards.sdv_smapi.SmapiWizard":
            from .smapi import source_choice
            row["smapi"] = {**source_choice(), "install_modes": ["game", "root", "mod"]}
        if tool.dialog_class_path == WRYE_CLASS:
            row["wrye_bash"] = wrye_options()
            row["headless"]["workflow_available"] = True
        if tool.dialog_class_path == SYNTHESIS_CLASS:
            row["synthesis"] = synthesis_options()
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
