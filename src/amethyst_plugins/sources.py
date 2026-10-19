from __future__ import annotations

from Utils.github.repositories import normalize_repository


def normalize_source(url, branch="", folder="", enabled=True):
    source = normalize_repository(url, branch, folder, enabled)
    if (source["repository"].lower(), source["branch"], source["folder"]) == (
            "chrisdkn/amethyst-mod-manager", "Resources", "Plugins/v2"):
        source["id"] = "builtin"
    return source


DEFAULT_SOURCE = normalize_source(
    "https://github.com/ChrisDKN/Amethyst-Mod-Manager", "Resources", "Plugins/v2")


def read_source(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid plugin repository configuration.")
    source = normalize_source(data["url"], data.get("branch", ""),
                              data.get("folder", ""), data.get("enabled", True))
    if data.get("id", source["id"]) != source["id"]:
        raise ValueError("Plugin repository identity does not match its configuration.")
    return source


def api_url(source):
    return f"https://api.github.com/repos/{source['repository']}"


def raw_url(source):
    return f"https://raw.githubusercontent.com/{source['repository']}"
