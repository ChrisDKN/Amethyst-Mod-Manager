"""Plugins disabled via the Mod Files tab.

The Mod Files tab lets the user tick a "Disable" checkbox on individual files
inside a mod, which excludes them from deploy (stored in
``profile_state.json`` as ``{mod_name: [raw_key_lower, ...]}``). When one of
those excluded files is a plugin (``.esp/.esm/.esl``), the plugin never deploys -
so it should read as "disabled" in the Plugins tab filter and its owning mod
should surface in the modlist "Mods with disabled plugins" filter.

This is separate from ``plugins.txt`` enable/disable (the Plugins tab's own
toggle). The consumers union the two sources.
"""

from __future__ import annotations

from pathlib import Path

_DEFAULT_PLUGIN_EXTS = (".esp", ".esm", ".esl")


def _plugin_exts(game) -> tuple[str, ...]:
    exts = tuple(e.lower() for e in (getattr(game, "plugin_extensions", []) or []))
    return exts or _DEFAULT_PLUGIN_EXTS


def mods_with_disabled_plugins(profile_dir: "Path | None", game=None) -> set[str]:
    """Mods that have at least one plugin file disabled in the Mod Files tab.

    Reads ``excluded_mod_files`` for *profile_dir* and keeps mods whose excluded
    keys include a plugin file (by extension). Returns an empty set on any error.
    """
    if profile_dir is None:
        return set()
    exts = _plugin_exts(game)
    out: set[str] = set()
    try:
        from Utils.profiles.state import read_excluded_mod_files
        for mod, keys in (read_excluded_mod_files(profile_dir, None) or {}).items():
            for key in keys:
                base = str(key).rsplit("/", 1)[-1].lower()
                if base.endswith(exts):
                    out.add(mod)
                    break
    except Exception:
        return set()
    return out


def disabled_plugin_files(profile_dir: "Path | None", game=None) -> set[str]:
    """Plugin filenames (lowercase basename) disabled via the Mod Files tab.

    Every excluded key that is a plugin file contributes its basename. Used by the
    Plugins-tab "Disabled plugins" filter to treat a Mod-Files-excluded plugin as
    disabled. Returns an empty set on any error.
    """
    if profile_dir is None:
        return set()
    exts = _plugin_exts(game)
    out: set[str] = set()
    try:
        from Utils.profiles.state import read_excluded_mod_files
        for _mod, keys in (read_excluded_mod_files(profile_dir, None) or {}).items():
            for key in keys:
                base = str(key).rsplit("/", 1)[-1].lower()
                if base.endswith(exts):
                    out.add(base)
    except Exception:
        return set()
    return out
