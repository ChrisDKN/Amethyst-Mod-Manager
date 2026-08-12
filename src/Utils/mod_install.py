"""Toolkit-neutral mod installation core (no Tk / no Qt).

A self-contained install pipeline the Qt UI calls: extract an archive, auto-strip
to a sensible mod root, copy into the game's staging folder, then update the mod
index (+ BSA index), modlist.txt and plugins.txt. FOMOD archives are installed
with their DEFAULT (recommended) selections - the interactive wizard is NOT run
here (deferred to a later Qt port); a plain non-FOMOD layout installs verbatim.

The heavy Tk-coupled `gui/install_mod.py` is the full-featured path (FOMOD/BAIN
wizards, replace dialogs, Nexus lookup). This module reuses the same neutral
`Utils.*` backend so behaviour matches for the common case.

Public API:
    install_archive(archive_path, game, profile_dir, *, log_fn, progress_fn,
                    preferred_name="") -> str | None   # installed mod name
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

from Utils.extract_budget import get_uncompressed_size

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, Optional[str]], None]

# Sentinels returned by install_collection_archive when an interactive FOMOD/BAIN
# has no author selections and must be deferred to the end of a collection install
# (opened one-by-one in phase order). gui/install_mod.py imports these back so the
# Tk and Qt installers agree on the sentinel value.
FOMOD_DEFERRED = "__FOMOD_DEFERRED__"
BAIN_DEFERRED = "__BAIN_DEFERRED__"

# Serialises the per-profile "commit" writes (modindex.bin, modlist.txt,
# plugins.txt) - read-modify-write files that concurrent install workers
# (parallel batch installs, collection install consumers) would otherwise
# race, silently dropping entries.
_commit_lock = threading.Lock()


# ---- case-insensitive copy core (moved from gui/install_mod.py, shared) ------
# These resolve FOMOD/archive paths case-insensitively against the real (case-
# sensitive Linux) filesystem and dedup by destination - the accumulated fixes
# for "wrong files installed" / duplicate-cased folders. gui/install_mod.py
# imports them back so there is ONE implementation.
def _case_candidates(current: Path, part: str,
                     _cache: "dict[Path, dict[str, list[str]]]") -> "list[str]":
    """Real on-disk names in *current* matching *part* case-insensitively, exact
    case first. A dir can hold SEVERAL case variants of one name (mods ship both
    ``meshes/`` and ``Meshes/``), so the sibling map keeps EVERY variant - folding
    them to one name silently loses whole subtrees. Sorted for determinism."""
    if current not in _cache:
        entries: "dict[str, list[str]]" = {}
        try:
            for p in current.iterdir():
                entries.setdefault(p.name.lower(), []).append(p.name)
        except OSError:
            pass
        for names in entries.values():
            names.sort()
        _cache[current] = entries
    names = _cache[current].get(part.lower())
    if not names:
        return [part]
    if part in names:
        return [part] + [n for n in names if n != part]
    return list(names)


def _resolve_src_case(src_root: Path, src_rel: str,
                      _cache: "dict[Path, dict[str, list[str]]] | None" = None) -> Path:
    """src_root / src_rel, resolved case-insensitively against what exists on disk
    (FOMOD XML is Windows-cased) - but ONLY as a fallback.

    The literal path is tried first: the direct-install list feeds REAL on-disk
    paths, and case-folding those is destructive, not corrective. A mod shipping
    case-variant sibling folders (Relics of Hyrule: 147 such groups, nested 6 deep)
    had every file under the "losing" variant rewritten to a path that does not
    exist, failing the ``src.is_file()`` guard in ``_copy_file_list`` and vanishing
    with no log - 2490 of 7597 files.

    When the literal path misses, walk the components case-insensitively and
    BACKTRACK: with both ``meshes/`` and ``Meshes/`` on disk, a mis-cased XML path
    must be able to try the other variant instead of dead-ending in the first."""
    if _cache is None:
        _cache = {}
    parts = [p for p in src_rel.replace("\\", "/").strip("/").split("/") if p]
    if not parts:
        return src_root
    literal = src_root.joinpath(*parts)
    if literal.exists():
        return literal

    def _walk(current: Path, idx: int) -> "Path | None":
        if idx == len(parts):
            return current
        for cand in _case_candidates(current, parts[idx], _cache):
            nxt = current / cand
            if not nxt.exists():
                continue
            hit = _walk(nxt, idx + 1)
            if hit is not None:
                return hit
        return None

    found = _walk(src_root, 0)
    # Nothing matched: hand back the literal path so the caller's is_file() check
    # fails as before (and any error message names the path actually asked for).
    return found if found is not None else literal


def _resolve_dst_case(dest_root: Path, dst_rel: str,
                      _cache: "dict[Path, dict[str, list[str]]] | None" = None) -> Path:
    """dest_root / dst_rel with each component resolved case-insensitively so a
    FOMOD install doesn't create duplicate folders differing only in case.

    Folding IS wanted here (merge into whatever casing the destination already
    has), but an exact-case hit wins over a variant - otherwise, once a mod folder
    legitimately holds two case variants, which one a file lands in is arbitrary."""
    if _cache is None:
        _cache = {}
    current = dest_root
    for part in dst_rel.replace("\\", "/").split("/"):
        if not part:
            continue
        current = current / _case_candidates(current, part, _cache)[0]
    return current


def _link_or_copy(src, dst) -> None:
    """Hardlink src→dst when same-fs (near-instant, zero extra disk), else copy."""
    try:
        os.link(src, dst)
        return
    except OSError:
        try:
            shutil.copy2(src, dst)
        except FileNotFoundError:
            shutil.copy2(src, dst)


def _copytree_case_insensitive(src: Path, dst: Path) -> int:
    """Recursive copy resolving each dst component case-insensitively against
    disk (a case-aware shutil.copytree(dirs_exist_ok=True)). Returns file count.

    The case-insensitive sibling map for *dst* is read ONCE up front and updated
    in-place as we create children - NOT re-listed per entry. Re-listing on every
    entry made this O(N²) in a directory's file count (an ``iterdir`` per file ×
    N files): on a 22k-file mod folder (e.g. an OStim animation pack, which the
    FOMOD/BAIN path stages as a single folder entry) it took ~275 s instead of
    ~2 s and silently dominated collection-install time.
    """
    copied = 0
    dst.mkdir(parents=True, exist_ok=True)
    try:
        existing = {p.name.lower(): p.name for p in dst.iterdir()}
    except OSError:
        existing = {}
    for entry in os.scandir(src):
        child_name = existing.get(entry.name.lower(), entry.name)
        child_dst = dst / child_name
        if entry.is_dir(follow_symlinks=False):
            copied += _copytree_case_insensitive(Path(entry.path), child_dst)
        elif entry.is_file(follow_symlinks=False):
            child_dst.parent.mkdir(parents=True, exist_ok=True)
            if child_dst.is_dir():
                shutil.rmtree(child_dst)
            elif child_dst.exists():
                child_dst.chmod(0o644)
                child_dst.unlink()
            _link_or_copy(entry.path, child_dst)
            copied += 1
        # Remember what we just created so a later sibling that differs only in
        # case resolves against it without another directory listing.
        existing.setdefault(entry.name.lower(), child_name)
    return copied


def _merge_case_variant_dirs(file_list, game, log_fn):
    """Unify one mod's case-variant folders (``meshes/`` + ``Meshes/``) onto a
    single casing - the same pick the filemap makes at deploy time.

    Windows-authored mods routinely ship both; on case-insensitive NTFS they ARE
    one folder, so splitting them in staging shows duplicate folders no Windows
    manager would, and disagrees with the merged view the Data tab renders.

    Lossless by construction: ``_copy_file_list`` already dedups on the
    LOWERCASED destination, so folding folder casing cannot change which files
    survive - only which folder they land in.

    Skipped when the game (or the global setting) turns folder-case
    normalization off - for Stardew Valley on Linux ``Music/`` and ``music/``
    really are different directories. Only file entries are rewritten; folder
    entries already merge via ``_copytree_case_insensitive``.
    """
    if game is None or not getattr(game, "normalize_folder_case", True):
        return file_list
    try:
        from Utils.ui_config import load_normalize_folder_case
        if not load_normalize_folder_case():
            return file_list
        from Utils.filemap import canonicalize_dir_casing
    except Exception:
        return file_list

    # A trailing slash marks a DIRECTORY destination ("copy as src.name") -
    # canonicalize treats a path's last segment as a filename and leaves it
    # alone, so dir destinations get a sentinel filename appended: every real
    # segment then counts as (and merges as) a folder.
    _SENTINEL = "\x00"
    rels: list[str] = []
    for _s, d, is_folder in file_list:
        if is_folder:
            continue
        key = d.replace("\\", "/").strip("/")
        if key and (d.endswith("/") or d.endswith("\\")):
            key += "/" + _SENTINEL
        rels.append(key)
    if not rels:
        return file_list
    mapping = canonicalize_dir_casing(
        rels,
        getattr(game, "filemap_casing", "upper") or "upper",
        getattr(game, "filemap_casing_pins", None))
    merged = {r.rsplit("/", 1)[0] for r, n in mapping.items() if r != n and "/" in r}
    if not merged:
        return file_list

    out = []
    for src_rel, dst_rel, is_folder in file_list:
        if is_folder:
            out.append((src_rel, dst_rel, is_folder))
            continue
        key = dst_rel.replace("\\", "/").strip("/")
        if dst_rel.endswith("/") or dst_rel.endswith("\\"):
            # Keep the trailing-slash marker _copy_file_list keys on; losing
            # it would write the file AT the directory path.
            new = mapping.get(key + "/" + _SENTINEL) if key else None
            new = new[:-2] if new else key
            out.append((src_rel, new + "/", is_folder))
        else:
            out.append((src_rel, mapping.get(key, key), is_folder))
    log_fn(f"Unified {len(merged)} case-variant folder(s) to one casing.")
    return out


def _copy_file_list(file_list, src_root: str, dest_root: Path, log_fn,
                    game=None) -> None:
    """Copy each (src_rel, dst_rel, is_folder) from src_root → dest_root with
    case-insensitive resolution + destination dedup (later wins = FOMOD priority).
    Folders via the recursive copytree; files in parallel. *game* enables the
    case-variant folder merge (None → leave each mod's casing exactly as shipped)."""
    from concurrent.futures import ThreadPoolExecutor

    file_list = _merge_case_variant_dirs(file_list, game, log_fn)

    folder_copied = 0
    file_entries: list = []
    _src_cache: dict = {}
    _dst_cache: dict = {}
    src_root_path = Path(src_root)

    for src_rel, dst_rel, is_folder in file_list:
        if src_rel:
            # Always resolve the SOURCE case-insensitively. FOMOD XML paths are
            # Windows-cased, but the extracted archive may be lower/other-cased
            # on disk (e.g. CACO ships "Complete Alchemy & Cooking Overhaul.esp"
            # in the XML but "complete alchemy...esp" on disk). A raw case-
            # sensitive join here fails src.is_file() and silently drops the
            # file - even when src_rel == dst_rel (the parser's rule for a
            # <file destination="">), which is the common case for main files.
            src = _resolve_src_case(src_root_path, src_rel, _src_cache)
        else:
            src = src_root_path
        dst = (_resolve_dst_case(dest_root, dst_rel, _dst_cache)
               if dst_rel else dest_root / dst_rel)
        if is_folder:
            if not dst_rel:
                dst = dest_root
            if src.is_dir():
                folder_copied += _copytree_case_insensitive(src, dst)
                _dst_cache.pop(dst.parent, None)
        else:
            if not dst_rel:
                dst = dest_root / src.name
            elif dst_rel.endswith("/") or dst_rel.endswith("\\"):
                dst = _resolve_dst_case(dest_root, dst_rel.rstrip("/\\"), _dst_cache) / src.name
            if src.is_file():
                file_entries.append((src, dst))

    if file_entries:
        by_dst: dict = {}
        for src, dst in file_entries:
            by_dst[str(dst).lower()] = (src, dst)
        file_entries = list(by_dst.values())

    dirs_seen: set = set()
    for _, dst in file_entries:
        d = dst.parent
        if d not in dirs_seen:
            d.mkdir(parents=True, exist_ok=True)
            dirs_seen.add(d)

    def _copy_one(src_dst):
        src, dst = src_dst
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists() or dst.is_symlink():
            try:
                dst.unlink()
            except PermissionError:
                dst.chmod(0o644)
                dst.unlink()
        _link_or_copy(src, dst)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for _ in pool.map(_copy_one, file_entries, chunksize=256):
            pass
    copied = folder_copied + len(file_entries)
    log_fn(f"Copied {copied} item(s) to staging area.")


# ---- file-list staging pipeline (moved from gui/install_mod.py, shared) -------
# Builds a (src, dst, is_folder) list for a non-FOMOD archive and normalises it to
# the game's expected layout: strip prefixes → install-prefix → required-top-level
# check → auto-strip folders → auto-strip to a required file type → (else) ask for
# a prefix → post-strip. gui/install_mod.py imports these back so Tk + Qt run the
# SAME staging code (fixes "mod installed with wrong structure" e.g. CET).
def resolve_direct_files(extract_dir: str) -> list[tuple[str, str, bool]]:
    """Every file under *extract_dir* as a (src, dst, is_folder) tuple (src == dst,
    both relative to the root). A top-level ``fomod/`` folder is skipped (installer
    metadata, not game content), as is a root-level ``meta.ini`` - some archives
    (e.g. re-uploaded MO2 mods) ship their own MO2 meta.ini, which would collide
    with the one we write and, being in MO2's valueless-key format, break every
    ConfigParser read of it (``read_meta`` etc.)."""
    result: list[tuple[str, str, bool]] = []
    root = Path(extract_dir)
    for entry in root.rglob("*"):
        if entry.is_file():
            rel = str(entry.relative_to(root))
            parts = rel.replace("\\", "/").split("/")
            first = parts[0]
            if first.lower() == "fomod":
                continue
            if len(parts) == 1 and first.lower() == "meta.ini":
                continue
            result.append((rel, rel, False))
    return result


def unwrap_single_folder(extract_dir: str) -> str:
    """If *extract_dir* has exactly one subdirectory and no files, return that
    subdirectory (archives wrapped in a single ModName/ folder)."""
    root = Path(extract_dir)
    try:
        entries = list(root.iterdir())
    except OSError:
        return extract_dir
    if len(entries) == 1 and entries[0].is_dir():
        return str(entries[0])
    return extract_dir


def apply_strip_prefixes_to_file_list(
    file_list: list[tuple[str, str, bool]],
    strip_prefixes: set[str],
) -> list[tuple[str, str, bool]]:
    """Strip leading path segments matching *strip_prefixes* (case-insensitive),
    repeatedly, until the first segment is not in the set."""
    if not strip_prefixes:
        return file_list
    strip_lower = {p.lower() for p in strip_prefixes}
    result: list[tuple[str, str, bool]] = []
    for src_rel, dst_rel, is_folder in file_list:
        had_trailing = dst_rel.endswith("/") or dst_rel.endswith("\\")
        d = dst_rel.replace("\\", "/").strip("/")
        while "/" in d:
            first, remainder = d.split("/", 1)
            if first.lower() in strip_lower:
                d = remainder
            else:
                break
        if had_trailing and d:
            d = d + "/"
        result.append((src_rel, d, is_folder))
    return result


def check_mod_top_level(file_list: list[tuple[str, str, bool]],
                        required: set[str]) -> bool:
    """True if at least one file's top-level folder matches a required name."""
    for _, dst_rel, _ in file_list:
        top = dst_rel.replace("\\", "/").split("/")[0].lower()
        if top in required:
            return True
    return False


def try_auto_strip_top_level(
    file_list: list[tuple[str, str, bool]],
    required: set[str],
    max_strip_depth: int = 20,
) -> tuple[list[tuple[str, str, bool]], bool]:
    """Strip leading path segments until at least one file's top-level folder is in
    *required*. Files shallower than the strip depth sit outside the wrapper and are
    kept at the root rather than dropped. Returns (new_list, True) on success, else
    (original, False)."""
    required_lower = {r.lower() for r in required}
    if check_mod_top_level(file_list, required_lower):
        return (file_list, True)
    for strip_depth in range(1, max_strip_depth + 1):
        new_list: list[tuple[str, str, bool]] = []
        has_required = False
        for src_rel, dst_rel, is_folder in file_list:
            parts = dst_rel.replace("\\", "/").strip("/").split("/")
            if len(parts) <= strip_depth:
                new_list.append((src_rel, parts[-1], is_folder))
                continue
            new_dst = "/".join(parts[strip_depth:])
            top = parts[strip_depth].lower()
            if top in required_lower:
                has_required = True
            new_list.append((src_rel, new_dst, is_folder))
        if has_required and new_list:
            return (new_list, True)
    return (file_list, False)


def check_mod_top_level_file_types(
    file_list: list[tuple[str, str, bool]],
    required_exts: set[str],
) -> bool:
    """True if at least one top-level file (no sub-folder) has a required ext."""
    exts_lower = {e.lower() for e in required_exts}
    for _, dst_rel, is_folder in file_list:
        if is_folder:
            continue
        dst_rel = dst_rel.replace("\\", "/").strip("/")
        if "/" not in dst_rel:
            ext = Path(dst_rel).suffix.lower()
            if ext in exts_lower:
                return True
    return False


def try_auto_strip_for_file_types(
    file_list: list[tuple[str, str, bool]],
    required_exts: set[str],
    max_strip_depth: int = 20,
) -> tuple[list[tuple[str, str, bool]], bool]:
    """Strip leading segments until a top-level file has a required ext. Files
    shallower than the strip depth are kept at the root rather than dropped. Returns
    (new_list, True) on success, else (original, False)."""
    if check_mod_top_level_file_types(file_list, required_exts):
        return (file_list, True)
    exts_lower = {e.lower() for e in required_exts}
    for strip_depth in range(1, max_strip_depth + 1):
        new_list: list[tuple[str, str, bool]] = []
        has_required = False
        for src_rel, dst_rel, is_folder in file_list:
            parts = dst_rel.replace("\\", "/").strip("/").split("/")
            if len(parts) <= strip_depth:
                new_list.append((src_rel, parts[-1], is_folder))
                continue
            new_dst = "/".join(parts[strip_depth:])
            if not is_folder and len(parts) == strip_depth + 1:
                ext = Path(new_dst).suffix.lower()
                if ext in exts_lower:
                    has_required = True
            new_list.append((src_rel, new_dst, is_folder))
        if has_required and new_list:
            return (new_list, True)
    return (file_list, False)


def stage_file_list(game, extract_dir: str, *, is_root_install: bool = False,
                    mod_name: str = "", on_need_prefix=None,
                    log_fn: LogFn = lambda _m: None
                    ) -> list[tuple[str, str, bool]] | None:
    """Build + normalise the (src, dst, is_folder) list for a non-FOMOD mod under
    *extract_dir*, applying the game's structure rules EXACTLY like the Tk
    installer (gui/install_mod.py lines ~2454–2570):

      strip_prefixes → mod_install_prefix → required-top-level check →
      auto-strip folders → auto-strip to a required file type →
      (unrecognised, not install-as-is) on_need_prefix() → post-strip.

    *on_need_prefix(required, file_list, mod_name) -> str | None* supplies the
    prefix interactively (the toolkit shows a dialog); returning None cancels the
    install (this function returns None). With no callback, an unrecognised mod is
    installed as-is. Returns the final file_list (or None if cancelled)."""
    file_list = resolve_direct_files(extract_dir)

    strip_prefixes = getattr(game, "mod_folder_strip_prefixes", set())
    if strip_prefixes and not is_root_install:
        file_list = apply_strip_prefixes_to_file_list(file_list, strip_prefixes)

    required = getattr(game, "mod_required_top_level_folders", set())
    required_lower = {r.lower() for r in required}

    install_prefix = getattr(game, "mod_install_prefix", "")
    if install_prefix and not is_root_install:
        install_prefix = install_prefix.strip().strip("/").replace("\\", "/")
        prefix_parts = install_prefix.lower().split("/")
        new_file_list = []
        for s, d, f in file_list:
            d_parts = d.replace("\\", "/").split("/")
            d_parts_lower = [p.lower() for p in d_parts]
            if d_parts_lower[0] in required_lower:
                new_file_list.append((s, d, f))
                continue
            match_len = 0
            for i in range(len(prefix_parts), 0, -1):
                if d_parts_lower[:i] == prefix_parts[-i:]:
                    match_len = i
                    break
            missing = "/".join(install_prefix.split("/")[:len(prefix_parts) - match_len])
            if missing:
                new_file_list.append((s, f"{missing}/{d}", f))
            else:
                new_file_list.append((s, d, f))
        file_list = new_file_list
        log_fn(f"Auto-prefixed mod files under '{install_prefix}/' (where needed).")

    required_file_types = getattr(game, "mod_required_file_types", set())
    auto_strip = getattr(game, "mod_auto_strip_until_required", False)
    install_as_is = getattr(game, "mod_install_as_is_if_no_match", False)
    did_auto_strip = False

    if required and not check_mod_top_level(file_list, required):
        root_marker_exts = {e.lower() for e in required_file_types} & {
            e.lower() for e in
            (list(getattr(game, "plugin_extensions", []) or [])
             + list(getattr(game, "archive_extensions", []) or []))
        }
        if root_marker_exts and check_mod_top_level_file_types(
                file_list, root_marker_exts):
            did_auto_strip = True
            log_fn("Mod has a plugin/archive at its root - archive root is already the mod root.")
        if not did_auto_strip and auto_strip:
            file_list, did_auto_strip = try_auto_strip_top_level(file_list, required)
            if did_auto_strip:
                log_fn("Auto-stripped top-level folder(s) so mod matches expected structure.")
        if not did_auto_strip and required_file_types:
            if check_mod_top_level_file_types(file_list, required_file_types):
                did_auto_strip = True
                log_fn("Mod contains recognised top-level file type(s) - skipping prefix check.")
            elif auto_strip:
                file_list, did_auto_strip = try_auto_strip_for_file_types(
                    file_list, required_file_types)
                if did_auto_strip:
                    log_fn("Auto-stripped top-level folder(s) to expose recognised file type(s).")
        if not did_auto_strip:
            if install_as_is:
                log_fn("Mod structure unrecognised - installing as-is (no prefix applied).")
            else:
                prefix = on_need_prefix(required, file_list, mod_name) if on_need_prefix else None
                if prefix is None and on_need_prefix is not None:
                    log_fn("Install cancelled - mod structure not mapped.")
                    return None
                if prefix:
                    prefix = prefix.strip().strip("/").replace("\\", "/")
                    file_list = [(s, f"{prefix}/{d}", f) for s, d, f in file_list]
                    log_fn(f"Remapped mod files under '{prefix}/'.")
    elif (not required and required_file_types
          and not check_mod_top_level_file_types(file_list, required_file_types)):
        if auto_strip:
            file_list, did_auto_strip = try_auto_strip_for_file_types(
                file_list, required_file_types)
            if did_auto_strip:
                log_fn("Auto-stripped top-level folder(s) to expose recognised file type(s).")
        if not did_auto_strip:
            if install_as_is:
                log_fn("Mod structure unrecognised - installing as-is (no prefix applied).")
            else:
                prefix = on_need_prefix(set(), file_list, mod_name) if on_need_prefix else None
                if prefix is None and on_need_prefix is not None:
                    log_fn("Install cancelled - mod structure not mapped.")
                    return None
                if prefix:
                    prefix = prefix.strip().strip("/").replace("\\", "/")
                    file_list = [(s, f"{prefix}/{d}", f) for s, d, f in file_list]
                    log_fn(f"Remapped mod files under '{prefix}/'.")

    post_strip_prefixes = getattr(game, "mod_folder_strip_prefixes_post", set())
    if post_strip_prefixes and not is_root_install:
        file_list = apply_strip_prefixes_to_file_list(file_list, post_strip_prefixes)

    # Drop any file whose FINAL destination is the mod-root meta.ini.
    filtered = [(s, d, f) for s, d, f in file_list
                if d.replace("\\", "/").strip("/").lower() != "meta.ini"]
    if len(filtered) != len(file_list):
        log_fn("Skipped bundled meta.ini from archive (Amethyst writes its own).")
    return filtered


# ---------------------------------------------------------------- temp location
# Guards /tmp space accounting so parallel extractions (collection installs run
# several workers at once) don't all claim the same free space before any of
# them has started writing - Tk parity (gui/install_mod.py _tmp_space_reserved).
_tmp_space_lock = threading.Lock()
_tmp_space_reserved: int = 0   # bytes claimed by in-flight /tmp extractions


def _release_tmp_reservation(nbytes: int) -> None:
    global _tmp_space_reserved
    if nbytes:
        with _tmp_space_lock:
            _tmp_space_reserved = max(0, _tmp_space_reserved - nbytes)


def _is_disk_full_error(text: "str | None") -> bool:
    """True if *text* (tool stderr / exception text) reports out-of-space.

    Covers ENOSPC ("No space left") and the tmpfs size-cap case, which the
    kernel reports as EDQUOT - surfaced by 7z/tar as "Disk Quota Exceeded"."""
    if not text:
        return False
    low = text.lower()
    return any(s in low for s in (
        "disk quota exceeded", "quota exceeded",
        "no space left", "enospc", "edquot",
    ))


def _is_truncated_archive_error(text: "str | None") -> bool:
    """True if *text* (tool stderr / exception text) reports a truncated or
    incomplete archive - an interrupted download, not an extractor problem.

    Covers 7z ("Unexpected end of archive"), bsdtar on rar/tar ("Truncated
    input file: needed N bytes, only M available") and bsdtar's streaming zip
    reader, which hits the short deflate stream instead ("ZIP decompression
    failed (-5)")."""
    if not text:
        return False
    low = text.lower()
    return any(s in low for s in (
        "truncated", "unexpected end of archive",
        "unexpected end of file", "unexpected end of data",
        "unexpected eof", "premature end",
        "zip decompression failed",
    ))


def _is_small_fs(path: str, limit_gib: int = 8) -> bool:
    """True if *path*'s mount is small enough to be a tmpfs ramdisk (e.g. /tmp)."""
    try:
        st = os.statvfs(path)
        return st.f_blocks * st.f_frsize < limit_gib * 1024 ** 3
    except OSError:
        return False


def _same_fs(a: str, b: str) -> bool:
    """True if *a* and *b* are on the same filesystem, or it can't be told
    (retrying an extraction there would be pointless either way)."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return True


def _free_bytes(path: str) -> int:
    try:
        st = os.statvfs(path)
        return st.f_frsize * st.f_bavail
    except OSError:
        return 0


def _choose_extract_parent(archive_path: str, staging_root: Path,
                           log_fn: LogFn) -> "tuple[Path | None, int]":
    """Pick a temp-dir PARENT that can hold the extraction. Default /tmp is a
    RAM-backed tmpfs (Steam Deck: roughly half of RAM) - if the archive won't
    fit there with headroom, extract NEXT TO the staging folder (real disk)
    instead. This mirrors the Tk app's reroute logic - without it large mods
    fail with 'No space left on device'.

    Returns ``(parent, tmp_reserved_bytes)``: *parent* None = use /tmp, in
    which case *tmp_reserved_bytes* has been claimed from the shared /tmp
    reservation pool so concurrent extractions don't jointly overflow it -
    release it with :func:`_release_tmp_reservation` once the extract dir is
    deleted."""
    global _tmp_space_reserved
    # Real metadata size where available (`7z l` probe / zip headers), 15×
    # fallback otherwise - a compressed-size multiple alone undershoots extreme
    # texture packs (a 120 MB .7z that unpacks to 3.6 GB is 30×).
    need = get_uncompressed_size(archive_path)
    headroom = 512 * 1024 * 1024
    tmp = tempfile.gettempdir()
    with _tmp_space_lock:
        if need + headroom + _tmp_space_reserved < _free_bytes(tmp):
            _tmp_space_reserved += need
            return None, need   # /tmp has room - claimed
    # /tmp too small (a RAM-backed tmpfs) → use the staging filesystem (real disk).
    disk_parent = staging_root.parent if staging_root else None
    if disk_parent is not None:
        try:
            disk_parent.mkdir(parents=True, exist_ok=True)
            if need + headroom < _free_bytes(str(disk_parent)):
                log_fn(f"Extracting to disk ({disk_parent}) - /tmp too small for "
                       f"~{need // (1024 * 1024)} MB.")
                return disk_parent, 0
            log_fn(f"Warning: extract target {disk_parent} may also be low on "
                   "space.")
            return disk_parent, 0
        except OSError:
            pass
    return None, 0


def _log_extract_location(extract_dir: Path, log_fn: LogFn) -> None:
    """Record where the archive unpacks and how much room is there, so
    disk-full failures are diagnosable from the log (Tk parity)."""
    try:
        free_gb = _free_bytes(str(extract_dir)) / (1024 ** 3)
        loc = "ramdisk" if _is_small_fs(str(extract_dir)) else "disk"
        log_fn(f"Extracting to {extract_dir} ({loc}, {free_gb:.1f} GB free)")
    except OSError:
        log_fn(f"Extracting to {extract_dir}")


def _extract_with_disk_retry(archive_path: str, staging_root: Path,
                             log_fn: LogFn, cancel: "threading.Event | None" = None,
                             progress_cb=None
                             ) -> "tuple[bool, Path, int, list[str]]":
    """Extract *archive_path* into a fresh temp dir under a parent with room
    (:func:`_choose_extract_parent`), retrying ONCE next to the staging folder
    when the first attempt fails disk-full on a DIFFERENT filesystem. The size
    probe can undershoot (solid .7z below the probe threshold falls back to
    ×15) and a RAM-backed /tmp caps out with EDQUOT - the retry gate compares
    st_dev rather than guessing "ramdisk" from total fs size, which broke on
    ≥16 GB RAM machines whose half-RAM tmpfs exceeds any size cutoff.

    Returns ``(extracted, extract_dir, tmp_reserved, errors)``; the caller
    owns the directory and the /tmp reservation."""
    parent, tmp_reserved = _choose_extract_parent(archive_path, staging_root,
                                                  log_fn)
    extract_dir = Path(tempfile.mkdtemp(prefix="mm_install_",
                                        dir=str(parent) if parent else None))
    _log_extract_location(extract_dir, log_fn)
    errors: list[str] = []
    extracted = _extract_archive(archive_path, str(extract_dir), log_fn,
                                 cancel=cancel, error_sink=errors,
                                 progress_cb=progress_cb)
    if (not extracted and (cancel is None or not cancel.is_set())
            and any(_is_disk_full_error(e) for e in errors)):
        disk_parent = staging_root.parent if staging_root else None
        new_dir = None
        if disk_parent is not None:
            try:
                disk_parent.mkdir(parents=True, exist_ok=True)
                if not _same_fs(str(extract_dir), str(disk_parent)):
                    new_dir = Path(tempfile.mkdtemp(prefix="mm_install_",
                                                    dir=str(disk_parent)))
            except OSError:
                new_dir = None
        if new_dir is not None:
            log_fn("Extraction ran out of space in the temp folder - "
                   "retrying next to the staging folder…")
            shutil.rmtree(extract_dir, ignore_errors=True)
            _release_tmp_reservation(tmp_reserved)
            tmp_reserved = 0
            extract_dir = new_dir
            _log_extract_location(extract_dir, log_fn)
            extracted = _extract_archive(archive_path, str(extract_dir),
                                         log_fn, cancel=cancel,
                                         error_sink=errors,
                                         progress_cb=progress_cb)
    return extracted, extract_dir, tmp_reserved, errors


# ---------------------------------------------------------------- extraction
def _debackslash_extracted_tree(extract_dir: str, log_fn: LogFn) -> int:
    """Repair an extraction that kept Windows backslash path separators.

    Some ZIPs (commonly packed by PowerShell's ``Compress-Archive``, or Nexus
    mods zipped on Windows) store member names with ``\\`` separators, which
    violates the ZIP spec. Native extractors (7z/bsdtar) and Python's
    ``zipfile`` then create *flat* files whose names literally contain
    backslashes - e.g. a single file called ``r6\\scripts\\foo.reds`` - instead
    of a nested folder tree. Downstream staging resolves paths with forward
    slashes, so those flat entries never match and the mod stages 0 files
    ("nothing staged"). This sweep relocates every backslash-named entry to its
    proper nested location. Returns the number of entries moved. Idempotent and
    cheap when no backslash names exist (the common case).

    Faithful port of ``gui/install_mod.py:_debackslash_extracted_tree``.
    """
    root = Path(extract_dir)
    # Collect first so we don't mutate the tree mid-walk. Deepest paths first so
    # files move before we try to clean up their (now-empty) flat parents.
    try:
        offenders = [p for p in root.rglob("*") if "\\" in p.name]
    except OSError:
        return 0
    if not offenders:
        return 0
    moved = 0
    for entry in sorted(offenders, key=lambda p: len(str(p)), reverse=True):
        if not entry.exists():
            continue
        rel = str(entry.relative_to(root)).replace("\\", "/")
        target = root / rel
        if target == entry:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry.is_dir():
                # A backslash-named directory: merge its contents, then drop it.
                target.mkdir(parents=True, exist_ok=True)
                for child in entry.iterdir():
                    dest = target / child.name
                    if not dest.exists():
                        shutil.move(str(child), str(dest))
                try:
                    entry.rmdir()
                except OSError:
                    pass
            else:
                if target.exists():
                    target.unlink()
                shutil.move(str(entry), str(target))
                moved += 1
        except OSError:
            continue
    if moved and log_fn is not None:
        log_fn(f"Normalised {moved} Windows backslash path(s) from archive.")
    return moved


def _fix_perms_extracted_tree(extract_dir: str, log_fn: LogFn) -> int:
    """Restore owner access on extracted entries the archive stored as mode 000.

    An archive packed on Windows can still carry a Unix permission attribute
    (7z reports ``Attributes = A ----------``), and 7z applies it faithfully -
    so every file lands with NO permission bits and the next step, staging the
    file list, dies with "Permission denied" on the first read. An archive's
    Unix bits are meaningless for Windows game data anyway, so force owner
    read/write on files and read/write/execute on directories, leaving every
    other bit (notably the execute bits Linux-native mods rely on) untouched.
    Top-down so a mode-000 directory is made traversable BEFORE we walk into
    it. Symlinks are skipped (their own mode is always 0777). Returns the
    number of entries fixed - 0, one stat per entry, in the common case.
    """
    def _ensure(path: str, want: int) -> int:
        try:
            mode = os.stat(path, follow_symlinks=False).st_mode & 0o7777
        except OSError:
            return 0
        if mode & want == want:
            return 0
        try:
            os.chmod(path, mode | want)
        except OSError:
            return 0
        return 1

    fixed = _ensure(extract_dir, 0o700)
    for dirpath, dirnames, filenames in os.walk(extract_dir, topdown=True):
        for name in dirnames:
            fixed += _ensure(os.path.join(dirpath, name), 0o700)
        for name in filenames:
            fixed += _ensure(os.path.join(dirpath, name), 0o600)
    if fixed and log_fn is not None:
        log_fn(f"Repaired unreadable permissions on {fixed} extracted entries.")
    return fixed


def _fix_nonutf8_names_extracted_tree(extract_dir: str, log_fn: LogFn) -> int:
    """Repair extracted file/dir names that are not valid UTF-8.

    Archives packed on Windows without the UTF-8 name flag store member names in
    a legacy code page (CP437/CP1252). Extractors write those bytes verbatim, so
    a file meant to be ``himinbjörg.mp3`` lands on disk as the raw byte
    ``0x94`` - not valid UTF-8. The OS surfaces such names surrogate-escaped
    (``\\udc94``), which (a) crashed logging that embedded them and (b) makes
    filemap.rebuild_mod_index SKIP THE WHOLE MOD (its _is_utf8_safe check), so
    the mod silently contributes no files / plugins / conflicts.

    This sweep renames every non-UTF-8 entry to a valid UTF-8 name by decoding
    the raw bytes with a legacy code page (CP1252 first - the dominant Windows
    source; the resulting character may not be the visually-intended one, but
    the file becomes addressable and the mod works). Deepest-first so a renamed
    parent dir doesn't invalidate child paths. Returns entries renamed.
    Idempotent; cheap when all names are already UTF-8 (the common case).
    Thin wrapper over the neutral filemap.repair_nonutf8_names so the extract
    path and the Refresh (heal-on-disk) path share one implementation.
    """
    from Utils.filemap import repair_nonutf8_names
    return repair_nonutf8_names(extract_dir, log_fn=log_fn)


def _extract_archive(archive_path: str, dest_dir: str, log_fn: LogFn,
                     cancel=None, error_sink: "list[str] | None" = None,
                     progress_cb: "Callable[[int], None] | None" = None) -> bool:
    """Extract *archive_path* into *dest_dir*. Native 7z → bsdtar → py7zr →
    Python zipfile/tarfile, mirroring gui.install_mod's fallback chain. After a
    successful native/zip extraction, backslash-named members are normalised
    into a real tree (see _debackslash_extracted_tree).

    *cancel* - optional ``threading.Event``; when set, the running native
    extractor (7z/bsdtar) is terminated and this returns False so the caller can
    clean up the partial extract dir (used by the collection-install pause).

    *error_sink* - optional list; each extractor's failure text is appended so
    the caller can classify the overall failure (e.g. disk-full → retry on a
    bigger filesystem).

    *progress_cb* - optional ``cb(percent)`` with real extraction progress.
    Only the 7z (``-bsp1``) and zipfile paths report; the rare fallbacks
    (bsdtar/py7zr/tarfile) never fire it, leaving the caller's indeterminate
    bar in place."""
    ext = Path(archive_path).suffix.lower()

    def _note(err) -> None:
        if error_sink is not None:
            error_sink.append(str(err))

    # tar.* and plain .tar → tarfile directly.
    if ext in (".tar", ".gz", ".bz2", ".xz", ".tgz") or \
            archive_path.lower().endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                tf.extractall(dest_dir, filter="data")
            # tarfile's "data" filter keeps member modes (masked to 0755), so a
            # mode-000 member stays unreadable here too.
            _fix_perms_extracted_tree(dest_dir, log_fn)
            log_fn("Extracted with tarfile.")
            return True
        except Exception as exc:
            _note(exc)
            log_fn(f"tarfile failed ({exc}).")
            # fall through to the generic extractors

    def _ok() -> bool:
        # Archives can carry a mode-000 Unix attribute; clear it FIRST or the
        # repair sweeps below (and staging) can't even read the tree.
        _fix_perms_extracted_tree(dest_dir, log_fn)
        # Native extractors (7z/bsdtar) and Python zipfile reproduce Windows
        # backslash member names as literal flat filenames; repair them into a
        # real tree so staging can resolve the paths (fixes "nothing staged").
        _debackslash_extracted_tree(dest_dir, log_fn)
        # Repair non-UTF-8 (legacy code page) names so the mod isn't skipped by
        # the index (rebuild_mod_index drops any mod with a non-UTF-8 filename).
        _fix_nonutf8_names_extracted_tree(dest_dir, log_fn)
        return True

    def _cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    if _cancelled():
        return False

    # Extraction resource limits (Settings ▸ Downloads & Collections). Read per
    # archive so a settings change applies to the next extraction without a
    # restart - the INI parse is trivial next to the extractor spawn it gates.
    try:
        from Utils.ui_config import load_extraction_settings
        _limits = load_extraction_settings()
    except Exception:
        _limits = {}
    _threads = int(_limits.get("cpu_threads", 0) or 0)
    _low_prio = bool(_limits.get("low_priority", False))
    _mmt = f"-mmt={_threads}" if _threads > 0 else "-mmt=on"

    _7z = (shutil.which("7zzs") or shutil.which("7zz")
           or shutil.which("7z") or shutil.which("7za"))
    if _7z:
        rc, err, killed = _run_extractor_cancellable(
            [_7z, "x", archive_path, f"-o{dest_dir}", "-y", _mmt, "-bsp1"],
            cancel, progress_cb=progress_cb, low_priority=_low_prio)
        if killed:
            log_fn("Extraction cancelled (7z terminated).")
            return False
        if rc == 0:
            log_fn("Extracted with 7z.")
            return _ok()
        _note(err)
        log_fn(f"7z failed ({err.strip()}), trying bsdtar…")
    if _cancelled():
        return False
    if shutil.which("bsdtar"):
        rc, err, killed = _run_extractor_cancellable(
            ["bsdtar", "-xf", archive_path, "-C", dest_dir], cancel,
            low_priority=_low_prio)
        if killed:
            log_fn("Extraction cancelled (bsdtar terminated).")
            return False
        if rc == 0 and any(os.scandir(dest_dir)):
            log_fn("Extracted with bsdtar.")
            return _ok()
        _note(err)
        log_fn(f"bsdtar failed ({err.strip()}), trying py7zr…")
    if _cancelled():
        return False
    try:
        import py7zr
        with py7zr.SevenZipFile(archive_path, "r") as z:
            z.extractall(dest_dir)
        log_fn("Extracted with py7zr.")
        return _ok()
    except Exception as exc:
        _note(exc)
        log_fn(f"py7zr failed ({exc}), trying zipfile…")
    if _cancelled():
        return False
    try:
        with zipfile.ZipFile(archive_path, "r") as z:
            infos = z.infolist()
            total = sum(i.file_size for i in infos) or 1
            done = 0
            last_pct = -1
            for info in infos:
                if _cancelled():
                    log_fn("Extraction cancelled (zipfile).")
                    return False
                z.extract(info, dest_dir)
                done += info.file_size
                if progress_cb is not None:
                    pct = min(100, int(done * 100 / total))
                    if pct != last_pct:
                        last_pct = pct
                        progress_cb(pct)
        log_fn("Extracted with zipfile.")
        return _ok()
    except Exception as exc:
        _note(exc)
        log_fn(f"zipfile failed ({exc}).")
    return False


def _run_extractor_cancellable(cmd: list, cancel,
                               progress_cb=None,
                               low_priority=False) -> "tuple[int, str, bool]":
    """Run *cmd* (7z/bsdtar), polling *cancel* so a pause/cancel kills the
    extractor promptly instead of waiting for it to finish. Returns
    ``(returncode, stderr, killed)`` - *killed* is True if we terminated it on a
    cancel request. When *cancel* is None this behaves like a blocking run.

    *progress_cb* - optional ``cb(percent)``; when given, stdout is kept (7z is
    invoked with ``-bsp1``) and scanned for percent updates. 7z redraws its
    progress line in place with backspaces rather than newlines, so the scan is
    a regex over raw chunks, not line reads. Both pipes are drained on
    background threads - waiting for the process first and reading after would
    deadlock once a pipe buffer fills.

    *low_priority* - run the extractor at low CPU and disk priority so it
    yields to foreground applications. CPU niceness is set post-spawn with
    ``os.setpriority`` (a ``preexec_fn`` is unsafe in this heavily-threaded
    process); disk priority via an ``ionice`` prefix when available - ionice
    exec()s the target without forking, so the PID (and thus terminate/renice)
    still reaches the extractor itself."""
    if low_priority and shutil.which("ionice"):
        # best-effort class 2 (lowest level) rather than idle class 3, which
        # can starve the extraction outright under sustained foreground I/O
        cmd = ["ionice", "-c2", "-n7"] + list(cmd)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if progress_cb is not None else subprocess.DEVNULL,
        stderr=subprocess.PIPE)
    if low_priority:
        try:
            os.setpriority(os.PRIO_PROCESS, proc.pid, 19)
        except (OSError, AttributeError):
            pass
    err_parts: "list[bytes]" = []

    def _drain_stderr():
        err_parts.append(proc.stderr.read() or b"")

    def _scan_stdout():
        pct_re = re.compile(rb"(\d{1,3})%")
        last = -1
        tail = b""
        while True:
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            hits = pct_re.findall(tail + chunk)
            # keep a few trailing bytes so a percent split across chunks
            # ("4" | "7%") still matches next round
            tail = chunk[-8:]
            if hits:
                pct = min(100, int(hits[-1]))
                if pct != last:
                    last = pct
                    try:
                        progress_cb(pct)
                    except Exception:
                        pass

    readers = [threading.Thread(target=_drain_stderr, daemon=True)]
    if progress_cb is not None:
        readers.append(threading.Thread(target=_scan_stdout, daemon=True))
    for t in readers:
        t.start()

    killed = False
    while True:
        try:
            proc.wait(timeout=0.25 if cancel is not None else None)
            break
        except subprocess.TimeoutExpired:
            if cancel.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                killed = True
                break
    for t in readers:
        t.join(timeout=5)
    err = b"".join(err_parts).decode("utf-8", "replace")
    return (proc.returncode if proc.returncode is not None else -1, err, killed)


# ---------------------------------------------------------------- root detection
def _single_root_unwrap(extract_dir: Path) -> Path:
    """If the archive extracted into a single wrapper folder (and nothing else),
    descend ONE level - matches Tk's `unwrap_single_folder`. Must NOT loop down a
    chain: e.g. CET ships everything under `bin/x64/`, and descending the whole
    `bin`→`x64` chain would strip the required `bin/` structure (then the mod
    installs to the wrong place / triggers the prefix dialog)."""
    try:
        children = [c for c in extract_dir.iterdir() if c.name not in (".", "..")]
    except OSError:
        return extract_dir
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extract_dir


def _find_fomod_archive(extract_dir: Path) -> Path | None:
    """Return the path to a `.fomod` file inside *extract_dir* if the archive
    is just a wrapper around one (optionally peeling a single top-level
    folder). A `.fomod` is itself a renamed 7z/zip - Nexus packages older
    Fallout/Oblivion mods this way and they need a second extraction pass
    before FOMOD detection can find `fomod/ModuleConfig.xml` (Tk parity)."""
    root = _single_root_unwrap(extract_dir)
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    fomods = [p for p in entries if p.is_file() and p.suffix.lower() == ".fomod"]
    if len(fomods) == 1:
        siblings = [p for p in entries if p != fomods[0]]
        if all(s.is_file() and s.suffix.lower() in
               (".txt", ".md", ".rtf", ".jpg", ".png", ".pdf") for s in siblings):
            return fomods[0]
    return None


# ------------------------------------------------------------ prepare / finish
class PreparedInstall:
    """An extracted-but-not-yet-staged archive. `extract_dir` lives until
    `cleanup()` (so an interactive FOMOD wizard can read images + the config and
    then call `finish_install`). When `fomod_base`/`fomod_config` are set the
    caller should run the wizard; otherwise it's a plain install."""
    def __init__(self, archive: Path, game, profile_dir: Path, mod_name: str,
                 extract_dir: Path, src_root: Path,
                 fomod_base: Path | None, fomod_config, prebuilt_meta=None,
                 on_need_prefix=None):
        self.archive = archive
        self.game = game
        self.profile_dir = profile_dir
        self.mod_name = mod_name
        self.extract_dir = extract_dir
        self.src_root = src_root
        self.fomod_base = fomod_base
        self.fomod_config = fomod_config
        # Path to the archive's fomod/ModuleConfig.xml while the extraction is
        # still around. Mirrored into the profile next to the saved selections
        # so exporting a collection can rebuild the installer's step/option
        # names later without needing the original archive.
        self.fomod_config_path: "str | None" = None
        # Optional NexusModMeta supplied by the caller (e.g. the Nexus browser,
        # which knows the real mod_id/file_id) - written verbatim instead of
        # parsing the (sometimes wrong) archive filename.
        self.prebuilt_meta = prebuilt_meta
        # Optional callback on_need_prefix(required, file_list, mod_name) -> str|None
        # invoked when a non-FOMOD mod's structure doesn't match the game and
        # auto-strip fails - the toolkit shows the Set-Prefix dialog. None = the
        # neutral default (install as-is).
        self.on_need_prefix = on_need_prefix
        # BAIN complex-package detection (mutually exclusive with FOMOD - a
        # non-FOMOD archive is probed for a BAIN sub-package layout at prepare
        # time so the caller can show the picker before finish_install stages it).
        # bain_subpkgs is a list[BainSubPackage] (or None); bain_root is the
        # single-folder-unwrapped extract dir the sub-package paths are relative
        # to. readme_text / saved_bain_selections feed the picker.
        self.bain_subpkgs = None
        self.bain_root = None
        self.readme_text = None
        self.saved_bain_selections = None
        # FOMOD wizard context (set at prepare time when a FOMOD is detected):
        # saved selections from the previous install of this mod (global config
        # JSON, restores + green-highlights prior choices - Tk parity) and the
        # (installed, active, loose) file sets its conditions evaluate against.
        self.saved_fomod_selections = None
        self.fomod_context = (set(), set(), set())
        # RE / Fluffy bundle detection (games with mod_supports_bundles, probed
        # at prepare time when the archive is neither FOMOD nor BAIN): the
        # grouped BundleLayout + the single-folder-unwrapped dir its variant
        # paths live under. multi_mods is the sibling shape - every top-level
        # folder has a modinfo.ini but there's no bundle grouping, so each
        # folder installs as its own independent mod.
        self.bundle_layout = None
        self.bundle_root = None
        self.multi_mods = None
        # Set by finish_install when the user chose to Replace an existing mod:
        # keep its modlist position + carry its endorsed flag and per-requirement
        # ignore list onto the new install.
        self._preserve_position = False
        self._preserved_endorsed = False
        self._preserved_ignored_reqs = ""
        # Bytes claimed from the shared /tmp reservation pool while extract_dir
        # lives there (0 when extracted to disk) - released by cleanup().
        self._tmp_reserved = 0

    def is_fomod(self) -> bool:
        return self.fomod_base is not None and self.fomod_config is not None

    def fomod_has_steps(self) -> bool:
        """True when the FOMOD config has at least one install step (i.e. the
        wizard has something to show). A config with only requiredInstallFiles /
        conditionalFileInstalls and no <installSteps> installs headlessly - the
        wizard would open on an empty step list and crash."""
        return self.is_fomod() and bool(
            getattr(self.fomod_config, "steps", None))

    def is_bain(self) -> bool:
        return bool(self.bain_subpkgs)

    def is_bundle(self) -> bool:
        return self.bundle_layout is not None

    def is_multi_mod(self) -> bool:
        return bool(self.multi_mods)

    def cleanup(self):
        shutil.rmtree(self.extract_dir, ignore_errors=True)
        _release_tmp_reservation(self._tmp_reserved)
        self._tmp_reserved = 0


def prepare_archive(archive_path: str, game, profile_dir: Path, *,
                    log_fn: LogFn, progress_fn: Optional[ProgressFn] = None,
                    preferred_name: str = "", prebuilt_meta=None,
                    on_need_prefix=None, cancel=None) -> PreparedInstall | None:
    """Extract *archive_path* to a kept temp dir and detect FOMOD. The caller
    either runs the wizard (is_fomod) then `finish_install(prepared, selections)`,
    or just calls `finish_install(prepared, None)` for a plain/default install.
    Returns None on failure (and cleans up).

    *cancel* - optional ``threading.Event``; when set the extraction is aborted
    and the partial temp dir removed (returns None)."""
    archive = Path(archive_path)
    if not archive.is_file():
        log_fn(f"Install: archive not found: {archive_path}")
        return None
    # Resolve staging from the TARGET profile dir, not the game's mutable
    # active-profile state: installs into a non-active profile (Profile Group
    # member picker) land in that profile's own mods folder.
    from Utils.mod_copy import resolve_target_staging
    staging_root = resolve_target_staging(game, profile_dir)
    if staging_root is None:
        log_fn("Install: no staging folder configured.")
        return None
    # Name the mod folder. Priority:
    #   1. preferred_name  - the caller forces it (Nexus browser, collections).
    #   2. prebuilt_meta   - the caller already resolved the Nexus file → use its
    #                        per-file display name if it has one.
    #   3. Nexus MD5/filename lookup - when logged in, name the folder after the
    #                        file's Nexus display name (e.g. "Engine Fixes - Main
    #                        File") and reuse the resolved meta as prebuilt_meta so
    #                        finish_install doesn't look it up a second time.
    #   4. _clean_mod_name - strip the archive stem (offline / not on Nexus).
    if preferred_name:
        mod_name = preferred_name
    else:
        # Thunderstore archives are named "{team}-{Package}-{version}.zip", so
        # the archive stem makes an ugly folder ("Subnautica_Modding-QModManager
        # -4.4.3"). The package's own name IS its display name, so prefer it.
        # This is only a DEFAULT - the Mod-Already-Exists dialog still applies,
        # unlike preferred_name which forces a silent replace.
        ts_name = _thunderstore_display_name(archive)
        nexus_name = ts_name or _nexus_file_display_name(prebuilt_meta, game)
        if not nexus_name:
            # Not supplied by the caller - try a live lookup ourselves. Reuse the
            # resolved meta downstream so the MD5 hash isn't recomputed later.
            resolved = _resolve_nexus_meta_for_naming(archive, game, log_fn)
            if resolved is not None:
                if prebuilt_meta is None:
                    prebuilt_meta = resolved
                nexus_name = _nexus_file_display_name(resolved, game)
        mod_name = nexus_name or _clean_mod_name(archive.stem, game)
        if ts_name:
            log_fn(f"Naming mod folder from Thunderstore: '{mod_name}'.")
        elif nexus_name:
            log_fn(f"Naming mod folder from Nexus: '{mod_name}'.")

    def _p(done, total, phase=None):
        if progress_fn is not None:
            progress_fn(done, total, phase)

    _p(0, 0, "Extracting")

    def _extract_progress(pct: int) -> None:
        # Real percent from 7z/zipfile; the fallback extractors never call this,
        # so the (0, 0) indeterminate bar above stays for them.
        _p(pct, 100, "Extracting")

    # Pick a temp parent big enough - /tmp is a RAM-backed tmpfs on the Deck, so
    # a large mod must extract to the staging disk instead (Tk parity).
    extracted, extract_dir, tmp_reserved, extract_errors = \
        _extract_with_disk_retry(str(archive), Path(staging_root), log_fn,
                                 cancel=cancel, progress_cb=_extract_progress)
    if not extracted:
        if cancel is not None and cancel.is_set():
            log_fn("Install: extraction cancelled - removing temp files.")
        elif any(_is_truncated_archive_error(e) for e in extract_errors):
            try:
                size_gb = archive.stat().st_size / (1024 ** 3)
                got = f" (file on disk is {size_gb:.2f} GB)"
            except OSError:
                got = ""
            log_fn(f"Install failed: the archive appears incomplete or "
                   f"truncated{got} - the download was likely interrupted. "
                   f"Re-download {archive.name} and try again.")
        else:
            log_fn("Install failed: could not extract the archive.")
        shutil.rmtree(extract_dir, ignore_errors=True)
        _release_tmp_reservation(tmp_reserved)
        return None

    # A `.fomod`-wrapper archive needs a second extraction pass before FOMOD
    # detection can find fomod/ModuleConfig.xml (Tk parity).
    fomod_wrapper = _find_fomod_archive(extract_dir)
    if fomod_wrapper is not None:
        log_fn(f"Archive contains a .fomod wrapper - extracting {fomod_wrapper.name}…")
        _p(0, 0, "Extracting")   # back to indeterminate for the second pass
        # The wrapper doubles peak usage (outer + inner trees exist at once),
        # so the inner pass gets its own probe/reservation and disk-full retry
        # instead of blindly landing in the outer dir's parent.
        inner_ok, inner_dir, inner_reserved, _inner_errors = \
            _extract_with_disk_retry(str(fomod_wrapper), Path(staging_root),
                                     log_fn, cancel=cancel,
                                     progress_cb=_extract_progress)
        if inner_ok:
            shutil.rmtree(extract_dir, ignore_errors=True)
            _release_tmp_reservation(tmp_reserved)
            extract_dir = inner_dir
            tmp_reserved = inner_reserved
        else:
            if cancel is not None and cancel.is_set():
                log_fn("Install: extraction cancelled - removing temp files.")
            else:
                log_fn("Install failed: could not extract the inner .fomod archive.")
            shutil.rmtree(inner_dir, ignore_errors=True)
            shutil.rmtree(extract_dir, ignore_errors=True)
            _release_tmp_reservation(tmp_reserved)
            _release_tmp_reservation(inner_reserved)
            return None

    src_root = _single_root_unwrap(extract_dir)
    # Shared Tk detection: case-insensitive fomod/moduleconfig.xml lookup with
    # wrapper-peel + bounded BFS. (Rebuilding the config path with a lowercase
    # "fomod" broke uppercase FOMOD/ dirs on case-sensitive filesystems and
    # misrouted such archives to the BAIN probe.)
    from Utils.fomod_parser import (detect_fomod, detect_scripted_fomod,
                                    parse_module_config)
    fomod_base: Path | None = None
    config = None
    fomod_result = detect_fomod(str(extract_dir))
    if fomod_result is not None:
        mod_root, config_path = fomod_result
        try:
            config = parse_module_config(config_path)
            fomod_base = Path(mod_root)
        except Exception as exc:
            log_fn(f"FOMOD parse failed ({exc}); will install verbatim.")
    elif detect_scripted_fomod(str(extract_dir)):
        log_fn("WARNING: This is a scripted (C#) FOMOD installer, which this "
               "manager cannot run. All files will be installed without "
               "showing any install options. If you need to choose optional "
               "components, look for an XML-based or manually-packaged "
               "version of this mod on Nexus.")
    prepared = PreparedInstall(archive, game, profile_dir, mod_name,
                               extract_dir, src_root, fomod_base, config,
                               prebuilt_meta=prebuilt_meta,
                               on_need_prefix=on_need_prefix)
    prepared._tmp_reserved = tmp_reserved
    if fomod_result is not None and config is not None:
        prepared.fomod_config_path = fomod_result[1]
    if fomod_base is not None:
        prepared.saved_fomod_selections = _read_saved_fomod_selections(
            game, mod_name, log_fn, profile_dir=profile_dir)
        try:
            prepared.fomod_context = _collection_plugin_context(game, profile_dir)
        except Exception:
            pass

    # BAIN is mutually exclusive with FOMOD (Tk parity): only probe an archive
    # with no FOMOD installer at all (a detected-but-unparseable FOMOD installs
    # verbatim, it must not fall into the BAIN picker). Detect a complex BAIN
    # layout so the caller can show the picker.
    if fomod_result is None and getattr(game, "supports_bain", True):
        try:
            from Utils.bain_installer import detect_bain, bain_unwrap_single_folder
            bain_root = bain_unwrap_single_folder(str(extract_dir))
            subpkgs = detect_bain(
                bain_root, extra_exts=getattr(game, "plugin_extensions", None))
        except Exception as exc:
            log_fn(f"BAIN detection failed ({exc}); will install verbatim.")
            subpkgs = None
            bain_root = str(extract_dir)
        if subpkgs:
            # Fill the per-package file sets here (worker thread) - the Qt
            # picker's win/lose recolour needs them and must not walk the
            # disk on the GUI thread.
            from Utils.bain_installer import scan_subpackage_files
            scan_subpackage_files(subpkgs)
            prepared.bain_subpkgs = subpkgs
            prepared.bain_root = bain_root
            prepared.readme_text = _read_bain_readme(bain_root)
            prepared.saved_bain_selections = _read_saved_bain_selections(
                game, mod_name, log_fn, profile_dir=profile_dir)
            log_fn(f"BAIN package detected - {len(subpkgs)} sub-package(s).")

    # RE / Fluffy bundle & multi-mod probe (Tk parity: gui/install_mod.py chain
    # FOMOD → BAIN → bundle → multi-mod → plain). Only for games that opt in via
    # mod_supports_bundles; the two shapes are mutually exclusive by definition
    # (detect_multi_mod rules itself out when any modinfo.ini carries
    # nameasbundle/AddonFor).
    if (fomod_result is None and not prepared.bain_subpkgs
            and getattr(game, "mod_supports_bundles", False)):
        try:
            from Utils.re_bundle import detect_re_bundle, detect_multi_mod
            bundle_root = unwrap_single_folder(str(extract_dir))
            layout = detect_re_bundle(bundle_root)
            if layout is not None:
                prepared.bundle_layout = layout
                prepared.bundle_root = bundle_root
                log_fn(f"Bundle detected: '{layout.bundle_name}' - "
                       f"{len(layout.groups)} group(s), "
                       f"{layout.variant_count} option(s).")
            else:
                multi = detect_multi_mod(bundle_root)
                if multi:
                    prepared.multi_mods = multi
                    prepared.bundle_root = bundle_root
                    log_fn(f"Multi-mod archive detected: {len(multi)} mod(s).")
        except Exception as exc:
            log_fn(f"Bundle detection failed ({exc}); will install verbatim.")
    return prepared


def _run_additional_install_logic(game, dest_root: Path, mod_name: str,
                                  log_fn: LogFn, *, interactive: bool,
                                  cleanup_on_cancel: bool) -> bool:
    """Run the game's post-install hooks; returns True on user cancel.

    Ported from the Tk installer (gui/install_mod.py) - the hooks (e.g. DAO's
    normalize_dao_mod) restructure the staged tree and MUST run before the mod
    is indexed. ``interactive`` is False for headless/collection installs so
    hooks that pop dialogs stay silent in those modes.
    """
    import inspect
    for fn in getattr(game, "additional_install_logic", None) or []:
        try:
            if "interactive" in inspect.signature(fn).parameters:
                fn(dest_root, mod_name, log_fn, interactive=interactive)
            else:
                fn(dest_root, mod_name, log_fn)
        except Exception as e:
            # A wizard hook may signal a user cancellation (marker attr) -
            # abort the whole install and clean up the staged folder rather
            # than leaving a half-installed mod in the modlist.
            if getattr(e, "install_cancelled", False):
                log_fn(f"Install cancelled by user - removing '{mod_name}'.")
                if cleanup_on_cancel:
                    def _force_remove(func, path, _exc):
                        os.chmod(path, 0o700)
                        func(path)
                    try:
                        shutil.rmtree(dest_root, onexc=_force_remove)
                    except OSError as rm_exc:
                        log_fn(f"  cleanup failed: {rm_exc}")
                return True
            log_fn(f"Additional install logic failed: {e}")
    return False


def wrap_flat_mod_dir(mod_dir: Path, signal_names: "set[str]",
                      signal_exts: "set[str]",
                      structured_markers: "set[str]") -> bool:
    """Wrap a flat mod folder's files one level deeper; True if restructured.

    Some games (e.g. Stardew/SMAPI ``Mods/<Name>/manifest.json``, JA3
    ``<Name>/metadata.lua``) require mods to live inside a named subfolder. A
    common mistake is copying the mod's CONTENTS into staging, so deploy puts
    the files straight into Mods/ instead of Mods/<Name>/. A marker file at
    the staging-folder root (name in *signal_names* or extension in
    *signal_exts*, both lowercase) is the definitive flat signal; a marker in
    *structured_markers* inside any immediate subdirectory means the mod is
    already correctly structured and must not be touched (a loose root file
    alongside it is part of a multi-destination mod).
    """
    from Utils.filemap import _EXCLUDE_NAMES
    if not mod_dir.is_dir():
        return False
    children = list(mod_dir.iterdir())
    if not children:
        return False
    if structured_markers and any(
        sub.is_dir() and any(
            f.is_file() and f.name.lower() in structured_markers
            for f in sub.iterdir()
        )
        for sub in children
    ):
        return False
    has_signal = any(
        c.is_file()
        and (c.name.lower() in signal_names or c.suffix.lower() in signal_exts)
        for c in children
    )
    if not has_signal:
        return False
    # Move everything (files and subdirs) into a new subfolder named after
    # the staging folder so the mod loader finds <ModName>/<marker>. The
    # manager's own metadata (meta.ini etc.) must stay at the staging root,
    # or the mod can no longer be matched to its meta.ini after wrapping.
    sub = mod_dir / mod_dir.name
    sub.mkdir(exist_ok=True)
    for child in children:
        if child.is_file() and child.name.lower() in _EXCLUDE_NAMES:
            continue
        shutil.move(str(child), str(sub / child.name))
    return True


def _game_wrap_signals(game) -> "tuple[set[str], set[str], set[str]]":
    """Lowercased (names, extensions, structured-markers) wrap signals."""
    names, exts = getattr(game, "mod_staging_wrap_signals",
                          ({"manifest.json"}, set()))
    guard = getattr(game, "mod_staging_already_structured_markers", set())
    return ({n.lower() for n in (names or {"manifest.json"})},
            {e.lower() for e in (exts or set())},
            {g.lower() for g in (guard or set())})


def _wrap_flat_if_needed(game, dest_root: Path, log_fn: LogFn) -> None:
    """Wrap a just-installed flat mod for games that require a subfolder."""
    if not getattr(game, "mod_staging_requires_subdir", False):
        return
    try:
        names, exts, guard = _game_wrap_signals(game)
        if wrap_flat_mod_dir(dest_root, names, exts, guard):
            log_fn(f"Auto-fixed flat staging structure: wrapped files into "
                   f"'{dest_root.name}/{dest_root.name}/'.")
    except OSError as exc:
        log_fn(f"Flat-staging wrap failed for '{dest_root.name}': {exc}")


def fix_flat_staging_folders(
    staging_root: Path,
    signal_filenames: "set[str] | None" = None,
    signal_extensions: "set[str] | None" = None,
    already_structured_markers: "set[str] | None" = None,
) -> list[str]:
    """Wrap every flat mod staging folder; returns the names restructured.

    Whole-staging heal used by the Refresh/rebuild path so manually-copied
    mods are fixed too, not just fresh installs (Tk parity: this ran at the
    start of every full filemap rebuild for games with
    ``mod_staging_requires_subdir``).
    """
    names = {n.lower() for n in (signal_filenames or {"manifest.json"})}
    exts = {e.lower() for e in (signal_extensions or set())}
    guard = {n.lower() for n in (already_structured_markers or set())}
    fixed: list[str] = []
    if not staging_root.is_dir():
        return fixed
    for mod_dir in staging_root.iterdir():
        # Never restructure through a symlinked mod dir (Profile Group link
        # farm) - the member profile heals its own real folder.
        if mod_dir.is_symlink():
            continue
        if mod_dir.is_dir() and wrap_flat_mod_dir(mod_dir, names, exts, guard):
            fixed.append(mod_dir.name)
    return fixed


def finish_install(prepared: "PreparedInstall", fomod_selections, *,
                   log_fn: LogFn, progress_fn: Optional[ProgressFn] = None,
                   on_exists=None, bain_selections=None,
                   interactive: bool = True) -> str | None:
    """Stage the prepared archive. *fomod_selections* is the wizard's
    {step_idx: {group: [plugins]}} dict (or None → FOMOD defaults / plain copy).
    *bain_selections* is the BAIN picker's ``{"selected": [name, ...]}`` dict for
    a BAIN package (or None → its default "00"-prefixed sub-packages).
    Always cleans up the extract dir. Returns the installed mod name (None on
    cancel).

    *on_exists* - optional callback invoked when the destination mod folder
    already exists: ``on_exists(mod_name, conflict, prepared) -> str`` returning
    one of
    ``"replace"`` (wipe + reinstall, keep modlist position + endorsed flag),
    ``"rename:<newname>"`` (install as a NEW mod), or ``"cancel"``. *conflict* is
    True on a re-prompt when a chosen rename target is itself taken. When
    *on_exists* is None the existing folder is silently replaced (collection /
    quick-update path)."""
    p = prepared
    from Utils.mod_copy import resolve_target_staging
    staging_root = Path(resolve_target_staging(p.game, p.profile_dir))
    staging_root.mkdir(parents=True, exist_ok=True)
    dest_root = staging_root / p.mod_name

    def _pp(done, total, phase=None):
        if progress_fn is not None:
            progress_fn(done, total, phase)

    # Multi-mod archives install several independent mods - route them to their
    # own loop (the single-mod exists-prompt below is keyed on the archive name,
    # which never becomes a mod folder for this shape).
    if p.is_multi_mod():
        return _install_multi_mod(p, log_fn, _pp)

    # Reinstall/update of an RE bundle: carry the user's saved option selection
    # (and order) onto the freshly detected spec before the folder is wiped -
    # captured only on replace, a rename installs as a NEW mod (Tk parity).
    old_bundle_spec = None

    # Existing same-named folder: ask the caller what to do (replace / rename /
    # cancel), or - with no callback - silently replace (collection installs).
    if dest_root.exists():
        if on_exists is None:
            # Silent replace is still a replace: keep the mod's modlist slot and
            # carry its endorsed flag + per-requirement ignore list, exactly like
            # the interactive Replace path (Tk parity: overwrote_existing →
            # was_existing_mod → ensure_mod_preserving_position).
            try:
                from Nexus.nexus_meta import read_meta
                _old = read_meta(dest_root / "meta.ini")
                p._preserved_endorsed = bool(_old.endorsed)
                p._preserved_ignored_reqs = \
                    getattr(_old, "ignored_requirements", "") or ""
            except Exception:
                p._preserved_endorsed = False
            if p.is_bundle():
                old_bundle_spec = _read_old_bundle_spec(dest_root)
            log_fn(f"Replacing existing mod folder: {p.mod_name}")
            shutil.rmtree(dest_root, ignore_errors=True)
            p._preserve_position = True
        else:
            conflict = False
            while dest_root.exists():
                # *p* is passed so the caller can offer naming suggestions
                # (archive filename / Nexus names) in its rename field.
                action = on_exists(p.mod_name, conflict, p)
                if action == "cancel" or not action:
                    log_fn(f"Install cancelled - '{p.mod_name}' already exists.")
                    p.cleanup()
                    return None
                if action == "replace":
                    # Carry the old install's endorsed flag + per-requirement
                    # ignore list onto the new one.
                    try:
                        from Nexus.nexus_meta import read_meta
                        _old = read_meta(dest_root / "meta.ini")
                        p._preserved_endorsed = bool(_old.endorsed)
                        p._preserved_ignored_reqs = \
                            getattr(_old, "ignored_requirements", "") or ""
                    except Exception:
                        p._preserved_endorsed = False
                    if p.is_bundle():
                        old_bundle_spec = _read_old_bundle_spec(dest_root)
                    log_fn(f"Replacing existing mod folder: {p.mod_name}")
                    shutil.rmtree(dest_root, ignore_errors=True)
                    p._preserve_position = True
                    break
                if action.startswith("rename:"):
                    new_name = action.split(":", 1)[1].strip()
                    if not new_name or new_name == p.mod_name:
                        conflict = True
                        continue
                    p.mod_name = new_name
                    dest_root = staging_root / p.mod_name
                    # Loop: if the new name is ALSO taken, re-prompt (conflict).
                    conflict = dest_root.exists()
                    # rename installs as a NEW mod (no position preserve).
                    continue
                # Unknown action → treat as cancel (safe default).
                p.cleanup()
                return None

    cancelled = False
    bain_selected: "list[str] | None" = None
    try:
        if p.is_fomod():
            # Persist the wizard's choices (restored + highlighted next time).
            # None = headless defaults install → nothing to remember (Tk parity).
            if fomod_selections is not None:
                _persist_fomod_selection(p.game, p.mod_name, fomod_selections,
                                         profile_dir=p.profile_dir)
                _write_profile_fomod_config(p.game, p.mod_name,
                                            p.fomod_config_path,
                                            p.profile_dir)
            ok = _install_fomod(p.fomod_base, p.fomod_config, dest_root,
                                fomod_selections, log_fn, _pp,
                                context=p.fomod_context, game=p.game)
            if not ok:
                log_fn("FOMOD resolve failed - installing all files verbatim.")
                _copy_tree(p.src_root, dest_root, log_fn, _pp)
        elif p.is_bain():
            # BAIN: merge the selected sub-packages (later ones override earlier),
            # with paths relative to the unwrapped bain_root - mirroring the
            # collection install path.
            from Utils.bain_installer import resolve_bain_files
            if bain_selections is not None and isinstance(
                    bain_selections.get("selected"), list):
                bain_selected = list(bain_selections["selected"])
            else:
                bain_selected = [pkg.name for pkg in p.bain_subpkgs
                                 if pkg.default_selected]
                log_fn("BAIN: using default sub-package selection.")
            file_list = resolve_bain_files(p.bain_subpkgs, set(bain_selected))
            log_fn(f"BAIN: {len(bain_selected)} sub-package(s), "
                   f"{len(file_list)} file(s) to install.")
            dest_root.mkdir(parents=True, exist_ok=True)
            _copy_file_list(file_list, p.bain_root, dest_root, log_fn, game=p.game)
        elif p.is_bundle():
            # RE / Fluffy bundle: installs as ONE normal mod. The original
            # option folders are tucked into a hidden <mod>/.mm_bundle/ library
            # (skipped by the file scanner); the selected options are
            # materialised (hardlinked) onto the mod root. The structure +
            # selection live in meta.ini's [Bundle] section; the Bundle Options
            # tab re-materialises on change. Downstream (scan/filemap/deploy/
            # update) sees a normal mod.
            from Utils.re_bundle import (layout_to_spec, merge_bundle_spec,
                                         write_bundle_spec,
                                         materialize_selection, BUNDLE_LIB_DIR)
            layout = p.bundle_layout
            spec = layout_to_spec(layout)
            log_fn(f"Installing bundle '{layout.bundle_name}' as one mod "
                   f"'{p.mod_name}'.")
            if old_bundle_spec is not None:
                spec = merge_bundle_spec(spec, old_bundle_spec)
                log_fn("Bundle: preserved existing option selection across "
                       "reinstall/update.")
            # Stash every extracted top-level folder under <mod>/.mm_bundle/.
            lib_dir = dest_root / BUNDLE_LIB_DIR
            lib_dir.mkdir(parents=True, exist_ok=True)
            for child in sorted(Path(p.bundle_root).iterdir()):
                if child.is_dir():
                    _copy_file_list(resolve_direct_files(str(child)),
                                    str(child), lib_dir / child.name, log_fn,
                                    game=p.game)
            # Persist the spec + materialise the selection. _write_install_meta
            # below preserves the [Bundle] section (write_meta keeps foreign
            # sections intact).
            write_bundle_spec(dest_root / "meta.ini", spec)
            materialize_selection(dest_root, spec)
            log_fn(f"Bundle: {len(spec.selected_folders())} of "
                   f"{layout.variant_count} option(s) active.")
        else:
            # Non-FOMOD: build + normalise the file list to the game's expected
            # structure (strip/required-top-level/auto-strip/prefix), EXACTLY like
            # the Tk installer, then copy. This is what makes e.g. CET land under
            # bin/x64 instead of installing verbatim. is_root_install mirrors Tk:
            # it's THIS mod's root_folder meta flag (default False), NOT a game flag.
            is_root = bool(getattr(p.prebuilt_meta, "root_folder", False)
                           if p.prebuilt_meta is not None else False)
            # Stage from the RAW extract dir (like Tk's direct-install path uses
            # `extract_dir`), NOT the single-folder-unwrapped src_root: e.g. CET
            # ships everything under bin/x64/, and staging from an unwrapped root
            # would have stripped the required bin/ folder.
            stage_root = str(p.extract_dir)
            file_list = stage_file_list(
                p.game, stage_root, is_root_install=is_root,
                mod_name=p.mod_name, on_need_prefix=p.on_need_prefix, log_fn=log_fn)
            if file_list is None:
                cancelled = True
            else:
                dest_root.mkdir(parents=True, exist_ok=True)
                _copy_file_list(file_list, stage_root, dest_root, log_fn, game=p.game)
    finally:
        p.cleanup()

    if cancelled:
        shutil.rmtree(dest_root, ignore_errors=True)
        return None

    if not dest_root.is_dir() or not any(dest_root.iterdir()):
        log_fn(f"Install failed: nothing was staged for '{p.mod_name}'.")
        try:
            dest_root.rmdir()
        except OSError:
            pass
        return None

    # For FOMOD installs, record the fileDependency plugins on options the user
    # did NOT select - the modlist flags a rerun if any of them appears in the
    # load order later. Computed against {} for headless-defaults installs
    # (fomod_selections is None), where no options are explicitly selected.
    fomod_pending_deps = ""
    fomod_active_deps = ""
    if p.is_fomod() and p.fomod_config is not None:
        try:
            from Utils.fomod_installer import (
                collect_unselected_dep_plugins, collect_selected_dep_plugins)
            sel = fomod_selections or {}
            fomod_pending_deps = ";".join(
                collect_unselected_dep_plugins(p.fomod_config, sel))
            fomod_active_deps = ";".join(
                collect_selected_dep_plugins(p.fomod_config, sel))
        except Exception as exc:
            log_fn(f"FOMOD dep scan skipped ({exc}).")

    _write_install_meta(dest_root, p.archive, p.game, log_fn,
                        prebuilt_meta=getattr(p, "prebuilt_meta", None),
                        endorsed=getattr(p, "_preserved_endorsed", False),
                        ignored_reqs=getattr(p, "_preserved_ignored_reqs", ""),
                        is_fomod=p.is_fomod(),
                        is_bain=bain_selected is not None,
                        fomod_pending_deps=fomod_pending_deps,
                        fomod_active_deps=fomod_active_deps)
    # Persist the BAIN sub-package selection (global + profile) so a re-install
    # restores the user's choices (Tk parity).
    if bain_selected is not None:
        _persist_bain_selection(p.game, p.mod_name, {"selected": bain_selected},
                                profile_dir=p.profile_dir)
    # Game post-install hooks (e.g. DAO's packages/core/override restructure)
    # and the flat-staging wrap both reshape dest_root, so they run BEFORE the
    # index/modlist commit below sees the layout.
    if _run_additional_install_logic(
            p.game, dest_root, p.mod_name, log_fn, interactive=interactive,
            cleanup_on_cancel=not getattr(p, "_preserve_position", False)):
        return None
    _wrap_flat_if_needed(p.game, dest_root, log_fn)
    _pp(0, 0, "Indexing")
    with _commit_lock:
        _update_indexes(p.game, p.profile_dir, p.mod_name, dest_root, log_fn)
        _add_to_modlist(p.profile_dir, p.mod_name, log_fn,
                        preserve_position=getattr(p, "_preserve_position", False))
        _add_plugins(p.game, p.profile_dir, dest_root, log_fn)
    # Stamp missing-requirements + endorsed flags now (one GraphQL call, no
    # rate-limit cost) so the modlist flags appear immediately without pressing
    # "Check updates".
    _check_nexus_flags_after_install(p.game, p.mod_name, log_fn,
                                     profile_dir=p.profile_dir)
    log_fn(f"Installed '{p.mod_name}'.")
    return p.mod_name


def install_archive(archive_path: str, game, profile_dir: Path, *,
                    log_fn: LogFn, progress_fn: Optional[ProgressFn] = None,
                    preferred_name: str = "") -> str | None:
    """Non-interactive install (FOMOD → default selections). Convenience wrapper
    over prepare_archive + finish_install."""
    prepared = prepare_archive(archive_path, game, profile_dir, log_fn=log_fn,
                               progress_fn=progress_fn, preferred_name=preferred_name)
    if prepared is None:
        return None
    return finish_install(prepared, None, log_fn=log_fn, progress_fn=progress_fn,
                          interactive=False)


# ---------------------------------------------------------- collection installs
def _collection_plugin_context(game, profile_dir: "Path | None"
                               ) -> "tuple[set[str], set[str], set[str]]":
    """Build the (installed_files, active_files, loose_files) sets a collection
    FOMOD needs to evaluate its conditions - a tkinter-free port of the set-up
    block in ``gui/install_mod.py`` (~1747-1794). Reads plugins.txt/loadorder.txt/
    filemap.txt next to the profile, then seeds vanilla/DLC plugins (loaded
    implicitly by the engine, so never in plugins.txt)."""
    installed_files: set[str] = set()
    active_files: set[str] = set()
    loose_files: set[str] = set()
    if profile_dir is not None:
        try:
            from Utils.plugins import read_plugins, read_loadorder
            # Respect the game's plugins.txt convention: star-prefix games mark
            # enabled plugins with a leading "*" (bare = disabled); no-star games
            # list only enabled plugins (all listed = active). Using the default
            # star_prefix=True on a no-star game would mark EVERY plugin disabled,
            # leaving active_files empty so FOMOD `state="Active"` deps never match
            # → conditional patch options never auto-select.
            star = bool(getattr(game, "plugins_use_star_prefix", True))
            for entry in read_plugins(profile_dir / "plugins.txt", star_prefix=star):
                installed_files.add(entry.name.lower())
                if entry.enabled:
                    active_files.add(entry.name.lower())
            for name in read_loadorder(profile_dir / "loadorder.txt"):
                installed_files.add(name.lower())
        except Exception:
            pass
        # <fileDependency> nodes can reference arbitrary asset paths; MO2 checks
        # the whole virtual tree, so mirror the filemap's relative paths.
        try:
            with open(profile_dir / "filemap.txt", "r", encoding="utf-8") as fmf:
                for line in fmf:
                    rel = line.split("\t", 1)[0].strip()
                    if rel:
                        loose_files.add(rel.replace("\\", "/").lower())
        except OSError:
            pass
    if game is not None:
        try:
            from Utils.game_helpers import _vanilla_plugins_for_game
            for vname_lower in _vanilla_plugins_for_game(game).keys():
                installed_files.add(vname_lower)
                active_files.add(vname_lower)
        except Exception:
            pass
    return installed_files, active_files, loose_files


def _archive_lists_fomod_config(archive_path: str) -> bool:
    """Best-effort probe: True when the archive LISTING (no extraction) shows a
    fomod/ModuleConfig.xml. Lets a collection install defer an interactive FOMOD
    immediately instead of paying a full extract that is thrown away and redone
    in the deferred phase. Conservative on any doubt: unreadable listings and
    ``.fomod`` wrapper archives (installer nested inside an inner archive)
    return False and the normal extract-then-detect path decides. A listed but
    unparseable ModuleConfig.xml means the mod defers and installs verbatim in
    the deferred phase instead of verbatim immediately - same outcome, later."""
    target = "fomod/moduleconfig.xml"

    def _hit(name: str) -> bool:
        # Backslash-zip members (Windows Compress-Archive) use literal "\".
        n = name.replace("\\", "/").lower()
        return n == target or n.endswith("/" + target)

    if archive_path.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                return any(_hit(n) for n in zf.namelist())
        except Exception:
            return False
    _7z = (shutil.which("7zzs") or shutil.which("7zz")
           or shutil.which("7z") or shutil.which("7za"))
    if _7z:
        try:
            res = subprocess.run(
                [_7z, "l", "-slt", archive_path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=30)
            if res.returncode != 0:
                return False
            for line in res.stdout.splitlines():
                if line.startswith("Path = ") and _hit(line[7:].strip()):
                    return True
        except Exception:
            pass
        return False
    if archive_path.lower().endswith(".7z"):
        # No 7z binary (extraction falls back to bsdtar/py7zr) - list via py7zr.
        try:
            import py7zr
            with py7zr.SevenZipFile(archive_path, "r") as z:
                return any(_hit(n) for n in z.getnames())
        except Exception:
            return False
    return False


def install_collection_archive(
        archive_path: str, game, profile_dir: Path, *,
        log_fn: LogFn, progress_fn: Optional[ProgressFn] = None,
        preferred_name: str = "",
        prebuilt_meta=None,
        fomod_auto_selections: "dict | None" = None,
        bain_auto_selections: "dict | None" = None,
        overwrite_existing: "bool | None" = None,
        skip_index_update: bool = True,
        defer_interactive_fomod: bool = False,
        defer_interactive_bain: bool = False,
        resolve_fomod=None,
        resolve_bain=None,
        on_installed=None,
        cancel=None) -> "str | None":
    """Install ONE collection mod from a downloaded archive - the tkinter-free
    equivalent of ``gui/install_mod.py:install_mod_from_archive`` for the paths a
    collection install exercises (FOMOD with author selections or deferred, BAIN,
    dinput/root_folder, plain). Reuses the shared neutral staging/meta/modlist
    helpers in this module.

    Returns the installed folder name, or the ``FOMOD_DEFERRED`` / ``BAIN_DEFERRED``
    sentinel when an interactive installer has no author selections and
    ``defer_interactive_*`` is set (the orchestrator processes those at the end,
    one-by-one, passing ``resolve_fomod`` / ``resolve_bain`` to show the wizard),
    or ``None`` on failure / user-cancel.

    resolve_fomod(config, fomod_base, mod_name, installed, active, loose,
                  saved_selections) -> dict|None
    resolve_bain(subpackages, mod_root, mod_name) -> {"selected":[...]}|None
        (return None to cancel that mod). ``on_installed(is_fomod: bool)`` fires
        after a successful stage so the orchestrator's archive-keep logic works.
    """
    archive = Path(archive_path)
    if not archive.is_file():
        log_fn(f"Collection install: archive not found: {archive_path}")
        return None
    from Utils.mod_copy import resolve_target_staging
    staging_root = resolve_target_staging(game, profile_dir)
    if staging_root is None:
        log_fn("Collection install: no staging folder configured.")
        return None
    staging_root = Path(staging_root)

    # Interactive FOMODs get deferred to the end of the collection install.
    # When the archive LISTING already shows fomod/ModuleConfig.xml, defer NOW -
    # skipping the full extract that would be discarded and repeated in the
    # deferred phase (double extraction of every interactive FOMOD; minutes of
    # 7z time on big texture packs). Listing misses fall through to the normal
    # extract-then-detect defer below.
    if (defer_interactive_fomod and fomod_auto_selections is None
            and _archive_lists_fomod_config(str(archive))):
        log_fn("FOMOD installer detected (archive listing) - deferring until "
               "dependencies are installed.")
        return FOMOD_DEFERRED

    # Extract + FOMOD-detect via the shared prepare step (kept temp dir).
    prepared = prepare_archive(
        str(archive), game, profile_dir, log_fn=log_fn, progress_fn=progress_fn,
        preferred_name=preferred_name, prebuilt_meta=prebuilt_meta, cancel=cancel)
    if prepared is None:
        return None

    def _pp(done, total, phase=None):
        if progress_fn is not None:
            progress_fn(done, total, phase)

    # RE / Fluffy multi-mod archive: several independent mods from one archive
    # (Tk parity: install_mod_from_archive ran this branch for collection
    # installs too). _install_multi_mod stages/meta/indexes/modlists each mod
    # and cleans up the extract dir itself.
    if prepared.is_multi_mod():
        name = _install_multi_mod(prepared, log_fn, _pp)
        if name is not None:
            _fire_on_installed(on_installed, False)
        return name

    is_fomod_install = False
    fomod_pending_deps = ""   # unselected-option fileDependency plugins (FOMOD)
    fomod_active_deps = ""    # selected-option fileDependency plugins (FOMOD)
    file_list: "list[tuple[str, str, bool]] | None" = None
    stage_src_root = str(prepared.extract_dir)   # copier's src root
    cancelled = False

    try:
        # ---- FOMOD --------------------------------------------------------
        if prepared.is_fomod():
            config = prepared.fomod_config
            fomod_base = prepared.fomod_base
            # resolve_files returns paths relative to the FOMOD BASE (the folder
            # containing fomod/ModuleConfig.xml), NOT the raw extract dir. When
            # the archive wraps the FOMOD in a top-level folder (e.g. "WTNC
            # Config/fomod/…"), fomod_base != extract_dir, so the copier must
            # stage from fomod_base or every source path is off by the wrapper
            # prefix → 0 files staged. (This is what _install_fomod does for the
            # manual path; the collection path previously left stage_src_root as
            # extract_dir, silently dropping nested-FOMOD collection mods.)
            stage_src_root = str(fomod_base)
            installed_files, active_files, loose_files = _collection_plugin_context(
                game, profile_dir)
            try:
                from Utils.fomod_installer import (
                    resolve_files, check_module_dependencies)
                ok, msg = check_module_dependencies(
                    config, installed_files, active_files, loose_files)
                if not ok:
                    log_fn("WARNING: this mod's <moduleDependencies> gate is not "
                           "satisfied - it may not work until these are met:")
                    for line in (msg or "").splitlines():
                        log_fn(f"  {line}")
            except Exception:
                resolve_files = None  # type: ignore

            # A FOMOD with no <installSteps> has nothing for the user to choose
            # (only requiredInstallFiles / conditionalFileInstalls) - never open
            # the wizard (it crashes on an empty step list) and no need to defer;
            # install defaults inline.
            has_steps = bool(getattr(config, "steps", None))

            if fomod_auto_selections is None and defer_interactive_fomod and has_steps:
                log_fn("FOMOD installer detected - deferring until dependencies "
                       "are installed.")
                prepared.cleanup()
                return FOMOD_DEFERRED

            if fomod_auto_selections is not None:
                log_fn("FOMOD installer detected - applying collection author's "
                       "choices automatically.")
                final_selections = fomod_auto_selections
            elif resolve_fomod is not None and has_steps:
                log_fn("FOMOD installer detected - opening wizard...")
                saved_sel = _read_saved_fomod_selections(
                    game, prepared.mod_name, log_fn)
                final_selections = resolve_fomod(
                    config, fomod_base, prepared.mod_name,
                    installed_files, active_files, loose_files, saved_sel)
                if final_selections is None:
                    log_fn("FOMOD install cancelled.")
                    cancelled = True
                else:
                    # Interactive wizard result → also remember it globally for
                    # the next install of this mod (Tk parity; the profile
                    # mirror is written below for every non-cancelled path).
                    _persist_fomod_selection(game, prepared.mod_name,
                                             final_selections, profile=False,
                                             profile_dir=prepared.profile_dir)
            else:
                # No auto-selections and no resolver → FOMOD defaults (parity with
                # the non-interactive single-mod fallback).
                log_fn("FOMOD installer detected - using default/recommended options.")
                final_selections = None

            if not cancelled:
                _write_profile_fomod_selection(game, prepared.mod_name,
                                               final_selections,
                                               prepared.profile_dir)
                _write_profile_fomod_config(game, prepared.mod_name,
                                            prepared.fomod_config_path,
                                            prepared.profile_dir)
                try:
                    if final_selections is None:
                        file_list = _default_fomod_file_list(
                            config, installed_files, active_files, loose_files, log_fn)
                    else:
                        file_list = resolve_files(
                            config, final_selections, installed_files,
                            active_files, loose_files)
                    is_fomod_install = True
                    log_fn(f"FOMOD complete - {len(file_list or [])} file(s) to install.")
                    try:
                        from Utils.fomod_installer import (
                            collect_unselected_dep_plugins,
                            collect_selected_dep_plugins)
                        _sel = final_selections or {}
                        fomod_pending_deps = ";".join(
                            collect_unselected_dep_plugins(config, _sel))
                        fomod_active_deps = ";".join(
                            collect_selected_dep_plugins(config, _sel))
                    except Exception as exc:
                        log_fn(f"FOMOD dep scan skipped ({exc}).")
                except Exception as exc:
                    log_fn(f"FOMOD resolve failed ({exc}) - installing verbatim.")
                    file_list = None
                    is_fomod_install = False

        # ---- BAIN ---------------------------------------------------------
        elif getattr(game, "supports_bain", True):
            from Utils.bain_installer import (
                detect_bain, resolve_bain_files, bain_unwrap_single_folder)
            bain_root = bain_unwrap_single_folder(str(prepared.extract_dir))
            bain_subpkgs = detect_bain(
                bain_root, extra_exts=getattr(game, "plugin_extensions", None))
            if bain_subpkgs:
                stage_src_root = bain_root
                default_names = [p.name for p in bain_subpkgs if p.default_selected]
                log_fn(f"BAIN package detected - {len(bain_subpkgs)} sub-package(s).")
                if bain_auto_selections is None and defer_interactive_bain:
                    log_fn("BAIN installer detected - deferring until other mods "
                           "are installed.")
                    prepared.cleanup()
                    return BAIN_DEFERRED
                if bain_auto_selections is not None:
                    selected = bain_auto_selections.get("selected", [])
                    log_fn("BAIN: applying exported selection automatically.")
                elif resolve_bain is not None:
                    # Worker thread: fill the per-package file sets before the
                    # picker shows (its recolour must not walk the disk on the
                    # GUI thread).
                    from Utils.bain_installer import scan_subpackage_files
                    scan_subpackage_files(bain_subpkgs)
                    result = resolve_bain(bain_subpkgs, bain_root, prepared.mod_name)
                    if result is None:
                        log_fn("BAIN install cancelled.")
                        cancelled = True
                        selected = []
                    else:
                        selected = result.get("selected", [])
                else:
                    selected = default_names
                    log_fn("BAIN: non-interactive install - using default selection.")
                if not cancelled:
                    _write_profile_bain_selection(
                        game, prepared.mod_name, {"selected": selected},
                        prepared.profile_dir)
                    file_list = resolve_bain_files(bain_subpkgs, set(selected))
                    log_fn(f"BAIN complete - {len(selected)} sub-package(s), "
                           f"{len(file_list)} file(s) to install.")

        if cancelled:
            return None

        # ---- stage --------------------------------------------------------
        dest_root = staging_root / prepared.mod_name
        _preserved_endorsed = False
        old_bundle_spec = None
        if dest_root.exists():
            # Collections pre-disambiguate folder names, so a collision means a
            # genuine replace: silent when overwrite_existing is True/None.
            try:
                from Nexus.nexus_meta import read_meta
                _preserved_endorsed = bool(read_meta(dest_root / "meta.ini").endorsed)
            except Exception:
                _preserved_endorsed = False
            if prepared.is_bundle():
                old_bundle_spec = _read_old_bundle_spec(dest_root)
            log_fn(f"Replacing existing mod folder: {prepared.mod_name}")
            shutil.rmtree(dest_root, ignore_errors=True)

        staging_root.mkdir(parents=True, exist_ok=True)
        if file_list is not None:
            # FOMOD / BAIN produced an explicit src→dst list.
            dest_root.mkdir(parents=True, exist_ok=True)
            _copy_file_list(file_list, stage_src_root, dest_root, log_fn, game=game)
        elif prepared.is_bundle():
            # RE / Fluffy bundle: installs as ONE normal mod, same as
            # finish_install - option folders stashed in <mod>/.mm_bundle/,
            # selection materialised onto the mod root, spec in meta.ini's
            # [Bundle] section (preserved by _write_install_meta below).
            from Utils.re_bundle import (layout_to_spec, merge_bundle_spec,
                                         write_bundle_spec,
                                         materialize_selection, BUNDLE_LIB_DIR)
            layout = prepared.bundle_layout
            spec = layout_to_spec(layout)
            log_fn(f"Installing bundle '{layout.bundle_name}' as one mod "
                   f"'{prepared.mod_name}'.")
            if old_bundle_spec is not None:
                spec = merge_bundle_spec(spec, old_bundle_spec)
                log_fn("Bundle: preserved existing option selection across "
                       "reinstall/update.")
            lib_dir = dest_root / BUNDLE_LIB_DIR
            lib_dir.mkdir(parents=True, exist_ok=True)
            for child in sorted(Path(prepared.bundle_root).iterdir()):
                if child.is_dir():
                    _copy_file_list(resolve_direct_files(str(child)),
                                    str(child), lib_dir / child.name, log_fn,
                                    game=game)
            write_bundle_spec(dest_root / "meta.ini", spec)
            materialize_selection(dest_root, spec)
            log_fn(f"Bundle: {len(spec.selected_folders())} of "
                   f"{layout.variant_count} option(s) active.")
        else:
            # Plain (non-FOMOD/BAIN) mod: normalise structure like the Tk direct
            # path. dinput mods (prebuilt_meta.root_folder) install verbatim.
            is_root = bool(getattr(prebuilt_meta, "root_folder", False))
            staged = stage_file_list(
                game, stage_src_root, is_root_install=is_root,
                mod_name=prepared.mod_name, on_need_prefix=None, log_fn=log_fn)
            if staged is None:
                # stage_file_list only returns None when a prefix was required
                # but no resolver was supplied - for a collection mod that means
                # the structure wasn't recognised. Log the extract contents so a
                # live re-run pinpoints why (the "missing mods" investigation).
                try:
                    _preview = []
                    for _r, _ds, _fs in os.walk(stage_src_root):
                        for _f in _fs:
                            _preview.append(os.path.relpath(
                                os.path.join(_r, _f), stage_src_root))
                            if len(_preview) >= 12:
                                break
                        if len(_preview) >= 12:
                            break
                    log_fn(f"Collection install: '{prepared.mod_name}' - "
                           f"stage_file_list returned None (structure not "
                           f"recognised). Extract root={stage_src_root}; "
                           f"first files: {_preview}")
                except Exception:
                    pass
                cancelled = True
            else:
                dest_root.mkdir(parents=True, exist_ok=True)
                if not staged:
                    log_fn(f"Collection install: '{prepared.mod_name}' - "
                           f"stage_file_list produced an EMPTY file list "
                           f"(0 files to copy).")
                _copy_file_list(staged, stage_src_root, dest_root, log_fn, game=game)
    finally:
        prepared.cleanup()

    if cancelled:
        shutil.rmtree(dest_root, ignore_errors=True)
        return None
    if not dest_root.is_dir() or not any(dest_root.iterdir()):
        log_fn(f"Collection install: nothing staged for '{prepared.mod_name}' "
               f"(file_list={'explicit' if file_list is not None else 'auto'}).")
        try:
            dest_root.rmdir()
        except OSError:
            pass
        return None

    _write_install_meta(dest_root, archive, game, log_fn,
                        prebuilt_meta=prebuilt_meta, endorsed=_preserved_endorsed,
                        is_fomod=is_fomod_install,
                        fomod_pending_deps=fomod_pending_deps,
                        fomod_active_deps=fomod_active_deps)
    # Post-install hooks + flat-staging wrap before the index/modlist commit
    # (headless: dialog-popping hooks stay silent).
    if _run_additional_install_logic(
            game, dest_root, prepared.mod_name, log_fn, interactive=False,
            cleanup_on_cancel=True):
        return None
    _wrap_flat_if_needed(game, dest_root, log_fn)
    with _commit_lock:
        if not skip_index_update:
            _pp(0, 0, "Indexing")
            _update_indexes(game, profile_dir, prepared.mod_name, dest_root, log_fn)
        _add_to_modlist(profile_dir, prepared.mod_name, log_fn, preserve_position=False)
        _add_plugins(game, profile_dir, dest_root, log_fn)
    log_fn(f"Installed '{prepared.mod_name}'.")
    _fire_on_installed(on_installed, is_fomod_install)
    return prepared.mod_name


def _fire_on_installed(cb, is_fomod: bool) -> None:
    """Call *cb* as either ``cb(is_fomod)`` or ``cb()`` (parity with Tk's
    ``_fire_on_installed``)."""
    if cb is None:
        return
    try:
        import inspect
        params = inspect.signature(cb).parameters
        if params:
            cb(is_fomod)
        else:
            cb()
    except (TypeError, ValueError):
        try:
            cb()
        except Exception:
            pass
    except Exception:
        pass


def _default_fomod_file_list(config, installed_files, active_files, loose_files,
                             log_fn: LogFn) -> "list[tuple[str, str, bool]]":
    """Resolve a FOMOD's file list using its default/recommended selections
    (threading flag state through steps), passing the collection context sets."""
    from Utils.fomod_installer import (
        resolve_files, get_default_selections, update_flags)
    selections: dict = {}
    flag_state: dict = {}
    for i, step in enumerate(getattr(config, "steps", []) or []):
        sels = get_default_selections(step, flag_state, installed_files)
        selections[str(i)] = sels
        flag_state = update_flags(step, sels, flag_state)
    return resolve_files(config, selections, installed_files, active_files, loose_files)


def _write_profile_fomod_config(game, mod_name: str, config_path,
                                profile_dir=None) -> None:
    """Copy the archive's ``ModuleConfig.xml`` to ``<profile>/fomod/<mod>.xml``.

    The saved selections only record step INDICES with group/option names, so
    rebuilding a Nexus collection's ``choices`` block (which needs step names
    and per-option indices) otherwise means re-reading the original archive -
    which may be gone, renamed, or updated by then. Keeping the installer
    config next to the selections makes the profile self-sufficient."""
    if not config_path:
        return
    pdir = profile_dir if profile_dir is not None \
        else getattr(game, "_active_profile_dir", None)
    if not pdir:
        return
    try:
        src = Path(config_path)
        if not src.is_file():
            return
        pfomod = Path(pdir) / "fomod"
        pfomod.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, pfomod / f"{mod_name}.xml")
    except OSError:
        pass


def _write_profile_fomod_selection(game, mod_name: str, selections,
                                   profile_dir=None) -> None:
    """Mirror FOMOD selections into ``<profile>/fomod/<mod>.json`` (Tk parity -
    profile-scoped, never the global config, so collection choices don't clobber
    the user's manual selections). No-op when selections is None (defaults).

    *profile_dir* - the target profile captured at prepare time. Prefer it over
    the live ``game._active_profile_dir``: the game object is shared across the
    UI + background workers, so a concurrent deploy / collection install can swap
    (or null) ``_active_profile_dir`` between the install being queued and this
    mirror write running, which otherwise lands the sidecar in the wrong profile
    (or nowhere)."""
    if selections is None:
        return
    pdir = profile_dir if profile_dir is not None \
        else getattr(game, "_active_profile_dir", None)
    if not pdir:
        return
    try:
        import json
        pfomod = Path(pdir) / "fomod"
        pfomod.mkdir(parents=True, exist_ok=True)
        with open(pfomod / f"{mod_name}.json", "w", encoding="utf-8") as f:
            json.dump(selections, f, indent=2)
    except OSError:
        pass


def _write_profile_bain_selection(game, mod_name: str, result,
                                  profile_dir=None) -> None:
    """Mirror BAIN selection into ``<profile>/bain/<mod>.json`` (Tk parity).

    *profile_dir* - captured target profile; preferred over the shared, mutable
    ``game._active_profile_dir`` (see _write_profile_fomod_selection)."""
    pdir = profile_dir if profile_dir is not None \
        else getattr(game, "_active_profile_dir", None)
    if not pdir:
        return
    try:
        import json
        pbain = Path(pdir) / "bain"
        pbain.mkdir(parents=True, exist_ok=True)
        with open(pbain / f"{mod_name}.json", "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
    except OSError:
        pass


def _read_bain_readme(bain_root: str) -> "str | None":
    """Return the text of a package readme at *bain_root* (``package.txt`` or
    ``readme.txt``, case-insensitive) - shown alongside the BAIN picker. A Wrye
    Bash installer *script* (``wizard.txt``) is deliberately NOT treated as a
    readme (Tk parity)."""
    try:
        root_files = {e.name.lower(): e.path
                      for e in os.scandir(bain_root) if e.is_file()}
    except OSError:
        return None
    for rn in ("package.txt", "readme.txt"):
        rp = root_files.get(rn)
        if rp:
            try:
                return Path(rp).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
    return None


def _read_saved_fomod_selections(game, mod_name: str, log_fn: LogFn,
                                 profile_dir=None) -> "dict | None":
    """Load a previously-saved FOMOD selection for *mod_name* so the wizard
    restores + highlights the user's last choices (Tk parity).

    Reads the global per-game config first; when that's absent, falls back to
    the profile-scoped copy (``<profile>/fomod/<mod>.json``) so selections still
    restore for mods installed only under a profile.

    *profile_dir* - captured target profile; preferred over the shared, mutable
    ``game._active_profile_dir`` (see _write_profile_fomod_selection)."""
    game_name = getattr(game, "name", "")
    if not game_name:
        return None
    import json
    from Utils.config_paths import get_fomod_selections_path
    candidates = [get_fomod_selections_path(game_name, mod_name)]
    pdir = profile_dir if profile_dir is not None \
        else getattr(game, "_active_profile_dir", None)
    if pdir:
        candidates.append(Path(pdir) / "fomod" / f"{mod_name}.json")
    for sel_path in candidates:
        try:
            if sel_path.is_file():
                with open(sel_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                log_fn("Restored previous FOMOD selections.")
                return data
        except (OSError, ValueError):
            continue
    return None


def _persist_fomod_selection(game, mod_name: str, selections,
                             profile: bool = True, profile_dir=None) -> None:
    """Write the wizard's selections to the global per-game JSON (restored on
    the next install of this mod) and optionally mirror them into the profile
    (Tk parity: interactive installs write both; the collection orchestrator
    mirrors to the profile itself, so it passes profile=False).

    *profile_dir* - captured target profile for the mirror (see
    _write_profile_fomod_selection); falls back to the live active profile."""
    if selections is None:
        return
    game_name = getattr(game, "name", "")
    if game_name:
        try:
            import json
            from Utils.config_paths import get_fomod_selections_path
            sel_path = get_fomod_selections_path(game_name, mod_name)
            with open(sel_path, "w", encoding="utf-8") as f:
                json.dump(selections, f, indent=2)
        except OSError:
            pass
    if profile:
        _write_profile_fomod_selection(game, mod_name, selections, profile_dir)


def _read_saved_bain_selections(game, mod_name: str, log_fn: LogFn,
                                profile_dir=None) -> "dict | None":
    """Load a previously-saved BAIN selection for *mod_name* so the picker
    restores the user's last choices (Tk parity).

    Reads the global per-game config first; when that's absent, falls back to
    the profile-scoped copy (``<profile>/bain/<mod>.json``) so selections still
    restore for mods installed only under a profile.

    *profile_dir* - captured target profile; preferred over the shared, mutable
    ``game._active_profile_dir`` (see _write_profile_fomod_selection)."""
    game_name = getattr(game, "name", "")
    if not game_name:
        return None
    import json
    from Utils.config_paths import get_bain_selections_path
    candidates = [get_bain_selections_path(game_name, mod_name)]
    pdir = profile_dir if profile_dir is not None \
        else getattr(game, "_active_profile_dir", None)
    if pdir:
        candidates.append(Path(pdir) / "bain" / f"{mod_name}.json")
    for sel_path in candidates:
        try:
            if sel_path.is_file():
                with open(sel_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                log_fn("Restored previous BAIN selections.")
                return data
        except (OSError, ValueError):
            continue
    return None


def _persist_bain_selection(game, mod_name: str, result, profile_dir=None) -> None:
    """Persist a BAIN selection to BOTH the global config
    (``get_bain_selections_path``) and the profile (``<profile>/bain/<mod>.json``),
    matching the Tk installer.

    *profile_dir* - captured target profile for the mirror (see
    _write_profile_fomod_selection); falls back to the live active profile."""
    _write_profile_bain_selection(game, mod_name, result, profile_dir)
    game_name = getattr(game, "name", "")
    if not game_name:
        return
    try:
        import json
        from Utils.config_paths import get_bain_selections_path
        sel_path = get_bain_selections_path(game_name, mod_name)
        sel_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sel_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- helpers
def _nexus_domain_for(game) -> str:
    """The Nexus API domain for *game* (e.g. "skyrimspecialedition")."""
    return getattr(game, "nexus_game_domain", None) or getattr(game, "game_id", "") or ""


def _nexus_domains_for(game) -> tuple[str, ...]:
    """All configured Nexus domains, with the legacy game-id fallback."""
    domains = tuple(getattr(game, "nexus_game_domains", ()) or ())
    return domains or tuple(d for d in (_nexus_domain_for(game),) if d)


def _nexus_file_display_name(meta, game) -> str:
    """The sanitized per-file Nexus display name from *meta*, or "" if absent.

    Uses ``file_details.name`` (NexusModMeta.nexus_file_name, e.g. "Engine Fixes
    - Main File") - the label shown against the download on the mod's Files tab -
    to name the staging folder. Sanitized to a valid folder name; returns "" so
    the caller falls back to the archive-stem name when the meta has no file name.
    """
    if meta is None:
        return ""
    raw = (getattr(meta, "nexus_file_name", "") or "").strip()
    if not raw:
        return ""
    try:
        from Utils.mod_name_utils import sanitize_mod_folder_name
        cleaned = sanitize_mod_folder_name(raw) or raw
    except Exception:
        import re
        cleaned = re.sub(r'[<>:"/\\|?*]', "_", raw).rstrip(". ")
    return cleaned.strip()


def _thunderstore_display_name(archive: Path) -> str:
    """Package name from a Thunderstore archive filename, or "".

    Thunderstore serves every package as ``{team}-{Package}-{version}.zip`` and
    the downloader keeps that name, so the folder would otherwise be the whole
    triple. The package name is Thunderstore's display name (the API exposes no
    prettier title), so it is what the folder should be called.

    Only accepts a strict ``{ns}-{name}-{numeric.version}`` stem plus a
    ``manifest.json`` inside the zip, so an unrelated hyphenated archive that
    merely looks similar is never renamed.
    """
    stem = archive.stem
    parts = stem.rsplit("-", 2)
    if len(parts) != 3:
        return ""
    _ns, name, version = parts
    if not name or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return ""
    # A Thunderstore package always ships a manifest.json at the zip root.
    try:
        if not zipfile.is_zipfile(archive):
            return ""
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
        if not any(n.lower() == "manifest.json"
                   or n.lower().endswith("/manifest.json") for n in names):
            return ""
    except Exception:
        return ""
    try:
        from Utils.mod_name_utils import sanitize_mod_folder_name
        return (sanitize_mod_folder_name(name) or "").strip()
    except Exception:
        return name.strip()


def _resolve_nexus_meta_for_naming(archive: Path, game, log_fn: LogFn):
    """Look up *archive* on Nexus (filename → MD5) so its folder can be named
    after the file's Nexus display name. Returns a NexusModMeta or None.

    Requires the user to be logged in (a live API) - offline / not-logged-in
    returns None and the caller falls back to the stripped archive stem. Failures
    are swallowed: naming from the archive name is always an acceptable fallback.
    """
    try:
        api = _build_nexus_api()
        if api is None:
            return None
        domains = _nexus_domains_for(game)
        if not domains:
            return None
        from Nexus.nexus_meta import resolve_nexus_meta_for_archive_domains
        return resolve_nexus_meta_for_archive_domains(
            archive, domains, api=api, log_fn=log_fn)
    except Exception as exc:
        log_fn(f"Nexus name lookup skipped ({exc}).")
        return None


def _clean_mod_name(stem: str, game) -> str:
    """Best-effort folder name from the archive stem.

    Uses the same derivation as the Tk installer: ``_suggest_mod_names(stem)[0]``
    - the *least-destructive* candidate, which strips only the Nexus id/version
    tail (and the new underscore ``_<version>_<slug>`` tail / mod.io UUID tail)
    while preserving the actual title, including meaningful parentheses and
    edition tags (``(SE)``, ``(CP)``, ``(Black)`` …).  ``_strip_title_metadata``
    is deliberately NOT used here: it aggressively removes those and was demoted
    from the Tk default because it silently destroyed real titles.
    """
    name = stem
    try:
        from Utils.mod_name_utils import _suggest_mod_names, sanitize_mod_folder_name
        suggestions = _suggest_mod_names(stem)
        name = (suggestions[0] if suggestions else stem) or stem
        name = sanitize_mod_folder_name(name) or name
    except Exception:
        # mod_name_utils may pull Tk transitively - fall back to a basic clean.
        import re
        name = re.sub(r"-\d+-.*$", "", stem).strip() or stem
        name = re.sub(r'[<>:"/\\|?*]', "_", name).rstrip(". ")
    return name or stem


def _copy_tree(src_root: Path, dest_root: Path, log_fn: LogFn, _p) -> None:
    files = [p for p in src_root.rglob("*") if p.is_file()]
    total = len(files)
    dest_root.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(files, 1):
        rel = f.relative_to(src_root)
        # Skip installer-only fomod/ metadata in a plain copy.
        if rel.parts and rel.parts[0].lower() == "fomod":
            continue
        target = dest_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        if i % 25 == 0 or i == total:
            _p(i, total, "Copying files")


def _install_fomod(fomod_base: Path, config, dest_root: Path,
                   selections, log_fn: LogFn, _p,
                   context: "tuple[set, set, set] | None" = None,
                   game=None) -> bool:
    """Stage a FOMOD's files. *selections* is the wizard's
    {step_idx_str: {group_name: [plugin_names]}} dict; None → the FOMOD's own
    default selections. *context* is the (installed, active, loose) file sets
    its conditions evaluate against. Uses the neutral `resolve_files` to map
    src→dst and apply requiredInstallFiles + conditional installs. Returns
    False on failure (caller falls back to verbatim copy)."""
    try:
        from Utils.fomod_installer import (
            resolve_files, get_default_selections, update_flags)
    except Exception as exc:
        log_fn(f"FOMOD installer unavailable ({exc}).")
        return False
    installed, active, loose = context or (set(), set(), set())

    if not selections:
        # Build default selections per step, threading flag state through.
        selections = {}
        flag_state: dict = {}
        for i, step in enumerate(getattr(config, "steps", []) or []):
            sels = get_default_selections(step, flag_state, installed,
                                          active, loose)
            selections[str(i)] = sels
            flag_state = update_flags(step, sels, flag_state)
        log_fn("FOMOD: using default/recommended options.")

    try:
        # [(src_rel, dst_rel, is_folder)]
        files = resolve_files(config, selections, installed, active, loose)
    except Exception as exc:
        log_fn(f"FOMOD resolve_files failed ({exc}).")
        return False
    if not files:
        return False

    _p(0, 0, "Installing FOMOD files")
    # Use the SHARED, proven copier (case-insensitive resolution + dst dedup +
    # priority "later wins"). fomod_base is the src root; resolve_files already
    # produced the src→dst mapping.
    _copy_file_list(files, str(fomod_base), dest_root, log_fn, game=game)
    return any(dest_root.iterdir())


def _read_old_bundle_spec(dest_root: Path):
    """The [Bundle] spec of an existing install about to be replaced (or None)."""
    try:
        from Utils.re_bundle import read_bundle_spec
        return read_bundle_spec(dest_root / "meta.ini")
    except Exception:
        return None


def _install_multi_mod(p: "PreparedInstall", log_fn: LogFn, _pp) -> str | None:
    """Install a multi-mod archive: each top-level folder (all carrying a
    modinfo.ini, no bundle grouping) becomes its own independent mod with its
    own meta/index/modlist row (Tk parity). Existing same-named folders are
    silently replaced. Returns the first installed name (None if nothing
    staged)."""
    from Utils.mod_copy import resolve_target_staging
    staging_root = Path(resolve_target_staging(p.game, p.profile_dir))
    staging_root.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    try:
        for m_name, m_path in p.multi_mods:
            file_list = stage_file_list(p.game, m_path, mod_name=m_name,
                                        log_fn=log_fn)
            if not file_list:
                log_fn(f"  '{m_name}': nothing to install - skipped.")
                continue
            m_dest = staging_root / m_name
            if m_dest.exists():
                log_fn(f"Replacing existing mod folder: {m_name}")
                shutil.rmtree(m_dest, ignore_errors=True)
            m_dest.mkdir(parents=True, exist_ok=True)
            _copy_file_list(file_list, m_path, m_dest, log_fn, game=p.game)
            _write_install_meta(m_dest, p.archive, p.game, log_fn,
                                prebuilt_meta=getattr(p, "prebuilt_meta", None))
            with _commit_lock:
                _update_indexes(p.game, p.profile_dir, m_name, m_dest, log_fn)
                _add_to_modlist(p.profile_dir, m_name, log_fn)
                _add_plugins(p.game, p.profile_dir, m_dest, log_fn)
            log_fn(f"  Installed '{m_name}' → {m_dest}")
            installed.append(m_name)
    finally:
        p.cleanup()
    if not installed:
        log_fn(f"Install failed: nothing was staged for '{p.mod_name}'.")
        return None
    _check_nexus_flags_after_install(p.game, installed, log_fn,
                                     profile_dir=p.profile_dir)
    log_fn(f"Installed {len(installed)} mod(s) from archive.")
    return installed[0]


def _build_nexus_api():
    """Build a shared NexusAPI from saved OAuth tokens, or None if not logged
    in. Neutral (no GUI dependency) so the install worker can reverse-look-up
    metadata by MD5 without the API being threaded down from the app. Mirrors
    what the Qt app's _ensure_nexus_api() does at startup."""
    try:
        from Nexus.nexus_oauth import load_oauth_tokens
        from Nexus.nexus_api import NexusAPI
        tokens = load_oauth_tokens()
        if tokens is None:
            return None
        return NexusAPI.from_oauth(tokens)
    except Exception:
        return None


def _clear_meta_key(meta_path: Path, ini_key: str) -> None:
    """Remove a single key from a meta.ini's [General] section, if present.
    Used to clear FOMOD pending-deps on a rerun that leaves nothing pending
    (write_meta deliberately never writes empty values for that key)."""
    try:
        import configparser
        if not meta_path.is_file():
            return
        cp = configparser.ConfigParser()
        cp.read(str(meta_path), encoding="utf-8")
        if cp.has_option("General", ini_key):
            cp.remove_option("General", ini_key)
            with open(meta_path, "w", encoding="utf-8") as f:
                cp.write(f)
    except Exception:
        pass


def _write_install_meta(dest_root: Path, archive: Path, game, log_fn: LogFn,
                        prebuilt_meta=None, endorsed: bool = False,
                        ignored_reqs: str = "",
                        is_fomod: bool = False,
                        is_bain: bool = False,
                        fomod_pending_deps: str = "",
                        fomod_active_deps: str = "") -> None:
    try:
        from Nexus.nexus_meta import (
            write_meta, resolve_nexus_meta_for_archive_domains, NexusModMeta)
        from datetime import datetime
        meta_path = dest_root / "meta.ini"
        meta = None
        domains = _nexus_domains_for(game)
        if prebuilt_meta is not None:
            # The caller (Nexus browser) knows the real mod_id/file_id - use it
            # verbatim instead of parsing the archive filename, which can be
            # wrong for mods whose name/version embeds digits.
            meta = prebuilt_meta
        else:
            try:
                # Pass a live API so resolve_nexus_meta_for_archive can do the
                # MD5 reverse lookup (its Strategy 2 is skipped when api=None) -
                # this is what the Tk installer did and the Qt port dropped.
                api = _build_nexus_api()
                meta = resolve_nexus_meta_for_archive_domains(
                    archive, domains, api=api, log_fn=log_fn)
            except Exception:
                meta = None
        if meta is None:
            meta = NexusModMeta()
        # Always stamp the local install fields.
        meta.installation_file = archive.name
        try:
            meta.file_size = archive.stat().st_size
        except OSError:
            pass
        if not getattr(meta, "installed", ""):
            meta.installed = datetime.now().isoformat(timespec="seconds")
        # Carry the endorsed flag from a replaced install (Tk parity).
        if endorsed:
            meta.endorsed = True
        # Carry the per-requirement ignore list from a replaced install so
        # ignored requirements survive a reinstall/update.
        if ignored_reqs and not getattr(meta, "ignored_requirements", ""):
            meta.ignored_requirements = ignored_reqs
        # Stamp the FOMOD / BAIN install-method flags so the modlist FOMOD/BAIN
        # filters (and is_fomod / is_bain sets) pick them up (Tk parity).
        if is_fomod:
            meta.is_fomod = True
            # Record fileDependency plugins from UNSELECTED FOMOD options so the
            # modlist can flag a rerun if one appears in the load order later
            # (pending), and from SELECTED options so it can flag when one is
            # REMOVED (active). An empty value on a FOMOD (re)install means
            # "nothing" - but write_meta skips empty values to protect the keys
            # from unrelated callers, so empty results are cleared explicitly.
            if fomod_pending_deps:
                meta.fomod_pending_deps = fomod_pending_deps
            if fomod_active_deps:
                meta.fomod_active_deps = fomod_active_deps
        if is_bain:
            meta.is_bain = True
        write_meta(meta_path, meta)
        if is_fomod and not fomod_pending_deps:
            _clear_meta_key(meta_path, "fomodPendingDeps")
        if is_fomod and not fomod_active_deps:
            _clear_meta_key(meta_path, "fomodActiveDeps")
        if is_fomod:
            # A (re)install resets the rerun-flag bookkeeping: the pending
            # baseline is re-derived on the next flag evaluation (against the
            # load order the wizard just ran in) and the active-clause
            # observations start over.
            _clear_meta_key(meta_path, "fomodPendingBaselined")
            _clear_meta_key(meta_path, "fomodActiveDepsSeen")
    except Exception as exc:
        log_fn(f"meta.ini write skipped ({exc}).")


def _check_nexus_flags_after_install(game, mod_names, log_fn: LogFn,
                                     profile_dir: "Path | None" = None) -> None:
    """Stamp the freshly installed mod(s)' missing requirements + endorsed flag.

    Both come from a SINGLE Nexus GraphQL v2 request (``modRequirements`` +
    ``viewerEndorsed``) via ``graphql_mod_update_info_batch``, which does NOT
    consume the REST hourly rate limit - so this runs on every install at zero
    rate-limit cost. Results are written to each mod's ``missingRequirements``
    and ``endorsed`` meta.ini fields, the same fields the "Check updates" flow
    populates and the modlist UI already reads, so the warning/★ flags appear
    immediately without the user pressing Check updates.

    *mod_names* is a single folder name or an iterable of them - multi-mod
    archives pass every installed name at once so the staging scan and the
    GraphQL request happen once per archive, not once per mod.

    Best-effort: offline / not-logged-in / any failure is swallowed - this must
    never break or delay a successful install.
    """
    try:
        names = {mod_names} if isinstance(mod_names, str) else set(mod_names)
        if not names:
            return
        api = _build_nexus_api()
        if api is None:
            return
        fallback_domain = _nexus_domain_for(game)
        if not fallback_domain:
            return
        try:
            from Utils.mod_copy import resolve_target_staging
            staging_root = (Path(resolve_target_staging(game, profile_dir))
                            if profile_dir is not None
                            else Path(game.get_effective_mod_staging_path()))
        except Exception:
            return
        from Nexus.nexus_meta import (
            normalise_game_domain, scan_installed_mods, read_meta, write_meta)
        from Nexus.nexus_requirements import check_requirements_from_gql

        all_installed = scan_installed_mods(staging_root)
        # The mods we just installed must have a Nexus mod_id to have flags.
        these_mods = [m for m in all_installed
                      if m.mod_name in names and m.mod_id > 0]
        if not these_mods:
            return

        by_domain: dict[str, list] = {}
        for meta in these_mods:
            domain = (normalise_game_domain(meta.game_domain)
                      or normalise_game_domain(fallback_domain))
            if domain:
                by_domain.setdefault(domain, []).append(meta)

        from Thunderstore.thunderstore_meta import is_thunderstore_mod

        for domain, domain_mods in by_domain.items():
            # One GraphQL request per Nexus game. Mod ids are only unique
            # inside a domain, so combining these batches would cross-wire ids.
            gql_info = api.graphql_mod_update_info_batch(
                [(domain, m.mod_id) for m in domain_mods])
            if not gql_info:
                continue

            # A mod that also carries Thunderstore metadata was installed from
            # Thunderstore, whose packages pin their own dependencies - the
            # Nexus mod page's requirements do not describe what it needs, and
            # flagging them would report requirements that are already
            # satisfied. Endorsement below is unaffected: endorsing a Nexus mod
            # page is orthogonal to where the file came from.
            # (A mod with ONLY [thunderstore] never reaches here - it has no
            # modid, so the mod_id > 0 filter above already dropped it.)
            domain_names = {
                m.mod_name for m in domain_mods
                if not is_thunderstore_mod(staging_root / m.mod_name / "meta.ini")}
            if domain_names:
                check_requirements_from_gql(
                    gql_info,
                    all_installed,
                    game_domain=domain,
                    staging_root=staging_root,
                    progress_cb=log_fn,
                    save_results=True,
                    enabled_only=domain_names,
                    api=api,
                )

            # viewerEndorsed is keyed by same-domain mod id.
            for m in domain_mods:
                info = gql_info.get(m.mod_id)
                ven = (getattr(info, "viewer_endorsed", None)
                       if info is not None else None)
                if ven is None:
                    continue
                meta_path = staging_root / m.mod_name / "meta.ini"
                if meta_path.is_file():
                    meta = read_meta(meta_path)
                    if meta.endorsed != bool(ven):
                        meta.endorsed = bool(ven)
                        write_meta(meta_path, meta)
    except Exception as exc:
        log_fn(f"Nexus flag check after install skipped ({exc}).")


def _update_indexes(game, profile_dir: Path, mod_name: str, dest_root: Path,
                    log_fn: LogFn) -> None:
    try:
        from Utils.filemap import rescan_mods_in_index
        from Utils.deploy import load_per_mod_strip_prefixes
        # The index MUST live where build_filemap reads it - next to the
        # effective filemap (= staging.parent / game root), NOT the profile dir.
        # Writing it to the profile dir leaves a fresh install invisible to the
        # filemap rebuild → no conflicts detected (the bug this fixes).
        try:
            index_dir = game.get_effective_filemap_path().parent
        except Exception:
            index_dir = profile_dir
        index_path = index_dir / "modindex.bin"
        staging_root = Path(dest_root).parent
        # Reuse rescan_mods_in_index (shares logic with rebuild_mod_index, the
        # Refresh path) so the single-mod entry is written with EXACTLY the same
        # strip-prefix / extension / per-mod / root-folder rules a full Refresh
        # applies. The canonical game attributes are mod_folder_strip_prefixes /
        # mod_install_extensions - the older strip_prefixes / install_extensions
        # names don't exist on the game classes (getattr → None), which wrote an
        # UNSTRIPPED entry (e.g. Bethesda "Data/…" kept), inconsistent with a
        # Refresh → deploy double-nested paths / wrong conflicts until Refresh.
        # A root-flagged mod (e.g. SKSE) must NOT be stripped - read the flag
        # from the just-written meta.ini (the modlist isn't updated yet here).
        root_mods = None
        try:
            from Nexus.nexus_meta import read_meta
            if read_meta(Path(dest_root) / "meta.ini").root_folder:
                root_mods = {mod_name}
        except Exception:
            root_mods = None
        rescan_mods_in_index(
            index_path, staging_root, [mod_name],
            strip_prefixes=set(getattr(game, "mod_folder_strip_prefixes", None) or ()) or None,
            per_mod_strip_prefixes=load_per_mod_strip_prefixes(profile_dir),
            allowed_extensions=set(getattr(game, "mod_install_extensions", None) or ()) or None,
            normalize_folder_case=getattr(game, "normalize_folder_case", True),
            root_folder_mods=root_mods,
            log_fn=log_fn,
        )
        archive_exts = frozenset(getattr(game, "archive_extensions", frozenset()) or frozenset())
        if archive_exts:
            from Utils.bsa_filemap import update_bsa_index
            update_bsa_index(index_dir / "bsa_index.bin", mod_name, dest_root, archive_exts)
    except Exception as exc:
        log_fn(f"index update skipped ({exc}) - next rebuild will rescan.")


def _add_to_modlist(profile_dir: Path, mod_name: str, log_fn: LogFn,
                    preserve_position: bool = False) -> None:
    # The user-configurable default state for a freshly installed mod (Downloads
    # tab option). Collection installs never route through here, so they are
    # unaffected. Only applies to brand-new entries - reinstalls/updates keep
    # their existing state via preserve_existing_state below.
    try:
        from Utils.ui_config import load_install_mods_disabled
        default_enabled = not load_install_mods_disabled()
    except Exception:
        default_enabled = True
    modlist_path = profile_dir / "modlist.txt"
    try:
        if preserve_position:
            # Replacing an existing mod - keep its load-order position and its
            # existing enabled/disabled state (a reinstall/update must not
            # silently re-enable a disabled mod).
            from Utils.modlist import ensure_mod_preserving_position
            ensure_mod_preserving_position(
                modlist_path, mod_name, enabled=default_enabled,
                preserve_existing_state=True)
        else:
            # New mods land at the top with the configured default state; but if
            # this name already exists (reinstall without position-preservation)
            # keep its current state.
            from Utils.modlist import prepend_mod
            prepend_mod(modlist_path, mod_name,
                        enabled=default_enabled, preserve_existing_state=True)
    except Exception as exc:
        log_fn(f"modlist update failed for '{mod_name}' in {modlist_path} "
               f"({exc}).")
        return
    # The write reported success, so a missing entry here means another writer
    # clobbered it or we wrote a path nothing else reads. Silent when healthy.
    try:
        from Utils.modlist import read_modlist
        if mod_name not in {e.name for e in read_modlist(modlist_path)}:
            log_fn(f"WARNING: '{mod_name}' was written to {modlist_path} but "
                   f"is not there on read-back - concurrent overwrite?")
    except Exception:
        pass


def _add_plugins(game, profile_dir: Path, dest_root: Path, log_fn: LogFn) -> None:
    exts = [e.lower() for e in (getattr(game, "plugin_extensions", []) or [])]
    if not exts:
        return
    try:
        from Utils.plugins import append_plugin
        star = getattr(game, "plugins_use_star_prefix", True)
        plugins_path = profile_dir / "plugins.txt"
        # Besides the mod root, scan the top level of the game's data subfolder
        # ('Data Files/' for Morrowind): plugins there still deploy to the top
        # of the data dir - the filemap strips the prefix for normal mods, and
        # root-flagged mods deploy '<subfolder>/x.esp' verbatim into it. Gated
        # on the subfolder being a declared strip prefix so a folder that would
        # deploy NESTED (never loadable) isn't scanned.
        scan_dirs = [dest_root]
        try:
            from Utils.game_helpers import game_data_subpath
            sub = game_data_subpath(game)
            strips = {s.lower() for s in
                      (getattr(game, "mod_folder_strip_prefixes", None) or ())}
            if sub and "/" not in sub and sub.lower() in strips:
                scan_dirs.extend(d for d in dest_root.iterdir()
                                 if d.is_dir() and d.name.lower() == sub.lower())
        except Exception:
            pass
        for scan_dir in scan_dirs:
            for entry in sorted(scan_dir.iterdir()):
                if entry.is_file() and entry.suffix.lower() in exts:
                    append_plugin(plugins_path, entry.name, enabled=True,
                                  star_prefix=star)
    except Exception as exc:
        log_fn(f"plugins update skipped ({exc}).")
