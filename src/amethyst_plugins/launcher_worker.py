from __future__ import annotations

import contextlib
import importlib.util
from Utils import json_limits as json
import sys


def main():
    output = sys.stdout
    try:
        request = json.loads(sys.stdin.buffer.read(4 * 1024 * 1024 + 1), max_bytes=4 * 1024 * 1024)
        from .catalog import discover, plugin_directory, settings_path, data_path
        plugins, errors = discover(None)
        plugin_id, provider_id = request["provider_id"].split("/", 1)
        plugin = next(p for p in plugins if p.id == plugin_id)
        provider = next(p for p in plugin.launcher_providers if p.id == provider_id)
        name = "_amethyst_launcher_provider"
        with contextlib.redirect_stdout(sys.stderr):
            spec = importlib.util.spec_from_file_location(
                name, provider.entrypoint, submodule_search_locations=[str(provider.entrypoint.parent)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            def read(path):
                try:
                    if path.stat().st_size > 1024 * 1024:
                        raise ValueError("Plugin data exceeds 1 MiB.")
                    return json.read_json(path, max_bytes=1024 * 1024)
                except FileNotFoundError:
                    return {}
            settings = {s["key"]: s["default"] for s in plugin.settings}
            settings.update(read(settings_path(plugin)))
            if settings.get("enabled") is False:
                raise ValueError("Launcher provider is disabled.")
            context = {"api_version": 1, "plugin_id": plugin.id,
                       "provider_id": request["provider_id"],
                       "plugin_dir": str(plugin_directory(plugin)),
                       "data_path": str(data_path(plugin)), "settings": settings,
                       "data": read(data_path(plugin)),
                       "log": lambda message: print(str(message), file=sys.stderr),
                       "check_cancelled": lambda: None}
            method = request["method"]
            if method not in {"list_entries", "get_entry", "update_entry"}:
                raise ValueError("Unknown launcher provider method.")
            callback = getattr(module, method, None)
            if not callable(callback):
                raise ValueError("This launcher provider is read-only." if method == "update_entry"
                                 else f"Missing provider callback: {method}")
            result = callback(context, *request.get("args", []))
            writable = callable(getattr(module, "update_entry", None))
        response = {"result": result, "writable": writable}
        encoded = json.dumps(response, max_bytes=4 * 1024 * 1024 - 1)
    except Exception as exc:
        encoded = json.dumps({"error": str(exc) or type(exc).__name__})
    output.write(encoded + "\n")
    output.flush()


if __name__ == "__main__":
    main()
