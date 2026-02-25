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

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from Nexus.nexus_api import NexusAPI, NexusModRequirement
from Nexus.nexus_meta import NexusModMeta, scan_installed_mods, read_meta, write_meta

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]


@dataclass
class MissingRequirementInfo:
    """Info about a mod that has missing requirements."""
    mod_name: str                                     # local folder name
    mod_id: int
    missing: list[NexusModRequirement] = field(default_factory=list)


def check_missing_requirements(
    api: NexusAPI,
    staging_root: Path,
    game_domain: str = "",
    progress_cb: Optional[ProgressCallback] = None,
    save_results: bool = True,
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

    checkable = [m for m in installed if m.mod_id > 0]
    if not checkable:
        _log("No mods with Nexus IDs to check requirements for.")
        return []

    # Determine the domain to use
    if not game_domain:
        game_domain = checkable[0].game_domain.strip().lower()
    if not game_domain:
        _log("No game domain available — cannot check requirements.")
        return []

    # 2. Build set of all installed Nexus mod IDs
    installed_mod_ids: set[int] = {m.mod_id for m in installed if m.mod_id > 0}

    # Deduplicate by mod_id
    by_mod_id: dict[int, list[NexusModMeta]] = {}
    for meta in checkable:
        by_mod_id.setdefault(meta.mod_id, []).append(meta)

    _log(f"Checking requirements for {len(by_mod_id)} Nexus mod(s)...")

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

        if not reqs:
            # No requirements listed — clear any stale flag
            if save_results:
                for meta in metas:
                    if meta.missing_requirements:
                        meta.missing_requirements = ""
                        meta_path = staging_root / meta.mod_name / "meta.ini"
                        write_meta(meta_path, meta)
            continue

        # 4. Filter to Nexus-hosted requirements whose mod_id is not installed
        missing: list[NexusModRequirement] = []
        for req in reqs:
            if req.is_external:
                # External requirements (non-Nexus) — skip, we can't track them
                continue
            if req.mod_id <= 0:
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
                # All requirements satisfied — clear flag
                if save_results and meta.missing_requirements:
                    meta.missing_requirements = ""
                    meta_path = staging_root / meta.mod_name / "meta.ini"
                    write_meta(meta_path, meta)

        if checked % 10 == 0:
            _log(f"  Checked {checked}/{total} mods...")

    _log(f"Requirements check complete: {len(results)} mod(s) with missing dependencies.")
    return results
