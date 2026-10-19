"""
GUI-neutral wizard availability gates.

Game files decide which wizard tools to offer by probing the install (exe in
staging, dll winning in the filemap). Those probes used to live in the Tk
wizard modules (wizards/bodyslide.py, sse_display_tweaks.py, engine_fixes.py),
which import customtkinter at module level - the Qt app can't import them, so
the neutral copies live here and the game files import from this module.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
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

_wizard_snapshot = ContextVar("wizard_snapshot", default=None)


@contextmanager
def wizard_snapshot_scope(game, snapshot):
    token = _wizard_snapshot.set((game, snapshot))
    try:
        yield
    finally:
        _wizard_snapshot.reset(token)


def _snapshot_for_game(game):
    current = _wizard_snapshot.get()
    if current is not None and current[0] is game and current[1] is not None:
        return current[1]
    from Utils.filegraph.service import active_snapshot
    return active_snapshot(game)


def _as_names(exe_name) -> tuple[str, ...]:
    """Accept a single name or an iterable of candidate names."""
    if isinstance(exe_name, str):
        return (exe_name,)
    return tuple(exe_name)


def find_staged_exe(game: "BaseGame", exe_name) -> Path | None:
    """Find an installed tool executable in the Filegraph snapshot."""
    staging = game.get_effective_mod_staging_path()
    if staging is None or not staging.is_dir():
        return None
    wanted = {n.lower() for n in _as_names(exe_name)}
    if not wanted:
        return None

    try:
        from Utils.filegraph.service import source_path
        snapshot = _snapshot_for_game(game)
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
        from Utils.filegraph.service import source_path
        winner = _snapshot_for_game(game).winner_by_suffix(rel_suffix)
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
