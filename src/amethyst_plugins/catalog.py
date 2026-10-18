from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Plugin:
    id: str
    name: str
    description: str
    entrypoint: Path
    methods: tuple[str, ...]
    settings: tuple[dict, ...] = ()
    ui: str = "stdio"


def plugins_dir():
    from Utils.config_paths import get_plugins_dir
    return get_plugins_dir()


def read_plugin(path):
    if path.stat().st_size > 65536:
        raise ValueError("Manifest exceeds 64 KiB.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or type(data.get("api_version")) is not int or data["api_version"] != 1:
        raise ValueError("Expected plugin api_version 1.")
    plugin_id = data.get("id", "")
    if not isinstance(plugin_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", plugin_id):
        raise ValueError("Invalid plugin id.")
    for key in ("name", "description", "entrypoint"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Missing {key}.")
    entrypoint = (path.parent / data["entrypoint"]).resolve()
    if (not entrypoint.is_relative_to(path.parent.resolve())
            or entrypoint.suffix != ".py" or not entrypoint.is_file()):
        raise ValueError("Entrypoint must be a Python file inside the plugin folder.")
    methods = data.get("methods")
    if not isinstance(methods, list) or not all(isinstance(m, str) and m for m in methods):
        raise ValueError("Expected a list of headless methods.")
    ui = data.get("ui", "stdio")
    if ui not in ("stdio", "qt"):
        raise ValueError("Expected ui to be stdio or qt.")
    settings = data.get("settings", [])
    if not isinstance(settings, list) or len(settings) > 32:
        raise ValueError("Expected at most 32 settings.")
    keys = set()
    for item in settings:
        if (not isinstance(item, dict) or not isinstance(item.get("key"), str)
                or not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", item["key"])
                or item["key"] in keys or not isinstance(item.get("label"), str)
                or item.get("type") not in {"boolean", "text"}):
            raise ValueError("Invalid plugin setting.")
        expected = bool if item["type"] == "boolean" else str
        if type(item.get("default")) is not expected:
            raise ValueError("Setting default has the wrong type.")
        keys.add(item["key"])
    games = data.get("game_ids", [])
    if not isinstance(games, list) or not all(isinstance(g, str) for g in games):
        raise ValueError("game_ids must be a list of game identifiers.")
    return Plugin(plugin_id, data["name"], data["description"], entrypoint,
                  tuple(methods), tuple(settings), ui), games


def discover(game_id, directory=None):
    plugins, errors = [], []
    seen = set()
    root = Path(directory) if directory is not None else plugins_dir()
    paths = sorted(root.glob("*/plugin.json"))
    paths.extend(sorted((root / "Headless").glob("*/plugin.json")))
    for path in paths:
        try:
            plugin, games = read_plugin(path)
            if plugin.id in seen:
                raise ValueError(f"Duplicate plugin id: {plugin.id}")
            seen.add(plugin.id)
            if not games or game_id in games:
                plugins.append(plugin)
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"{path.parent.relative_to(root)}: {exc}")
    return sorted(plugins, key=lambda p: p.name.casefold()), errors


def settings_path(plugin):
    from Utils.config_paths import get_config_dir
    return get_config_dir() / "plugin_settings" / f"{plugin.id}.json"


def load_settings(plugin):
    try:
        saved = json.loads(settings_path(plugin).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    return {s["key"]: saved.get(s["key"], s["default"])
            if type(saved.get(s["key"], s["default"])) is type(s["default"])
            else s["default"] for s in plugin.settings}


def save_settings(plugin, settings):
    from Utils.atomic_write import write_atomic
    path = settings_path(plugin)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(settings, ensure_ascii=True).encode())
