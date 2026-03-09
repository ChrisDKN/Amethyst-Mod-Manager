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

Mod Index
---------
modindex.bin lives next to filemap.txt and caches the file list of every
mod so that build_filemap() can skip the expensive disk scan on every
enable/disable/reorder.  The index is only updated when mods are installed
or removed (or when the user hits the Refresh button).

Index format — msgpack binary, v3:
    {"v": 3, "mods": [[mod_name, [[rel_key, rel_str, kind], ...]], ...]}
where <kind> is "n" (normal) or "r" (root-deploy).
Paths stored in the index are already folder-case-normalized across all mods
so build_filemap() can skip the normalize step entirely.
"""

from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import msgpack

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
_POOL = ThreadPoolExecutor(max_workers=20)

_INDEX_VERSION = 3

# In-memory cache: (path_str, mtime) → parsed index
# Avoids re-parsing the ~5 MB index file on every filemap rebuild.
_IndexCache = dict[str, tuple[dict[str, str], dict[str, str]]]
_index_cache: tuple[str, float, _IndexCache] | None = None  # (path, mtime, data)


def _scan_dir(
    source_name: str,
    source_dir: str,
    strip_prefixes: frozenset[str] = frozenset(),
    allowed_extensions: frozenset[str] = frozenset(),
    root_deploy_folders: frozenset[str] = frozenset(),
    strip_path_prefixes: list[str] | None = None,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Walk source_dir with os.scandir (fast, no Pathlib overhead).

    Returns (source_name, normal_files, root_files) where each dict is
    {rel_key_lower: rel_str_original}.
    Pure function — no shared state, safe to call from any thread.

    strip_path_prefixes — full path prefixes to strip once (e.g. ["Tree", "Meshes/Architecture"]).
    Applied first, before strip_prefixes. Longest match wins. Case-insensitive.

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
    # Pre-sort once (longest match first) so we don't re-sort inside the per-file loop.
    # Each entry is (lowercase_prefix, len_of_original_prefix) for O(1) strip-by-length.
    sorted_path_prefixes: list[tuple[str, int]] = (
        sorted(((p.lower(), len(p)) for p in strip_path_prefixes), key=lambda t: -t[1])
        if strip_path_prefixes else []
    )
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
                        # Strip full path prefixes first (per-mod "ignore this folder" paths).
                        if sorted_path_prefixes:
                            rel_lower = rel_str.lower()
                            for p_lower, p_len in sorted_path_prefixes:
                                if rel_lower == p_lower or rel_lower.startswith(p_lower + "/"):
                                    rel_str = rel_str[p_len:].lstrip("/")
                                    break
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


def fix_flat_staging_folders(staging_root: Path) -> list[str]:
    """Wrap any flat mod staging folders so files are one level deeper.

    Some games (e.g. Stardew Valley) require mods to live inside a named
    subdirectory: Mods/<ModName>/<files>.  The staging folder should therefore
    look like mods/<StagingName>/<ModName>/<files>.

    A common mistake is copying Mods/<ModName>/ directly into staging, giving
    mods/<ModName>/<files> — the <ModName> wrapper is missing and deploy puts
    the files straight into Mods/ instead of Mods/<ModName>/.

    This function detects staging folders whose contents are entirely loose
    files (no subdirectory at all) and moves those files into a new subfolder
    named after the staging folder itself.

    Only folders that contain *exclusively* loose files (no existing subdir) are
    touched, so mods that are already correctly structured are never modified.

    Returns a list of staging folder names that were restructured.
    """
    fixed: list[str] = []
    if not staging_root.is_dir():
        return fixed

    for mod_dir in staging_root.iterdir():
        if not mod_dir.is_dir():
            continue

        children = list(mod_dir.iterdir())
        if not children:
            continue

        # For games that require a subdir wrapper (e.g. Stardew Valley / SMAPI),
        # manifest.json at the staging root is the definitive signal that the
        # mod was copied flat and needs wrapping — regardless of whether there
        # are also subdirectories (assets/, i18n/, etc.) present.
        has_manifest = any(c.name.lower() == "manifest.json"
                           for c in children if c.is_file())
        if not has_manifest:
            continue

        # Move everything (files and subdirs) into a new subfolder named after
        # the staging folder so the mod loader finds <ModName>/manifest.json.
        sub = mod_dir / mod_dir.name
        sub.mkdir(exist_ok=True)
        for child in children:
            shutil.move(str(child), str(sub / child.name))
        fixed.append(mod_dir.name)

    return fixed


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
    # We only mutate values (never add/remove keys), so iterating keys() directly is safe.
    for files in all_files.values():
        for rel_key in files:
            rel_str = files[rel_key]
            parts = rel_str.split("/")
            # Normalise folder segments (all but the last), leave filename alone
            new_parts = [
                canonical.get(seg.lower(), seg) for seg in parts[:-1]
            ] + [parts[-1]]
            new_rel = "/".join(new_parts)
            if new_rel != rel_str:
                files[rel_key] = new_rel


# ---------------------------------------------------------------------------
# Mod index — persistent cache of each mod's file list
# ---------------------------------------------------------------------------

def read_mod_index(
    index_path: Path,
) -> dict[str, tuple[dict[str, str], dict[str, str]]] | None:
    """Read modindex.bin and return {mod_name: (normal_files, root_files)}.

    Returns None if the index does not exist or has an unrecognised version
    (caller should fall back to a full disk scan).
    Paths in the returned dicts are already folder-case-normalized.
    Results are cached in memory by (path, mtime) so repeated calls within
    the same session are free.
    """
    global _index_cache
    path_str = str(index_path)
    try:
        mtime = index_path.stat().st_mtime
    except OSError:
        return None
    if _index_cache is not None and _index_cache[0] == path_str and _index_cache[1] == mtime:
        return _index_cache[2]
    try:
        with index_path.open("rb") as f:
            data = msgpack.unpack(f, raw=False)
        if not isinstance(data, dict) or data.get("v") != _INDEX_VERSION:
            return None
        index: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
        for mod_name, files in data["mods"]:
            normal: dict[str, str] = {}
            root:   dict[str, str] = {}
            for rel_key, rel_str, kind in files:
                (root if kind == "r" else normal)[rel_key] = rel_str
            index[mod_name] = (normal, root)
    except Exception:
        return None
    _index_cache = (path_str, mtime, index)
    return index


def _write_mod_index(
    index_path: Path,
    index: dict[str, tuple[dict[str, str], dict[str, str]]],
) -> None:
    """Normalize paths, write the full index atomically, then update the cache."""
    global _index_cache
    # Normalize folder-case across all mods before writing so build_filemap
    # can skip the normalize step entirely on every rebuild.
    _normalize_folder_cases({name: normal for name, (normal, _) in index.items()})
    _normalize_folder_cases({name: root   for name, (_, root)   in index.items() if root})
    index_path.parent.mkdir(parents=True, exist_ok=True)
    mods = []
    for mod_name, (normal, root) in index.items():
        files = [[k, v, "n"] for k, v in normal.items()]
        files += [[k, v, "r"] for k, v in root.items()]
        mods.append([mod_name, files])
    payload = {"v": _INDEX_VERSION, "mods": mods}
    tmp = index_path.with_suffix(".tmp")
    try:
        with tmp.open("wb") as f:
            msgpack.pack(payload, f, use_bin_type=True)
        tmp.replace(index_path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    # Update the in-memory cache to match what was just written.
    try:
        mtime = index_path.stat().st_mtime
        _index_cache = (str(index_path), mtime, index)
    except OSError:
        _index_cache = None


def update_mod_index(
    index_path: Path,
    mod_name: str,
    normal_files: dict[str, str],
    root_files: dict[str, str],
) -> None:
    """Add or replace a single mod's entry in the index.

    Reads the existing index (if any), replaces the entry for mod_name,
    and writes the result atomically.  Call this after installing a mod.
    """
    index = read_mod_index(index_path) or {}
    index[mod_name] = (normal_files, root_files)
    _write_mod_index(index_path, index)


def remove_from_mod_index(index_path: Path, mod_names: list[str]) -> None:
    """Remove one or more mods from the index and rewrite it atomically.

    Call this after deleting mod folders from staging.
    No-op if the index does not exist or the mod is not in it.
    """
    if not index_path.is_file():
        return
    index = read_mod_index(index_path)
    if not index:
        return
    changed = False
    for name in mod_names:
        if name in index:
            del index[name]
            changed = True
    if changed:
        _write_mod_index(index_path, index)


def rebuild_mod_index(
    index_path: Path,
    staging_root: Path,
    strip_prefixes: set[str] | None = None,
    per_mod_strip_prefixes: dict[str, list[str]] | None = None,
    allowed_extensions: set[str] | None = None,
    root_deploy_folders: set[str] | None = None,
) -> None:
    """Scan every mod folder under staging_root and rewrite the full index.

    This is the slow path, triggered by the Refresh button.  Normal filemap
    rebuilds (enable/disable/reorder) use the cached index instead.

    The overwrite folder is also indexed under OVERWRITE_NAME.
    """
    _strip = frozenset(s.lower() for s in strip_prefixes) if strip_prefixes else frozenset()
    _per_mod = per_mod_strip_prefixes or {}
    _exts  = frozenset(e.lower() for e in allowed_extensions) if allowed_extensions else frozenset()
    _root  = frozenset(s.lower() for s in root_deploy_folders) if root_deploy_folders else frozenset()

    staging_str   = str(staging_root)
    overwrite_str = str(staging_root.parent / "overwrite")

    # Collect all mod folders that exist on disk
    scan_targets: list[tuple[str, str]] = []
    try:
        with os.scandir(staging_str) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    scan_targets.append((entry.name, entry.path))
    except OSError:
        pass
    scan_targets.append((OVERWRITE_NAME, overwrite_str))

    def _strip_for_mod(name: str) -> frozenset[str]:
        mod_strip = _per_mod.get(name)
        if not mod_strip:
            return _strip
        segment_names = [s for s in mod_strip if "/" not in s]
        return _strip | frozenset(s.lower() for s in segment_names)

    def _path_prefixes_for_mod(name: str) -> list[str]:
        mod_strip = _per_mod.get(name)
        if not mod_strip:
            return []
        return [s for s in mod_strip if "/" in s]

    futures = {
        _POOL.submit(
            _scan_dir, name, d, _strip_for_mod(name), _exts, _root,
            strip_path_prefixes=_path_prefixes_for_mod(name),
        ): name
        for name, d in scan_targets
    }

    index: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
    for fut in futures:
        name, normal, root = fut.result()
        index[name] = (normal, root)

    _write_mod_index(index_path, index)


# ---------------------------------------------------------------------------
# Main filemap builder
# ---------------------------------------------------------------------------

def build_filemap(
    modlist_path: Path,
    staging_root: Path,
    output_path: Path,
    strip_prefixes: set[str] | None = None,
    per_mod_strip_prefixes: dict[str, list[str]] | None = None,
    allowed_extensions: set[str] | None = None,
    root_deploy_folders: set[str] | None = None,
    disabled_plugins: dict[str, list[str]] | None = None,
    conflict_ignore_filenames: set[str] | None = None,
    excluded_mod_files: dict[str, set[str]] | None = None,
) -> tuple[int, dict[str, int], dict[str, set[str]], dict[str, set[str]]]:
    """
    Build filemap.txt from the current modlist.

    Reads file lists from modindex.bin (fast path) when available.
    Falls back to a full disk scan if the index is missing or corrupt,
    and writes a fresh index as a side-effect of that scan.

    per_mod_strip_prefixes — optional dict mapping mod name to a list of
    top-level folder names to strip for that mod only (contents move up one
    level during deployment).  Merged with strip_prefixes when scanning.

    allowed_extensions — when non-empty, only files with a matching lowercase
    extension (e.g. {".pak"}) are included in the filemap.  Pass None or an
    empty set to include all files (default behaviour).

    root_deploy_folders — top-level folder names whose files should be
    deployed to the game root instead of the mod data path.  These are
    written to a sibling ``filemap_root.txt`` and bypass the extension
    filter.  Pass None or an empty set to disable (default).

    conflict_ignore_filenames — lowercase filenames (not paths) excluded from
    conflict tracking.  Files still appear in the filemap but do not count
    toward a mod's conflict status.  Pass None or an empty set to disable.

    excluded_mod_files — dict mapping mod name to a set of lowercase rel_key
    paths that should be excluded from the filemap for that mod.  Excluded
    files are treated as if the mod does not have them, so the next
    lower-priority mod that has the same file wins instead.

    Returns:
        (count, conflict_map, overrides, overridden_by)
    """
    entries = read_modlist(modlist_path)

    # Only enabled, non-separator mods
    enabled = [e for e in entries if not e.is_separator and e.enabled]

    # Walk lowest-priority → highest-priority so higher-priority mods win
    # (modlist index 0 = highest priority, last index = lowest priority)
    enabled_low_to_high = list(reversed(enabled))

    priority_order = [e.name for e in enabled_low_to_high if e.name != ROOT_FOLDER_NAME] + [OVERWRITE_NAME]

    index_path = output_path.parent / "modindex.bin"
    index = read_mod_index(index_path)

    if index is None:
        # Index missing or corrupt — fall back to full disk scan and rebuild it.
        rebuild_mod_index(
            index_path, staging_root,
            strip_prefixes=strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip_prefixes,
            allowed_extensions=allowed_extensions,
            root_deploy_folders=root_deploy_folders,
        )
        index = read_mod_index(index_path) or {}

    # Build raw / raw_root from the index for the mods we care about.
    raw:      dict[str, dict[str, str]] = {}
    raw_root: dict[str, dict[str, str]] = {}
    for name in priority_order:
        entry = index.get(name)
        if entry is None:
            continue
        normal, root = entry
        if normal:
            raw[name] = dict(normal)
        if root:
            raw_root[name] = dict(root)

    # filemap: lowercase_rel_path → (winning_mod_name,)
    filemap_winner: dict[str, str] = {}
    mod_files: dict[str, set[str]] = {}

    # Build per-mod excluded-file sets for fast lookup (lowercase rel_keys)
    _excluded: dict[str, set[str]] = excluded_mod_files or {}

    # Merge in priority order so higher-priority mods overwrite lower ones
    for name in priority_order:
        files = raw.get(name)
        if not files:
            continue
        exc = _excluded.get(name)
        active_keys = set(files.keys()) if not exc else {k for k in files if k not in exc}
        mod_files[name] = active_keys
        for rel_key in active_keys:
            filemap_winner[rel_key] = name

    # Rebuild filemap using the normalised (canonical) rel_str for the destination
    # path so that all mods writing to the same logical folder produce files under
    # one consistent directory name (e.g. always "Scripts/", never "scripts/").
    filemap: dict[str, tuple[str, str]] = {}
    for rel_key, winner in filemap_winner.items():
        rel_str = raw[winner].get(rel_key, rel_key)
        filemap[rel_key] = (rel_str, winner)

    # Build overrides / overridden_by
    overrides:     dict[str, set[str]] = {s: set() for s in priority_order}
    overridden_by: dict[str, set[str]] = {s: set() for s in priority_order}

    _ignore_fnames = {f.lower() for f in conflict_ignore_filenames} if conflict_ignore_filenames else set()

    current_holder: dict[str, str] = {}
    for name in priority_order:
        for key in mod_files.get(name, ()):
            if _ignore_fnames and key.rsplit("/", 1)[-1] in _ignore_fnames:
                continue
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

    # Build per-mod disabled-plugin sets for fast lookup (lowercase filenames, root-level only)
    _disabled_lower: dict[str, set[str]] = {}
    if disabled_plugins:
        for _mod, _names in disabled_plugins.items():
            _disabled_lower[_mod] = {n.lower() for n in _names}

    # Write sorted output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_keys = sorted(filemap)
    count = 0
    with output_path.open("w", encoding="utf-8") as f:
        for rel_key in sorted_keys:
            rel_str, mod_name = filemap[rel_key]
            # Skip root-level files that the user has disabled for this mod
            if _disabled_lower and "/" not in rel_key and mod_name in _disabled_lower:
                if rel_key in _disabled_lower[mod_name]:
                    continue
            f.write(f"{rel_str}\t{mod_name}\n")
            count += 1

    # Write root-deploy filemap if any root files were found.
    root_output = output_path.parent / "filemap_root.txt"
    if raw_root:
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
