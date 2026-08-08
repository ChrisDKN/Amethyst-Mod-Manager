"""
nexus_file_requirements.py
File-level requirements (dependencies) via the Nexus v3 REST API.

Nexus's newer "file requirements" system lets authors declare dependencies
per uploaded file, with version ranges and OR-alternatives. These do NOT
appear in the mod-level GraphQL ``modRequirements`` that
``nexus_requirements.py`` reads, so a mod whose dependencies are declared
only file-level (increasingly common since the feature went live) would
otherwise never be flagged.

This module resolves the materialized dependency candidates for the
installed files and reduces them to per-mod missing requirements that merge
into the existing meta.ini ``missingRequirements`` pipeline.

Satisfaction policy (v1): a dependency definition is satisfied when ANY of
its OR-alternative candidate mods is installed at all - an installed but
out-of-range version counts as satisfied, because the meta format and the
missing-requirements UI can only express missing *mods*. Version-range
strictness is future work.

The v3 API is marked Experimental by Nexus; any failure here degrades to
"no file-level results" and never disturbs the mod-level check.
Kill switch: ``AMM_FILE_REQS=0``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable

from Nexus.nexus_api import FileDependencyCandidate, NexusModRequirement
from Nexus.nexus_requirements import (
    GameScope,
    _alternative_satisfied_for_game,
    _is_external_for_game,
)

if TYPE_CHECKING:
    from Nexus.nexus_api import NexusAPI
    from Nexus.nexus_meta import NexusModMeta

FILE_REQS_ENV = "AMM_FILE_REQS"


def file_reqs_enabled() -> bool:
    """False when the AMM_FILE_REQS=0 kill switch is set."""
    return os.environ.get(FILE_REQS_ENV) != "0"


def make_version_uid(numeric_game_id: int, file_id: int) -> int:
    """Composite v3 file-version UID: (game id << 32) | game-scoped file id."""
    return (numeric_game_id << 32) | file_id


def resolve_missing_definitions(
    candidates: list[FileDependencyCandidate],
    installed_mod_ids: set[int],
    game_domain: str,
    external_set: set[tuple[GameScope, int]],
    alternatives_dict: dict[tuple[GameScope, int], set[int]],
) -> dict[int, list[FileDependencyCandidate]]:
    """Reduce candidate rows to unsatisfied dependency definitions.

    Returns {source_version_uid: [one representative candidate per
    unsatisfied definition]}. Pure function - unit-testable offline.

    A definition (all rows sharing (source_version_id, definition_id)) is
    satisfied/suppressed when any of its candidates' game-scoped mod ids is
    installed, is an external tool for this game, or has an installed
    alternative. Definitions with no valid published candidate are skipped
    (nothing installable to point at). Because satisfaction is recomputed
    from all OR-alternatives each run, installing any alternative clears the
    requirement on the next check even though only one representative was
    reported.
    """
    groups: dict[tuple[int, int], list[FileDependencyCandidate]] = {}
    for c in candidates:
        groups.setdefault((c.source_version_id, c.definition_id), []).append(c)

    out: dict[int, list[FileDependencyCandidate]] = {}
    seen_per_source: dict[int, set[int]] = {}
    for (source_uid, _def_id), rows in groups.items():
        satisfied = False
        for c in rows:
            gsid = c.game_scoped_mod_id
            if gsid <= 0:
                continue
            if (gsid in installed_mod_ids
                    or _is_external_for_game(game_domain, gsid, external_set)
                    or _alternative_satisfied_for_game(
                        game_domain, gsid, installed_mod_ids, alternatives_dict)):
                satisfied = True
                break
        if satisfied:
            continue

        valid = [c for c in rows
                 if c.game_scoped_mod_id > 0 and c.mod_status == "published"]
        if not valid:
            continue

        # Representative: prefer "main" files, then oldest chain position
        # (the canonical file), then newest version within it.
        rep = sorted(valid, key=lambda c: (c.category != "main",
                                           c.position, -c.version_id))[0]
        seen = seen_per_source.setdefault(source_uid, set())
        if rep.game_scoped_mod_id in seen:
            continue
        seen.add(rep.game_scoped_mod_id)
        out.setdefault(source_uid, []).append(rep)
    return out


def resolve_all_definitions(
    candidates: list[FileDependencyCandidate],
) -> dict[int, list[FileDependencyCandidate]]:
    """Reduce candidate rows to ALL dependency definitions, regardless of
    install state (unlike ``resolve_missing_definitions`` which drops satisfied
    ones). Powers the View Requirements full list, where installed dependencies
    must still be shown.

    Returns {source_version_uid: [one representative candidate per definition]}.
    A definition (rows sharing (source_version_id, definition_id)) is
    represented by its preferred published candidate; definitions with no valid
    published candidate are skipped (nothing to point at). OR-alternatives
    collapse to a single representative - the full list can only express one
    mod per definition anyway.
    """
    groups: dict[tuple[int, int], list[FileDependencyCandidate]] = {}
    for c in candidates:
        groups.setdefault((c.source_version_id, c.definition_id), []).append(c)

    out: dict[int, list[FileDependencyCandidate]] = {}
    seen_per_source: dict[int, set[int]] = {}
    for (source_uid, _def_id), rows in groups.items():
        valid = [c for c in rows
                 if c.game_scoped_mod_id > 0 and c.mod_status == "published"]
        if not valid:
            continue
        rep = sorted(valid, key=lambda c: (c.category != "main",
                                           c.position, -c.version_id))[0]
        seen = seen_per_source.setdefault(source_uid, set())
        if rep.game_scoped_mod_id in seen:
            continue
        seen.add(rep.game_scoped_mod_id)
        out.setdefault(source_uid, []).append(rep)
    return out


def compute_file_level_all(
    api: "NexusAPI",
    source_metas: list["NexusModMeta"],
    game_domain: str,
    log: Callable[[str], None] = lambda m: None,
) -> dict[int, list[NexusModRequirement]]:
    """ALL file-level requirements for the given metas (installed or not).

    Same batch/resolution pipeline as ``compute_file_level_missing`` but with
    no satisfaction filtering - it returns every file-level dependency so the
    View Requirements full list can show installed deps normally and missing
    ones dimmed. Returns {source mod_id: [NexusModRequirement, ...]}, or {} on
    the kill switch / no usable file id / any v3 failure.
    """
    if not file_reqs_enabled():
        return {}
    try:
        game_id_cache: dict[str, int] = {}
        uid_sources: dict[int, set[int]] = {}
        uid_domain: dict[int, str] = {}
        for meta in source_metas:
            if meta.mod_id <= 0 or meta.file_id <= 0:
                continue
            domain = (meta.game_domain or game_domain).strip().lower()
            if not domain:
                continue
            if domain not in game_id_cache:
                game_id_cache[domain] = api._resolve_game_id(domain)
            gid = game_id_cache[domain]
            if gid <= 0:
                continue
            uid = make_version_uid(gid, meta.file_id)
            uid_sources.setdefault(uid, set()).add(meta.mod_id)
            uid_domain[uid] = domain
        if not uid_sources:
            return {}

        candidates = api.get_file_dependency_candidates_batch(list(uid_sources))
        if not candidates:
            return {}

        all_by_uid = resolve_all_definitions(candidates)
        if not all_by_uid:
            return {}

        rep_uids = {c.mod_uid for reps in all_by_uid.values() for c in reps}
        mod_details = api.get_mods_batch(sorted(rep_uids))

        result: dict[int, list[NexusModRequirement]] = {}
        for uid, reps in all_by_uid.items():
            for source_mod_id in uid_sources.get(uid, ()):
                bucket = result.setdefault(source_mod_id, [])
                have = {r.mod_id for r in bucket}
                for c in reps:
                    gsid = c.game_scoped_mod_id
                    if gsid in have:
                        continue
                    have.add(gsid)
                    detail = mod_details.get(c.mod_uid) or {}
                    bucket.append(NexusModRequirement(
                        mod_id=gsid,
                        mod_name=str(detail.get("name") or ""),
                        game_domain=uid_domain.get(uid, game_domain),
                    ))
        return result
    except Exception as exc:
        log(f"File-level requirements (all) check skipped ({exc})")
        return {}


def compute_file_level_missing(
    api: "NexusAPI",
    source_metas: list["NexusModMeta"],
    installed_mod_ids: set[int],
    game_domain: str,
    external_set: set[tuple[GameScope, int]],
    alternatives_dict: dict[tuple[GameScope, int], set[int]],
    log: Callable[[str], None] = lambda m: None,
) -> dict[int, list[NexusModRequirement]]:
    """File-level missing requirements for the given installed metas.

    Returns {source mod_id: [missing NexusModRequirement, ...]} suitable for
    merging with the mod-level results. Returns {} when the kill switch is
    set, no meta has a usable file id, or the v3 API fails in any way.
    """
    if not file_reqs_enabled():
        return {}
    try:
        # Map installed file version UIDs back to their source mod ids.
        # Cross-domain mods keep their own domain (numeric game id + filter
        # scope), falling back to the active game's domain.
        game_id_cache: dict[str, int] = {}
        uid_sources: dict[int, set[int]] = {}
        uid_domain: dict[int, str] = {}
        for meta in source_metas:
            if meta.mod_id <= 0 or meta.file_id <= 0:
                continue
            domain = (meta.game_domain or game_domain).strip().lower()
            if not domain:
                continue
            if domain not in game_id_cache:
                game_id_cache[domain] = api._resolve_game_id(domain)
            gid = game_id_cache[domain]
            if gid <= 0:
                continue
            uid = make_version_uid(gid, meta.file_id)
            uid_sources.setdefault(uid, set()).add(meta.mod_id)
            uid_domain[uid] = domain
        if not uid_sources:
            return {}

        candidates = api.get_file_dependency_candidates_batch(list(uid_sources))
        if not candidates:
            return {}

        # Resolve per source domain so game-scoped filter rules apply correctly.
        missing_by_uid: dict[int, list[FileDependencyCandidate]] = {}
        by_domain: dict[str, list[FileDependencyCandidate]] = {}
        for c in candidates:
            dom = uid_domain.get(c.source_version_id, game_domain)
            by_domain.setdefault(dom, []).append(c)
        for dom, rows in by_domain.items():
            missing_by_uid.update(resolve_missing_definitions(
                rows, installed_mod_ids, dom, external_set, alternatives_dict))
        if not missing_by_uid:
            return {}

        rep_uids = {c.mod_uid for reps in missing_by_uid.values() for c in reps}
        mod_details = api.get_mods_batch(sorted(rep_uids))

        result: dict[int, list[NexusModRequirement]] = {}
        for uid, reps in missing_by_uid.items():
            for source_mod_id in uid_sources.get(uid, ()):
                bucket = result.setdefault(source_mod_id, [])
                have = {r.mod_id for r in bucket}
                for c in reps:
                    gsid = c.game_scoped_mod_id
                    if gsid in have:
                        continue
                    have.add(gsid)
                    detail = mod_details.get(c.mod_uid) or {}
                    bucket.append(NexusModRequirement(
                        mod_id=gsid,
                        mod_name=str(detail.get("name") or ""),
                        game_domain=uid_domain.get(uid, game_domain),
                    ))
        return result
    except Exception as exc:
        log(f"File-level requirements check skipped ({exc})")
        return {}
