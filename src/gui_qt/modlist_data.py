"""Real modlist metadata (versions / installed dates / flags from meta.ini, and
conflicts from filemap overrides). Pure backend calls - no Qt, no gui.* - so
they can run on a worker thread.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from Utils.modlist import ModEntry


# Flag bits for the Flags column - mirrors the full Tk set (FOMOD/BAIN are
# install methods, NOT flag icons).
FLAG_UPDATE = 1 << 0       # has_update & not ignored
FLAG_ENDORSED = 1 << 1
FLAG_ROOT = 1 << 2         # meta.root_folder
FLAG_MODIFIED_MF = 1 << 3  # modified in the Mod Files tab (excluded files/strip)
FLAG_MISSING_REQS = 1 << 4  # meta.missing_requirements has un-ignored entries
FLAG_COLLECTION_BUNDLED = 1 << 5  # meta.from_collection_bundled (bundled by a collection)
FLAG_COLLECTION_PATCHED = 1 << 6  # meta.from_collection_patched (diff-patched by a collection)
FLAG_NOTE = 1 << 7         # a saved per-profile user note (read_mod_notes)
FLAG_XEDIT = 1 << 8        # meta.xedit_modified_plugins non-empty (xEdit-edited plugins)
FLAG_BUNDLE = 1 << 9       # RE/Fluffy bundle (a [Bundle] section in meta.ini)
FLAG_MODIO_UPDATE = 1 << 10  # BG3 mod.io update (modioFileId != modioLatestFileId)
FLAG_PRERTX = 1 << 11      # contains pre-RTX (natives/x64) files - filemap-derived
FLAG_ROOT_RULE = 1 << 12   # owns files with a custom root-routing rule - filemap-derived
FLAG_RERUN_FOMOD = 1 << 13  # a FOMOD option's fileDependency plugin is now in the load order - live overlay


def _parse_missing_req_pairs(raw: str) -> list[tuple[int, str]]:
    """`(modId, name)` pairs from a meta.ini `missing_requirements` value:
    semicolon-separated `modId:name` entries. The name half may be blank
    (locally-seeded requirements - e.g. the TTW installer - store `modId:`
    with no name), so entries are keyed on the id, not the name."""
    pairs: list[tuple[int, str]] = []
    for part in (raw or "").split(";"):
        raw_id, _, name = part.partition(":")
        raw_id = raw_id.strip()
        if not raw_id:
            continue
        try:
            pairs.append((int(raw_id), name.strip()))
        except ValueError:
            pass
    return pairs


def _apply_req_substitutions(pairs: list[tuple[int, str]], domain: str,
                             cache: dict) -> list[tuple[int, str]]:
    """Rewrite requirement pairs through the filter file's substitution rules
    (e.g. Nemesis 60033 → Pandora 133232), deduping the result.

    meta.ini values stamped before a rule existed still name the old mod, so the
    flag pass and the Missing Requirements panel remap on read; the next
    requirements check rewrites the stored value for good. Rules come from the
    local filter cache only (no network) and are memoised per domain."""
    if domain not in cache:
        try:
            from Nexus.nexus_requirements import load_requirement_substitutions
            cache[domain] = load_requirement_substitutions(domain)
        except Exception:
            cache[domain] = {}
    subs = cache[domain]
    if not subs:
        return pairs
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for mid, name in pairs:
        repl = subs.get(mid)
        if repl is not None:
            mid, name = repl[0], (repl[1] or name)
        if mid in seen:
            continue
        seen.add(mid)
        out.append((mid, name))
    return out


def read_meta_for_entries(entries: list[ModEntry], staging_dir: Path,
                          ignored_reqs: frozenset[str] = frozenset(),
                          profile_dir: "Path | None" = None,
                          is_bg3: bool = False):
    """Return a MetaInfo-ish tuple keyed by mod name.

    versions[name]     -> version string ("" if none)
    installed[name]    -> short date string ("" if none)
    flags[name]        -> int bitmask of FLAG_* above
    categories[name]   -> Nexus category display name ("" if none)
    updates            -> set of mod names with a pending update
    fomod              -> set of mod names installed via FOMOD (meta.is_fomod)
    bain               -> set of mod names installed via BAIN (meta.is_bain)
    missing_reqs       -> set of mod names with un-ignored missing requirements
    descriptions[name] -> Nexus summary text for the name-column hover tooltip
    authors[name]      -> Nexus uploader username (Author column, "" if none)

    *ignored_reqs* - requirement names the user has dismissed (per-profile); a
    mod is only flagged if it still has missing requirements outside this set.
    *profile_dir* - the active profile dir; when given, per-mod user notes are
    read (Note flag). *is_bg3* - enable the BG3-only mod.io update flag.
    """
    versions: dict[str, str] = {}
    installed: dict[str, str] = {}
    flags: dict[str, int] = {}
    categories: dict[str, str] = {}
    updates: set[str] = set()
    fomod: set[str] = set()
    bain: set[str] = set()
    missing_reqs: set[str] = set()
    descriptions: dict[str, str] = {}
    authors: dict[str, str] = {}
    # Requirement resolution is a two-pass job (Tk parity): collect every
    # installed Nexus mod_id first, then flag a mod only for requirement ids
    # that aren't present. Keyed on id, not name, so locally-seeded id-only
    # requirements (e.g. the TTW installer) still surface.
    installed_ids: set[int] = set()
    raw_missing_pairs: dict[str, list[tuple[int, str]]] = {}
    # domain → substitution rules, filled on first use (see _apply_req_substitutions)
    subs_cache: dict[str, dict[int, tuple[int, str]]] = {}

    try:
        from Nexus.nexus_meta import read_meta
    except Exception:
        return (versions, installed, flags, categories, updates, fomod, bain,
                missing_reqs, descriptions, authors)

    # Per-profile user notes (Note flag) - one read for the whole list.
    notes: dict[str, str] = {}
    if profile_dir is not None:
        try:
            from Utils.profile_state import read_mod_notes
            notes = read_mod_notes(profile_dir)
        except Exception:
            notes = {}

    for e in entries:
        if e.is_separator:
            continue
        meta_path = staging_dir / e.name / "meta.ini"
        if not meta_path.is_file():
            continue
        try:
            meta = read_meta(meta_path)
        except Exception:
            continue

        if meta.version:
            versions[e.name] = meta.version

        if meta.installed:
            try:
                dt = datetime.fromisoformat(meta.installed)
                if dt.date() == datetime.now().date():
                    installed[e.name] = dt.strftime("%H:%M")
                else:
                    installed[e.name] = dt.strftime("%m/%d/%y")
            except Exception:
                installed[e.name] = meta.installed[:10]

        if meta.category_name:
            categories[e.name] = meta.category_name

        desc = (getattr(meta, "description", "") or "").strip()
        if desc:
            descriptions[e.name] = desc

        uploader = (getattr(meta, "uploaded_by", "") or "").strip()
        if uploader:
            authors[e.name] = uploader

        if getattr(meta, "is_fomod", False):
            fomod.add(e.name)
        if getattr(meta, "is_bain", False):
            bain.add(e.name)

        bits = 0
        if meta.has_update and meta.latest_version != meta.ignored_version:
            bits |= FLAG_UPDATE
            updates.add(e.name)
        if meta.endorsed:
            bits |= FLAG_ENDORSED
        if meta.root_folder:
            bits |= FLAG_ROOT
        # Missing requirements are finalized in a post-loop pass, once every
        # installed mod_id is known (so requirements already satisfied by an
        # installed mod are filtered out). "Ignore requirements" in the Missing
        # Requirements panel adds the OWNING mod's name to `ignored_reqs`.
        if getattr(meta, "mod_id", 0):
            installed_ids.add(int(meta.mod_id))
        if getattr(meta, "missing_requirements", "") and e.name not in ignored_reqs:
            dom = (getattr(meta, "game_domain", "") or "").strip().lower()
            pairs = _apply_req_substitutions(
                _parse_missing_req_pairs(meta.missing_requirements),
                dom, subs_cache)
            # Per-requirement ignores (meta.ini ignoredRequirements): those ids
            # never raise the ⚠ flag, but stay in missing_requirements so the
            # Missing Requirements panel still lists them (un-ignorable there).
            ign_ids = {mid for mid, _ in _apply_req_substitutions(
                _parse_missing_req_pairs(
                    getattr(meta, "ignored_requirements", "") or ""),
                dom, subs_cache)}
            if ign_ids:
                pairs = [pr for pr in pairs if pr[0] not in ign_ids]
            if pairs:
                raw_missing_pairs[e.name] = pairs
        # Collection-install provenance (stamped in meta.ini at install time).
        if getattr(meta, "from_collection_bundled", False):
            bits |= FLAG_COLLECTION_BUNDLED
        if getattr(meta, "from_collection_patched", False):
            bits |= FLAG_COLLECTION_PATCHED
        # xEdit-modified plugins (semicolon-separated list in meta).
        if (getattr(meta, "xedit_modified_plugins", "") or "").strip():
            bits |= FLAG_XEDIT
        # RE/Fluffy bundle mod ([Bundle] section in meta.ini).
        try:
            from Utils.re_bundle import read_bundle_spec
            if read_bundle_spec(meta_path) is not None:
                bits |= FLAG_BUNDLE
        except Exception:
            pass
        # BG3 mod.io update (installed file id differs from the latest).
        if is_bg3:
            try:
                import configparser as _cp_modio
                cp = _cp_modio.ConfigParser(interpolation=None)
                cp.read(str(meta_path), encoding="utf-8")
                _fid = int(cp.get("General", "modioFileId", fallback="0") or "0")
                _lfid = int(cp.get("General", "modioLatestFileId", fallback="0") or "0")
                if _lfid and _fid and _lfid != _fid:
                    bits |= FLAG_MODIO_UPDATE
                    updates.add(e.name)
            except Exception:
                pass
        # Per-profile user note.
        if notes.get(e.name):
            bits |= FLAG_NOTE
        if bits:
            flags[e.name] = bits

    # Second pass: flag mods whose requirements aren't satisfied by any
    # installed mod_id. The full seeded list stays in meta.ini, so a
    # requirement reappears automatically if its mod is later removed.
    for name, pairs in raw_missing_pairs.items():
        if any(mid not in installed_ids for mid, _ in pairs):
            missing_reqs.add(name)
            flags[name] = flags.get(name, 0) | FLAG_MISSING_REQS

    return (versions, installed, flags, categories, updates, fomod, bain,
            missing_reqs, descriptions, authors)


# ---- mod folder sizes (Size column) - ported from gui/modlist_panel.py --------
def _dir_size_bytes(path: Path) -> int:
    """Recursively sum file sizes under path (bytes). Safe to run in a thread."""
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        total += _dir_size_bytes(Path(entry.path))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def count_files_in(path: Path) -> int:
    """Recursively count files under path (symlinks count as files, they're
    what a deployed mod leaves behind). Safe to run in a thread."""
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        total += count_files_in(Path(entry.path))
                    else:
                        total += 1
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _format_size(num_bytes: int) -> str:
    """Format a byte count as a short KB/MB/GB string."""
    if num_bytes <= 0:
        return ""
    kb = num_bytes / 1024
    if kb < 1024:
        return f"{kb:.0f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


def compute_sizes(entries: list[ModEntry], staging_dir: Path
                  ) -> tuple[dict[str, str], dict[str, int]]:
    """(formatted, raw-bytes) folder size per non-separator mod. Bytes back
    the Size column sort. Walks the staging dir, so only call it when the Size
    column is visible (Tk gates the same way)."""
    sizes: dict[str, str] = {}
    size_bytes: dict[str, int] = {}
    if staging_dir is None:
        return sizes, size_bytes
    for e in entries:
        if e.is_separator:
            continue
        mod_dir = staging_dir / e.name
        if not mod_dir.is_dir():
            continue
        b = _dir_size_bytes(mod_dir)
        s = _format_size(b)
        if s:
            sizes[e.name] = s
            size_bytes[e.name] = b
    return sizes, size_bytes


def compute_plugin_stats(rows) -> dict:
    """Aggregate plugin stats for the plugins footer stats row: total / ESL /
    non-ESL. ESL = the PF_ESL (light-flagged or .esl) bit. In-memory, instant."""
    from gui_qt.plugin_state import PF_ESL
    total = len(rows)
    esl = sum(1 for r in rows if getattr(r, "flags", 0) & PF_ESL)
    return {"total": total, "esl": esl, "non_esl": total - esl}


# Qt display conflict codes (drawn by the delegate). Mirrors the Tk app's
# icon mapping: WINS→winner, LOSES→loser, PARTIAL→mixed, FULL→redundant.
DISP_NONE = 0
DISP_WINS = 1
DISP_LOSES = -1
DISP_PARTIAL = 2
DISP_FULL = 3


def display_codes_from_conflict_map(conflict_map: dict):
    """Map the backend's full conflict_map (CONFLICT_* from Utils.filemap:
    NONE=0 WINS=1 LOSES=2 PARTIAL=3 FULL=4) to the Qt delegate's display codes.
    This preserves FULL (fully-overridden / redundant) which the old
    override-set re-derivation lost."""
    from Utils.filemap import (
        CONFLICT_WINS, CONFLICT_LOSES, CONFLICT_PARTIAL, CONFLICT_FULL,
    )
    out: dict[str, int] = {}
    for name, code in (conflict_map or {}).items():
        if code == CONFLICT_WINS:
            out[name] = DISP_WINS
        elif code == CONFLICT_LOSES:
            out[name] = DISP_LOSES
        elif code == CONFLICT_PARTIAL:
            out[name] = DISP_PARTIAL
        elif code == CONFLICT_FULL:
            out[name] = DISP_FULL
    return out


def conflicts_from_filemap(overrides: dict, overridden_by: dict):
    """[legacy] Re-derive per-mod codes from override sets (no FULL). Kept for
    BSA conflicts which only expose override maps; prefer
    display_codes_from_conflict_map for loose conflicts."""
    codes: dict[str, int] = {}
    wins = {m for m, v in (overrides or {}).items() if v}
    loses = {m for m, v in (overridden_by or {}).items() if v}
    for m in wins | loses:
        if m in wins and m in loses:
            codes[m] = 2
        elif m in wins:
            codes[m] = 1
        else:
            codes[m] = -1
    return codes
