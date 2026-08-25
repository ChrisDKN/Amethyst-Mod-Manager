"""Archive discovery and rank rules used while cataloging candidates."""

from __future__ import annotations

import os

from Utils.bsa_reader import read_bsa_file_list
from Utils.ue_pak_reader import UE_ARCHIVE_EXTENSIONS, read_ue_archive_file_list


def scan_mod_archives(
    mod_name: str,
    mod_dir: str,
    archive_extensions: frozenset[str],
    cached_archives: dict[str, tuple[str, float, list[str]]] | None = None,
) -> tuple[str, list[tuple[str, float, list[str]]], int]:
    """Return parsed archive members for one raw mod manifest."""
    results = []
    parsed = 0
    recursive = bool(archive_extensions & UE_ARCHIVE_EXTENSIONS)
    candidates: list[tuple[str, str, str, float]] = []
    try:
        if recursive:
            for root, directories, filenames in os.walk(
                    mod_dir, followlinks=False):
                directories[:] = [
                    name for name in directories if name.lower() != "fomod"]
                for filename in filenames:
                    extension = os.path.splitext(filename)[1].lower()
                    if extension not in archive_extensions:
                        continue
                    full_path = os.path.join(root, filename)
                    try:
                        modified = os.stat(full_path).st_mtime
                    except OSError:
                        continue
                    relative = os.path.relpath(
                        full_path, mod_dir).replace(os.sep, "/")
                    candidates.append(
                        (relative, full_path, extension, modified))
        else:
            with os.scandir(mod_dir) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        extension = os.path.splitext(entry.name)[1].lower()
                        if extension not in archive_extensions:
                            continue
                        modified = entry.stat(
                            follow_symlinks=False).st_mtime
                        candidates.append(
                            (entry.name, entry.path, extension, modified))
                    except OSError:
                        continue
    except OSError:
        return mod_name, results, parsed

    for key, full_path, extension, modified in candidates:
        cached = (cached_archives or {}).get(key)
        if cached is not None and cached[1] == modified:
            results.append(cached)
            continue
        if extension in UE_ARCHIVE_EXTENSIONS:
            paths = read_ue_archive_file_list(full_path)
        else:
            paths = read_bsa_file_list(full_path)
        parsed += 1
        if paths:
            results.append((key, modified, paths))
    return mod_name, results, parsed


def owning_plugin(
    archive_stem: str,
    plugin_stems: set[str],
) -> str | None:
    if not plugin_stems:
        return None
    name = archive_stem.lower()
    if name in plugin_stems:
        return name
    end = len(name)
    while True:
        separator = name.rfind(" - ", 0, end)
        if separator <= 0:
            return None
        stem = name[:separator]
        if stem in plugin_stems:
            return stem
        end = separator


def pak_name_rank(archive_key: str) -> tuple[int, str]:
    basename = archive_key.rsplit("/", 1)[-1].lower()
    stem = basename.rsplit(".", 1)[0]
    return 1 if stem.endswith("_p") else 0, basename


# Names retained for the old reference resolver and focused parser tests.
_scan_mod_bsas = scan_mod_archives
_bsa_owning_plugin = owning_plugin
_pak_name_rank = pak_name_rank
