from __future__ import annotations

import importlib
from Utils import json_limits as json
import re
import sys
from contextlib import redirect_stdout


_themes: dict[str, dict] | None = None


def _read_theme(path, directory, builtins):
    path = path.resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Theme file must remain inside the plugin folder.")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Theme exceeds 1 MiB.")
    data = json.read_json(path, max_bytes=1024 * 1024)
    if not isinstance(data, dict):
        raise ValueError("Expected a theme JSON object.")
    name, palette = data.get("NAME"), data.get("PALETTE")
    base = data.get("BASE", "amethyst")
    if (not isinstance(name, str) or not name.strip() or len(name) > 200
            or not isinstance(palette, dict) or not palette or len(palette) > 1024
            or not isinstance(base, str) or base not in builtins):
        raise ValueError("Themes require NAME, a nonempty PALETTE, and a built-in BASE.")
    for key, value in palette.items():
        values = value if isinstance(value, list) else [value]
        if (not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", key)
                or isinstance(value, list) and len(value) != 2
                or any(not isinstance(v, str) or len(v) > 128 or not re.fullmatch(
                    r"(?:#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}|[A-Za-z][A-Za-z0-9]*|[0-9]+)", v)
                       for v in values)):
            raise ValueError(f"Invalid palette value for {key}.")
    module = importlib.import_module(f"themes.{base}")
    appearance = data.get("CTK_APPEARANCE", getattr(module, "CTK_APPEARANCE", "dark"))
    if appearance not in ("light", "dark"):
        raise ValueError("CTK_APPEARANCE must be light or dark.")
    return {"name": name.strip(), "palette": {**module.PALETTE, **palette},
            "appearance": appearance}


def load_theme_packs(directory=None):
    """Reload enabled JSON theme packs after queued plugin changes apply."""
    from amethyst_plugins.catalog import discover
    from themes import _iter_theme_modules

    global _themes
    with redirect_stdout(sys.stderr):
        try:
            plugins, errors = discover(None, directory)
        except (OSError, ValueError, TypeError) as exc:
            plugins, errors = [], [f"Theme pack discovery: {exc}"]
        builtins = set(_iter_theme_modules())
        loaded = {}
        for plugin in sorted(plugins, key=lambda p: p.id):
            for tid, path in plugin.themes:
                try:
                    loaded[f"plugin:{plugin.id}:{tid}"] = _read_theme(path, plugin.directory, builtins)
                except (OSError, ValueError, TypeError, RecursionError) as exc:
                    errors.append(f"{plugin.id}: theme {tid}: {exc}")
    _themes = loaded
    return errors


def plugin_themes():
    if _themes is None:
        for error in load_theme_packs():
            print(f"[theme-packs] {error}", file=sys.stderr, flush=True)
    return _themes
