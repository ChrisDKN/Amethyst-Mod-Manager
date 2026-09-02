"""Toolkit-neutral discovery and content search for the Text Files tab.

Lists config/text files from four sources - resolved mod winners, the
active profile folder, the vanilla game folder, and (Bethesda) My Games - grouped
by source. Ported from the pure-Python parts of the Tk `gui/plugin_panel_ini.py`
(internally "Ini Files"; the UI is "Text Files") so the Qt tab stays in lockstep.
Pure stdlib + Utils.* - no GUI toolkit.
"""

from __future__ import annotations

import os
from pathlib import Path

TEXT_EXTENSIONS = frozenset({
    ".ini", ".json", ".toml", ".txt", ".cfg", ".conf", ".config",
    ".yaml", ".yml", ".xml", ".log", ".md",
})

# Synthetic source names used in the mod_name field for non-mod entries.
SRC_GAME = "Game Folder"
SRC_PROFILE = "Profile"
SRC_MYGAMES = "My Games"

SOURCE_LABELS = (
    ("mod", "Mod folders"),
    ("profile", "Profile"),
    ("game", "Game folder"),
    ("mygames", "My Games"),
    ("logs", "Logs"),
)
_SOURCE_ORDER = {key: i for i, (key, _label) in enumerate(SOURCE_LABELS)}

# Game-folder / My-Games .log files get their own top-level source so crash and
# script logs (the ones users are usually hunting for) aren't buried among the
# INIs. Mod/profile logs stay under their own source - those are shipped files.
LOG_EXTENSION = ".log"
_LOG_SOURCES = frozenset({"game", "mygames"})

# Profile subfolders surfaced by other sources / holding backups - skipped so we
# don't dump thousands of duplicate mod files under "Profile".
_PROFILE_SKIP_DIRS = frozenset({"mods", "overwrite", "root_folder", "backups",
                                "fomod"})


def entry_source(mod_name: str, rel_path: str | None = None) -> str:
    """Source key for an entry. Pass *rel_path* to route game/My-Games .log
    files into the synthetic "logs" source."""
    if mod_name == SRC_GAME:
        src = "game"
    elif mod_name == SRC_PROFILE:
        src = "profile"
    elif mod_name == SRC_MYGAMES:
        src = "mygames"
    else:
        src = "mod"
    if (rel_path and src in _LOG_SOURCES
            and rel_path.lower().endswith(LOG_EXTENSION)):
        return "logs"
    return src


def display_name(rel_path: str) -> str:
    """'<parent>/<filename>' when nested, else just '<filename>' (Tk parity)."""
    p = Path(rel_path)
    if p.parent != Path("."):
        return f"{p.parent.name}/{p.name}"
    return p.name


def sort_key(entry: tuple[str, str, Path]) -> tuple:
    rel_path, mod_name, _p = entry
    src = entry_source(mod_name, rel_path)
    return (_SOURCE_ORDER.get(src, len(_SOURCE_ORDER)),
            rel_path.lower(), mod_name.lower())


def _collect_profile_files(profile_dir: Path,
                           exts: frozenset) -> list[tuple[str, Path]]:
    if not profile_dir or not Path(profile_dir).is_dir():
        return []
    root = Path(profile_dir)
    out: list[tuple[str, Path]] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            if Path(dirpath) == root:
                dirnames[:] = [d for d in dirnames
                               if d.lower() not in _PROFILE_SKIP_DIRS]
            for name in filenames:
                fpath = Path(dirpath) / name
                if fpath.suffix.lower() not in exts:
                    continue
                if not fpath.is_file() or fpath.is_symlink():
                    continue
                out.append((fpath.relative_to(root).as_posix(), fpath))
    except OSError:
        return []
    return out


