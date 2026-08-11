"""
nexus_requirements.py
Check installed Nexus mods for missing requirements (dependencies).

Workflow:
  1. Scan ``meta.ini`` files in the staging root to find mods with Nexus IDs.
  2. Build a set of all installed Nexus mod IDs.
  3. For each installed mod, query the Nexus GraphQL API for its listed
     requirements.
  4. Cross-reference required mod IDs against the installed set.
  5. Return a mapping of mod names → list of missing requirements.

Usage::

    from Nexus.nexus_requirements import check_missing_requirements

    missing = check_missing_requirements(api, staging_root, "skyrimspecialedition")
    for mod_name, reqs in missing.items():
        print(f"{mod_name} is missing: {[r.mod_name for r in reqs]}")
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

import requests

# Game scope: None = apply to all games; str = Nexus game domain (e.g. "fallout4", "skyrimspecialedition")
GameScope = Optional[str]

from Nexus.nexus_api import NexusAPI, NexusModRequirement, NexusModUpdateInfo
from Nexus.nexus_meta import (
    NexusModMeta, normalise_game_domain, scan_installed_mods, write_meta)
from Utils.config_paths import get_requirement_external_tool_mod_ids_path
from Utils.ca_bundle import resolve_ca_bundle

ProgressCallback = Callable[[str], None]

# Remote list of mod IDs to treat as external tools (script extenders, xEdit, etc.).
# Fetched on each requirement check; new IDs are merged into the local cache.
REQUIREMENT_FILTER_URL = (
    "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/Nexus/updatefilter.txt"
)
_FETCH_TIMEOUT = 10


@dataclass
class MissingRequirementInfo:
    """Info about a mod that has missing requirements."""
    mod_name: str                                     # local folder name
    mod_id: int
    missing: list[NexusModRequirement] = field(default_factory=list)


def _parse_filter_text(
    text: str,
) -> tuple[
    set[tuple[GameScope, int]],
    dict[tuple[GameScope, int], set[int]],
    dict[tuple[GameScope, int], tuple[int, str]],
]:
    """
    Parse filter file: one entry per line. Skip empty lines and # comments.

    Optional game prefix: "game_domain:..." applies the rule only to that game.
    No prefix applies to all games (backward compatible).

    Lines:
      - "12345"              -> external for all games (never flag 12345).
      - "fallout4:42147"     -> external only for Fallout 4 (F4SE).
      - "33746#92109"        -> alternative for all games.
      - "skyrimspecialedition:33746#92109" -> alternative only for Skyrim SE.
      - "60033>133232|Pandora Behaviour Engine+" -> substitution: wherever a mod
        requires 60033 (Nemesis), report 133232 (Pandora) instead. The "|name"
        half is optional and only supplies the display name written to meta.ini.

    Returns (external_set, alternatives_dict, substitutions_dict).
    external_set contains (game_scope, mod_id); alternatives and substitutions
    are keyed on (game_scope, req_id).
    """
    external: set[tuple[GameScope, int]] = set()
    alternatives: dict[tuple[GameScope, int], set[int]] = {}
    substitutions: dict[tuple[GameScope, int], tuple[int, str]] = {}
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        # Optional "game_domain:" prefix (first colon only)
        scope: GameScope = None
        rest = raw
        if ":" in raw:
            before, _, after = raw.partition(":")
            if before.strip() and after.strip():
                scope = before.strip().lower()
                rest = after.strip()
        if ">" in rest:
            src_part, _, dst_part = rest.partition(">")
            dst_id_part, _, dst_name = dst_part.partition("|")
            try:
                src_id = int(src_part.strip())
                dst_id = int(dst_id_part.strip())
            except ValueError:
                continue
            if src_id <= 0 or dst_id <= 0 or src_id == dst_id:
                continue
            substitutions[(scope, src_id)] = (dst_id, dst_name.strip())
        elif "#" in rest:
            part0, _, part1 = rest.partition("#")
            try:
                req_id = int(part0.strip())
                alt_id = int(part1.strip())
                key = (scope, req_id)
                alternatives.setdefault(key, set()).add(alt_id)
            except ValueError:
                continue
        else:
            try:
                external.add((scope, int(rest)))
            except ValueError:
                continue
    return external, alternatives, substitutions


def _filter_content_lines(text: str) -> list[str]:
    """Return list of stripped non-empty, non-comment lines (for merge comparison)."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def bundled_filter_text() -> str:
    """The copy of ``updatefilter.txt`` shipped next to this module.

    The remote list is fetched from the repo's ``main`` branch, so without this
    a rule added to the packaged file would do nothing until it was merged and
    re-downloaded - the app would ignore its own bundled list.
    """
    try:
        return (Path(__file__).resolve().parent / "updatefilter.txt").read_text(
            encoding="utf-8")
    except OSError:
        return ""


