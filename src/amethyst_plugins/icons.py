from __future__ import annotations

from pathlib import Path


ICONS_DIR = Path(__file__).resolve().parents[1] / "icons"
_overrides: dict[str, Path] = {}


def load_icon_packs(directory=None, *, validate):
    """Load enabled icon declarations once, after queued plugin changes apply."""
    from amethyst_plugins.catalog import discover, load_settings

    try:
        plugins, errors = discover(None, directory)
    except OSError as exc:
        plugins, errors = [], [f"Icon pack discovery: {exc}"]
    overrides, owners = {}, {}
    for plugin in sorted(plugins, key=lambda p: p.id):
        controls = dict(plugin.icon_settings)
        settings = load_settings(plugin) if controls else {}
        for name, path in plugin.icon_overrides:
            if name in controls and not settings[controls[name]]:
                continue
            try:
                if not validate(path):
                    raise ValueError("Unsupported or invalid image.")
            except (OSError, ValueError) as exc:
                errors.append(f"{plugin.id}: {name}: {exc}")
                continue
            if name in overrides:
                errors.append(f"Icon {name}: {owners[name]} takes precedence over {plugin.id}.")
                continue
            overrides[name], owners[name] = path, plugin.id
    _overrides.clear()
    _overrides.update(overrides)
    return errors


def icon_override(name: str) -> Path | None:
    return _overrides.get(name)


def has_icon_overrides() -> bool:
    return bool(_overrides)


def icon_path(name: str) -> Path:
    return icon_override(name) or ICONS_DIR / name
