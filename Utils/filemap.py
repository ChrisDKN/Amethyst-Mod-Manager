"""
filemap.py
Build and write a filemap.txt that resolves mod file conflicts.

Algorithm: walk enabled mods from lowest priority to highest priority.
For each file, record (relative_path, source_mod). Higher-priority mods
overwrite lower-priority entries — no conflicts remain in the output.

Format (one line per file):
    <relative/path/to/file>\t<mod_name>

Paths are stored in their original case but deduplicated case-insensitively
so that Windows-style case-insensitive conflicts are handled correctly.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from Utils.modlist import read_modlist

# Conflict status constants (returned per-mod in build_filemap result)
CONFLICT_NONE    = 0   # no conflicts at all
CONFLICT_WINS    = 1   # wins some/all conflicts, loses none (green dot)
CONFLICT_LOSES   = 2   # loses some conflicts, wins none (red dot)
CONFLICT_PARTIAL = 3   # wins some, loses some (yellow dot)
CONFLICT_FULL    = 4   # all files overridden — nothing reaches the game (white dot)

# Sentinel name used in filemap.txt and conflict dicts for the overwrite folder
OVERWRITE_NAME   = "[Overwrite]"

# Sentinel name for the root folder — files deploy to the game root, not mod data path
ROOT_FOLDER_NAME = "[Root_Folder]"

# MO2 metadata files present in every mod folder — not real game files
_EXCLUDE_NAMES = frozenset({"meta.ini"})

# Reuse a modest thread pool across calls rather than creating one per call
_POOL = ThreadPoolExecutor(max_workers=8)


def _scan_dir(
    source_name: str,
    source_dir: str,
    strip_prefixes: frozenset[str] = frozenset(),
    allowed_extensions: frozenset[str] = frozenset(),
    root_deploy_folders: frozenset[str] = frozenset(),
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Walk source_dir with os.scandir (fast, no Pathlib overhead).

    Returns (source_name, normal_files, root_files) where each dict is
    {rel_key_lower: rel_str_original}.
    Pure function — no shared state, safe to call from any thread.

    strip_prefixes — lowercase top-level folder names to remove from the
    start of each relative path before adding it to the result.  Only the
    first path segment is ever stripped, and only when it matches one of the
    listed names (case-insensitive).  e.g. strip_prefixes={"plugins"} turns
    "plugins/MyMod/MyMod.dll" into "MyMod/MyMod.dll".

    allowed_extensions — when non-empty, only files whose lowercase extension
    (including the leading dot) appears in this set are included.  e.g.
    allowed_extensions={".pak"} drops all non-.pak files from the result.

    root_deploy_folders — lowercase top-level folder names (checked after
    strip-prefix processing) whose files should be deployed to the game root
    instead of the mod data path.  These files bypass the allowed_extensions
    filter and are returned in the separate root_files dict.
    """
    result: dict[str, str] = {}
    root_result: dict[str, str] = {}
    # Iterative scandir stack — avoids rglob/Pathlib per-entry object cost
    stack = [("", source_dir)]
    while stack:
        prefix, current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((
                            prefix + entry.name + "/",
                            entry.path,
                        ))
                    elif entry.is_file(follow_symlinks=False):
                        if entry.name in _EXCLUDE_NAMES:
                            continue
                        rel_str = prefix + entry.name
                        # Strip leading wrapper folders declared by the game.
                        # Repeat until no more matching prefixes remain so that
                        # e.g. "bepinex/plugins/Mod/Mod.dll" → "Mod/Mod.dll"
                        # when strip_prefixes = {"bepinex", "plugins"}.
                        if strip_prefixes and "/" in rel_str:
                            while "/" in rel_str:
                                first_seg, remainder = rel_str.split("/", 1)
                                if first_seg.lower() in strip_prefixes:
                                    rel_str = remainder
                                else:
                                    break
                        # Route files under root_deploy_folders to the root dict
                        # (bypasses the extension filter).
                        if root_deploy_folders and "/" in rel_str:
                            top_seg = rel_str.split("/", 1)[0]
                            if top_seg.lower() in root_deploy_folders:
                                root_result[rel_str.lower()] = rel_str
                                continue
                        # Extension filter — drop files not in the allowed set
                        if allowed_extensions:
                            ext = os.path.splitext(entry.name)[1].lower()
                            if ext not in allowed_extensions:
                                continue
                        result[rel_str.lower()] = rel_str
        except OSError:
            pass
    return source_name, result, root_result


