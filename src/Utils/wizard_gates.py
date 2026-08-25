"""
GUI-neutral wizard gating helpers.

Game files decide which wizard tools to offer by probing the install (exe in
staging, dll winning in the filemap). Those probes used to live in the Tk
wizard modules (wizards/bodyslide.py, sse_display_tweaks.py, engine_fixes.py),
which import customtkinter at module level - the Qt app can't import them, so
the neutral copies live here and the game files import from this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# Managed-mod names + prefix-relative paths used by the config wizards.
SDT_MOD_NAME = "SSE Display Tweaks ini"
SDT_REL_INI_PATH = "SKSE/Plugins/SSEDisplayTweaks.ini"
SDT_REL_DLL_PATH = "SKSE/Plugins/SSEDisplayTweaks.dll"

EF_MOD_NAME = "EngineFixes toml"
EF_REL_TOML_PATH = "SKSE/Plugins/EngineFixes.toml"
EF_REL_DLL_PATH = "SKSE/Plugins/EngineFixes.dll"


def _as_names(exe_name) -> tuple[str, ...]:
    """Accept a single name or an iterable of candidate names."""
    if isinstance(exe_name, str):
        return (exe_name,)
    return tuple(exe_name)


def find_staged_exe(game: "BaseGame", exe_name) -> Path | None:
    """Find *exe_name* (one name or several candidates) anywhere in the mod
    staging tree, returning its full on-disk path (or None).

    Used to gate wizards on whether a tool's exe is installed. On a large
    modlist a raw ``staging.rglob()`` walks tens of thousands of files and is
    called several times per Wizard-menu open - so this reads the memory-cached
    ``modindex.bin`` (every mod's file list, kept fresh on install/remove/
    refresh) instead, and only falls back to a disk walk when the index is
    missing or the match can't be resolved to a real file.
    """
    staging = game.get_effective_mod_staging_path()
    if staging is None or not staging.is_dir():
        return None
    wanted = {n.lower() for n in _as_names(exe_name)}
    if not wanted:
        return None

    try:
        from Utils.filegraph_service import active_snapshot, source_path
        snapshot = active_snapshot(game)
        for mod_name, relative in snapshot.raw_files_by_basename(wanted):
            candidate = source_path(game, mod_name, relative)
            if candidate.is_file():
                return candidate
    except Exception:
        return None
    return None


# Backwards-compatible alias (the original name the game files import).
find_mod_exe = find_staged_exe


def filemap_find(game: "BaseGame", rel_suffix: str) -> Path | None:
    """Return the exact source of the winning destination suffix."""
    try:
        from Utils.filegraph_service import active_snapshot, source_path
        winner = active_snapshot(game).winner_by_suffix(rel_suffix)
        if winner is None:
            return None
        candidate = source_path(game, winner.mod_name, winner.source_rel)
        return candidate if candidate.is_file() else None
    except Exception:
        return None


def sse_display_tweaks_installed(game: "BaseGame") -> bool:
    """True when SSEDisplayTweaks.dll is the winning file in the filemap."""
    return filemap_find(game, SDT_REL_DLL_PATH) is not None


def engine_fixes_installed(game: "BaseGame") -> bool:
    """True when EngineFixes.dll is the winning file in the filemap."""
    return filemap_find(game, EF_REL_DLL_PATH) is not None
