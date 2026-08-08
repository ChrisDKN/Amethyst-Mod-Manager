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

    # Fast path: scan the in-memory index. rel_key is lowercased with "/"
    # separators, so a basename match is the last path segment.
    try:
        from Utils.filemap import read_mod_index
        index = read_mod_index(staging.parent / "modindex.bin")
    except Exception:
        index = None
    if index is not None:
        for mod_name, (normal_files, root_files) in index.items():
            for files in (normal_files, root_files):
                for rel_key, rel_str in files.items():
                    if rel_key.rsplit("/", 1)[-1] in wanted:
                        candidate = staging / mod_name / rel_str
                        if candidate.is_file():
                            return candidate
        # Index present but no match - trust it (it's the deploy source of
        # truth) rather than paying for a full-tree walk that would find the
        # same nothing.
        return None

    # No usable index - fall back to the disk walk.
    for name in _as_names(exe_name):
        for candidate in staging.rglob(name):
            if candidate.is_file():
                return candidate
    return None


# Backwards-compatible alias (the original name the game files import).
find_mod_exe = find_staged_exe


def filemap_find(game: "BaseGame", rel_suffix: str) -> Path | None:
    """Return the staging path of the file whose filemap entry ends with
    rel_suffix.

    Matches the winning mod for that relative path in the active profile's
    ``filemap.txt`` and resolves it to ``<staging>/<mod>/<rel>``.
    """
    try:
        filemap_path = game.get_effective_filemap_path()
        staging = game.get_effective_mod_staging_path()
    except Exception:
        return None
    if not filemap_path.is_file():
        return None
    target = rel_suffix.lower().replace("\\", "/")
    try:
        text = filemap_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if "\t" not in line:
            continue
        rel_str, mod_name = line.split("\t", 1)
        norm = rel_str.replace("\\", "/").lower()
        if norm.endswith(target):
            candidate = staging / mod_name / rel_str.replace("\\", "/")
            if candidate.is_file():
                return candidate
    return None


def sse_display_tweaks_installed(game: "BaseGame") -> bool:
    """True when SSEDisplayTweaks.dll is the winning file in the filemap."""
    return filemap_find(game, SDT_REL_DLL_PATH) is not None


def engine_fixes_installed(game: "BaseGame") -> bool:
    """True when EngineFixes.dll is the winning file in the filemap."""
    return filemap_find(game, EF_REL_DLL_PATH) is not None
