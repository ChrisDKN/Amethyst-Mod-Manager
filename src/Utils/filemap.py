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
where <kind> is "n" (normal) or "r" (unused legacy, kept for format compatibility).
Paths stored in the index are already folder-case-normalized across all mods
so build_filemap() can skip the normalize step entirely.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
import threading
from pathlib import Path
from functools import lru_cache

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
_index_cache_lock = threading.Lock()

# Per-output-path cache of the last filemap_winner dict.
# If the new winner dict is identical, we skip the file write entirely.
# Maps output_path_str → frozenset of (rel_key, mod_name) pairs.
_filemap_winner_cache: dict[str, frozenset] = {}
_filemap_winner_cache_lock = threading.Lock()


def _scan_dir(
    source_name: str,
    source_dir: str,
    strip_prefixes: frozenset[str] = frozenset(),
    allowed_extensions: frozenset[str] = frozenset(),
    _unused_root_deploy_folders: frozenset[str] = frozenset(),
    strip_path_prefixes: list[str] | None = None,
    exclude_dirs: frozenset[str] = frozenset(),
) -> tuple[str, dict[str, str], dict[str, str], list[str]]:
    """Walk source_dir with os.scandir (fast, no Pathlib overhead).

    Returns (source_name, normal_files, {}, invalid_names) where normal_files
    is {rel_key_lower: rel_str_original} and invalid_names is a list of
    relative paths whose filenames contain non-UTF-8 bytes (surrogates).
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

    exclude_dirs — lowercase directory names to skip entirely during the walk.
    Any directory whose name (case-insensitive) matches an entry here is never
    pushed onto the scan stack, so none of its files reach the filemap.
    e.g. exclude_dirs={"fomod"} prevents FOMOD installer metadata from being
    deployed to the game's data directory.

    _unused_root_deploy_folders — retained for call-site compatibility only;
    the root-deploy routing has been removed in favour of custom_routing_rules.
    """
    result: dict[str, str] = {}
    root_result: dict[str, str] = {}  # always empty; kept for tuple compat
    invalid_names: list[str] = []
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
                        if exclude_dirs and entry.name.lower() in exclude_dirs:
                            continue
                        if not _is_utf8_safe(entry.name):
                            invalid_names.append(prefix + entry.name + "/")
                            continue
                        stack.append((
                            prefix + entry.name + "/",
                            entry.path,
                        ))
                    elif entry.is_file(follow_symlinks=False):
                        if entry.name in _EXCLUDE_NAMES:
                            continue
                        if not _is_utf8_safe(entry.name):
                            invalid_names.append(prefix + entry.name)
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
                        # Extension filter — drop files not in the allowed set
                        if allowed_extensions:
                            ext = os.path.splitext(entry.name)[1].lower()
                            if ext not in allowed_extensions:
                                continue
                        key = rel_str.lower()
                        if key in result:
                            # Two physical files map to the same case-insensitive path
                            # (e.g. Interface/ vs interface/).  Prefer the one whose
                            # folder segments have more uppercase characters.
                            existing = result[key]
                            ex_folders = "/".join(existing.split("/")[:-1])
                            new_folders = "/".join(rel_str.split("/")[:-1])
                            if _upper_count(new_folders) > _upper_count(ex_folders):
                                result[key] = rel_str
                        else:
                            result[key] = rel_str
        except OSError:
            pass
    return source_name, result, root_result, invalid_names


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


@lru_cache(maxsize=2048)
def _upper_count(s: str) -> int:
    return sum(1 for c in s if c.isupper())


def _is_utf8_safe(s: str) -> bool:
    """Return True if s can be encoded as UTF-8 (no lone surrogates)."""
    try:
        s.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _pick_canonical_segment(a: str, b: str) -> str:
    """Choose the folder name with more uppercase characters.
    On a tie, prefer the one that comes first alphabetically (stable choice).
    """
    return a if _upper_count(a) >= _upper_count(b) else b


