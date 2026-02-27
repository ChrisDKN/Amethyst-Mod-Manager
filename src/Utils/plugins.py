"""
plugins.py
Read and write a MO2-compatible plugins.txt file.

Format (one plugin per line):
  *PluginName.esp   — enabled plugin
  PluginName.esp    — disabled plugin (no prefix)

Order in the file defines load order (line 0 = first loaded).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

@dataclass
class PluginEntry:
    name: str
    enabled: bool


def read_plugins(path: Path) -> list[PluginEntry]:
    """
    Parse plugins.txt and return entries in file order (index 0 = first loaded).
    Lines that are blank or start with '#' are skipped.
    '*Name' = enabled; bare 'Name' = disabled.
    """
    entries: list[PluginEntry] = []
    if not path.is_file():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("*"):
            entries.append(PluginEntry(name=line[1:], enabled=True))
        else:
            entries.append(PluginEntry(name=line, enabled=False))
    return entries


def write_plugins(path: Path, entries: list[PluginEntry]) -> None:
    """
    Write entries back to plugins.txt.
    Creates parent directories if needed.
    Enabled entries are written as '*Name', disabled as bare 'Name'.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"*{e.name}" if e.enabled else e.name
        for e in entries
    ]
    path.write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )


def read_loadorder(path: Path) -> list[str]:
    """Read loadorder.txt and return plugin names in order.

    loadorder.txt stores the full load order including vanilla plugins
    (which are excluded from plugins.txt).  One bare filename per line.
    """
    if not path.is_file():
        return []
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


def write_loadorder(path: Path, entries: list[PluginEntry]) -> None:
    """Write the full load order (bare filenames) to loadorder.txt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [e.name for e in entries]
    path.write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )


def append_plugin(path: Path, plugin_name: str, enabled: bool = True) -> None:
    """
    Append a plugin to the bottom of plugins.txt if not already present.
    The check is case-insensitive so 'Plugin.esp' and 'plugin.esp' are treated
    as the same plugin.
    Does nothing if the plugin already exists in the file.
    """
    entries = read_plugins(path)
    existing_lower = {e.name.lower() for e in entries}
    if plugin_name.lower() in existing_lower:
        return
    entries.append(PluginEntry(name=plugin_name, enabled=enabled))
    write_plugins(path, entries)


def prune_plugins_from_filemap(
    filemap_path: Path,
    plugins_path: Path,
    plugin_extensions: list[str],
    data_dir: Path | None = None,
) -> int:
    """
    Remove entries from plugins.txt whose plugin file no longer appears in
    filemap.txt (i.e. the mod providing that plugin was disabled).

    Plugins that exist in data_dir (vanilla game plugins) are always kept,
    even if absent from the filemap.

    Only root-level files are considered (matching how Bethesda plugins work).
    Returns the count of removed entries.
    """
    if not plugin_extensions:
        return 0

    exts_lower = {ext.lower() for ext in plugin_extensions}

    # Collect all root-level plugin filenames present in the current filemap
    in_filemap: set[str] = set()
    if filemap_path.is_file():
        with filemap_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if "\t" not in line:
                    continue
                rel_path, _ = line.split("\t", 1)
                rel_path = rel_path.replace("\\", "/")
                if "/" in rel_path:
                    continue
                if Path(rel_path).suffix.lower() in exts_lower:
                    in_filemap.add(rel_path.lower())

    # Also keep plugins that exist as vanilla files in the game's Data/ dir.
    # Prefer Data_Core/ when it exists — after deployment Data/ contains
    # hard-linked mod files, so Data_Core/ is the reliable source of truth
    # for what plugins are truly vanilla.
    in_data_dir: set[str] = set()
    if data_dir and data_dir.is_dir():
        vanilla_dir = data_dir.parent / (data_dir.name + "_Core")
        scan_dir = vanilla_dir if vanilla_dir.is_dir() else data_dir
        for entry in scan_dir.iterdir():
            if entry.is_file() and entry.suffix.lower() in exts_lower:
                in_data_dir.add(entry.name.lower())

    keep = in_filemap | in_data_dir
    existing = read_plugins(plugins_path)
    kept = [e for e in existing if e.name.lower() in keep]
    removed = len(existing) - len(kept)
    if removed:
        write_plugins(plugins_path, kept)
    return removed


def sync_plugins_from_data_dir(
    data_dir: Path,
    plugins_path: Path,
    plugin_extensions: list[str],
) -> int:
    """
    Scan the game's Data directory for root-level plugin files and append any
    not already in plugins.txt (e.g. vanilla ESMs like Fallout4.esm).
    Returns the count of newly added plugins.
    """
    if not plugin_extensions or not data_dir.is_dir():
        return 0

    exts_lower = {ext.lower() for ext in plugin_extensions}
    existing = read_plugins(plugins_path)
    existing_lower = {e.name.lower() for e in existing}

    new_entries: list[PluginEntry] = []
    for entry in data_dir.iterdir():
        if entry.is_file() and entry.suffix.lower() in exts_lower:
            if entry.name.lower() not in existing_lower:
                new_entries.append(PluginEntry(name=entry.name, enabled=True))
                existing_lower.add(entry.name.lower())

    if new_entries:
        write_plugins(plugins_path, existing + new_entries)

    return len(new_entries)


def sync_plugins_from_filemap(
    filemap_path: Path,
    plugins_path: Path,
    plugin_extensions: list[str],
) -> int:
    """
    Scan filemap.txt for files matching plugin_extensions and append any
    not already in plugins.txt.  Returns the count of newly added plugins.

    The filemap format is: <relative/path/to/file>\\t<mod_name>
    Only root-level files (no directory separator in relative path) are
    considered, because Bethesda plugins live at the root of the Data folder.
    """
    if not filemap_path.is_file() or not plugin_extensions:
        return 0

    exts_lower = {ext.lower() for ext in plugin_extensions}

    existing = read_plugins(plugins_path)
    existing_lower = {e.name.lower() for e in existing}

    new_entries: list[PluginEntry] = []

    with filemap_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if "\t" not in line:
                continue
            rel_path, _ = line.split("\t", 1)
            rel_path = rel_path.replace("\\", "/")
            if "/" in rel_path:
                # Plugin is inside a subfolder — not a root-level plugin file
                continue
            filename = rel_path
            if (Path(filename).suffix.lower() in exts_lower
                    and filename.lower() not in existing_lower):
                new_entries.append(PluginEntry(name=filename, enabled=True))
                existing_lower.add(filename.lower())

    if new_entries:
        write_plugins(plugins_path, existing + new_entries)

    return len(new_entries)
