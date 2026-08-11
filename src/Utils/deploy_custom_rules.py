"""
deploy_custom_rules.py
Custom routing rules (flexible file routing used by Bethesda + others).

Originally extracted from deploy.py during the 2026-04 refactor, with
behaviour preserved at the time of extraction.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import os
import re
import shutil
from functools import lru_cache
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.deploy_shared import (
    CustomRule,
    LinkMode,
    _deploy_workers,
    _do_link,
    _iter_map_batched,
    _mkdir_leaves,
    _move_crash_safe,
    _prune_empty_dirs,
    _resolve_source,
    _restore_backup_dir,
)


_CUSTOM_RULES_LOG_NAME = "custom_rules_deployed.txt"
_CUSTOM_RULES_BACKUP_DIR = "custom_rules_backup"
_CUSTOM_RULES_PREFIX_BACKUP_DIR = "custom_rules_prefix_backup"


def _ext_match(filename: str, exts: list[str]) -> str | None:
    """Return the longest extension in ``exts`` that ``filename`` ends with
    (as ``.something``), or None. ``exts`` must be sorted longest-first.
    """
    for e in exts:
        if filename.endswith(e) and len(filename) > len(e):
            return e
    return None


def _name_match(filename: str, names: set[str]) -> bool:
    """Match ``filename`` (lowercased) against ``names``. Glob characters
    (``*``, ``?``, ``[seq]``) are honoured; plain entries match by equality.
    """
    for n in names:
        if any(c in n for c in "*?["):
            if fnmatch.fnmatchcase(filename, n):
                return True
        elif filename == n:
            return True
    return False


@lru_cache(maxsize=512)
def _declared_segment_casing(declared: tuple[str, ...]) -> dict[str, str]:
    """Map lowercase folder-segment name -> the casing the rule spelled it with."""
    out: dict[str, str] = {}
    for name in declared:
        for seg in name.replace("\\", "/").strip("/").split("/"):
            if seg:
                out.setdefault(seg.lower(), seg)
    return out


def canonicalize_declared_folders(tail: str, declared: tuple[str, ...]) -> str:
    """Force folder segments a rule names to the casing that rule declared.

    Folder matching is case-insensitive, so two mods shipping ``~Mods`` and
    ``~mods`` both hit the same rule - but each would otherwise deploy its own
    casing and create two sibling folders on a case-sensitive filesystem, where
    the engine only reads one of them. The rule names the folder, so the rule's
    spelling is the canonical one. Filenames (the last segment) are never
    touched; unnamed folders keep the mod's casing.
    """
    if "/" not in tail or not declared:
        return tail
    casing = _declared_segment_casing(declared)
    if not casing:
        return tail
    parts = tail.split("/")
    changed = False
    for i in range(len(parts) - 1):  # skip the filename
        canon = casing.get(parts[i].lower())
        if canon is not None and canon != parts[i]:
            parts[i] = canon
            changed = True
    return "/".join(parts) if changed else tail


def _match_single_rule(
    rel_lower: str,
    rule: "CustomRule", folders: set[str], exts: list[str], filenames: set[str],
) -> tuple[int, str] | None:
    """Check whether ``rel_lower`` matches a *single* rule.

    Returns ``(strip_len, matched_ext)`` on a match, or None.
    """
    parts = rel_lower.split("/")
    filename = parts[-1]
    if rule.exclude_extensions:
        for e in rule.exclude_extensions:
            if filename.endswith(e.lower()):
                return None
    is_loose = len(parts) == 1
    strip_len = -1
    folder_hit = False
    if folders:
        for f in folders:
            if "/" in f:
                idx = rel_lower.find(f + "/")
                if idx < 0 and rel_lower.endswith(f):
                    idx = len(rel_lower) - len(f)
                if idx >= 0 and (idx == 0 or rel_lower[idx - 1] == "/"):
                    strip_len = idx
                    folder_hit = True
                    break
            else:
                for pi, seg in enumerate(parts[:-1]):
                    if seg == f:
                        strip_len = sum(len(parts[j]) + 1 for j in range(pi))
                        folder_hit = True
                        break
                if folder_hit:
                    break
        if folder_hit and rule.loose_only and strip_len != 0:
            return None
    matched_ext = _ext_match(filename, exts) if exts else None
    if folder_hit and (not exts or matched_ext is not None):
        return strip_len, matched_ext or ""
    if rule.loose_only and not is_loose:
        return None
    if matched_ext is not None and not folders and not filenames:
        return -1, matched_ext
    if filenames and _name_match(filename, filenames):
        return -1, ""
    return None


def _normalise_rule(rule: "CustomRule") -> tuple["CustomRule", set[str], list[str], set[str]]:
    """Return ``(rule, folders_lower, exts_sorted, filenames_lower)`` for a
    single CustomRule - the form expected by ``_match_single_rule``."""
    return (
        rule,
        {f.lower() for f in rule.folders},
        sorted({e.lower() for e in rule.extensions}, key=len, reverse=True),
        {n.lower() for n in rule.filenames},
    )


def compute_prefix_handled(
    entries: list[tuple[str, str]], rules: list["CustomRule"],
) -> tuple[set[str], list[tuple[str, str, "CustomRule", int, str]]]:
    """Return ``(prefix_handled, prefix_primaries)`` - only the entries
    claimed by ``to_prefix`` rules.

    Non-prefix rules are evaluated alongside prefix rules to preserve correct
    first-match ordering (so a non-prefix rule earlier in the list can prevent
    a later prefix rule from claiming the same file), but their matches do
    *not* appear in either return value. Callers (e.g. UE5 deploy) use the
    returned set to skip prefix-routed files in their own placement pipeline
    without disturbing files claimed by ordinary rules.
    """
    norm_rules = [_normalise_rule(r) for r in rules]
    all_handled: set[str] = set()       # claimed by any rule (for ordering)
    prefix_handled: set[str] = set()    # subset claimed by a to_prefix rule
    prefix_primaries: list[tuple[str, str, "CustomRule", int, str]] = []
    indexed = [(rel.replace("\\", "/"), mod, rel.replace("\\", "/").lower())
               for rel, mod in entries]
    for rule, folders, exts, filenames in norm_rules:
        new_primary_keys: list[tuple[str, str, int]] = []
        for rel_str, mod_name, rel_lower in indexed:
            if rel_lower in all_handled:
                continue
            hit = _match_single_rule(rel_lower, rule, folders, exts, filenames)
            if hit is None:
                continue
            strip_len, matched_ext = hit
            all_handled.add(rel_lower)
            if rule.to_prefix:
                prefix_handled.add(rel_lower)
                prefix_primaries.append((rel_str, mod_name, rule, strip_len, matched_ext))
                new_primary_keys.append((rel_str, mod_name, strip_len))
        if not rule.include_siblings or not new_primary_keys:
            continue
        drags: list[tuple[str, str, bool]] = []
        for rel_str, mod_name, strip_len in new_primary_keys:
            info = _sibling_container(rel_str, strip_len, mod_name)
            if info is None:
                continue
            cont_path, _cont_name = info
            drags.append((cont_path.lower(), mod_name, cont_path == ""))
        drags.sort(key=lambda t: (0 if t[2] else 1, -len(t[0])))
        seen_drags: set[tuple[str, str]] = set()
        for cont_lower, mod_name, is_whole in drags:
            key = (cont_lower, mod_name)
            if key in seen_drags:
                continue
            seen_drags.add(key)
            prefix_lower = cont_lower + "/" if cont_lower else ""
            for sib_rel_str, sib_mod_name, sib_lower in indexed:
                if sib_lower in all_handled:
                    continue
                if sib_mod_name != mod_name:
                    continue
                if not is_whole and not sib_lower.startswith(prefix_lower):
                    continue
                all_handled.add(sib_lower)
                if rule.to_prefix:
                    prefix_handled.add(sib_lower)
                    prefix_primaries.append((sib_rel_str, sib_mod_name, rule, -2, ""))
    return prefix_handled, prefix_primaries


def _sibling_container(
    rel_str: str, strip_len: int, mod_name: str,
) -> tuple[str, str] | None:
    """Return (container_path, container_name) for an include_siblings primary.

    Include Siblings drags the **topmost folder containing the matched file**:
    every same-mod file under that top-level folder rides along, preserving
    the full rel_path under ``dest``. This way a mod with multiple top-level
    folders (each potentially routed by a different rule) only drags the one
    folder containing the matched file, not the whole mod.

    For "VanillaHUD Plus/lua/vanillahud/utils/x.lua" matching ``utils``,
    the container is "VanillaHUD Plus" - every file under that folder rides
    along to ``dest/VanillaHUD Plus/...``.

    For a file at the mod root (no folder above it), there's nothing to drag
    - returns None.
    """
    del strip_len, mod_name  # unused - container is always the topmost folder
    norm_rel = rel_str.replace("\\", "/")
    if "/" not in norm_rel:
        return None
    container = norm_rel.split("/", 1)[0]
    return (container, container)


def _strip_top_prefix(rel_lower: str, strip_prefixes: set[str]) -> str:
    """Drop the first path segment of ``rel_lower`` if it is a strip prefix.

    Mirrors the single-level top-folder strip that ``deploy_filemap_to_root``
    applies to non-routed files (only the first segment, one level, matching
    the scan-time strip in ``filemap._scan_dir``).
    """
    if not strip_prefixes or "/" not in rel_lower:
        return rel_lower
    head, _, tail = rel_lower.partition("/")
    if head in strip_prefixes:
        return tail
    return rel_lower


def compute_routed_dest(
    rel_key: str,
    norm_rules: list[tuple["CustomRule", set[str], list[str], set[str]]],
    strip_prefixes: set[str] | None = None,
    data_prefix: str = "",
) -> str:
    """Return the game-root-relative destination a single file deploys to.

    Given a lowercased staged rel_path and the game's normalised routing rules
    (from :func:`normalise_rules`), replicate the destination the deploy step
    would compute so that two staged paths landing at the *same* game location
    can be recognised as conflicting even when their staged keys differ.

    This mirrors the ``tail`` math in ``deploy_custom_rules._place_primary``:
    the first matching rule (declaration order) wins; a folder match with
    ``flatten`` strips everything above the matched folder; an ext/filename
    match with ``flatten`` collapses to the bare filename; a non-flatten match
    keeps the full rel_path under ``dest``.

    Files matching no rule fall through to their strip-prefixed staged path -
    exactly what ``deploy_filemap_to_root`` places verbatim under the game root.

    ``to_prefix`` rules (deployed into the Proton prefix, not the game root)
    and ``mirror_dests`` are intentionally ignored for keying - prefix files
    live in a separate namespace, and mirroring never *removes* the primary
    destination, so the primary dest is the correct conflict key.

    ``data_prefix`` - the game-root-relative folder that *non-routed* files
    deploy into (a standard custom game's ``mod_data_path``, e.g. ``data``).
    Routed files already carry their absolute ``dest`` and are unaffected; this
    just anchors the verbatim fallback to the same game-root frame so a routed
    file whose ``dest`` equals ``mod_data_path`` collides correctly. Empty for
    root-deploy games (files go to the game root directly).
    """
    rel_lower = rel_key.replace("\\", "/").lower()
    for rule, folders, exts, filenames in norm_rules:
        if rule.to_prefix:
            # Prefix-routed files land in the Proton prefix, a distinct
            # namespace from game-root files - never a cross conflict here.
            hit = _match_single_rule(rel_lower, rule, folders, exts, filenames)
            if hit is not None:
                return "\x00prefix\x00" + rel_lower  # unique, never collides w/ root
            continue
        hit = _match_single_rule(rel_lower, rule, folders, exts, filenames)
        if hit is None:
            continue
        strip_len, _matched_ext = hit
        dest = rule.dest.replace("\\", "/").strip("/").lower()
        if rule.include_siblings and "/" in rel_lower:
            # Include-siblings preserves the full mod-relative path under dest
            # (container = topmost folder, so tail == the whole rel_path).
            tail = rel_lower
        elif rule.flatten:
            if strip_len >= 0:
                tail = rel_lower[strip_len:].lstrip("/")
            else:
                tail = rel_lower.rsplit("/", 1)[-1]
        else:
            tail = rel_lower
        return f"{dest}/{tail}" if dest else tail
    # No rule matched - verbatim deploy under mod_data_path (after top strip).
    tail = _strip_top_prefix(rel_lower, {p.lower() for p in (strip_prefixes or set())})
    _dp = data_prefix.replace("\\", "/").strip("/").lower()
    return f"{_dp}/{tail}" if _dp else tail


def normalise_rules(
    rules: list["CustomRule"],
) -> list[tuple["CustomRule", set[str], list[str], set[str]]]:
    """Return the routing rules pre-processed for :func:`compute_routed_dest`."""
    return [_normalise_rule(r) for r in rules]


def deploy_custom_rules(
    filemap_path: Path,
    game_root: Path,
    staging_root: Path,
    rules: list[CustomRule],
    mode: LinkMode = LinkMode.HARDLINK,
    strip_prefixes: set[str] | None = None,
    per_mod_strip_prefixes: dict[str, list[str]] | None = None,
    per_mod_link_modes: dict[str, LinkMode] | None = None,
    raw_mods: set[str] | None = None,
    log_fn=None,
    progress_fn=None,
    prefix_root: Path | None = None,
) -> set[str]:
    """Deploy filemap entries that match a CustomRule to their designated dirs.

    Matching logic (first matching rule wins): file matches a rule by folder
    (any path segment in rule.folders), extension (rule.extensions), or
    filename (rule.filenames). Placement under ``game_root / rule.dest``
    depends on rule.flatten:
    - flatten=False (default) - preserve the full mod-relative path under dest
    - flatten=True + folder match - strip the prefix above the matched folder,
      keep matched folder + contents under dest
    - flatten=True + ext/filename match - bare filename under dest

    Returns the set of lowercased rel_paths that were handled so the caller
    can exclude them from the normal deploy step.

    A log of placed absolute paths is written to
    filemap_path.parent / "custom_rules_deployed.txt" for use by
    restore_custom_rules().
    """
    if not rules:
        return set()

    _log = _safe_log(log_fn)
    _strip = {p.lower() for p in strip_prefixes} if strip_prefixes else set()
    _per_mod_strip = per_mod_strip_prefixes or {}

    # Per-separator deploy-method overrides. Self-load (mirroring deploy_filemap)
    # when the caller didn't supply them so a separator's File Transfer Method
    # applies to custom-routed files too, not just the normal deploy step.
    _per_mode = per_mod_link_modes
    if _per_mode is None:
        try:
            from Utils.deploy_shared import (
                load_separator_deploy_paths as _lsdp,
                expand_separator_link_modes as _eslm,
            )
            from Utils.modlist import read_modlist as _rml
            _per_mode = _eslm(_lsdp(filemap_path.parent),
                              _rml(filemap_path.parent / "modlist.txt"))
        except Exception:
            _per_mode = {}
    _per_mode = _per_mode or {}

    # Mods sitting under a separator with "Ignore deployment rules" (raw deploy)
    # on must bypass custom routing entirely - their files are placed as-is by
    # the normal deploy step (deploy_standard) under the separator's custom dir.
    # Self-load the set (mirroring _per_mode) when the caller didn't supply it.
    _raw_mods = raw_mods
    if _raw_mods is None:
        try:
            from Utils.deploy_shared import (
                load_separator_deploy_paths as _lsdp_raw,
                expand_separator_raw_deploy as _esrd,
            )
            from Utils.modlist import read_modlist as _rml_raw
            _raw_mods = _esrd(_lsdp_raw(filemap_path.parent),
                              _rml_raw(filemap_path.parent / "modlist.txt"))
        except Exception:
            _raw_mods = set()
    _raw_mods = _raw_mods or set()

    def _rule_base(rule: CustomRule) -> Path | None:
        """Return the root directory this rule's ``dest`` is resolved under,
        or ``None`` if the rule requires a prefix and none is available."""
        if rule.to_prefix:
            return prefix_root
        return game_root

    def _rule_dest_bases(rule: CustomRule) -> list[Path]:
        """Return every destination dir for this rule: ``dest`` plus any
        ``mirror_dests``, each resolved under the rule's base root."""
        base = _rule_base(rule)
        return [base / d if d else base for d in (rule.dest, *rule.mirror_dests)]

    # Drop rules that want the prefix but have none - otherwise they'd silently
    # land at game_root, which is worse than skipping them.
    skipped = [r for r in rules if r.to_prefix and prefix_root is None]
    if skipped:
        _log(f"  Skipping {len(skipped)} prefix-routed rule(s): no Proton prefix configured.")
    rules = [r for r in rules if not (r.to_prefix and prefix_root is None)]
    if not rules:
        return set()
    overwrite_dir = staging_root.parent / "overwrite"
    _overwrite_str = str(overwrite_dir)
    _staging_str   = str(staging_root)
    nocase_cache: dict[Path, dict[str, list[Path]]] = {}
    sorted_strip   = sorted(_strip) if _strip else []

    # Pre-process rules into normalised form for fast matching.
    # Extensions are kept as a list sorted longest-first so that multi-dot
    # extensions like ".dekcns.json" win over their plain ".json" suffix.
    _rules: list[tuple[CustomRule, set[str], list[str], set[str]]] = []
    for rule in rules:
        ext_list = sorted({e.lower() for e in rule.extensions}, key=len, reverse=True)
        _rules.append((
            rule,
            {f.lower() for f in rule.folders},
            ext_list,
            {n.lower() for n in rule.filenames},
        ))

    def _match_rule(rel_lower: str) -> tuple[CustomRule, int, str] | None:
        """First-match-wins multi-rule lookup. Kept for backwards-compat;
        the main flow uses ``_match_single_rule`` rule-by-rule so earlier
        rules' include_siblings drags can claim files before later rules
        get to run their primary match.
        """
        for rule, folders, exts, filenames in _rules:
            hit = _match_single_rule(rel_lower, rule, folders, exts, filenames)
            if hit is not None:
                strip_len, matched_ext = hit
                return rule, strip_len, matched_ext
        return None

    tasks: list[tuple[Path, Path, str]] = []   # (src, dst, mod_name)
    handled_lower: set[str] = set()
    # primary_matches: rel_lower -> (rule, strip_len, rel_str, mod_name, matched_ext)
    primary_matches: dict[str, tuple[CustomRule, int, str, str, str]] = {}
    # entries_by_parent: parent_lower -> list of (rel_str, mod_name, name_lower)
    entries_by_parent: dict[str, list[tuple[str, str, str]]] = {}
    # all_entries: full list of (rel_str, mod_name, rel_lower)
    all_entries: list[tuple[str, str, str]] = []
    # Pre-load every entry once so the per-rule loop below can iterate them
    # repeatedly (skipping any already claimed by an earlier rule).
    seen_lower: set[str] = set()
    # surrogateescape throughout: filemap.txt entries and the deploy log carry
    # filesystem-derived paths whose non-UTF-8 bytes decode to surrogate code
    # points; a plain utf-8 read/write raises on them.
    with filemap_path.open(encoding="utf-8", errors="surrogateescape") as f:
        for line in f:
            line = line.rstrip("\n")
            if "\t" not in line:
                continue
            rel_str, mod_name = line.split("\t", 1)
            # Raw-deploy mods bypass routing rules entirely - leave their files
            # for the normal deploy step (placed as-is under the custom dir).
            if mod_name in _raw_mods:
                continue
            rel_lower = rel_str.lower()
            if rel_lower in seen_lower:
                continue
            seen_lower.add(rel_lower)
            parent_lower, _, name_lower = rel_lower.rpartition("/")
            entries_by_parent.setdefault(parent_lower, []).append(
                (rel_str, mod_name, name_lower)
            )
            all_entries.append((rel_str, mod_name, rel_lower))

    # Global pre-filter: a file cannot be a *primary* match for any rule
    # unless its path contains one of the rules' folders, extensions, or
    # filenames.  This lets the per-rule loops below skip the overwhelming
    # majority of files (textures, meshes, ...) in a large modlist.  Sibling
    # drags still consider every entry, so nothing claimable is lost.
    _pf_folders_simple: set[str] = set()
    _pf_folders_path: list[str] = []
    _pf_exts: list[str] = []
    _pf_filenames: set[str] = set()
    for _r, _fo, _ex, _fn in _rules:
        for _f in _fo:
            if "/" in _f:
                _pf_folders_path.append(_f)
            else:
                _pf_folders_simple.add(_f)
        _pf_exts.extend(_ex)
        _pf_filenames.update(_fn)
    match_candidates: list[tuple[str, str, str]] = []
    for rel_str, mod_name, rel_lower in all_entries:
        parts = rel_lower.split("/")
        filename = parts[-1]
        could_match = False
        if _pf_folders_simple:
            for seg in parts[:-1]:
                if seg in _pf_folders_simple:
                    could_match = True
                    break
        if not could_match and _pf_folders_path:
            for _f in _pf_folders_path:
                if (_f + "/") in rel_lower or rel_lower.endswith(_f):
                    could_match = True
                    break
        if not could_match and _pf_exts:
            # Over-accepts (no length guard) - the real rule check confirms.
            for _e in _pf_exts:
                if filename.endswith(_e):
                    could_match = True
                    break
        if not could_match and _pf_filenames and _name_match(filename, _pf_filenames):
            could_match = True
        if could_match:
            match_candidates.append((rel_str, mod_name, rel_lower))

    def _place_primary(rel_str: str, mod_name: str, rule: CustomRule,
                       strip_len: int, matched_ext: str) -> None:
        """Resolve source, compute destination, and append a copy task for a
        rule's primary match. Updates primary_matches/handled_lower/tasks.
        """
        rel_lower = rel_str.lower()
        primary_matches[rel_lower] = (rule, strip_len, rel_str, mod_name, matched_ext)
        src_str = _resolve_source(
            mod_name, rel_str, rel_lower, overwrite_dir, staging_root,
            _overwrite_str, _staging_str, sorted_strip, _per_mod_strip,
            nocase_cache,
        )
        if src_str is None:
            _log(f"  WARN: source not found - {rel_str} ({mod_name})")
            handled_lower.add(rel_lower)  # claim it anyway so later rules don't re-try
            return
        src = Path(src_str)
        container_info = _sibling_container(rel_str, strip_len, mod_name) \
            if rule.include_siblings else None
        if container_info is not None:
            norm_rel = rel_str.replace("\\", "/")
            container_path, container_name = container_info
            if container_path:
                rel_in_container = norm_rel[len(container_path) + 1:]
            else:
                rel_in_container = norm_rel
            tail = container_name + "/" + rel_in_container
        elif rule.flatten:
            if strip_len >= 0:
                tail = rel_str[strip_len:].lstrip("/")
            else:
                tail = src.name
        else:
            tail = rel_str
        tail = canonicalize_declared_folders(
            tail.replace("\\", "/"), tuple(rule.folders))
        for dest_base in _rule_dest_bases(rule):
            tasks.append((src, dest_base / tail if tail else dest_base, mod_name))
        handled_lower.add(rel_lower)

    def _drag_container(container_lower: str, container_name: str,
                        primary_mod: str, rule: CustomRule, is_whole_mod: bool) -> None:
        """Drag every unclaimed same-mod file under ``container_lower`` to
        ``dest/container_name/<rel-from-container>``.
        """
        prefix_lower = container_lower + "/" if container_lower else ""
        dest_bases = _rule_dest_bases(rule)
        for sib_rel_str, sib_mod_name, sib_lower in all_entries:
            if sib_lower in handled_lower:
                continue
            if sib_mod_name != primary_mod:
                continue
            if is_whole_mod:
                rel_in_container = sib_rel_str.replace("\\", "/")
            else:
                if not sib_lower.startswith(prefix_lower):
                    continue
                rel_in_container = sib_rel_str.replace("\\", "/")[len(container_lower) + 1:]
            src_str = _resolve_source(
                sib_mod_name, sib_rel_str, sib_lower, overwrite_dir, staging_root,
                _overwrite_str, _staging_str, sorted_strip, _per_mod_strip,
                nocase_cache,
            )
            if src_str is None:
                _log(f"  WARN: source not found - {sib_rel_str} ({sib_mod_name})")
                handled_lower.add(sib_lower)
                continue
            src = Path(src_str)
            for dest_base in dest_bases:
                tasks.append((src, dest_base / container_name / rel_in_container,
                              sib_mod_name))
            handled_lower.add(sib_lower)

    # Process rules in declaration order. For each rule:
    #   1. Find every still-unclaimed file that matches this rule and place
    #      it as a primary.
    #   2. If include_siblings is on, immediately drag the container of
    #      every just-placed primary so later rules can't claim those files.
    # This ordering is what enforces "rule order wins" - if rule 1's drag
    # would swallow a file that rule 2 would also match, rule 1 takes it.
    for rule, folders, exts, filenames in _rules:
        # Step 1: claim primaries for this rule among unclaimed files.
        new_primaries: list[tuple[str, str, int, str]] = []
        for rel_str, mod_name, rel_lower in match_candidates:
            if rel_lower in handled_lower:
                continue
            hit = _match_single_rule(rel_lower, rule, folders, exts, filenames)
            if hit is None:
                continue
            strip_len, matched_ext = hit
            _place_primary(rel_str, mod_name, rule, strip_len, matched_ext)
            new_primaries.append((rel_str, mod_name, strip_len, matched_ext))
        # Step 2: drag siblings for include_siblings primaries (per-mod).
        # Whole-mod drags subsume nested ones, so process them first.
        if not rule.include_siblings or not new_primaries:
            continue
        drags: list[tuple[str, str, str, bool]] = []  # (cont_lower, cont_name, mod_name, whole)
        for rel_str, mod_name, strip_len, _matched_ext in new_primaries:
            info = _sibling_container(rel_str, strip_len, mod_name)
            if info is None:
                continue
            container_path, container_name = info
            drags.append((container_path.lower(), container_name, mod_name,
                          container_path == ""))
        # Whole-mod first, then longest container first.
        drags.sort(key=lambda t: (0 if t[3] else 1, -len(t[0])))
        seen_drags: set[tuple[str, str]] = set()  # (container_lower, mod_name)
        for cont_lower, cont_name, mod_name, is_whole_mod in drags:
            key = (cont_lower, mod_name)
            if key in seen_drags:
                continue
            seen_drags.add(key)
            _drag_container(cont_lower, cont_name, mod_name, rule, is_whole_mod)

    # Second pass: companion files ride along with their primary match.
    # Companions are matched longest-first too so a ".dekcns.json" companion
    # would beat a ".json" one.
    for rel_lower, (rule, strip_len, rel_str, _mod_name, matched_ext) in list(primary_matches.items()):
        companions = sorted(
            {c.lower() for c in rule.companion_extensions}, key=len, reverse=True
        )
        if not companions:
            continue
        parent_lower, _, name_lower = rel_lower.rpartition("/")
        # Stem is the primary filename minus the extension that matched.
        # Falls back to splitext when there was no extension match (folder/
        # filename rules) - companions remain stem-relative in that case.
        if matched_ext and name_lower.endswith(matched_ext):
            stem_lower = name_lower[: -len(matched_ext)]
        else:
            stem_lower, _ = os.path.splitext(name_lower)
        siblings = entries_by_parent.get(parent_lower, ())
        stem_dot = stem_lower + "."
        for sib_rel_str, sib_mod_name, sib_name_lower in siblings:
            sib_lower = sib_rel_str.lower()
            if sib_lower in handled_lower:
                continue
            if not sib_name_lower.startswith(stem_dot):
                continue
            sib_ext = None
            for c in companions:
                if sib_name_lower.endswith(c) and len(sib_name_lower) > len(c):
                    sib_ext = c
                    break
            if sib_ext is None:
                continue
            src_str = _resolve_source(
                sib_mod_name, sib_rel_str, sib_lower, overwrite_dir, staging_root,
                _overwrite_str, _staging_str, sorted_strip, _per_mod_strip,
                nocase_cache,
            )
            if src_str is None:
                _log(f"  WARN: source not found - {sib_rel_str} ({sib_mod_name})")
                continue
            src = Path(src_str)
            if rule.flatten:
                if strip_len >= 0:
                    tail = sib_rel_str[strip_len:].lstrip("/")
                else:
                    tail = src.name
            else:
                tail = sib_rel_str
            for dest_base in _rule_dest_bases(rule):
                tasks.append((src, dest_base / tail if tail else dest_base, sib_mod_name))
            handled_lower.add(sib_lower)

    if not tasks:
        return handled_lower

    # Backup directories for vanilla files that will be overwritten.
    # Game-root-routed files mirror under ``backup_dir``; prefix-routed files
    # mirror under ``prefix_backup_dir`` so Restore knows which root to
    # reconstruct each backup under.
    backup_dir = filemap_path.parent / _CUSTOM_RULES_BACKUP_DIR
    prefix_backup_dir = filemap_path.parent / _CUSTOM_RULES_PREFIX_BACKUP_DIR

    # Self-heal: a leftover deploy log means the previous deploy was never
    # restored (crashed or failed restore).  Restore it now - otherwise the
    # rmtree below would destroy the backed-up vanilla originals.
    if (filemap_path.parent / _CUSTOM_RULES_LOG_NAME).is_file():
        _log("  Previous custom-rules deploy log still present - restoring it before redeploying.")
        restore_custom_rules(filemap_path, game_root, rules=[],
                             log_fn=log_fn, prefix_root=prefix_root)

    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    if prefix_backup_dir.exists():
        shutil.rmtree(prefix_backup_dir)

    # Create destination directories (skip parents implied by deeper leaves)
    _mkdir_leaves({str(dst.parent) for _, dst, _ in tasks})

    placed_abs: list[str] = []
    total = len(tasks)
    _game_root = game_root
    _prefix_root = prefix_root

    def _pick_backup(dst: Path) -> tuple[Path, Path] | None:
        """Return (backup_root, rel) for ``dst``, or None if it lives under
        neither root."""
        try:
            return backup_dir, dst.relative_to(_game_root)
        except ValueError:
            pass
        if _prefix_root is not None:
            try:
                return prefix_backup_dir, dst.relative_to(_prefix_root)
            except ValueError:
                pass
        return None

    # Back up any vanilla files we are about to overwrite (must be serial).
    # One lstat per destination instead of exists()+is_symlink().
    import stat as _stat
    for src, dst, _mod in tasks:
        try:
            _st = os.lstat(dst)
        except OSError:
            continue
        if _stat.S_ISLNK(_st.st_mode):
            dst.unlink()
        elif _stat.S_ISREG(_st.st_mode):
            picked = _pick_backup(dst)
            if picked is None:
                _log(f"  WARN: could not back up {dst}: outside known roots")
            else:
                bak_root, rel = picked
                try:
                    bak = bak_root / rel
                    _move_crash_safe(dst, bak)
                except OSError as e:
                    _log(f"  WARN: could not back up {dst}: {e}")

    # Transfer files in parallel. Each task carries the effective link mode:
    # a separator's File Transfer Method override (if any) wins over the global
    # mode, matching deploy_filemap's per-mod behaviour.
    transfer_tasks: list[tuple[str, str, LinkMode]] = [
        (str(s), str(d), _per_mode.get(m, mode)) for s, d, m in tasks
    ]

    def _do_custom(item: tuple[str, str, LinkMode]) -> tuple[str | None, tuple[str, OSError] | None]:
        src_s, dst_s, eff_mode = item
        err = _do_link(src_s, dst_s, eff_mode)
        if err is None:
            return dst_s, None
        return None, (dst_s, err)

    done_count = 0
    for result, err in _iter_map_batched(_do_custom, transfer_tasks):
        done_count += 1
        if result is not None:
            placed_abs.append(result)
        elif err is not None:
            dst_err, exc = err
            _log(f"  WARN: could not transfer {dst_err}: {exc}")
        if progress_fn is not None and (done_count % 200 == 0 or done_count == total):
            progress_fn(done_count, total)

    log_path = filemap_path.parent / _CUSTOM_RULES_LOG_NAME
    try:
        if placed_abs:
            log_path.write_text("\n".join(placed_abs), encoding="utf-8",
                                errors="surrogateescape")
        elif log_path.exists():
            log_path.unlink()
    except OSError:
        pass

    _log(f"  Custom rules: placed {len(placed_abs)} file(s).")
    return handled_lower


def restore_custom_rules(
    filemap_path: Path,
    game_root: Path,
    rules: list[CustomRule],
    log_fn=None,
    prefix_root: Path | None = None,
) -> int:
    """Remove files placed by deploy_custom_rules() and prune empty dest dirs.

    Reads filemap_path.parent / "custom_rules_deployed.txt", deletes every
    listed absolute path, then tries to rmdir each rule's destination directory
    (silently ignored if non-empty).  Returns the number of files removed.

    ``prefix_root`` allows removing files placed by prefix-routed rules
    (``to_prefix=True``) and restoring their backups from
    ``custom_rules_prefix_backup``.
    """
    del rules  # unused - log file is the source of truth for what was placed
    _log = _safe_log(log_fn)
    log_path = filemap_path.parent / _CUSTOM_RULES_LOG_NAME
    backup_dir = filemap_path.parent / _CUSTOM_RULES_BACKUP_DIR
    prefix_backup_dir = filemap_path.parent / _CUSTOM_RULES_PREFIX_BACKUP_DIR

    if not log_path.is_file():
        return 0

    placed = [p for p in log_path.read_text(
        encoding="utf-8", errors="surrogateescape").splitlines() if p]
    removed = 0
    dirs_to_prune: set[Path] = set()
    _game_root_resolved = game_root.resolve()
    _prefix_root_resolved = prefix_root.resolve() if prefix_root else None
    # Pre-filter for path traversal (cheap, serial) so the worker pool only
    # does syscalls - one lstat + (maybe) one unlink per file.
    safe_targets: list[Path] = []
    for abs_str in placed:
        p = Path(abs_str)
        # Allow paths under either game_root or prefix_root. Try the
        # unresolved path first so symlinks pointing outside the root are
        # not incorrectly blocked.
        under_root: Path | None = None
        for root, root_resolved in (
            (game_root, _game_root_resolved),
            (prefix_root, _prefix_root_resolved),
        ):
            if root is None:
                continue
            try:
                p.relative_to(root)
                under_root = root
                break
            except ValueError:
                try:
                    p.resolve().relative_to(root_resolved)
                    under_root = root
                    break
                except ValueError:
                    continue
        if under_root is None:
            _log(f"  SKIP: path traversal blocked - {abs_str}")
            continue
        safe_targets.append(p)
        # Collect parent dirs for pruning (stop at the matched root)
        parent = p.parent
        while parent != under_root:
            try:
                parent.relative_to(under_root)
            except ValueError:
                break
            dirs_to_prune.add(parent)
            parent = parent.parent

    import stat as _stat

    def _unlink_one(p: Path) -> int:
        try:
            st = os.lstat(p)
        except OSError:
            return 0
        if _stat.S_ISLNK(st.st_mode) or _stat.S_ISREG(st.st_mode):
            try:
                os.unlink(p)
                return 1
            except OSError:
                return 0
        return 0

    if safe_targets:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_deploy_workers()) as pool:
            for n in pool.map(_unlink_one, safe_targets):
                removed += n

    # Restore backed-up vanilla files
    _restore_backup_dir(backup_dir, game_root, _log)
    if prefix_root is not None:
        _restore_backup_dir(prefix_backup_dir, prefix_root, _log)

    # Prune empty subdirectories deepest-first; never touch either root itself
    stop_dirs = {game_root}
    if prefix_root is not None:
        stop_dirs.add(prefix_root)
    _prune_empty_dirs(dirs_to_prune, stop_dirs=stop_dirs)

    log_path.unlink()
    _log(f"  Custom rules restore: removed {removed} file(s).")
    return removed