def _normalize_folder_cases(*all_files_list: dict[str, dict[str, str]]) -> None:
    """Normalize folder name casing across all mods in-place.

    Folder names are case-insensitive on Windows (and in the game engine), so
    "Plugins" and "plugins" are the same folder.  When multiple mods use
    different casings we pick the variant with the most uppercase characters
    (e.g. "Plugins" beats "plugins") and rewrite every rel_str that uses the
    losing variant so the whole filemap is consistent.

    File *names* are left exactly as they are.
    Accepts one or more dicts (e.g. normal and root) and builds canonical
    casing from all in one pass, then rewrites each in turn.
    """
    # Collect canonical casing per folder segment, keyed by its full ancestor
    # path so that identically-named segments at different tree locations are
    # independent.  e.g. "textures/effects" vs "interface/photomode/overlays/effects"
    # produce different keys, so Photo Mode's uppercase EFFECTS can never
    # influence Particle Patch's lowercase effects.
    # Key: (lowercase_parent_path, lowercase_segment) -> canonical segment str
    canonical: dict[tuple[str, str], str] = {}
    for all_files in all_files_list:
        if not all_files:
            continue
        for files in all_files.values():
            for rel_str in files.values():
                parts = rel_str.split("/")
                if len(parts) < 2:
                    continue
                parent = ""
                for seg in parts[:-1]:
                    ctx_key = (parent, seg.lower())
                    if ctx_key not in canonical:
                        canonical[ctx_key] = seg
                    else:
                        canonical[ctx_key] = _pick_canonical_segment(canonical[ctx_key], seg)
                    parent = parent + seg.lower() + "/"

    if not canonical:
        return

    # Rewrite rel_str values so every folder segment uses the canonical casing.
    for all_files in all_files_list:
        if not all_files:
            continue
        for files in all_files.values():
            for rel_key in files:
                rel_str = files[rel_key]
                if "/" not in rel_str:
                    continue
                parts = rel_str.split("/")
                changed = False
                parent = ""
                new_parts = []
                for seg in parts[:-1]:
                    ctx_key = (parent, seg.lower())
                    c = canonical.get(ctx_key, seg)
                    if c != seg:
                        changed = True
                    new_parts.append(c)
                    parent = parent + seg.lower() + "/"
                if not changed:
                    continue
                new_parts.append(parts[-1])
                files[rel_key] = "/".join(new_parts)


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
    with _index_cache_lock:
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
    with _index_cache_lock:
        _index_cache = (path_str, mtime, index)
    return index


def invalidate_filemap_cache(output_path: Path) -> None:
    """Discard the skip-if-unchanged snapshot for output_path.

    Call this whenever the mod index changes (install, remove, rebuild) so the
    next build_filemap() always writes a fresh filemap.txt rather than skipping.
    """
    with _filemap_winner_cache_lock:
        _filemap_winner_cache.pop(str(output_path), None)


def _write_mod_index(
    index_path: Path,
    index: dict[str, tuple[dict[str, str], dict[str, str]]],
    normalize_folder_case: bool = True,
) -> None:
    """Normalize paths, write the full index atomically, then update the cache."""
    global _index_cache
    if normalize_folder_case:
        _normalize_folder_cases(
            {name: normal for name, (normal, _) in index.items()},
            {name: root for name, (_, root) in index.items() if root},
        )
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
    # Update the in-memory index cache to match what was just written.
    with _index_cache_lock:
        try:
            mtime = index_path.stat().st_mtime
            _index_cache = (str(index_path), mtime, index)
        except OSError:
            _index_cache = None
    # Invalidate the filemap skip-cache: the index changed so the next
    # build_filemap() must write a fresh filemap.txt regardless.
    profile_dir = str(index_path.parent)
    with _filemap_winner_cache_lock:
        for key in list(_filemap_winner_cache):
            if key.startswith(profile_dir):
                del _filemap_winner_cache[key]


def update_mod_index(
    index_path: Path,
    mod_name: str,
    normal_files: dict[str, str],
    root_files: dict[str, str],
    normalize_folder_case: bool = True,
) -> None:
    """Add or replace a single mod's entry in the index.

    Reads the existing index (if any), replaces the entry for mod_name,
    and writes the result atomically.  Call this after installing a mod.
    """
    index = read_mod_index(index_path) or {}
    index[mod_name] = (normal_files, root_files)
    _write_mod_index(index_path, index, normalize_folder_case=normalize_folder_case)


def remove_from_mod_index(
    index_path: Path,
    mod_names: list[str],
    normalize_folder_case: bool = True,
) -> None:
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
        _write_mod_index(index_path, index, normalize_folder_case=normalize_folder_case)


def rename_in_mod_index(
    index_path: Path,
    old_name: str,
    new_name: str,
    normalize_folder_case: bool = True,
) -> None:
    """Rename a mod's entry in the index from *old_name* to *new_name*.

    Call this after renaming a mod's staging folder so build_filemap() can
    still find its files (build_filemap keys the index by the modlist name).
    No-op if the index does not exist or the old name is not in it.
    """
    if not index_path.is_file() or old_name == new_name:
        return
    index = read_mod_index(index_path)
    if not index or old_name not in index:
        return
    index[new_name] = index.pop(old_name)
    _write_mod_index(index_path, index, normalize_folder_case=normalize_folder_case)