def _load_requirement_filter() -> tuple[
    set[tuple[GameScope, int]],
    dict[tuple[GameScope, int], set[int]],
    dict[tuple[GameScope, int], tuple[int, str]],
]:
    """
    Load external-tool IDs, alternatives and substitutions from the three
    sources: the list bundled with the app, the local cache, and GitHub.

    Returns (external_set, alternatives_dict, substitutions_dict) with
    game-scoped keys so e.g. 42147 can be external for Fallout 4 only, not for
    Skyrim. No prefix = all games. New bundled/remote lines are appended to the
    cache, which keeps the user's own additions.
    """
    cache_path = get_requirement_external_tool_mod_ids_path()
    cache_text = ""
    if cache_path.exists():
        try:
            cache_text = cache_path.read_text(encoding="utf-8")
        except OSError:
            pass

    cache_external, cache_alternatives, cache_subs = _parse_filter_text(cache_text)
    cache_line_set = set(_filter_content_lines(cache_text))

    bundled_text = bundled_filter_text()
    bundled_external, bundled_alternatives, bundled_subs = _parse_filter_text(bundled_text)

    remote_text = ""
    try:
        resp = requests.get(REQUIREMENT_FILTER_URL, timeout=_FETCH_TIMEOUT, verify=resolve_ca_bundle() or True)
        if resp.ok:
            remote_text = resp.text
    except Exception:
        pass

    remote_external, remote_alternatives, remote_subs = _parse_filter_text(remote_text)

    # Merge: union of externals; for alternatives, union sets per key
    merged_external = cache_external | bundled_external | remote_external
    merged_alternatives: dict[tuple[GameScope, int], set[int]] = {}
    for key in set(cache_alternatives) | set(bundled_alternatives) | set(remote_alternatives):
        merged_alternatives[key] = (cache_alternatives.get(key, set())
                                    | bundled_alternatives.get(key, set())
                                    | remote_alternatives.get(key, set()))
    # Substitutions can't be unioned (one target per requirement) - the local
    # cache wins, so a hand-edited redirect isn't overridden by the shipped or
    # remote list.
    merged_subs = {**bundled_subs, **remote_subs, **cache_subs}

    # Append new bundled/remote lines to the cache (keeps user additions).
    new_lines = [line for line in
                 _filter_content_lines(bundled_text) + _filter_content_lines(remote_text)
                 if line not in cache_line_set]
    if new_lines:
        try:
            with cache_path.open("a", encoding="utf-8") as f:
                if cache_path.stat().st_size > 0:
                    f.write("\n")
                for line in dict.fromkeys(new_lines):
                    f.write(line + "\n")
        except OSError:
            pass

    return merged_external, merged_alternatives, merged_subs


# (mtime_ns, size) → flattened rules, so the UI read paths can consult the
# substitution list per mod without re-parsing the file (or hitting GitHub).
_SUBS_CACHE: tuple[tuple[int, int], dict[tuple[GameScope, int], tuple[int, str]]] | None = None


