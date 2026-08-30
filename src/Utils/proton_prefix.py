"""Proton/Wine prefix helpers - toolkit-neutral.

Resolving a Steam/Heroic ``compatdata`` path from a user-selected ``pfx/``
folder, and reading the Proton runner name out of ``config_info``. These are
pure path/file operations with no GUI dependency; they live here so both the
GUI panels and backend (protontricks, wizards) share one implementation.
"""

from __future__ import annotations

from pathlib import Path


def resolve_compat_data(prefix_path: Path) -> Path:
    """Return the STEAM_COMPAT_DATA_PATH for a given user-selected pfx/ folder.

    Steam layout: compatdata/<id>/pfx/ → compat_data = prefix_path.parent.
    Heroic layout: <prefix>/pfx is a symlink to "." → compat_data = prefix_path
    itself (config_info lives alongside the pfx symlink, not one level up).
    Lutris layout: the prefix root IS the compat data - marked by lutris.json
    (present even before the first run writes config_info), or by the same
    self-referencing pfx symlink umu creates.
    Faugus layout: umu-made like Lutris's, but with no marker file at all -
    a fresh, never-launched prefix has neither config_info nor the pfx
    symlink, so it is identified by its games.json entry instead (checked
    last; the common cases short-circuit without reading games.json)."""
    if (prefix_path / "config_info").is_file():
        return prefix_path
    parent = prefix_path.parent
    if (parent / "config_info").is_file():
        return parent
    if (prefix_path / "lutris.json").is_file():
        return prefix_path
    pfx = prefix_path / "pfx"
    try:
        if pfx.is_symlink() and pfx.resolve() == prefix_path.resolve():
            return prefix_path
    except OSError:
        pass
    try:
        from Utils.faugus_finder import is_faugus_prefix
        if is_faugus_prefix(prefix_path):
            return prefix_path
    except Exception:
        pass
    return parent


def normalize_prefix_path(prefix_path: Path) -> Path:
    """Return the pfx/ folder for a hand-typed prefix path.

    Callers store the prefix root (the folder holding drive_c/), and derive
    compat_data from it via resolve_compat_data. Typing the compatdata/<id>
    parent instead is an easy mistake: the launch env still works (Proton
    appends pfx itself), but every drive_c-relative path built off the stored
    value silently loses the pfx/ segment - plugins.txt then lands outside the
    prefix, where the game never reads it.

    Only rewrites when the answer is unambiguous: no drive_c here, a real
    pfx/drive_c one level down. Heroic/Lutris/Faugus prefixes - where pfx is a
    self-referencing symlink, or absent - are left exactly as given.
    """
    try:
        if (prefix_path / "drive_c").is_dir():
            return prefix_path
        pfx = prefix_path / "pfx"
        if pfx.is_symlink() or not (pfx / "drive_c").is_dir():
            return prefix_path
        return pfx
    except OSError:
        return prefix_path


def read_prefix_runner(compat_data: Path) -> str:
    """Read the Proton runner name from <compat_data>/config_info (first line).
    Returns an empty string if the file is absent or unreadable."""
    try:
        return (compat_data / "config_info").read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return ""
