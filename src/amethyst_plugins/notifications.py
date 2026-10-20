from __future__ import annotations

import re
from copy import deepcopy


def notification_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", value):
        raise ValueError("Notification IDs must use the plugin ID format.")
    return value


def normalize_notification(plugin, value):
    if not isinstance(value, dict) or set(value) - {"id", "title", "message", "severity", "details", "actions", "toast"}:
        raise ValueError("Expected a notification object with known fields.")
    result = {"id": notification_id(value.get("id")), "title": value.get("title", plugin.name),
              "message": value.get("message"), "severity": value.get("severity", "info"),
              "details": value.get("details", ""), "actions": deepcopy(value.get("actions", [])),
              "toast": value.get("toast", True)}
    for key, minimum, maximum in (("title", 1, 200), ("message", 1, 4000), ("details", 0, 64000)):
        text = result[key]
        if not isinstance(text, str) or len(text) > maximum or minimum and not text.strip():
            raise ValueError(f"Invalid notification {key} (maximum {maximum} characters).")
    if result["severity"] not in ("info", "success", "warning", "error") or type(result["toast"]) is not bool:
        raise ValueError("Invalid notification severity or toast setting.")
    actions = result["actions"]
    if not isinstance(actions, list) or len(actions) > 8:
        raise ValueError("Notifications support at most eight actions.")
    fields = {"report": set(), "select": {"location", "names"}, "open_plugin": set(),
              "open_tab": {"target"}, "run_action": {"action_id", "location", "names"},
              "run_command": {"command_id"}}
    for action in actions:
        if not isinstance(action, dict) or not isinstance(action.get("type"), str) or action["type"] not in fields:
            raise ValueError("Invalid notification action type.")
        if set(action) - ({"type", "label"} | fields[action["type"]]):
            raise ValueError("Unknown notification action fields.")
        label = action.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 100:
            raise ValueError("Notification action labels must contain 1 to 100 characters.")
        if action["type"] in ("select", "run_action"):
            names = action.get("names", [])
            if (action.get("location") not in ("modlist", "plugins") or not isinstance(names, list)
                    or len(names) > 256 or any(not isinstance(n, str) or not n or len(n) > 1000 for n in names)
                    or action["type"] == "select" and not names):
                raise ValueError("Selection actions require modlist/plugins and at most 256 names.")
        if action["type"] == "run_action" and not any(a.id == action.get("action_id") for a in plugin.context_menu_actions):
            raise ValueError("run_action must reference a declared context-menu action.")
        if action["type"] == "run_command" and not any(c.id == action.get("command_id") for c in plugin.commands):
            raise ValueError("run_command must reference a declared command.")
        if action["type"] == "open_tab" and not any(e.id == action.get("target") and e.location == "tab" for e in plugin.ui_extensions):
            raise ValueError("open_tab must reference a declared tab.")
    return result