def load_requirement_substitutions(game_domain: str = "") -> dict[int, tuple[int, str]]:
    """{req_id: (replacement_id, replacement_name)} for *game_domain*, read from
    the bundled list + the local filter cache - no network, memoised on the
    cache file's mtime/size.

    For UI paths (modlist flags, Missing Requirements panel) that need to honour
    a substitution against meta.ini values stamped before the rule existed. The
    checkers use ``_load_requirement_filter`` instead, which also refreshes the
    cache from GitHub.
    """
    global _SUBS_CACHE
    path = get_requirement_external_tool_mod_ids_path()
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = (0, 0)        # no cache yet - the bundled rules still apply
    if _SUBS_CACHE is None or _SUBS_CACHE[0] != key:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        # Same precedence as the checkers: a cache entry (user edit or a line
        # appended from the remote list) wins over the shipped one.
        _SUBS_CACHE = (key, {**_parse_filter_text(bundled_filter_text())[2],
                             **_parse_filter_text(text)[2]})
    return _substitutions_for_game(game_domain, _SUBS_CACHE[1])


def _is_external_for_game(
    game_domain: str,
    mod_id: int,
    external_set: set[tuple[GameScope, int]],
) -> bool:
    """True if mod_id is treated as external (don't flag) for this game."""
    scope: GameScope = (game_domain.strip().lower() or None) if game_domain else None
    return (None, mod_id) in external_set or (scope, mod_id) in external_set


def _alternative_satisfied_for_game(
    game_domain: str,
    req_id: int,
    installed_mod_ids: set[int],
    alternatives_dict: dict[tuple[GameScope, int], set[int]],
) -> bool:
    """True if requirement req_id is satisfied by an alternative for this game."""
    scope: GameScope = (game_domain.strip().lower() or None) if game_domain else None
    for key in ((None, req_id), (scope, req_id)):
        if key in alternatives_dict and alternatives_dict[key] & installed_mod_ids:
            return True
    return False


def _substitutions_for_game(
    game_domain: str,
    substitutions: dict[tuple[GameScope, int], tuple[int, str]],
) -> dict[int, tuple[int, str]]:
    """Flatten the game-scoped substitution rules for one game: {req_id:
    (replacement_id, replacement_name)}. A game-scoped rule beats an all-games
    one for the same requirement."""
    scope: GameScope = (game_domain.strip().lower() or None) if game_domain else None
    flat: dict[int, tuple[int, str]] = {
        src: val for (sc, src), val in substitutions.items() if sc is None}
    if scope is not None:
        flat.update({src: val for (sc, src), val in substitutions.items()
                     if sc == scope})
    return flat


def substitute_requirements(
    reqs: list[NexusModRequirement],
    game_domain: str,
    substitutions: dict[tuple[GameScope, int], tuple[int, str]],
) -> list[NexusModRequirement]:
    """Rewrite requirements that have a replacement rule (e.g. Nemesis 60033 →
    Pandora 133232, since Nemesis doesn't run under Proton).

    Applied to the whole requirement pool before the installed/external filters,
    so the replacement is what gets satisfaction-checked, stored in meta.ini and
    offered for install. Substitution is single-hop (a rule whose target is
    itself a rule's source is not followed) and duplicates collapse, so a mod
    requiring both Nemesis and Pandora ends up with one Pandora entry.
    """
    flat = _substitutions_for_game(game_domain, substitutions)
    if not flat:
        return list(reqs)
    out: list[NexusModRequirement] = []
    seen: set[int] = set()
    for req in reqs:
        repl = flat.get(req.mod_id) if not req.is_external else None
        if repl is not None and req.mod_id > 0:
            new_id, new_name = repl
            # The old url points at the replaced mod's page - drop it so the
            # panel rebuilds it from the new id.
            req = replace(req, mod_id=new_id,
                          mod_name=new_name or req.mod_name or f"Mod {new_id}",
                          url="")
        if req.mod_id > 0:
            if req.mod_id in seen:
                continue
            seen.add(req.mod_id)
        out.append(req)
    return out


def _merge_reqs(
    mod_level: list[NexusModRequirement],
    file_level: list[NexusModRequirement],
) -> list[NexusModRequirement]:
    """Merge file-level missing requirements into mod-level ones (dedupe by mod_id)."""
    seen = {r.mod_id for r in mod_level}
    return mod_level + [r for r in file_level if r.mod_id not in seen]