def _pick_canonical_segment(a: str, b: str) -> str:
    """Choose the folder name with more uppercase characters.
    On a tie, prefer the one that comes first alphabetically (stable choice).
    """
    if sum(1 for c in a if c.isupper()) >= sum(1 for c in b if c.isupper()):
        return a
    return b


def _normalize_folder_cases(all_files: dict[str, dict[str, str]]) -> None:
    """Normalize folder name casing across all mods in-place.

    Folder names are case-insensitive on Windows (and in the game engine), so
    "Plugins" and "plugins" are the same folder.  When multiple mods use
    different casings we pick the variant with the most uppercase characters
    (e.g. "Plugins" beats "plugins") and rewrite every rel_str that uses the
    losing variant so the whole filemap is consistent.

    File *names* are left exactly as they are.
    """
    # Collect every unique folder-segment casing seen across all mods.
    # key: segment.lower()  →  canonical casing (most uppercase wins)
    canonical: dict[str, str] = {}
    for files in all_files.values():
        for rel_str in files.values():
            parts = rel_str.split("/")
            # All parts except the last are folder segments
            for seg in parts[:-1]:
                key = seg.lower()
                if key not in canonical:
                    canonical[key] = seg
                else:
                    canonical[key] = _pick_canonical_segment(canonical[key], seg)

    if not canonical:
        return

    # Rewrite rel_str values so every folder segment uses the canonical casing.
    for files in all_files.values():
        for rel_key, rel_str in list(files.items()):
            parts = rel_str.split("/")
            # Normalise folder segments (all but the last), leave filename alone
            new_parts = [
                canonical.get(seg.lower(), seg) for seg in parts[:-1]
            ] + [parts[-1]]
            new_rel = "/".join(new_parts)
            if new_rel != rel_str:
                files[rel_key] = new_rel