def mods_matching_root_rules(
    mod_files: dict[str, list[str]],
    rules: list["CustomRule"],
) -> set[str]:
    """Return the set of mod names that own at least one file matched by a
    rule whose ``dest`` is empty (i.e. routes to the game root).

    ``mod_files`` maps mod name -> list of relative file paths (any casing,
    forward or back slashes).  Only rules with ``dest == ""`` and
    ``to_prefix is False`` are considered.
    """
    root_rules = [r for r in rules if r.dest == "" and not r.to_prefix]
    if not root_rules or not mod_files:
        return set()
    norm = [_normalise_rule(r) for r in root_rules]

    # Global pre-filter: a file CANNOT match any root rule unless at least one
    # of these is true. Cheap O(1) per file; lets us skip the rule loop entirely
    # for the overwhelming majority of files (textures, meshes, etc.) in a
    # typical 70k-file modlist. Reduces this from ~935 ms to ~100 ms on Skyrim.
    any_folders_simple: set[str] = set()    # folder names with no "/"
    any_folders_path:   list[str] = []      # folder names containing "/"
    any_exts:           set[str] = set()
    exact_names:        set[str] = set()    # plain filenames → O(1) set lookup
    glob_pats:          list[str] = []      # wildcard filenames → one regex
    for _rule, folders, exts, filenames in norm:
        for f in folders:
            if "/" in f:
                any_folders_path.append(f)
            else:
                any_folders_simple.add(f)
        any_exts.update(exts)
        # Split filenames once here: calling _name_match per FILE re-scans
        # every pattern for glob chars and fnmatches them one-by-one (~1.2 s
        # on a 105k-file Skyrim modlist with the ~21 ENB/SKSE root-rule
        # names). An exact-name set + one combined compiled regex is ~10×
        # faster; the per-rule loop below still confirms real matches.
        for n in filenames:
            if any(c in n for c in "*?["):
                glob_pats.append(fnmatch.translate(n))
            else:
                exact_names.add(n)
    glob_re = re.compile("|".join(glob_pats)) if glob_pats else None
    exts_tuple = tuple(any_exts)
    # Build a quick suffix-style match: pre-compute the set of file extensions
    # we care about for the cheap extension check below. _ext_match handles
    # double-extensions (.tar.gz) but for the pre-filter a simple endswith is
    # safe (over-accepts → rule loop confirms).

    hits: set[str] = set()
    for mod_name, files in mod_files.items():
        if mod_name in hits:
            continue
        for rel in files:
            # Cheap normalisation: avoid replace() if no backslash present
            if "\\" in rel:
                rel_lower = rel.replace("\\", "/").lower()
            else:
                rel_lower = rel.lower()

            # Global pre-filter - skip the rule loop unless this file COULD
            # match something.  Splitting once and reusing across the per-rule
            # match is also cheaper than splitting inside _match_single_rule
            # for every rule.
            parts = rel_lower.split("/")
            filename = parts[-1]
            could_match = False
            # Folder check: any path segment (excluding filename) is in the
            # simple folder set, OR any path-style folder appears anywhere.
            if any_folders_simple:
                for seg in parts[:-1]:
                    if seg in any_folders_simple:
                        could_match = True
                        break
            if not could_match and any_folders_path:
                for f in any_folders_path:
                    if (f + "/") in rel_lower or rel_lower.endswith("/" + f) or rel_lower == f or rel_lower.endswith(f):
                        could_match = True
                        break
            if not could_match and exts_tuple and filename.endswith(exts_tuple):
                # Cheap extension check - over-accepts, real check inside the rule.
                could_match = True
            if not could_match and (filename in exact_names
                                    or (glob_re is not None
                                        and glob_re.match(filename))):
                could_match = True
            if not could_match:
                continue

            for rule, folders, exts, filenames in norm:
                if _match_single_rule(rel_lower, rule, folders, exts, filenames) is not None:
                    hits.add(mod_name)
                    break
            if mod_name in hits:
                break
    return hits


__all__ = [
    "_CUSTOM_RULES_LOG_NAME",
    "_CUSTOM_RULES_BACKUP_DIR",
    "_CUSTOM_RULES_PREFIX_BACKUP_DIR",
    "deploy_custom_rules",
    "restore_custom_rules",
    "mods_matching_root_rules",
]
