"""Toolkit-neutral logic for the Mod Files tab (Qt view drives this).

Columns: **Top Level** promotes a path by adding strip-prefix entries
(``mod_strip_prefixes``); **Root** routes files to the game root
(``root_mod_files``); **Disable** drops them from deploy
(``excluded_mod_files``).

Both per-file stores key on the RAW on-disk path (lowercase, forward-slash).
Post-strip keys cannot be authoritative: they collide when a promoted folder
holds a same-named file, and they rename on every Top Level edit. Candidate
derivation translates raw identities when a catalog variant is refreshed.
"""

from __future__ import annotations

import os
from pathlib import Path

from Utils.profiles.state import (
    read_excluded_mod_files, write_excluded_mod_files,
    read_mod_strip_prefixes, write_mod_strip_prefixes,
    read_root_mod_files, write_root_mod_files,
)
from Utils.filegraph.adapter import OVERWRITE_NAME, ROOT_FOLDER_NAME


# ---------------------------------------------------------------------------
# File listing before the initial catalog snapshot is published
# ---------------------------------------------------------------------------
def load_mod_files(game, mod_name: str) -> dict[str, str]:
    """Return {rel_key (lower) -> rel_str (raw on-disk casing)} for *mod_name*.

    This is used only while first migration is still publishing its snapshot;
    it scans the one selected mod and never consults a legacy index.
    """
    return scan_mod_files(
        _mod_dir_for(game, mod_name),
        getattr(game, "filemap_exclude_dirs", None) or ())


def scan_mod_files(mod_dir: Path | None, excluded_dirs=()) -> dict[str, str]:
    if mod_dir is None or not mod_dir.is_dir():
        return {}
    files: dict[str, str] = {}
    excluded_dirs = {str(name).lower() for name in excluded_dirs}
    try:
        for directory, dirnames, filenames in os.walk(
                mod_dir, followlinks=False):
            dirnames[:] = [
                name for name in dirnames
                if (name.lower() not in excluded_dirs
                    and not name.startswith("prefix_")
                    and name != ".mm_bundle")
            ]
            relative_dir = Path(directory).relative_to(mod_dir)
            for filename in filenames:
                if filename in {"meta.ini", ".DS_Store"} \
                        or filename.startswith("._"):
                    continue
                relative = ((relative_dir / filename).as_posix()
                            if relative_dir != Path(".") else filename)
                files[relative.lower()] = relative
    except OSError:
        return files
    return files