def check_missing_requirements(
    api: NexusAPI,
    staging_root: Path,
    game_domain: str = "",
    progress_cb: Optional[ProgressCallback] = None,
    save_results: bool = True,
    enabled_only: Optional[set] = None,
) -> list[MissingRequirementInfo]:
    """Check requirements in separate Nexus-domain batches."""
    _log = progress_cb or (lambda m: None)
    installed = scan_installed_mods(staging_root)
    selected = [m for m in installed
                if enabled_only is None or m.mod_name in enabled_only]
    fallback = normalise_game_domain(game_domain)
    by_domain: dict[str, set[str]] = {}
    for meta in selected:
        domain = normalise_game_domain(meta.game_domain) or fallback
        if domain:
            by_domain.setdefault(domain, set()).add(meta.mod_name)
    if not by_domain:
        _log("No Nexus-sourced mods with a game domain found.")
        return []

    results: list[MissingRequirementInfo] = []
    for domain, names in by_domain.items():
        results.extend(_check_missing_requirements_one_domain(
            api, staging_root, game_domain=domain, progress_cb=progress_cb,
            save_results=save_results, enabled_only=names))
    return results


def _check_missing_requirements_one_domain(
    api: NexusAPI,
    staging_root: Path,
    game_domain: str = "",
    progress_cb: Optional[ProgressCallback] = None,
    save_results: bool = True,
    enabled_only: Optional[set] = None,
) -> list[MissingRequirementInfo]:
    """
    Check all Nexus-sourced mods under *staging_root* for missing requirements.

    For every mod that has a ``mod_id`` in its ``meta.ini``, we query the
    Nexus GraphQL API for that mod's listed requirements.  Any required
    mod ID not found among installed mods is flagged as missing.

    Parameters
    ----------
    api : NexusAPI
        Authenticated API client.
    staging_root : Path
        Root of the mod staging area (``game.get_mod_staging_path()``).
    game_domain : str
        The Nexus API game domain (e.g. ``"skyrimspecialedition"``).
    progress_cb : callable, optional
        Called with status strings for UI feedback.
    save_results : bool
        If True, write ``missingRequirements`` back to each mod's
        ``meta.ini`` so the UI can show warning flags without re-checking.

    Returns
    -------
    list[MissingRequirementInfo]
        Mods that have at least one missing requirement.
    """
    _log = progress_cb or (lambda m: None)

    # 1. Scan installed mods with Nexus metadata
    installed = scan_installed_mods(staging_root)
    if not installed:
        _log("No Nexus-sourced mods found.")
        return []

    wanted_domain = normalise_game_domain(game_domain)
    installed = [m for m in installed
                 if (normalise_game_domain(m.game_domain) or wanted_domain)
                 == wanted_domain]

    all_installed = installed
    if enabled_only is not None:
        installed = [m for m in installed if m.mod_name in enabled_only]

    checkable = [m for m in installed if m.mod_id > 0]
    if not checkable:
        _log("No mods with Nexus IDs to check requirements for.")
        return []

    # Determine the domain to use
    if not game_domain:
        game_domain = checkable[0].game_domain.strip().lower()
    if not game_domain:
        _log("No game domain available - cannot check requirements.")
        return []

    # 2. Build set of all installed Nexus mod IDs
    installed_mod_ids: set[int] = {
        m.mod_id for m in all_installed if m.mod_id > 0}

    # External tools (never flag), requirement alternatives and requirement
    # substitutions; all three can be game-scoped
    external_set, alternatives_dict, substitutions = _load_requirement_filter()

    # Deduplicate by mod_id
    by_mod_id: dict[int, list[NexusModMeta]] = {}
    for meta in checkable:
        by_mod_id.setdefault(meta.mod_id, []).append(meta)

    _log(f"Checking requirements for {len(by_mod_id)} Nexus mod(s)...")

    # File-level requirements (v3 API) for all checkable mods, in one batch.
    # {} on kill switch or any v3 failure - mod-level results are unaffected.
    from Nexus.nexus_file_requirements import compute_file_level_missing
    file_missing = compute_file_level_missing(
        api, checkable, installed_mod_ids, game_domain,
        external_set, alternatives_dict, log=_log)

    results: list[MissingRequirementInfo] = []
    checked = 0
    total = len(by_mod_id)

    for mod_id, metas in by_mod_id.items():
        checked += 1
        representative = metas[0]

        # 3. Query requirements via GraphQL
        try:
            reqs = api.get_mod_requirements(game_domain, mod_id)
        except Exception as exc:
            _log(f"  [{checked}/{total}] {representative.mod_name}: "
                 f"could not fetch requirements ({exc})")
            continue

        # File-level (v3) missing requirements join the pool before filtering so
        # substitutions and the installed/external filters see every requirement.
        reqs = _merge_reqs(list(reqs), file_missing.get(mod_id, []))
        # Replacement rules (e.g. Nemesis → Pandora) applied first: the
        # replacement is what we satisfaction-check and store.
        reqs = substitute_requirements(reqs, game_domain, substitutions)

        # 4. Filter to Nexus-hosted requirements whose mod_id is not installed
        missing: list[NexusModRequirement] = []
        for req in reqs:
            if req.is_external:
                # External requirements (non-Nexus) - skip, we can't track them
                continue
            if req.mod_id <= 0:
                continue
            if _is_external_for_game(game_domain, req.mod_id, external_set):
                # External tools (script extenders, xEdit) - installed to game folder, not mod list
                continue
            if _alternative_satisfied_for_game(game_domain, req.mod_id, installed_mod_ids, alternatives_dict):
                # e.g. 33746#92109: requirement 33746 satisfied if 92109 (Open Animation Replacer) installed
                continue
            if req.mod_id not in installed_mod_ids:
                missing.append(req)

        # 5. Record results for each local mod entry under this mod_id
        for meta in metas:
            if missing:
                info = MissingRequirementInfo(
                    mod_name=meta.mod_name,
                    mod_id=mod_id,
                    missing=missing,
                )
                results.append(info)
                names = ", ".join(r.mod_name for r in missing[:3])
                suffix = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
                _log(f"  ⚠ {meta.mod_name}: missing {names}{suffix}")

                if save_results:
                    # Store as comma-separated "modId:name" pairs
                    meta.missing_requirements = ";".join(
                        f"{r.mod_id}:{r.mod_name}" for r in missing
                    )
                    meta_path = staging_root / meta.mod_name / "meta.ini"
                    write_meta(meta_path, meta)
            else:
                # All requirements satisfied - clear flag
                if save_results and meta.missing_requirements:
                    meta.missing_requirements = ""
                    meta_path = staging_root / meta.mod_name / "meta.ini"
                    write_meta(meta_path, meta)

        if checked % 10 == 0:
            _log(f"  Checked {checked}/{total} mods...")

    _log(f"Requirements check complete: {len(results)} mod(s) with missing dependencies.")
    return results


