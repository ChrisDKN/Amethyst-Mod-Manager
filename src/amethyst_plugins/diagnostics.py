from __future__ import annotations

from difflib import get_close_matches
from pathlib import Path

from Utils import json_limits as json


MANIFEST_FIELDS = frozenset("""api_version id name description entrypoint methods settings ui
    ui_extensions service version author min_manager_version context_menu_actions launch_hooks
    menu_tab flags flag_provider icon_overrides columns column_provider filters filter_provider
    diagnostic_provider lifecycle_hooks file_previews commands installers download_providers
    requires_game launcher_providers persistent_directories game_handlers themes show_in_menu
    modlist_sections game_ids wizards""".split())


def manifest_warnings(data):
    messages = []
    for key in sorted(set(data) - MANIFEST_FIELDS):
        match = get_close_matches(key, MANIFEST_FIELDS, n=1)
        suggestion = f" Did you mean '{match[0]}'?" if match else ""
        messages.append(f"Unknown manifest field '{key}'.{suggestion}")
    return tuple(messages)


def validate_path(path):
    from .catalog import compatibility_error, parse_plugin, settings_error
    from amethyst_backend.methods import METHOD_NAMES
    path = Path(path).expanduser().absolute()
    if path.is_dir():
        path /= "plugin.json"
    result = {"path": str(path), "errors": [], "warnings": []}
    try:
        data = json.read_json(path, max_bytes=65536)
        plugin, _ = parse_plugin(data, path.parent, check_compatibility=False)
        result.update(plugin_id=plugin.id, warnings=list(plugin.warnings))
        if error := compatibility_error(plugin):
            result["errors"].append(error)
        for method in sorted(set(plugin.methods) - set(METHOD_NAMES)):
            result["errors"].append(f"Unavailable backend method: {method}")
        if problem := settings_error(plugin):
            result["warnings"].append(f"Unreadable plugin settings: {problem}")
    except (OSError, ValueError, TypeError) as exc:
        result["errors"].append(str(exc))
    result["ok"] = not result["errors"]
    return result
