"""Shared toolkit-neutral logic for the Data tab.

The Data tab shows the merged deployment layout as a folder tree, with the
winning mod per file and conflict highlighting - "what
actually lands in the game folder". The intricate bit is resolving each filemap
entry to its real deploy destination (UE5 rule resolution + custom routing rules
with include_siblings / flatten / prefix+root hiding). That logic is lifted almost
verbatim from the Tk ModFiles… er, Data mixin (gui/plugin_panel_data.py) so the Qt
Data tab stays in lockstep. Pure stdlib + Utils.*/Games.* - no GUI toolkit.

Production callers provide one immutable Filegraph snapshot generation.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def deploys_to_subfolder(game) -> bool:
    """True when mods deploy into a SUBFOLDER of the game root (Skyrim's Data/,
    Morrowind's Data Files/) - the Data tab shows that subfolder, so files that
    deploy to the game root fall outside its scope and must be hidden. False
    for root-deployed games (deploy dir == game root, e.g. Witcher 3), where
    root-bound files land inside the shown tree. Falls back to the mods_dir
    property when the paths aren't configured."""
    try:
        gp = game.get_game_path()
        dp = game.get_mod_data_path()
    except Exception:
        gp = dp = None
    if gp is not None and dp is not None:
        return Path(dp) != Path(gp)
    return bool((getattr(game, "mods_dir", None) or "").strip("/ "))


# ---------------------------------------------------------------------------
# Front half: parse filemap.txt + drop hidden mods, then resolve destinations
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
def data_display_paths(game, entries: list[tuple[str, str]]) -> list[str]:
    """Return the path shown for each resolved Data-tab entry.

    Most games display the resolved deployment path verbatim.  Loader-based
    games can provide ``data_tab_display_paths(entries)`` to add meaningful
    destination roots without changing the logical paths used by conflict
    detection.  Keeping those two concepts separate matters for Elden Ring:
    me3 assets are served from staging, while Elden Mod Loader files are
    physically copied below ``<root>/Game``.
    """
    fallback = [path for path, _mod in entries]
    hook = getattr(game, "data_tab_display_paths", None)
    if not callable(hook):
        return fallback
    try:
        shown = list(hook(entries))
    except Exception:
        return fallback
    if len(shown) != len(entries) or not all(
            isinstance(path, str) and path for path in shown):
        return fallback
    return shown


def build_data_tree(entries: list[tuple[str, str]],
                    contested_keys: set[str] | None = None, *,
                    only_conflicts: bool = False,
                    inc_exts: frozenset | None = None,
                    exc_exts: frozenset | None = None,
                    keep_extra=None,
                    display_paths: Sequence[str] | None = None) -> dict:
    """Build the nested tree dict from resolved [(rel_path, mod_name)] entries.

    Folders are sub-dicts; files live in a "__files__" list of
    (fname, mod_name, rel_key_lower). Mirrors Tk _build_data_tree_from_entries
    (plugin_panel_data.py:879-903). only_conflicts / inc_exts / exc_exts apply the
    filter side panel; keep_extra(rel_key_lower, mod) is an optional extra
    predicate (used for the search box).  ``display_paths`` may supply a
    presentation-only path for each entry; filtering and conflict lookup still
    use the original resolved path."""
    contested_keys = contested_keys or set()
    inc_exts = inc_exts or frozenset()
    exc_exts = exc_exts or frozenset()
    tree: dict = {}
    if display_paths is not None and len(display_paths) != len(entries):
        display_paths = None
    for entry_idx, (rel_path, mod_name) in enumerate(entries):
        rel_norm = rel_path.replace("\\", "/")
        rel_key_lower = rel_norm.lower()
        display_norm = (
            display_paths[entry_idx].replace("\\", "/")
            if display_paths is not None else rel_norm
        )
        if only_conflicts and rel_key_lower not in contested_keys:
            continue
        if inc_exts or exc_exts:
            dot = rel_key_lower.rfind(".")
            slash = rel_key_lower.rfind("/")
            if dot <= slash:
                if inc_exts:
                    continue
            else:
                ext = rel_key_lower[dot:]
                if inc_exts and ext not in inc_exts:
                    continue
                if exc_exts and ext in exc_exts:
                    continue
        if keep_extra is not None and not keep_extra(rel_key_lower, mod_name):
            continue
        parts = display_norm.split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault("__files__", []).append(
            (parts[-1], mod_name, rel_key_lower))
    return tree


def filetype_counts(entries: list[tuple[str, str]]) -> dict[str, int]:
    """Map extension (lower, with dot) → file count across resolved entries."""
    counts: dict[str, int] = {}
    for rel_path, _mod in entries:
        rl = rel_path.replace("\\", "/").lower()
        dot = rl.rfind(".")
        slash = rl.rfind("/")
        if dot > slash:
            ext = rl[dot:]
            counts[ext] = counts.get(ext, 0) + 1
    return counts