def build_filemap(
    modlist_path: Path,
    staging_root: Path,
    output_path: Path,
    strip_prefixes: set[str] | None = None,
    allowed_extensions: set[str] | None = None,
    root_deploy_folders: set[str] | None = None,
) -> tuple[int, dict[str, int], dict[str, set[str]], dict[str, set[str]]]:
    """
    Build filemap.txt from the current modlist.

    allowed_extensions — when non-empty, only files with a matching lowercase
    extension (e.g. {".pak"}) are included in the filemap.  Pass None or an
    empty set to include all files (default behaviour).

    root_deploy_folders — top-level folder names whose files should be
    deployed to the game root instead of the mod data path.  These are
    written to a sibling ``filemap_root.txt`` and bypass the extension
    filter.  Pass None or an empty set to disable (default).

    Returns:
        (count, conflict_map, overrides, overridden_by)
    """
    entries = read_modlist(modlist_path)

    # Only enabled, non-separator mods
    enabled = [e for e in entries if not e.is_separator and e.enabled]

    # Walk lowest-priority → highest-priority so higher-priority mods win
    # (modlist index 0 = highest priority, last index = lowest priority)
    enabled_low_to_high = list(reversed(enabled))

    staging_str   = str(staging_root)
    overwrite_str = str(staging_root.parent / "overwrite")

    scan_targets: list[tuple[str, str]] = [
        (e.name, os.path.join(staging_str, e.name)) for e in enabled_low_to_high
        if e.name != ROOT_FOLDER_NAME
    ] + [(OVERWRITE_NAME, overwrite_str)]

    _strip = frozenset(s.lower() for s in strip_prefixes) if strip_prefixes else frozenset()
    _exts  = frozenset(e.lower() for e in allowed_extensions) if allowed_extensions else frozenset()
    _root  = frozenset(s.lower() for s in root_deploy_folders) if root_deploy_folders else frozenset()

    # Scan all directories in parallel (I/O bound)
    raw: dict[str, dict[str, str]] = {}
    raw_root: dict[str, dict[str, str]] = {}
    futures = {_POOL.submit(_scan_dir, name, d, _strip, _exts, _root): name
               for name, d in scan_targets}
    for fut in futures:
        name, files, root_files = fut.result()
        if files:
            raw[name] = files
        if root_files:
            raw_root[name] = root_files

    # Keep a copy of the original on-disk paths before normalisation so that
    # the filemap always records the real path for the winning mod's file.
    # rel_key_lower → actual rel_str as found on disk for that mod.
    raw_orig: dict[str, dict[str, str]] = {
        name: dict(files) for name, files in raw.items()
    }

    # Normalise folder-name casing across all mods so that "Plugins" and
    # "plugins" from different mods resolve to the same canonical folder name
    # for deduplication purposes.  raw_orig is left untouched.
    _normalize_folder_cases(raw)

    # filemap: lowercase_rel_path → (winning_mod_name,)
    # We track the winner name here and look up the real path from raw_orig.
    filemap_winner: dict[str, str] = {}
    mod_files: dict[str, set[str]] = {}

    # Merge in priority order so higher-priority mods overwrite lower ones
    priority_order = [e.name for e in enabled_low_to_high] + [OVERWRITE_NAME]
    for name in priority_order:
        files = raw.get(name)
        if not files:
            continue
        mod_files[name] = set(files.keys())
        for rel_key in files:
            filemap_winner[rel_key] = name

    # Rebuild filemap using the normalised (canonical) rel_str for the destination
    # path so that all mods writing to the same logical folder produce files under
    # one consistent directory name (e.g. always "Scripts/", never "scripts/").
    filemap: dict[str, tuple[str, str]] = {}
    for rel_key, winner in filemap_winner.items():
        # normalised path from raw[winner] — folder segments have canonical casing
        rel_str = raw[winner].get(rel_key, rel_key)
        filemap[rel_key] = (rel_str, winner)

    # Build overrides / overridden_by
    overrides:     dict[str, set[str]] = {s: set() for s in priority_order}
    overridden_by: dict[str, set[str]] = {s: set() for s in priority_order}

    current_holder: dict[str, str] = {}
    for name in priority_order:
        for key in mod_files.get(name, ()):
            if key in current_holder:
                loser = current_holder[key]
                overrides[name].add(loser)
                overridden_by[loser].add(name)
            current_holder[key] = name

    # Compute per-source conflict status
    conflict_map: dict[str, int] = {}
    for name in priority_order:
        keys = mod_files.get(name)
        has_wins  = bool(overrides[name])
        has_loses = bool(overridden_by[name])
        if not keys or (not has_wins and not has_loses):
            conflict_map[name] = CONFLICT_NONE
        elif has_loses and all(filemap[k][1] != name for k in keys):
            conflict_map[name] = CONFLICT_FULL
        elif has_wins and not has_loses:
            conflict_map[name] = CONFLICT_WINS
        elif has_loses and not has_wins:
            conflict_map[name] = CONFLICT_LOSES
        else:
            conflict_map[name] = CONFLICT_PARTIAL

    # Write sorted output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_keys = sorted(filemap)
    count = len(sorted_keys)
    with output_path.open("w", encoding="utf-8") as f:
        for rel_key in sorted_keys:
            rel_str, mod_name = filemap[rel_key]
            f.write(f"{rel_str}\t{mod_name}\n")

    # Write root-deploy filemap if any root files were found.
    root_output = output_path.parent / "filemap_root.txt"
    if raw_root:
        _normalize_folder_cases(raw_root)
        root_winner: dict[str, str] = {}
        for name in priority_order:
            rfiles = raw_root.get(name)
            if not rfiles:
                continue
            for rel_key in rfiles:
                root_winner[rel_key] = name
        root_filemap: dict[str, tuple[str, str]] = {}
        for rel_key, winner in root_winner.items():
            rel_str = raw_root[winner].get(rel_key, rel_key)
            root_filemap[rel_key] = (rel_str, winner)
        sorted_root = sorted(root_filemap)
        with root_output.open("w", encoding="utf-8") as f:
            for rel_key in sorted_root:
                rel_str, mod_name = root_filemap[rel_key]
                f.write(f"{rel_str}\t{mod_name}\n")
        count += len(sorted_root)
    elif root_output.is_file():
        root_output.unlink()

    return count, conflict_map, overrides, overridden_by
