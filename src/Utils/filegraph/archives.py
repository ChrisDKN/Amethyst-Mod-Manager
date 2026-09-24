"""Filegraph archive discovery and rank rules used while cataloging candidates."""

from __future__ import annotations

import os
from dataclasses import dataclass

from Utils.bsa.reader import read_bsa_file_list
from Utils.memory_cache import ByteLruCache
from Utils.unreal.archives import UE_ARCHIVE_EXTENSIONS, read_ue_archive_file_list


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    relative: str
    path: str
    extension: str
    stat: os.stat_result


_member_cache = ByteLruCache(32 * 1024 * 1024)


def scan_mod_archives(
    mod_name: str,
    mod_dir: str,
    archive_extensions: frozenset[str],
    cached_archives: dict[str, tuple[str, float, list[str]]] | None = None,
    *,
    discovered: list[ArchiveFile] | None = None,
) -> tuple[str, list[tuple[str, float, list[str]]], int]:
    """Return parsed archive members for one raw mod manifest."""
    results = []
    parsed = 0
    recursive = bool(archive_extensions & UE_ARCHIVE_EXTENSIONS)
    candidates = [] if discovered is None else discovered
    try:
        if discovered is None and recursive:
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
                        info = os.stat(full_path)
                    except OSError:
                        continue
                    relative = os.path.relpath(
                        full_path, mod_dir).replace(os.sep, "/")
                    candidates.append(ArchiveFile(
                        relative, full_path, extension, info))
        elif discovered is None:
            with os.scandir(mod_dir) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        extension = os.path.splitext(entry.name)[1].lower()
                        if extension not in archive_extensions:
                            continue
                        info = entry.stat(follow_symlinks=False)
                        candidates.append(ArchiveFile(
                            entry.name, entry.path, extension, info))
                    except OSError:
                        continue
    except OSError:
        return mod_name, results, parsed

    for archive in candidates:
        key, full_path, extension = archive.relative, archive.path, archive.extension
        info = archive.stat
        modified = info.st_mtime
        cached = (cached_archives or {}).get(key)
        if cached is not None and cached[1] == modified:
            results.append(cached)
            continue
        signature = (os.path.abspath(full_path), info.st_dev, info.st_ino,
                     info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        paths = _member_cache.get(signature)
        if paths is None:
            if extension in UE_ARCHIVE_EXTENSIONS:
                paths = read_ue_archive_file_list(full_path)
            else:
                paths = read_bsa_file_list(full_path)
            parsed += 1
            if paths:
                _member_cache.put(signature, paths)
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
