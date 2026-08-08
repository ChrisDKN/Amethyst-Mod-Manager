"""
morrowind_ini.py
Utility for managing the [Game Files] section of Morrowind.ini.

Morrowind does not use plugins.txt - active plugins are listed under
[Game Files] as GameFile0=..., GameFile1=..., etc.

Load order is determined by the last-modified date of the actual plugin
file, not by position in the ini. We set mtimes on deployed plugins to
match the desired order (earlier in list = older mtime).

Deploy behaviour:
  1. Read Morrowind.ini and preserve all sections except [Game Files].
  2. Rewrite [Game Files] with:
       - The 3 vanilla masters hardcoded first (always present).
       - Then all active plugins from plugins.txt, in plugins.txt order.
  3. Set mtimes on the deployed plugin files so load order matches the
     plugins.txt order (1-second spacing, vanilla masters get the oldest
     timestamps).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# Vanilla masters are always present and always load first.
_VANILLA_MASTERS = [
    "Morrowind.esm",
    "Tribunal.esm",
    "Bloodmoon.esm",
]

# Spacing between mtime values (seconds).
_MTIME_STEP = 1


def _read_ini_sections(ini_path: Path) -> list[tuple[str, list[str]]]:
    """Parse Morrowind.ini into an ordered list of (section_header, lines).

    section_header is the raw '[Section]' string (or '' for lines before any
    section).  lines contains the raw content lines for that section,
    without a trailing newline.
    """
    sections: list[tuple[str, list[str]]] = []
    current_header = ""
    current_lines: list[str] = []

    if not ini_path.is_file():
        return sections

    for raw in ini_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if line.startswith("[") and line.endswith("]"):
            sections.append((current_header, current_lines))
            current_header = line
            current_lines = []
        else:
            current_lines.append(line)

    sections.append((current_header, current_lines))
    return sections


def _read_plugins_txt(plugins_txt: Path) -> list[str]:
    """Return the ordered list of active plugin filenames from plugins.txt.

    Lines starting with '#' or '*' prefixes (MO2-style) are handled:
      - Lines starting with '*' are active (strip the '*').
      - Lines starting with '#' are comments - skipped.
      - Plain lines are treated as active.
    """
    if not plugins_txt.is_file():
        return []

    plugins: list[str] = []
    for raw in plugins_txt.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("*"):
            line = line[1:].strip()
        dot = line.rfind(".")
        if dot != -1:
            line = line[:dot] + line[dot:].lower()
        plugins.append(line)
    return plugins


_MGEXE_PLUGIN = "XE Sky Variations.esp"


def _base_plugins(data_files_dir: Path) -> list[str]:
    """Return the always-present plugin list for this installation.

    Includes the MGE XE sky plugin only when its .esp is present in
    Data Files/, indicating MGE XE is installed.
    """
    plugins = list(_VANILLA_MASTERS)
    if (data_files_dir / _MGEXE_PLUGIN).is_file():
        plugins.append(_MGEXE_PLUGIN)
    return plugins


def _find_file_nocase(directory: Path, filename: str) -> Path | None:
    """Return the path of a file in directory whose name matches filename
    case-insensitively, or None if not found."""
    filename_lower = filename.lower()
    if not directory.is_dir():
        return None
    for f in directory.iterdir():
        if f.is_file() and f.name.lower() == filename_lower:
            return f
    return None


def _find_staging_plugin(staging_root: Path, plugin_name: str) -> Path | None:
    """Return the path to a plugin file inside the staging folder tree.

    Searches all immediate mod subdirectories of staging_root for a file
    whose name matches plugin_name (case-insensitively).
    """
    if not staging_root.is_dir():
        return None
    for mod_dir in staging_root.iterdir():
        if not mod_dir.is_dir():
            continue
        found = _find_file_nocase(mod_dir, plugin_name)
        if found:
            return found
    return None


def update_morrowind_ini(
    ini_path: Path,
    plugins_txt: Path,
    data_files_dir: Path,
    staging_root: Path | None = None,
    log_fn=None,
) -> None:
    """Rewrite the [Game Files] section of Morrowind.ini and set plugin mtimes.

    Args:
        ini_path:       Path to Morrowind.ini (game root).
        plugins_txt:    Path to the profile's plugins.txt.
        data_files_dir: Path to the deployed 'Data Files/' directory.
        staging_root:   Path to the mod staging directory. When provided,
                        mtimes are also set on the source files in staging
                        so hardlink inode and symlink target stay in sync.
        log_fn:         Optional logging callable.
    """
    _log = log_fn or (lambda _: None)

    # ------------------------------------------------------------------
    # Build the new plugin list
    # ------------------------------------------------------------------
    active = _read_plugins_txt(plugins_txt)
    base   = _base_plugins(data_files_dir)

    # Exclude base plugins from the user list - they're always written first.
    base_lower = {p.lower() for p in base}
    user_plugins = [p for p in active if p.lower() not in base_lower]

    ordered: list[str] = base + user_plugins

    # ------------------------------------------------------------------
    # Rewrite Morrowind.ini
    # ------------------------------------------------------------------
    sections = _read_ini_sections(ini_path)

    out_lines: list[str] = []
    wrote_game_files = False

    for header, lines in sections:
        if header.lower() == "[game files]":
            # Replace with our freshly built section.
            out_lines.append("[Game Files]")
            for i, plugin in enumerate(ordered):
                out_lines.append(f"GameFile{i}={plugin}")
            wrote_game_files = True
        else:
            if header:
                out_lines.append(header)
            out_lines.extend(lines)

    if not wrote_game_files:
        # No [Game Files] section existed - append one.
        out_lines.append("")
        out_lines.append("[Game Files]")
        for i, plugin in enumerate(ordered):
            out_lines.append(f"GameFile{i}={plugin}")

    ini_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    _log(f"  Wrote {len(ordered)} plugin(s) to [Game Files] in {ini_path.name}.")

    # ------------------------------------------------------------------
    # Set mtimes on deployed plugin files to enforce load order.
    # Earlier in the list = older mtime so the game loads them first.
    # Also stamp the staging source so hardlink inodes and symlink targets
    # stay consistent with the deployed copy.
    # ------------------------------------------------------------------
    base_time = time.time() - len(ordered) * _MTIME_STEP
    for i, plugin in enumerate(ordered):
        target_mtime = base_time + i * _MTIME_STEP

        plugin_path = _find_file_nocase(data_files_dir, plugin)
        if plugin_path:
            os.utime(plugin_path, (target_mtime, target_mtime))
        else:
            _log(f"  WARN: deployed plugin not found for mtime update: {plugin}")

        if staging_root:
            staging_path = _find_staging_plugin(staging_root, plugin)
            if staging_path:
                os.utime(staging_path, (target_mtime, target_mtime))

    _log(f"  Set mtimes on {len(ordered)} plugin(s) to enforce load order.")


def restore_morrowind_ini(ini_path: Path, data_files_dir: Path, log_fn=None) -> None:
    """Rewrite [Game Files] in Morrowind.ini keeping only base plugins.

    Base plugins are the 3 vanilla masters plus the MGE XE sky plugin if its
    .esp is present in Data Files/ (i.e. MGE XE is installed into the game).

    Args:
        ini_path:       Path to Morrowind.ini (game root).
        data_files_dir: Path to the game's 'Data Files/' directory.
        log_fn:         Optional logging callable.
    """
    _log = log_fn or (lambda _: None)

    base = _base_plugins(data_files_dir)
    sections = _read_ini_sections(ini_path)

    out_lines: list[str] = []
    wrote_game_files = False

    for header, lines in sections:
        if header.lower() == "[game files]":
            out_lines.append("[Game Files]")
            for i, plugin in enumerate(base):
                out_lines.append(f"GameFile{i}={plugin}")
            wrote_game_files = True
        else:
            if header:
                out_lines.append(header)
            out_lines.extend(lines)

    if not wrote_game_files:
        out_lines.append("")
        out_lines.append("[Game Files]")
        for i, plugin in enumerate(base):
            out_lines.append(f"GameFile{i}={plugin}")

    ini_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    _log(f"  Restored [Game Files] to {len(base)} base plugin(s).")
