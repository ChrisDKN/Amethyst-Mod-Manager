"""
nexus_update_checker.py
Check installed Nexus mods for available updates.

Workflow:
  1. Scan ``meta.ini`` files in the staging root to find mods with Nexus IDs.
  2. For each mod with a stored ``file_id``, fetch the mod's file list from
     the API and compare against the latest MAIN file.
  3. Return a list of mods that have newer files available.

Usage::

    from Nexus.nexus_update_checker import check_for_updates

    results = check_for_updates(api, staging_root, game_domain="fallout4", progress_cb=print)
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from Nexus.nexus_api import NexusAPI, NexusAPIError, NexusModUpdateInfo
from Nexus.nexus_meta import NexusModMeta, scan_installed_mods, write_meta
from Nexus.nexus_requirements import MissingRequirementInfo, check_requirements_from_gql

ProgressCallback = Callable[[str], None]


def _parse_install_date(meta: "NexusModMeta") -> Optional[datetime]:
    """
    Return the installation date for a mod, if determinable.

    Checks in priority order:
    1. ``installed`` field (our ISO format: ``2026-03-05T17:21:25``)
    2. MO2 date-version format: ``version = d2026.1.31.0``

    Returns a timezone-aware UTC datetime, or None if no date is available.
    """
    # 1. Our own installed timestamp
    if meta.installed:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(meta.installed.strip(), fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

    # 2. MO2 date-version: "d2026.1.31.0"
    version = (meta.version or "").strip()
    if version.lower().startswith("d"):
        # Strip leading 'd', then take the first 3 numeric parts (Y.M.D)
        parts = version[1:].split(".")
        try:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            return datetime(year, month, day, tzinfo=timezone.utc)
        except (IndexError, ValueError):
            pass

    return None


import re

_VERSION_PART_RE = re.compile(r"(\d+)|([a-z]+)", re.IGNORECASE)
# Tokens that, when present anywhere in the version string, mark it as a
# pre-release (so "1.2b" and "2.0-rc1" sort BEFORE "1.2" and "2.0").
_PRERELEASE_TOKENS = ("alpha", "beta", "pre", "dev", "rc")
# Splits a version at the first pre-release marker: a known token (optionally
# preceded by a separator, e.g. "-rc"), or a single trailing letter glued
# directly after a digit ("1.2b"). Everything from the marker onward is the
# pre-release tail, excluded from the core version tuple.
_PRERELEASE_SPLIT_RE = re.compile(
    r"(?:[.\-_+]?(?:" + "|".join(_PRERELEASE_TOKENS) + r"))|(?<=\d)(?=[a-z]$)",
    re.IGNORECASE,
)


def _alpha_key(segment: str) -> tuple[int, str]:
    """
    Odometer / base-26 sort key for a standalone revision letter run.

    A longer run is always greater, so once an author wraps past ``z`` the
    sequence keeps ascending: ``z < aa < ab < ba < zz < aaa``.  Equal-length
    runs fall back to lexical order.  (Plain lexical order would be wrong here -
    ``"aa" < "z"`` would make a wrap to ``.aa`` look like a downgrade.)
    """
    return (len(segment), segment)


def _segment_parts(
    text: str,
) -> tuple[tuple[tuple[int, tuple[int, str]], ...], bool]:
    """
    Split *text* on version separators into comparable
    ``(number, alpha_key)`` pairs, returning ``(parts, has_number)``.

    A purely numeric segment becomes ``(n, (0, ""))``; a purely alphabetic
    segment (a standalone revision letter, e.g. the ``x`` in ``10.0.x``)
    becomes ``(0, _alpha_key(letter))`` so it sorts AFTER the same-numbered
    bare release and follows odometer ordering past ``z``.  Keeping every
    position the same ``(int, (int, str))`` shape makes the tuples
    type-uniform and comparable.
    """
    parts: list[tuple[int, tuple[int, str]]] = []
    has_number = False
    for segment in re.split(r"[.\-_+]", text):
        if not segment:
            continue
        if segment.isalpha():
            parts.append((0, _alpha_key(segment)))
            continue
        for num, _alpha in _VERSION_PART_RE.findall(segment):
            if num:
                parts.append((int(num), (0, "")))
                has_number = True
    return tuple(parts), has_number


def _parse_version(v: str) -> tuple | None:
    """
    Parse a version string into a comparable tuple.

    Handles leading ``v``/``V``, mixed numeric/alpha segments, and common
    separators (``.``, ``-``, ``_``).  Two letter conventions are handled
    distinctly:

    * A letter glued directly after digits (``1.2b``) or a known token
      (``2.0-rc1``) is a PRE-RELEASE, ranked BELOW the bare release so
      ``1.2b < 1.2`` and ``2.0-rc1 < 2.0`` (and ``2.0-rc1 < 2.0-rc2``).
    * A letter that is its OWN dotted segment (``10.0.x``) is a sequential
      REVISION, ordered alphabetically ABOVE the bare number so
      ``10.0 < 10.0.v < 10.0.w < 10.0.x``.

    Returns ``None`` if no numeric component is found (unparseable).
    """
    if not v:
        return None
    s = v.strip().lower()
    if s.startswith("v"):
        s = s[1:].lstrip()
    if not s:
        return None

    # Split off any pre-release tail so its digits don't inflate the core
    # version (e.g. the "1" in "rc1" must not make "2.0-rc1" outrank "2.0").
    m = _PRERELEASE_SPLIT_RE.search(s)
    if m:
        core_text, pre_text, is_prerelease = s[: m.start()], s[m.start():], True
    else:
        core_text, pre_text, is_prerelease = s, "", False

    core, has_number = _segment_parts(core_text)
    if not has_number:
        return None

    # Tuple order: core version, then release_rank (1 release > 0 pre-release,
    # so "1.2" > "1.2b" but "1.3b" > "1.2"), then the pre-release ordinal as a
    # final tie-breaker so "2.0-rc2" > "2.0-rc1".
    pre_parts, _ = _segment_parts(pre_text)
    release_rank = 0 if is_prerelease else 1
    return (core, release_rank, pre_parts)


def _version_is_newer(latest: str, installed: str) -> bool:
    """
    Return True if *latest* represents a strictly newer version than *installed*.

    Both strings are parsed via ``_parse_version``.  If either is unparseable,
    falls back to a case-insensitive string inequality (only flags "newer" when
    the normalised strings differ - conservative).
    """
    lp = _parse_version(latest)
    ip = _parse_version(installed)
    if lp is not None and ip is not None:
        return lp > ip
    # Fallback: can't parse - only say "newer" if clearly different
    ln = (latest or "").strip().lower().lstrip("v").strip()
    iv = (installed or "").strip().lower().lstrip("v").strip()
    if not ln or not iv:
        return False
    return ln != iv


@dataclass
class UpdateInfo:
    """Information about an available update for a mod."""
    mod_name: str               # local folder name
    mod_id: int
    game_domain: str
    installed_file_id: int
    installed_version: str
    latest_file_id: int
    latest_version: str
    latest_file_name: str = ""
    nexus_url: str = ""


def check_for_updates(
    api: NexusAPI,
    staging_root: Path,
    game_domain: str = "",
    progress_cb: Optional[ProgressCallback] = None,
    save_results: bool = True,
    enabled_only: Optional[set] = None,
    max_workers: int = 10,
) -> tuple[list["UpdateInfo"], list["MissingRequirementInfo"]]:
    """
    Check all Nexus-sourced mods under *staging_root* for updates and missing
    requirements in a single pass.

    A single set of batched GraphQL requests fetches both ``updatedAt``
    (for update detection) and ``modRequirements`` (for dependency checking)
    simultaneously so both checks cost the same as running either one alone -
    and neither consumes the REST hourly rate limit.

    **Update detection:**
    For each mod, ``viewerUpdateAvailable`` is used when non-null (requires the
    user to have tracked/downloaded the mod via Nexus).  Otherwise ``updatedAt``
    is compared against the local install date.

    **Requirements check:**
    Each mod’s listed Nexus requirements are cross-referenced against the set of
    installed mod IDs.  External tools and configured alternatives are filtered
    out via the requirement filter file.

    **REST fallback (threaded):**
    A small number of mods fall back to ``GET /mods/{id}/files`` for an exact
    file-ID + version comparison when GraphQL returns no data for them.  These
    requests consume the hourly rate limit but are expected to be very few.

    Parameters
    ----------
    api : NexusAPI
        Authenticated API client.
    staging_root : Path
        Root of the mod staging area (e.g. ``game.get_mod_staging_path()``).
    game_domain : str
        The Nexus API game domain (e.g. ``"skyrimspecialedition"``).
    progress_cb : callable, optional
        Called with status strings for UI feedback.
    save_results : bool
        If True, write results back to each mod’s ``meta.ini``.
    max_workers : int
        Parallel threads for the REST fallback.  Default is 10.

    Returns
    -------
    tuple[list[UpdateInfo], list[MissingRequirementInfo]]
        ``(updates, missing_requirements)``
    """
    _log = progress_cb or (lambda m: None)

    # 1. Scan installed mods with Nexus metadata
    installed = scan_installed_mods(staging_root)
    if not installed:
        _log("No Nexus-sourced mods found.")
        return [], []

    # Keep a reference to ALL installed mods before applying the enabled_only
    # filter - requirements checking needs the full set to build installed_mod_ids.
    all_installed = installed

    if enabled_only is not None:
        installed = [m for m in installed if m.mod_name in enabled_only]

    # A mod is checkable if we have *any* way to compare it:
    # - meta.version (version compare, Path B)
    # - file_id     (REST lookup to get version, Path C)
    # - install date (date compare, Path D)
    checkable = [
        m for m in installed
        if (m.version and m.version.strip())
        or m.file_id > 0
        or _parse_install_date(m) is not None
    ]
    skipped = len(installed) - len(checkable)

    _log(f"Checking {len(checkable)} Nexus mod(s) for updates"
         f"{f' ({skipped} skipped - no version, file id, or install date)' if skipped else ''}...")

    if not checkable:
        _log("No checkable mods found (need a modid plus a version, file id, or install date).")
        return [], []

    # Determine the domain to use
    if not game_domain:
        game_domain = checkable[0].game_domain.strip().lower()
    if not game_domain:
        _log("No game domain available - cannot check updates.")
        return [], []

    # Deduplicate by mod_id
    by_mod_id: dict[int, list[NexusModMeta]] = {}
    for meta in checkable:
        by_mod_id.setdefault(meta.mod_id, []).append(meta)

    updates: list[UpdateInfo] = []

    # -----------------------------------------------------------------------
    # 2. Batch GraphQL call - fetch updatedAt + viewerUpdateAvailable for
    #    all mods in a handful of requests instead of one REST call per mod.
    #    This does not consume the REST hourly rate limit.
    # -----------------------------------------------------------------------
    _log("Fetching update info via GraphQL...")
    gql_ids = [(game_domain, mod_id) for mod_id in by_mod_id]
    gql_info: dict[int, NexusModUpdateInfo] = api.graphql_mod_update_info_batch(gql_ids)

    # Backfill meta.description / meta.uploaded_by from the GraphQL data
    # whenever they differ (or were empty). Free - same batch call already
    # fetched the data.
    if save_results:
        desc_updated = 0
        for mod_id, info in gql_info.items():
            summary = (info.summary or "").strip()
            uploader = (getattr(info, "uploaded_by", "") or "").strip()
            for meta in by_mod_id.get(mod_id, []):
                changed = False
                if summary and meta.description != summary:
                    meta.description = summary
                    changed = True
                if uploader and meta.uploaded_by != uploader:
                    meta.uploaded_by = uploader
                    changed = True
                if changed:
                    write_meta(staging_root / meta.mod_name / "meta.ini", meta)
                    desc_updated += 1
        if desc_updated:
            _log(f"  Description/uploader backfilled for {desc_updated} mod(s).")

    # -------------------------------------------------------------------
    # Fetch per-mod file lists via GraphQL (rate-limit-free) for any mod
    # that has a file_id - needed to (a) discover the installed file's
    # category when meta.ini doesn't record it, and (b) compare against
    # files in the same category as the installed file so non-MAIN
    # installs don't false-positive off a newer MAIN upload.
    # -------------------------------------------------------------------
    mods_needing_files = [
        mod_id for mod_id, metas in by_mod_id.items()
        if any(m.file_id > 0 for m in metas)
    ]
    if mods_needing_files:
        _log(f"Fetching file lists for {len(mods_needing_files)} mod(s) via GraphQL...")
        try:
            gql_files = api.graphql_mod_files_batch(game_domain, mods_needing_files)
        except Exception as exc:
            _log(f"  GraphQL modFilesBatch failed ({exc}); will use REST fallback.")
            gql_files = {}
        category_backfilled = 0
        for mod_id, files in gql_files.items():
            info = gql_info.get(mod_id)
            if info is not None:
                info.files = files
            by_fid = {f.file_id: f for f in files}
            for meta in by_mod_id.get(mod_id, []):
                if meta.file_id <= 0:
                    continue
                f = by_fid.get(meta.file_id)
                if f is None or not f.category_name:
                    continue
                if meta.file_category != f.category_name:
                    meta.file_category = f.category_name
                    if save_results:
                        write_meta(staging_root / meta.mod_name / "meta.ini", meta)
                    category_backfilled += 1
        if category_backfilled:
            _log(f"  File category backfilled for {category_backfilled} mod(s).")

    # Sync endorsement state via a single REST call - get_endorsements()
    # returns every endorsement the user has across all games in one request,
    # so the cost is exactly 1 rate-limit call regardless of mod count.
    # GraphQL's legacyMod type does not expose viewer endorsement status.
    if save_results:
        try:
            all_endorsements = api.get_endorsements()
            endorsed_ids: set[int] = {
                int(e.get("mod_id", 0))
                for e in all_endorsements
                if e.get("domain_name", "") == game_domain
                and (e.get("status", "") or "").lower() == "endorsed"
                and int(e.get("mod_id", 0)) > 0
            }
            endorsed_changed = 0
            for mod_id, metas in by_mod_id.items():
                want_endorsed = mod_id in endorsed_ids
                for meta in metas:
                    if meta.endorsed != want_endorsed:
                        meta.endorsed = want_endorsed
                        write_meta(staging_root / meta.mod_name / "meta.ini", meta)
                        endorsed_changed += 1
            if endorsed_changed:
                _log(f"  Endorsement status updated for {endorsed_changed} mod(s).")
        except NexusAPIError as exc:
            _log(f"  Endorsement sync skipped ({exc})")

    # -----------------------------------------------------------------------
    # 3. Classify each mod.
    #    - file_id known → file-level check (installed file's actual
    #      version vs latest-in-same-category; authoritative and not
    #      affected by meta-page version drift or stale meta.ini strings).
    #    - file_id missing (legacy MO2 imports):
    #        Path A  viewerUpdateAvailable == True  → trusted update
    #        Path B  meta.version + gql.version     → version-compare
    #        Path D  meta.version absent + date     → date-compare
    #        else skip.
    # -----------------------------------------------------------------------
    rest_fallback: dict[int, list[NexusModMeta]] = {}

    for mod_id, metas in by_mod_id.items():
        info = gql_info.get(mod_id)

        for meta in metas:
            install_date = _parse_install_date(meta)
            has_update: bool
            gql_version_backfilled = False

            # --- File-level check whenever we can identify the installed
            # file: the file record is authoritative for both version and
            # category, so we compare installed_file.version to the latest
            # file in the same category. This avoids (1) false positives
            # from non-MAIN installs being compared against a newer MAIN
            # upload, and (2) stale `meta.version` strings from install-
            # time filename parsing (e.g. "Mod-3-0-1" but real version is
            # 3.0.2) producing phantom updates.
            if meta.file_id > 0:
                rest_fallback.setdefault(mod_id, []).append(meta)
                continue

            # --- Path A: Nexus-native flag = True (trusted).
            # Only True is reliable - False can mean "author didn't bump the
            # page version" even when a new file exists.
            if info is not None and info.viewer_update_available is True:
                has_update = True
                if not meta.version and info.version:
                    meta.version = info.version
                    gql_version_backfilled = True

            # --- Path B: version-compare using GraphQL data only.
            elif meta.version and info is not None and info.version:
                # If either side is unparseable and we have a file_id, fall
                # through to the REST file_id check rather than silently
                # trusting a fuzzy string compare.
                if (
                    meta.file_id > 0
                    and (
                        _parse_version(info.version) is None
                        or _parse_version(meta.version) is None
                    )
                ):
                    rest_fallback.setdefault(mod_id, []).append(meta)
                    continue
                has_update = _version_is_newer(info.version, meta.version)

            # --- Path C: no meta.version but file_id known → need REST to
            # look up the installed file's version before comparing.
            elif not meta.version and meta.file_id > 0:
                rest_fallback.setdefault(mod_id, []).append(meta)
                continue

            # --- Path D: no version, no file_id → date compare.
            elif info is not None and info.updated_at is not None and install_date is not None:
                has_update = info.updated_at > install_date
                if not has_update and not meta.version and info.version:
                    meta.version = info.version
                    gql_version_backfilled = True

            else:
                # No usable comparison data - skip
                continue

            _apply_update_result(
                has_update, meta, mod_id, game_domain,
                latest_version=info.version if info else "",
                latest_file_id=0,
                latest_file_name="",
                updates=updates,
                staging_root=staging_root,
                save_results=save_results,
                _log=_log,
                category_id=info.category_id if info else 0,
                category_name=info.category_name if info else "",
                version_backfilled=gql_version_backfilled,
            )

    if not gql_info:
        _log("  GraphQL returned no results - falling back to REST for all mods.")
    else:
        _log(f"  {len(rest_fallback)} mod(s) will be compared at file level.")

    # -----------------------------------------------------------------------
    # 4. File-level check - use batch files when available, REST only when
    #    GraphQL didn't return the mod (avoids N REST calls for batched mods).
    # -----------------------------------------------------------------------
    if rest_fallback:
        _lock = threading.Lock()

        def _check_with_files(mod_id: int, metas: list[NexusModMeta], files: list) -> None:
            """Process mod(s) using file list - from GraphQL batch or REST."""
            gql_mod_info = gql_info.get(mod_id)
            cat_id = gql_mod_info.category_id if gql_mod_info else 0
            if not files:
                return

            # Pick the "latest" file for version comparison.  Prefer matching
            # the installed file's display name (same slot / variant) when we
            # can identify it; otherwise fall back to the newest file overall.
            newest_overall = max(files, key=lambda f: f.uploaded_timestamp)

            for meta in metas:
                installed_file = (
                    next((f for f in files if f.file_id == meta.file_id), None)
                    if meta.file_id > 0
                    else None
                )

                # Backfill meta.version from the installed file record.
                # The file record on Nexus is authoritative - meta.ini's
                # version field is often stale because it was parsed from
                # the archive filename at install time (which lags behind
                # the actual upload version, e.g. archive "...-1-0-33-..."
                # for file 1.0.34). Overwriting with the file's true
                # version prevents false "update available" flags when
                # installed_file_id already equals the latest file id.
                version_backfilled = False
                if installed_file:
                    _vf = installed_file.version or installed_file.mod_version or ""
                    if _vf and meta.version != _vf:
                        meta.version = _vf
                        version_backfilled = True

                # Pick comparison target - only consider files in the
                # same category (MAIN, OPTIONAL, etc.) so that e.g. a
                # newer OPTIONAL file doesn't flag a MAIN install.
                #
                # Special case: if the installed file has been moved to
                # OLD_VERSION / ARCHIVED / REMOVED, the author has
                # superseded it. We don't know which category the user
                # originally installed from (MAIN vs OPTIONAL), so
                # compare against the newest non-superseded file of any
                # current category - any of those could be the intended
                # replacement.
                installed_cat = (
                    installed_file.category_name
                    if installed_file
                    else meta.file_category or ""
                ).strip().upper()
                _SUPERSEDED = {"OLD_VERSION", "ARCHIVED", "REMOVED"}
                if installed_cat in _SUPERSEDED:
                    target_cat = ""
                    cat_matches = [
                        f for f in files
                        if (f.category_name or "").strip().upper()
                        not in _SUPERSEDED
                    ]
                else:
                    target_cat = installed_cat
                    cat_matches = (
                        [f for f in files if (f.category_name or "").strip().upper() == target_cat]
                        if target_cat else []
                    )

                # When we can identify the installed file, further restrict
                # comparison to files sharing its display name - same Nexus
                # "slot"/variant. Otherwise a mod page like Engine Fixes,
                # which ships both "Engine Fixes - Main File" and
                # "Engine Fixes - SKSE64 Preloader" under MAIN, would flag
                # the Preloader as updated whenever the Main File bumps,
                # even though the Preloader itself hasn't changed.
                # Only apply this narrowing if the installed file's name
                # actually matches at least one file in the current list -
                # otherwise the file may have been renamed and we should
                # fall back to the broader category set.
                if cat_matches and installed_file and installed_file.name:
                    name_in_cat = [f for f in cat_matches
                                   if f.name == installed_file.name]
                    if name_in_cat:
                        cat_matches = name_in_cat

                if cat_matches:
                    latest = max(cat_matches, key=lambda f: f.uploaded_timestamp)
                elif installed_file:
                    # Fallback: match by display name if no category info
                    name_matches = [f for f in files if f.name == installed_file.name]
                    latest = max(name_matches, key=lambda f: f.uploaded_timestamp)
                else:
                    latest = newest_overall

                latest_ver = latest.version or latest.mod_version or ""

                install_date = _parse_install_date(meta)
                if meta.version:
                    # Version compare - authoritative per user spec.
                    has_update = _version_is_newer(latest_ver, meta.version)
                elif install_date is not None:
                    # No version even after REST lookup - date-compare fallback.
                    latest_upload = datetime.fromtimestamp(
                        latest.uploaded_timestamp, tz=timezone.utc
                    )
                    has_update = latest_upload > install_date
                else:
                    continue

                with _lock:
                    _apply_update_result(
                        has_update, meta, mod_id, game_domain,
                        latest_version=latest.version or latest.mod_version or "",
                        latest_file_id=latest.file_id if installed_file else 0,
                        latest_file_name=latest.file_name,
                        updates=updates,
                        staging_root=staging_root,
                        save_results=save_results,
                        _log=_log,
                        category_id=cat_id,
                        category_name=gql_mod_info.category_name if gql_mod_info else "",
                        version_backfilled=version_backfilled,
                    )

        # Use batch files when GraphQL returned them; REST only for mods not in gql_info
        rest_only: dict[int, list[NexusModMeta]] = {}
        for mid, metas in rest_fallback.items():
            info = gql_info.get(mid)
            if info and info.files:
                _check_with_files(mid, metas, info.files)
            else:
                rest_only[mid] = metas

        if rest_only:
            _log(f"  Falling back to REST get_mod_files for {len(rest_only)} mod(s) "
                 "(GraphQL returned no file list).")
            def _check_rest(mod_id: int, metas: list[NexusModMeta]) -> None:
                try:
                    files_resp = api.get_mod_files(game_domain, mod_id)
                    _check_with_files(mod_id, metas, files_resp.files)
                except NexusAPIError as exc:
                    _log(f"  {metas[0].mod_name}: could not fetch files ({exc})")

            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(_check_rest, mid, metas): mid
                    for mid, metas in rest_only.items()
                }
                for future in as_completed(futures):
                    future.result()

    _log(f"Update check complete: {len(updates)} update(s) available.")

    # -----------------------------------------------------------------------
    # 5. Requirements check - mod-level from the same gql_info batch data;
    #    file-level (v3) via the api client in one extra batched call.
    # -----------------------------------------------------------------------
    _log("Checking mod requirements...")
    missing_reqs = check_requirements_from_gql(
        gql_info,
        all_installed,
        game_domain=game_domain,
        staging_root=staging_root,
        progress_cb=_log,
        save_results=save_results,
        enabled_only=enabled_only,
        api=api,
    )

    return updates, missing_reqs


def _apply_update_result(
    has_update: bool,
    meta: NexusModMeta,
    mod_id: int,
    game_domain: str,
    latest_version: str,
    latest_file_id: int,
    latest_file_name: str,
    updates: list,
    staging_root: Path,
    save_results: bool,
    _log: Callable,
    category_id: int = 0,
    category_name: str = "",
    version_backfilled: bool = False,
) -> None:
    """Record an update (or clear the flag) and persist to meta.ini."""
    # If the user has ignored updates for this mod, check whether a genuinely
    # newer version has appeared since they set the ignore flag.
    if has_update and meta.ignore_update:
        if meta.ignored_version and _version_is_newer(latest_version, meta.ignored_version):
            # A real new version has landed beyond the one they chose to ignore -
            # lift the ignore flag so the update badge re-appears.
            meta.ignore_update = False
            meta.ignored_version = ""
        else:
            # Still at (or before) the ignored version - suppress the update.
            has_update = False

    if has_update:
        info = UpdateInfo(
            mod_name=meta.mod_name,
            mod_id=mod_id,
            game_domain=game_domain,
            installed_file_id=meta.file_id,
            installed_version=meta.version,
            latest_file_id=latest_file_id,
            latest_version=latest_version,
            latest_file_name=latest_file_name,
            nexus_url=meta.nexus_page_url,
        )
        updates.append(info)
        _log(f"  ↑ {meta.mod_name}: {meta.version or '?'} → {latest_version or '?'}")
        if save_results:
            meta.latest_file_id = latest_file_id
            meta.latest_version = latest_version
            meta.has_update = True
            if category_id > 0:
                meta.category_id = category_id
            if category_name:
                meta.category_name = category_name
            write_meta(staging_root / meta.mod_name / "meta.ini", meta)
    else:
        if save_results:
            changed = False
            if meta.has_update:
                meta.has_update = False
                changed = True
            if latest_file_id and meta.latest_file_id != latest_file_id:
                meta.latest_file_id = latest_file_id
                changed = True
            # Never write a latest_version that is older than what is installed.
            # The Nexus mod-page "current version" field can lag behind the
            # actual latest uploaded file (author forgets to update it), so
            # the API may return e.g. "0.2.6" even though the user already
            # has "0.2.8" installed.
            effective_latest = latest_version
            if (
                effective_latest
                and meta.version
                and _version_is_newer(meta.version, effective_latest)
            ):
                effective_latest = meta.version
            if effective_latest and meta.latest_version != effective_latest:
                meta.latest_version = effective_latest
                changed = True
            if category_id > 0 and meta.category_id != category_id:
                meta.category_id = category_id
                changed = True
            if category_name and meta.category_name != category_name:
                meta.category_name = category_name
                changed = True
            if version_backfilled:
                changed = True
            if changed:
                write_meta(staging_root / meta.mod_name / "meta.ini", meta)