def _mod_dir_for(game, mod_name: str) -> Path | None:
    if game is None:
        return None
    try:
        if mod_name == OVERWRITE_NAME and hasattr(game, "get_effective_overwrite_path"):
            return Path(game.get_effective_overwrite_path())
        if mod_name == ROOT_FOLDER_NAME and hasattr(game, "get_effective_root_folder_path"):
            return Path(game.get_effective_root_folder_path())
        if hasattr(game, "get_effective_mod_staging_path"):
            return Path(game.get_effective_mod_staging_path()) / mod_name
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def parent_path(path: str) -> str:
    """Parent folder path of *path* (or '')."""
    p = path.replace("\\", "/").rstrip("/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def ancestor_paths(path: str) -> list[str]:
    """Ancestor folder paths of *path*, root → parent."""
    p = path.replace("\\", "/").rstrip("/")
    if "/" not in p:
        return []
    out: list[str] = []
    cur = ""
    for seg in p.split("/")[:-1]:
        cur = f"{cur}/{seg}" if cur else seg
        out.append(cur)
    return out


def rel_key_after_strip(raw_rel_key: str, stripped_paths: set[str]) -> str:
    """Apply the saved strip prefixes (longest-match-first, like _scan_dir) to a
    raw rel_key so it can be looked up in the post-strip conflict/filemap data."""
    k = raw_rel_key
    for s in sorted(stripped_paths, key=len, reverse=True):
        sl = s.lower()
        if k == sl or k.startswith(sl + "/"):
            return k[len(sl):].lstrip("/")
    return k


def is_top_level(path: str, stripped_paths: set[str]) -> bool:
    """True if *path* deploys at the top level given the strip list (its parent
    path is fully covered by a strip entry, or it has no parent)."""
    parent = parent_path(path)
    if not parent:
        return True
    return parent.lower() in stripped_paths


# ---------------------------------------------------------------------------
# Tree building
# ---------------------------------------------------------------------------
def build_tree(files: dict[str, str], *,
               keep_rel_key=None) -> dict:
    """Build a nested dict from {rel_key: rel_str}. Folders are sub-dicts; files
    live in a "__files__" list of (filename, rel_key, rel_str). *keep_rel_key*
    is an optional predicate(rel_key, rel_str) -> bool to drop filtered rows."""
    tree: dict = {}
    for rel_key, rel_str in sorted(files.items()):
        if keep_rel_key is not None and not keep_rel_key(rel_key, rel_str):
            continue
        parts = rel_str.replace("\\", "/").split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault("__files__", []).append((parts[-1], rel_key, rel_str))
    return tree


# ---------------------------------------------------------------------------
# Strip-prefix (Top Level) toggle - returns the new strip set
# ---------------------------------------------------------------------------
def toggle_top_level(path: str, stripped_paths: set[str]) -> set[str]:
    """Compute the new strip-prefix set after toggling *path*'s Top Level box.

    - path already stripped → un-strip it (+ any stripped descendants).
    - path currently top-level (a promoted row) → demote: remove the FULL
      ancestor chain that made it top-level, so a second click on the same row
      cleanly REVERSES the promotion (no orphaned `meshes`/`meshes/actors`
      leftovers). Ancestors still needed by another promoted sibling are kept.
    - else → promote: strip every ancestor segment up to it.

    Returns a NEW set (does not mutate the input). Root-level rows that are
    already top-level return the set unchanged (nothing to demote).
    """
    out = set(stripped_paths)
    path_l = path.lower()

    def _unstrip_subtree(root_l: str):
        prefix = root_l + "/"
        for s in list(out):
            if s == root_l or s.startswith(prefix):
                out.discard(s)

    if path_l in out:
        _unstrip_subtree(path_l)
    elif is_top_level(path, out):
        ancestors = [a.lower() for a in ancestor_paths(path)]
        if not ancestors:
            return out                      # root-level: nothing to demote
        # Remove only the ancestor strip entries that aren't ALSO promoting some
        # other branch. An ancestor `a` is still needed if a different stripped
        # entry sits strictly below `a` on a path that doesn't lead to `path`.
        to_remove = set(ancestors)
        keep_path = path_l
        for s in out:
            if s in to_remove:
                # Does another stripped descendant of `s` exist that ISN'T on
                # this row's own ancestor chain? If so, `s` is shared - keep it.
                for other in out:
                    if other == s:
                        continue
                    if other.startswith(s + "/") and not (
                            keep_path == other or keep_path.startswith(other + "/")
                            or other.startswith(keep_path + "/")):
                        to_remove.discard(s)
                        break
        for a in to_remove:
            out.discard(a)
    else:
        for anc in ancestor_paths(path):
            out.add(anc.lower())
    return out


def save_strip_prefixes(profile_dir: Path, mod_name: str,
                        stripped_paths: set[str],
                        case_hints: dict[str, str] | None = None) -> list[str]:
    """Persist *stripped_paths* for *mod_name*, preferring original-case forms
    from *case_hints* (lower → original). Returns the merged list written."""
    case_hints = case_hints or {}
    strip_map = read_mod_strip_prefixes(profile_dir, None)
    for e in strip_map.get(mod_name, []):
        if e:
            case_hints.setdefault(e.lower(), e)
    merged = sorted({case_hints.get(s, s) for s in stripped_paths if s})
    if merged:
        strip_map[mod_name] = merged
    else:
        strip_map.pop(mod_name, None)
    write_mod_strip_prefixes(profile_dir, strip_map)
    return merged


# ---------------------------------------------------------------------------
# Exclusion (Disable) save - merge visible state with preserved filtered rows
# ---------------------------------------------------------------------------
def save_exclusions(profile_dir: Path, mod_name: str,
                    visible_keys: set[str], excluded_visible: set[str]) -> set[str]:
    """Persist exclusions for *mod_name* (RAW keys), keeping entries for rows
    the active filter hides. Returns the full new excluded set."""
    all_excluded = read_excluded_mod_files(profile_dir, None)
    preserved = {k for k in all_excluded.get(mod_name, set())
                 if k not in visible_keys}
    excluded = preserved | excluded_visible
    if excluded:
        all_excluded[mod_name] = sorted(excluded)
    else:
        all_excluded.pop(mod_name, None)
    write_excluded_mod_files(profile_dir, all_excluded)
    return excluded


def read_exclusions(profile_dir: Path, mod_name: str) -> set[str]:
    """Saved excluded RAW keys for *mod_name* (empty set if none)."""
    if profile_dir is None:
        return set()
    return set(read_excluded_mod_files(profile_dir, None).get(mod_name, set()))


def save_root_tags(profile_dir: Path, mod_name: str,
                   visible_keys: set[str], tagged_visible: set[str]) -> set[str]:
    """Persist root tags for *mod_name* (RAW keys), keeping tags on rows the
    active filter hides. Returns the full new tagged set."""
    all_tags = read_root_mod_files(profile_dir, None)
    preserved = {k for k in all_tags.get(mod_name, set())
                 if k not in visible_keys}
    tagged = preserved | tagged_visible
    if tagged:
        all_tags[mod_name] = sorted(tagged)
    else:
        all_tags.pop(mod_name, None)
    write_root_mod_files(profile_dir, all_tags)
    return tagged


def read_root_tags(profile_dir: Path, mod_name: str) -> set[str]:
    """Saved root-tagged RAW keys for *mod_name* (empty set if none)."""
    if profile_dir is None:
        return set()
    return set(read_root_mod_files(profile_dir, None).get(mod_name, set()))


def _migrate_keys_to_raw(reader, writer, profile_dir: Path, mod_name: str,
                         raw_keys, stripped: set[str],
                         keep_unmatched: bool) -> bool:
    """Convert one mod's stored keys from legacy post-strip to raw.

    A key that is some file's post-strip key becomes that file - all of them
    when several collapse onto it, preserving the ticks the user sees."""
    data = reader(profile_dir, None)
    cur = data.get(mod_name)
    if not cur:
        return False
    raw_set = set(raw_keys)
    by_old: dict[str, list[str]] = {}
    for raw in raw_keys:
        by_old.setdefault(rel_key_after_strip(raw, stripped), []).append(raw)
    converted: set[str] = set()
    for k in cur:
        if k in raw_set:
            converted.add(k)
        elif k in by_old:
            converted.update(by_old[k])
        elif keep_unmatched:
            converted.add(k)
    if converted == set(cur):
        return False
    if converted:
        data[mod_name] = sorted(converted)
    else:
        data.pop(mod_name, None)
    writer(profile_dir, data)
    return True


def migrate_root_tags_to_raw(profile_dir: Path | None, mod_name: str | None,
                             raw_keys, stripped: set[str]) -> bool:
    """Convert a mod's root tags from legacy post-strip keys to raw paths.
    No-op for mods with no strip prefixes - the two spaces are identical."""
    if profile_dir is None or not mod_name or not stripped or not raw_keys:
        return False
    return _migrate_keys_to_raw(read_root_mod_files, write_root_mod_files,
                                profile_dir, mod_name, raw_keys, stripped,
                                keep_unmatched=False)


def migrate_exclusions_to_raw(profile_dir: Path | None, mod_name: str | None,
                              raw_keys, stripped: set[str]) -> bool:
    """Convert a mod's exclusions to raw paths, keeping keys that match no file
    - BSA pack excludes loose files it then deletes, and unpack needs those."""
    if profile_dir is None or not mod_name or not stripped or not raw_keys:
        return False
    return _migrate_keys_to_raw(read_excluded_mod_files,
                                write_excluded_mod_files,
                                profile_dir, mod_name, raw_keys, stripped,
                                keep_unmatched=True)


def translate_exclusions_for_engine(
        profile_dir: Path | None, staging_root: Path | None,
        strip_prefixes=None, per_mod_strip_prefixes: dict | None = None,
        root_folder_mods=None) -> dict[str, set[str]]:
    """Stored (raw-keyed) exclusions → engine/index key space, collision-safe.

    An index key is excluded only when EVERY raw file collapsing onto it is -
    a partial exclusion means the user picked a variant, so the key survives and
    the deploy source resolver skips the excluded ones. Keys matching no file
    pass through (BSA-packed entries whose loose files were deleted)."""
    if profile_dir is None:
        return {}
    from Utils.filegraph.paths import index_key_for_raw, mod_strip_args, scan_dir
    raw_exc = read_excluded_mod_files(profile_dir, None)
    out: dict[str, set[str]] = {}
    for mod, keys in raw_exc.items():
        kset = {k.lower() for k in keys if k}
        if not kset:
            continue
        strips, paths = mod_strip_args(mod, strip_prefixes,
                                       per_mod_strip_prefixes, root_folder_mods)
        if not strips and not paths:
            out[mod] = kset          # raw space == index space
            continue
        mod_dir = staging_root / mod if staging_root is not None else None
        if mod_dir is None or not mod_dir.is_dir():
            out[mod] = kset          # can't verify - mod deploys nothing anyway
            continue
        try:
            _n, raw_files, _r, _i = scan_dir(mod, str(mod_dir))
        except Exception:
            out[mod] = kset
            continue
        groups: dict[str, list[str]] = {}
        for raw in raw_files:
            groups.setdefault(
                index_key_for_raw(raw, strips, paths), []).append(raw)
        translated = {k for k in kset if k not in raw_files}   # passthrough
        translated |= {ik for ik, members in groups.items()
                       if all(m in kset for m in members)}
        if translated:
            out[mod] = translated
    return out


def index_keys_to_raw(mod_dir: Path | None, mod_name: str, index_keys,
                      strip_prefixes=None,
                      per_mod_strip_prefixes: dict | None = None,
                      root_folder_mods=None) -> set[str]:
    """Raw keys under *mod_dir* whose index key is in *index_keys*.

    The inverse of index_keys_for_mod, for callers that work in raw space (BSA
    packing walks the mod folder). Returns the input untouched when the mod has
    no strips or the folder can't be scanned - there the spaces are the same."""
    keys = {k.lower() for k in index_keys if k}
    if not keys:
        return set()
    from Utils.filegraph.paths import index_key_for_raw, mod_strip_args, scan_dir
    strips, paths = mod_strip_args(mod_name, strip_prefixes,
                                   per_mod_strip_prefixes, root_folder_mods)
    if not strips and not paths:
        return keys
    if mod_dir is None or not mod_dir.is_dir():
        return keys
    try:
        _n, raw_files, _r, _i = scan_dir(mod_name, str(mod_dir))
    except Exception:
        return keys
    return {raw for raw in raw_files
            if index_key_for_raw(raw, strips, paths) in keys}


def excluded_raw_by_mod(profile_dir: Path | None) -> dict[str, set[str]]:
    """Per-mod raw excluded keys (lowercase) for deploy source resolution."""
    if profile_dir is None:
        return {}
    return {m: {k.lower() for k in v if k}
            for m, v in read_excluded_mod_files(profile_dir, None).items() if v}


def prune_orphan_root_tags(profile_dir: Path | None, mod_name: str | None,
                           valid_keys: set[str]) -> bool:
    """Drop root tags matching no current file. No-op on an empty file list -
    an unreadable/symlinked mod folder scans empty and must not wipe tags."""
    if profile_dir is None or not mod_name or not valid_keys:
        return False
    tags = read_root_mod_files(profile_dir, None)
    cur = tags.get(mod_name)
    if not cur:
        return False
    kept = [k for k in cur if k in valid_keys]
    if len(kept) == len(cur):
        return False
    if kept:
        tags[mod_name] = kept
    else:
        tags.pop(mod_name, None)
    write_root_mod_files(profile_dir, tags)
    return True


def mod_has_changes(profile_dir: Path | None, mod_name: str | None) -> bool:
    """True when *mod_name* has any Mod Files tab state (strip / root / exclude)."""
    if profile_dir is None or not mod_name:
        return False
    try:
        return bool(read_mod_strip_prefixes(profile_dir, None).get(mod_name)
                    or read_root_mod_files(profile_dir, None).get(mod_name)
                    or read_excluded_mod_files(profile_dir, None).get(mod_name))
    except Exception:
        return False


def reset_mod_state(profile_dir: Path | None, mod_name: str | None) -> bool:
    """Drop every Mod Files edit for *mod_name*; True if anything was cleared."""
    if profile_dir is None or not mod_name:
        return False
    changed = False
    for reader, writer in ((read_mod_strip_prefixes, write_mod_strip_prefixes),
                           (read_root_mod_files, write_root_mod_files),
                           (read_excluded_mod_files, write_excluded_mod_files)):
        data = reader(profile_dir, None)
        if data.pop(mod_name, None):
            writer(profile_dir, data)
            changed = True
    return changed


def read_strip_prefixes(profile_dir: Path, mod_name: str) -> set[str]:
    """Saved strip-prefix entries (lowercased) for *mod_name*."""
    if profile_dir is None:
        return set()
    return {e.lower() for e in read_mod_strip_prefixes(profile_dir, None).get(mod_name, [])
            if e}
