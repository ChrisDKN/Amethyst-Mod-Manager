"""Toolkit-neutral discovery + content search for the Text Files tab.

Lists config/text files from four sources - mod folders (via filemap.txt), the
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
)
_SOURCE_ORDER = {key: i for i, (key, _label) in enumerate(SOURCE_LABELS)}

# Profile subfolders surfaced by other sources / holding backups - skipped so we
# don't dump thousands of duplicate mod files under "Profile".
_PROFILE_SKIP_DIRS = frozenset({"mods", "overwrite", "root_folder", "backups",
                                "fomod"})


def entry_source(mod_name: str) -> str:
    if mod_name == SRC_GAME:
        return "game"
    if mod_name == SRC_PROFILE:
        return "profile"
    if mod_name == SRC_MYGAMES:
        return "mygames"
    return "mod"


def display_name(rel_path: str) -> str:
    """'<parent>/<filename>' when nested, else just '<filename>' (Tk parity)."""
    p = Path(rel_path)
    if p.parent != Path("."):
        return f"{p.parent.name}/{p.name}"
    return p.name


def sort_key(entry: tuple[str, str, Path]) -> tuple:
    rel_path, mod_name, _p = entry
    src = entry_source(mod_name)
    return (_SOURCE_ORDER.get(src, len(_SOURCE_ORDER)),
            rel_path.lower(), mod_name.lower())


def resolve_file_path(rel_path: str, mod_name: str,
                      staging_root: Path,
                      dir_cache: dict | None = None) -> Path | None:
    """Resolve a filemap entry to a full path (case-insensitive fallback).

    *dir_cache* - optional {dir_path: {lower_name: real_name}} memo shared across
    calls. Filemap entries under the same mod share parent directories, so caching
    each directory's case-folded listing turns the fallback from O(entries × depth)
    ``iterdir`` calls into one listing per directory. Pass an empty dict once per
    scan; omit it (None) for a one-off resolve."""
    if staging_root is None:
        return None
    from Utils.filemap import OVERWRITE_NAME, ROOT_FOLDER_NAME
    rel_path = rel_path.replace("\\", "/")
    if mod_name == OVERWRITE_NAME:
        base = staging_root.parent / "overwrite"
    elif mod_name == ROOT_FOLDER_NAME:
        base = staging_root.parent / "Root_Folder"
    else:
        base = staging_root / mod_name
    exact = base / rel_path
    if exact.exists():
        return exact

    def _listing(d: Path) -> dict | None:
        """{lower_name: real_name} for directory *d*, or None if not a dir."""
        if dir_cache is not None:
            cached = dir_cache.get(d)
            if cached is not None:
                return cached if cached else None
        try:
            entries = {c.name.lower(): c.name for c in d.iterdir()}
        except OSError:
            entries = {}
        if dir_cache is not None:
            dir_cache[d] = entries
        return entries or None

    current = base
    for segment in rel_path.split("/"):
        listing = _listing(current)
        if listing is None:
            return exact
        real = listing.get(segment.lower())
        if real is None:
            return exact
        current = current / real
    return current


def _parse_filemap(filemap_path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        with filemap_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if "\t" in line:
                    rel, mod = line.split("\t", 1)
                    out.append((rel, mod))
    except OSError:
        return []
    return out


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
                        filemap_path: Path | None,
                        staging_root: Path | None) -> list[tuple[str, str, Path]]:
    """Return sorted [(rel_path, source_mod, full_path)] across all four sources.
    Port of Tk `_refresh_ini_files_tab`. Deferred/expensive - call off the hot
    path (recursive game + My Games scans)."""
    entries: list[tuple[str, str, Path]] = []

    # 1. Mod-deployed text files (filemap). Entries under the same mod share
    #    parent dirs, so a listing cache makes the case-insensitive fallback
    #    resolve each directory once instead of per-file.
    if filemap_path and Path(filemap_path).is_file() and staging_root is not None:
        dir_cache: dict = {}
        for rel, mod in _parse_filemap(Path(filemap_path)):
            dot = rel.rfind(".")
            slash = max(rel.rfind("/"), rel.rfind("\\"))
            if dot <= slash or rel[dot:].lower() not in TEXT_EXTENSIONS:
                continue
            full = resolve_file_path(rel, mod, staging_root, dir_cache)
            if full is not None:
                entries.append((rel, mod, full))

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