def check_requirements_from_gql(
    gql_info: dict[int, NexusModUpdateInfo],
    all_installed: list,
    game_domain: str = "",
    staging_root: Path = Path(),
    progress_cb: Optional[ProgressCallback] = None,
    save_results: bool = True,
    enabled_only: Optional[set] = None,
    api: Optional[NexusAPI] = None,
) -> list[MissingRequirementInfo]:
    """
    Check for missing requirements using pre-fetched GraphQL data.

    Unlike ``check_missing_requirements``, this function reads mod-level
    requirements from *gql_info* which was already retrieved by the update
    checker's batch GraphQL call, so both checks share a single set of
    GraphQL requests.  When *api* is provided, file-level requirements are
    additionally fetched from the v3 API (one or two batched calls) and
    merged in.

    Parameters
    ----------
    gql_info :
        Mapping of mod_id → NexusModUpdateInfo as returned by
        ``NexusAPI.graphql_mod_update_info_batch``.  Each entry's
        ``requirements`` list is used directly.
    all_installed :
        Full list of all installed NexusModMeta objects (used to build the
        set of installed mod IDs for dependency resolution - includes both
        enabled and disabled mods).
    game_domain :
        Nexus game domain string (e.g. ``"skyrimspecialedition"``).
    staging_root :
        Root of the mod staging area.
    progress_cb :
        Called with status strings for UI feedback.
    save_results :
        If True, write ``missingRequirements`` back to each mod's
        ``meta.ini``.
    enabled_only :
        When provided, only mods whose folder name is in this set are
        checked.  All installed mod IDs are still used for dependency
        resolution regardless of this filter.
    api :
        Optional authenticated API client.  When provided, file-level
        requirements (Nexus v3 API) are checked and merged with the
        mod-level ones; when None, only mod-level requirements are checked.

    Returns
    -------
    list[MissingRequirementInfo]
    """
    _log = progress_cb or (lambda m: None)

    if not gql_info:
        return []

    # Work within one Nexus game. The update checker calls this once per
    # domain, and the filter also protects direct callers from treating an
    # equal id in another game as an installed dependency.
    wanted_domain = normalise_game_domain(game_domain)
    domain_installed = [
        m for m in all_installed
        if (normalise_game_domain(getattr(m, "game_domain", ""))
            or wanted_domain) == wanted_domain
    ]

    # All same-domain installed IDs (including disabled) so disabled mods don't
    # trigger spurious "missing requirement" warnings.
    installed_mod_ids: set[int] = {
        m.mod_id for m in domain_installed if m.mod_id > 0}

    external_set, alternatives_dict, substitutions = _load_requirement_filter()

    # Build by_mod_id only for enabled mods (the ones we actually report on)
    checkable = [
        m for m in domain_installed
        if m.mod_id > 0 and (enabled_only is None or m.mod_name in enabled_only)
    ]
    by_mod_id: dict[int, list] = {}
    for meta in checkable:
        by_mod_id.setdefault(meta.mod_id, []).append(meta)

    # File-level requirements (v3 API) for all checkable mods, in one batch.
    # We fetch ALL of them (installed or not) so the View Requirements full list
    # can show installed file-level deps too, then derive the *missing* subset
    # locally - one batch of API calls instead of two. Only runs when the caller
    # provides an API client; {} on kill switch or any v3 failure - mod-level
    # results are unaffected.
    file_all: dict[int, list[NexusModRequirement]] = {}
    if api is not None:
        from Nexus.nexus_file_requirements import compute_file_level_all
        file_all = compute_file_level_all(api, checkable, game_domain, log=_log)

    results: list[MissingRequirementInfo] = []

    for mod_id, metas in by_mod_id.items():
        info = gql_info.get(mod_id)
        if info is None:
            # Not returned by GraphQL (hidden/deleted mod) - leave flags unchanged
            continue

        # Full requirement pool: mod-level (GraphQL) + file-level (v3), deduped,
        # then rewritten through the replacement rules (e.g. Nemesis → Pandora)
        # so both the missing list and the stored full list name the mod we
        # actually want installed.
        reqs = _merge_reqs(list(info.requirements), file_all.get(mod_id, []))
        reqs = substitute_requirements(reqs, game_domain, substitutions)

        missing: list[NexusModRequirement] = []
        for req in reqs:
            if req.is_external:
                continue
            if req.mod_id <= 0:
                continue
            if _is_external_for_game(game_domain, req.mod_id, external_set):
                continue
            if _alternative_satisfied_for_game(game_domain, req.mod_id, installed_mod_ids, alternatives_dict):
                continue
            if req.mod_id not in installed_mod_ids:
                missing.append(req)

        # Full requirements list (installed or not) - powers View Requirements.
        # ';' in names would corrupt the pair format, swap for ','.
        full_str = ";".join(
            f"{max(r.mod_id, 0)}:{(r.mod_name or '').replace(';', ',')}"
            for r in reqs
        )
        missing_str = ";".join(f"{r.mod_id}:{r.mod_name}" for r in missing)

        for meta in metas:
            if missing:
                info_result = MissingRequirementInfo(
                    mod_name=meta.mod_name,
                    mod_id=mod_id,
                    missing=missing,
                )
                results.append(info_result)
                names = ", ".join(r.mod_name for r in missing[:3])
                suffix = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
                _log(f"  ⚠ {meta.mod_name}: missing {names}{suffix}")
            if save_results and (meta.missing_requirements != missing_str
                                 or meta.nexus_requirements != full_str):
                meta.missing_requirements = missing_str
                meta.nexus_requirements = full_str
                write_meta(staging_root / meta.mod_name / "meta.ini", meta)

    _log(f"Requirements check complete: {len(results)} mod(s) with missing dependencies.")
    return results