def _collect_mygames_files(game, exts: frozenset) -> list[tuple[str, Path]]:
    fn = getattr(game, "_mygames_paths", None) if game else None
    if not callable(fn):
        return []
    try:
        dirs = fn()
    except Exception:
        return []
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for mygames in dirs:
        mygames = Path(mygames)
        if not mygames.is_dir():
            continue
        stack = [str(mygames)]
        while stack:
            try:
                scan = os.scandir(stack.pop())
            except OSError:
                continue
            with scan:
                for de in scan:
                    try:
                        if de.is_dir(follow_symlinks=False):
                            stack.append(de.path)
                            continue
                    except OSError:
                        continue
                    name = de.name
                    dot = name.rfind(".")
                    if dot < 0 or name[dot:].lower() not in exts:
                        continue
                    try:
                        if de.is_symlink() or not de.is_file():
                            continue
                    except OSError:
                        continue
                    fpath = Path(de.path)
                    rel = fpath.relative_to(mygames).as_posix()
                    if rel in seen:
                        continue
                    seen.add(rel)
                    out.append((rel, fpath))
    return out


def discover_text_files(game, profile_dir: Path | None,
                        snapshot=None) -> list[tuple[str, str, Path]]:
    """Return sorted [(rel_path, source_mod, full_path)] across all four sources.
    Port of Tk `_refresh_ini_files_tab`. Deferred/expensive - call off the hot
    path (recursive game + My Games scans)."""
    entries: list[tuple[str, str, Path]] = []

    # 1. Mod-deployed text winners from one pinned graph generation.
    if snapshot is not None and game is not None:
        from Utils.filegraph.adapter import FLAG_TEXT
        from Utils.filegraph.service import source_path
        for winner in snapshot.flagged_winners(FLAG_TEXT):
            entries.append((
                winner.legacy_rel,
                winner.mod_name,
                source_path(game, winner.mod_name, winner.source_rel),
            ))

    # 2. Vanilla game folder (skip symlinks/hardlinks = deployed files). Use
    #    os.walk + scandir so the extension check (cheap) gates the stat (costly)
    #    - most game files aren't text and never get stat'd.
    game_path = (game.get_game_path()
                 if game and hasattr(game, "get_game_path") else None)
    if game_path and Path(game_path).is_dir():
        root = Path(game_path)
        stack = [str(root)]
        while stack:
            try:
                scan = os.scandir(stack.pop())
            except OSError:
                continue
            with scan:
                for de in scan:
                    try:
                        if de.is_dir(follow_symlinks=False):
                            stack.append(de.path)
                            continue
                    except OSError:
                        continue
                    name = de.name
                    dot = name.rfind(".")
                    if dot < 0 or name[dot:].lower() not in TEXT_EXTENSIONS:
                        continue
                    try:
                        if de.is_symlink() or not de.is_file():
                            continue
                        if de.stat(follow_symlinks=False).st_nlink > 1:
                            continue
                    except OSError:
                        continue
                    fpath = Path(de.path)
                    entries.append((fpath.relative_to(root).as_posix(),
                                    SRC_GAME, fpath))

    # 3. Profile folder.
    if profile_dir is not None:
        for rel, fpath in _collect_profile_files(Path(profile_dir),
                                                 TEXT_EXTENSIONS):
            entries.append((rel, SRC_PROFILE, fpath))

    # 4. My Games (Bethesda).
    for rel, fpath in _collect_mygames_files(game, TEXT_EXTENSIONS):
        entries.append((rel, SRC_MYGAMES, fpath))

    entries.sort(key=sort_key)
    return entries


def content_search(entries: list[tuple[str, str, Path]],
                   keyword: str) -> set[tuple[str, str]]:
    """Return {(rel_path, mod_name)} whose file text contains *keyword*
    (case-insensitive). Port of Tk `_run_ini_content_search`."""
    needle = keyword.casefold()
    matched: set[tuple[str, str]] = set()
    for rel, mod, full in entries:
        try:
            if not full.is_file():
                continue
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                if needle in f.read().casefold():
                    matched.add((rel, mod))
        except OSError:
            continue
    return matched