def rebuild_mod_index(
    index_path: Path,
    staging_root: Path,
    strip_prefixes: set[str] | None = None,
    per_mod_strip_prefixes: dict[str, list[str]] | None = None,
    allowed_extensions: set[str] | None = None,
    root_deploy_folders: set[str] | None = None,  # unused, kept for call-site compat
    normalize_folder_case: bool = True,
    exclude_dirs: frozenset[str] | None = None,
    log_fn: "Callable[[str], None] | None" = None,
) -> None:
    """Scan every mod folder under staging_root and rewrite the full index.

    This is the slow path, triggered by the Refresh button.  Normal filemap
    rebuilds (enable/disable/reorder) use the cached index instead.

    The overwrite folder is also indexed under OVERWRITE_NAME.
    """
    _strip = frozenset(s.lower() for s in strip_prefixes) if strip_prefixes else frozenset()
    _per_mod = per_mod_strip_prefixes or {}
    _exts  = frozenset(e.lower() for e in allowed_extensions) if allowed_extensions else frozenset()
    _root  = frozenset()  # root_deploy_folders routing removed; param kept for compat
    _excl_dirs = exclude_dirs if exclude_dirs is not None else frozenset()

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
            exclude_dirs=_excl_dirs,
        ): name
        for name, d in scan_targets
    }

    index: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
    for fut in futures:
        name, normal, root, invalid_names = fut.result()
        if invalid_names:
            if log_fn is not None:
                log_fn(
                    f"WARN: Mod \"{name}\" skipped — contains file(s) with "
                    f"non-UTF-8 name(s): {', '.join(invalid_names)}"
                )
            continue  # skip the entire mod
        index[name] = (normal, root)

    _write_mod_index(index_path, index, normalize_folder_case=normalize_folder_case)


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
    root_deploy_folders: set[str] | None = None,  # unused, kept for call-site compat
    disabled_plugins: dict[str, list[str]] | None = None,
    conflict_ignore_filenames: set[str] | None = None,
    excluded_mod_files: dict[str, set[str]] | None = None,
    normalize_folder_case: bool = True,
    conflict_key_fn: "Callable[[str], str] | None" = None,
    exclude_dirs: frozenset[str] | None = None,
    log_fn: "Callable[[str], None] | None" = None,
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

    root_deploy_folders — no longer used; kept for call-site compatibility.
    Previously wrote a ``filemap_root.txt``; routing is now done via
    ``custom_routing_rules`` at deploy time.

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
            normalize_folder_case=normalize_folder_case,
            exclude_dirs=exclude_dirs,
            log_fn=log_fn,
        )
        index = read_mod_index(index_path) or {}

    # Pre-compile ignore patterns once into a single regex for O(1) matching.
    _ignore_re: re.Pattern[str] | None = None
    if conflict_ignore_filenames:
        parts = [fnmatch.translate(p.lower()) for p in conflict_ignore_filenames]
        _ignore_re = re.compile("|".join(parts))

    def _is_ignored(rel_key: str) -> bool:
        if _ignore_re is None:
            return False
        return bool(_ignore_re.match(rel_key.rsplit("/", 1)[-1]))

    # Build per-mod excluded-file sets for fast lookup (lowercase rel_keys)
    _excluded: dict[str, set[str]] = excluded_mod_files or {}

    # Single-pass merge: priority order (low→high) so later mods overwrite earlier ones.
    # Simultaneously builds filemap_winner, filemap, overrides/overridden_by, win_count.
    filemap_winner: dict[str, str] = {}
    filemap: dict[str, tuple[str, str]] = {}
    overrides:     dict[str, set[str]] = {s: set() for s in priority_order}
    overridden_by: dict[str, set[str]] = {s: set() for s in priority_order}
    win_count: dict[str, int] = {}
    # Track which mods had at least one active file (for conflict status "no files" check).
    mods_with_files: set[str] = set()
    # Separate winner dict keyed by the effective deploy path (for conflict detection).
    # When conflict_key_fn is provided (e.g. UE5 routing), two staged paths that land
    # at the same game location are treated as conflicting even if their staged keys differ.
    conflict_winner: dict[str, str] = {}

    for name in priority_order:
        entry = index.get(name)
        if not entry:
            continue
        normal, _ = entry
        if not normal:
            continue
        # Guard against surrogate-encoded filenames left in an old modindex.bin.
        # (Old scans ran before the _scan_dir surrogate-skip fix.)  Skip the
        # entire mod and log it so the user knows to Refresh / reinstall it.
        bad_names = [rs for rs in normal.values() if not _is_utf8_safe(rs)]
        if bad_names:
            if log_fn is not None:
                log_fn(
                    f"WARN: Mod \"{name}\" skipped \u2014 contains file(s) with "
                    f"non-UTF-8 name(s): {', '.join(bad_names[:5])}"
                )
            continue
        exc = _excluded.get(name)
        had_file = False
        for rel_key, rel_str in normal.items():
            if exc and rel_key in exc:
                continue
            if _is_ignored(rel_key):
                continue
            had_file = True
            prev = filemap_winner.get(rel_key)
            if prev is not None:
                win_count[prev] = win_count.get(prev, 0) - 1
                # conflict tracking (staged-key level)
                overrides[name].add(prev)
                overridden_by[prev].add(name)
            filemap_winner[rel_key] = name
            filemap[rel_key] = (rel_str, name)
            win_count[name] = win_count.get(name, 0) + 1
            # Conflict detection on effective deploy path when a key transform is provided.
            # Two mods whose staged paths differ but resolve to the same game location
            # are treated as conflicting; the lower-priority entry is removed from the
            # filemap so the data tab and deploy both reflect the actual winner.
            if conflict_key_fn is not None:
                ck = conflict_key_fn(rel_key).lower()
                prev_ck = conflict_winner.get(ck)
                if prev_ck is not None and prev_ck != name:
                    # Find the staged key the previous winner used for this deploy path
                    prev_staged = next(
                        (k for k, v in filemap_winner.items() if v == prev_ck and conflict_key_fn(k) == ck),
                        None,
                    )
                    if prev_staged is not None and prev_staged != rel_key:
                        # Remove the lower-priority entry so only one file lands at this dest
                        filemap_winner.pop(prev_staged, None)
                        filemap.pop(prev_staged, None)
                        win_count[prev_ck] = win_count.get(prev_ck, 0) - 1
                    overrides[name].add(prev_ck)
                    overridden_by[prev_ck].add(name)
                conflict_winner[ck] = name
        if had_file:
            mods_with_files.add(name)

    # Compute per-source conflict status
    conflict_map: dict[str, int] = {}
    for name in priority_order:
        has_wins  = bool(overrides[name])
        has_loses = bool(overridden_by[name])
        if name not in mods_with_files or (not has_wins and not has_loses):
            conflict_map[name] = CONFLICT_NONE
        elif has_loses and win_count.get(name, 0) <= 0:
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

    # Skip-if-unchanged: fingerprint the winner map + disabled state.
    # If identical to the last write for this output path, skip the expensive
    # sort + string build + disk write (and post_build_filemap re-read).
    # disabled_plugins is rare but must be included since it affects written lines.
    _disabled_frozen = (
        frozenset((m, frozenset(ns)) for m, ns in _disabled_lower.items())
        if _disabled_lower else frozenset()
    )
    _winner_snapshot = (frozenset(filemap_winner.items()), _disabled_frozen)
    _output_key = str(output_path)
    with _filemap_winner_cache_lock:
        _unchanged = _filemap_winner_cache.get(_output_key) == _winner_snapshot
    if _unchanged and output_path.is_file():
        # Conflict data is still valid; file on disk is already correct.
        count = sum(1 for _ in filemap_winner)  # approx — disabled_plugins may trim a few
        return count, conflict_map, overrides, overridden_by

    # Write sorted output — sort keys once, build one big string, write in a single call.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_keys = sorted(filemap)
    parts: list[str] = []
    for rel_key in sorted_keys:
        rel_str, mod_name = filemap[rel_key]
        # Skip root-level files that the user has disabled for this mod
        if _disabled_lower and "/" not in rel_key and mod_name in _disabled_lower:
            if rel_key in _disabled_lower[mod_name]:
                continue
        parts.append(rel_str)
        parts.append("\t")
        parts.append(mod_name)
        parts.append("\n")
    output = "".join(parts)
    with output_path.open("w", encoding="utf-8") as f:
        f.write(output)
    count = output.count("\n")

    with _filemap_winner_cache_lock:
        _filemap_winner_cache[_output_key] = _winner_snapshot

    return count, conflict_map, overrides, overridden_by
