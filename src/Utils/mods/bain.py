"""
Utils.mods.bain
Stateless logic engine for BAIN (Wrye Bash bundled archive) installation.
No UI. detect_bain/resolve_bain_files mirror the role of fomod/installer.py.

Detection follows Wrye Bash's structural rule for a "complex" (type 2) package
rather than a numeric-prefix heuristic: a top-level folder is a sub-package if
it directly contains either a recognised data sub-folder (Meshes, Textures, …)
or a top-level data/doc file (.esp, .esm, .bsa, .ini, .txt, …). Folders that
Wrye Bash silently skips (``--`` prefix, ``bash``, ``omod conversion data``,
``wizard images``) don't count. See Mopy/bash/bosh/bain.py in the Wrye Bash
source (``_reset_cache`` / ``dataDirsPlus`` / ``_silentSkipsStart``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Recognised Data sub-folders (Wrye Bash: Bain.data_dirs ∪ wrye_bash_data_dirs ∪
# screenshot_dirs ∪ {'docs'}). A top-level folder containing one of these as an
# immediate child behaves like a simple package, so its parent is a sub-package.
_DATA_DIRS = {
    # game asset dirs
    "meshes", "textures", "sound", "music", "video", "ini", "scripts",
    "interface", "strings", "shaders", "materials", "sdf", "trees", "facegen",
    "distantlod", "lodsettings", "menus", "fonts", "dialogueviews", "grass",
    "seq", "asi", "calientetools", "skse", "obse", "f4se", "nvse", "mwse",
    "source", "tools", "docs",
    # morrowind / openmw asset + localisation dirs
    "l10n", "animation", "bookart", "distantland", "icons", "splash", "mge3",
    # screenshots
    "screenshots", "screens", "ss",
    # wrye bash dirs
    "bash patches", "bashtags", "ini tweaks",
}

# Data/config file extensions (Wrye Bash: _top_files_extensions, WITHOUT docs).
# A loose file with one of these at the archive root makes it a *simple*
# package (type 1); inside a sub-folder it marks that folder as a sub-package.
# Game-specific plugin extensions are added at call time via detect_bain's
# *extra_exts* (e.g. OpenMW's .omwaddon / .omwscripts).
_DATA_EXTS = {
    ".esp", ".esm", ".esu", ".esl", ".bsa", ".ba2", ".bsl", ".ckm", ".csv",
    ".ini", ".modgroups", ".toml",
    # morrowind / openmw
    ".omwaddon", ".omwscripts", ".omwgame",
}

# Documentation extensions (Wrye Bash: docExts). These count toward sub-package
# detection (_top_files_plus_docs) but a doc at the archive *root* must NOT make
# the package look like a simple/type-1 package - e.g. a Wrye Bash package.txt.
_DOC_EXTS = {
    ".txt", ".rtf", ".htm", ".html", ".doc", ".docx", ".odt", ".mht", ".pdf",
    ".css", ".xls", ".xlsx", ".ods", ".odp", ".ppt", ".pptx", ".md", ".rst",
    ".url",
}

# Folders/files Wrye Bash silently skips when classifying or installing a
# package. Lower-case.
_SKIP_DIR_NAMES = {"bash", "fomod", "omod conversion data", "wizard images"}
_SKIP_PREFIXES = ("--",)
_SKIP_FILE_NAMES = {
    "desktop.ini", "meta.ini", "thumbs.db", "__folder_managed_by_vortex",
}

# A sub-package folder name like "00 Core" / "01 Faction Integration" /
# "01a Variant" - Wrye Bash allows an optional letter suffix after the digits
# to group mutually-exclusive variants (01a, 01b, …). Used only to prettify the
# display name; it is not required for detection.
_NUMERIC_PREFIX_RE = re.compile(r"^(\d+)([a-zA-Z]*)[\s_.\-]")


@dataclass
class BainSubPackage:
    name: str            # full folder name, e.g. "00 Core"
    display_name: str    # prefix stripped, e.g. "Core"
    path: str            # absolute path to the folder
    default_selected: bool = False
    # Relative file paths (lower-cased) under `path` - populated by
    # scan_subpackage_files on the prepare/install WORKER so the Qt picker
    # never walks the disk on the GUI thread. None = not scanned yet.
    file_keys: set | None = None


def _strip_prefix(path: str, strip_prefixes: set[str]) -> str:
    parts = path.replace("\\", "/").strip("/").split("/")
    while len(parts) > 1 and parts[0].lower() in strip_prefixes:
        parts.pop(0)
    return "/".join(parts)


def _walk_package_files(root: str):
    root_abs = os.path.abspath(root)
    for dirpath, dirnames, files in os.walk(root):
        at_root = os.path.abspath(dirpath) == root_abs
        if at_root:
            dirnames[:] = [d for d in dirnames if not _is_skipped_dir(d)]
        for fn in files:
            low = fn.lower()
            if (low not in _SKIP_FILE_NAMES
                    and not (at_root and low.startswith(_SKIP_PREFIXES))):
                yield dirpath, fn


def scan_subpackage_files(
        subpackages: "list[BainSubPackage]",
        strip_prefixes: "set[str] | list[str] | None" = None) -> None:
    """Fill each sub-package's ``file_keys`` (the picker's live win/lose
    conflict recolour needs the per-package file sets). Call from the worker
    that detected the package, right before handing off to the picker."""
    strip_lower = {p.lower() for p in (strip_prefixes or ())}
    for pkg in subpackages:
        out: set = set()
        try:
            for dirpath, fn in _walk_package_files(pkg.path):
                rel = os.path.relpath(os.path.join(dirpath, fn), pkg.path)
                out.add(_strip_prefix(rel, strip_lower).lower())
        except OSError:
            pass
        pkg.file_keys = out


def _strip_numeric_prefix(name: str) -> str:
    """'00 Core' -> 'Core'. Leaves names without a numeric prefix unchanged."""
    m = _NUMERIC_PREFIX_RE.match(name)
    if not m:
        return name
    return name[m.end():].strip() or name


def _default_selected(name: str) -> bool:
    """Sub-packages are off by default; ``00``-prefixed ones (the core/required
    packages by BAIN convention) are pre-selected."""
    m = _NUMERIC_PREFIX_RE.match(name)
    return bool(m) and int(m.group(1)) == 0


def _is_skipped_dir(name: str) -> bool:
    low = name.lower()
    return low in _SKIP_DIR_NAMES or low.startswith(_SKIP_PREFIXES)


def _looks_like_subpackage(dir_path: str, data_exts: set[str],
                           data_dirs: set[str]) -> bool:
    """True if *dir_path* behaves like a simple package: it directly contains a
    recognised data sub-folder or a top-level data/doc file."""
    top_exts = data_exts | _DOC_EXTS
    try:
        with os.scandir(dir_path) as it:
            for e in it:
                if e.is_dir():
                    if e.name.lower() in data_dirs:
                        return True
                else:
                    low = e.name.lower()
                    if low in _SKIP_FILE_NAMES or low.startswith(_SKIP_PREFIXES):
                        continue
                    ext = os.path.splitext(e.name)[1].lower()
                    if ext in top_exts:
                        return True
    except OSError:
        return False
    return False


def bain_unwrap_single_folder(
        extract_dir: str,
        extra_data_dirs: "set[str] | list[str] | None" = None) -> str:
    """Peel a single wrapping top-level folder for BAIN detection, but NOT when
    that folder is itself a recognised data dir.

    Many simple (type-1) packages ship as ``<archive>/SKSE/...`` or
    ``<archive>/Meshes/...`` with nothing else at the root. A blind single-folder
    unwrap would descend into ``SKSE/`` and then mistake its child folders
    (``Plugins/``, ``Scripts/`` …) for BAIN sub-packages. Refusing to peel a
    recognised data dir keeps such packages classified as simple, matching Wrye
    Bash (which treats a top-level data dir as package content, not a wrapper)."""
    data_dirs = _DATA_DIRS | {p.lower() for p in (extra_data_dirs or ())}
    try:
        entries = list(os.scandir(extract_dir))
    except OSError:
        return extract_dir
    if len(entries) == 1 and entries[0].is_dir():
        if entries[0].name.lower() in data_dirs:
            return extract_dir
        return entries[0].path
    return extract_dir


def detect_bain(extract_dir: str,
                extra_exts: "set[str] | list[str] | None" = None,
                extra_data_dirs: "set[str] | list[str] | None" = None,
                ) -> list[BainSubPackage] | None:
    """Detect a BAIN complex package: a root holding ≥2 sub-package folders,
    each of which behaves like a simple package (contains data sub-folders or
    top-level data/doc files).

    *extra_exts* lets the caller add game-specific plugin extensions (e.g. the
    game's ``plugin_extensions``) so detection recognises formats beyond the
    built-in defaults. Extensions may be given with or without a leading dot.

    *extra_data_dirs* adds game-specific content directories and wrappers such
    as Morrowind's ``iwy`` and ``Data Files``.

    Returns the ordered list of sub-packages, or ``None`` if the directory
    doesn't look like a complex BAIN package. The caller is expected to pass an
    already single-folder-unwrapped path.
    """
    data_exts = set(_DATA_EXTS)
    data_dirs = _DATA_DIRS | {p.lower() for p in (extra_data_dirs or ())}
    if extra_exts:
        for x in extra_exts:
            x = x.lower()
            data_exts.add(x if x.startswith(".") else "." + x)

    try:
        entries = list(os.scandir(extract_dir))
    except OSError:
        return None

    # If the root itself holds loose data files or recognised data dirs, it's a
    # *simple* package (type 1), not a complex one - don't show a picker. Note:
    # doc files at the root (e.g. a Wrye Bash package.txt) do NOT count, matching
    # Wrye Bash's _re_top_extensions (which excludes docExts).
    for e in entries:
        if e.is_dir():
            if e.name.lower() in data_dirs:
                return None
        else:
            low = e.name.lower()
            if low in _SKIP_FILE_NAMES or low.startswith(_SKIP_PREFIXES):
                continue
            if os.path.splitext(e.name)[1].lower() in data_exts:
                return None

    subpackages: list[BainSubPackage] = []
    for e in entries:
        if not e.is_dir() or _is_skipped_dir(e.name):
            continue
        if _looks_like_subpackage(e.path, data_exts, data_dirs):
            subpackages.append(BainSubPackage(
                name=e.name,
                display_name=_strip_numeric_prefix(e.name),
                path=e.path,
                default_selected=_default_selected(e.name),
            ))

    if len(subpackages) < 2:
        return None

    # Wrye Bash orders sub-packages alphabetically (dict_sort). Numbered
    # prefixes ("00", "01", "10") sort correctly as strings.
    subpackages.sort(key=lambda p: p.name.lower())
    return subpackages


def resolve_bain_files(subpackages: list[BainSubPackage],
                       selected_names: set[str],
                       strip_prefixes: "set[str] | list[str] | None" = None,
                       ) -> list[tuple[str, str, bool]]:
    """Build the (src_rel, dst_rel, is_folder) install list for the chosen
    sub-packages.

    ``src_rel`` is relative to the extract root (e.g. ``00 Core/MWSE/x``);
    ``dst_rel`` strips the sub-package folder and any game-specific content
    wrapper so chosen packages merge into a single namespace (e.g. ``Data
    Files/MWSE/x`` becomes ``MWSE/x``). Files are emitted in sub-package order
    so later packages overwrite earlier ones on conflict (BAIN semantics).
    """
    strip_lower = {p.lower() for p in (strip_prefixes or ())}
    result: list[tuple[str, str, bool]] = []
    for pkg in subpackages:
        if pkg.name not in selected_names:
            continue
        root = pkg.path
        for dirpath, fn in _walk_package_files(root):
            abs_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_path, root)
            src_rel = os.path.join(pkg.name, rel)
            result.append((src_rel, _strip_prefix(rel, strip_lower), False))
    return result
