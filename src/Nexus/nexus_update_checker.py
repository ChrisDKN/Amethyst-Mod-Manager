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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from Nexus.nexus_api import NexusAPI, NexusAPIError
from Nexus.nexus_meta import NexusModMeta, scan_installed_mods, read_meta, write_meta

ProgressCallback = Callable[[str], None]


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
) -> list[UpdateInfo]:
    """
    Check all Nexus-sourced mods under *staging_root* for updates.

    For every mod that has a ``file_id`` in its ``meta.ini``, we fetch the
    mod's file list and compare against the latest MAIN file.  This catches
    all updates regardless of when they were uploaded.

    Parameters
    ----------
    api : NexusAPI
        Authenticated API client.
    staging_root : Path
        Root of the mod staging area (e.g. ``game.get_mod_staging_path()``).
    game_domain : str
        The Nexus API game domain (e.g. ``"skyrimspecialedition"``).
        When provided, all mods are checked against this domain regardless
        of what ``gameName`` is stored in their ``meta.ini``.
    progress_cb : callable, optional
        Called with status strings for UI feedback.
    save_results : bool
        If True, write ``latestFileId`` / ``hasUpdate`` back to each mod's
        ``meta.ini`` so the UI can show update flags without re-checking.

    Returns
    -------
    list[UpdateInfo]
        Mods that have a newer file available on Nexus.
    """
    _log = progress_cb or (lambda m: None)

    # 1. Scan installed mods with Nexus metadata
    installed = scan_installed_mods(staging_root)
    if not installed:
        _log("No Nexus-sourced mods found.")
        return []

    if enabled_only is not None:
        installed = [m for m in installed if m.mod_name in enabled_only]

    # Only check mods that have a file_id (otherwise we can't compare)
    checkable = [m for m in installed if m.file_id > 0]
    skipped = len(installed) - len(checkable)

    _log(f"Checking {len(checkable)} Nexus mod(s) for updates"
         f"{f' ({skipped} skipped — no file ID)' if skipped else ''}...")

    if not checkable:
        _log("No mods with file IDs to check.")
        return []

    # Determine the domain to use
    if not game_domain:
        # Fall back to whatever is in the first mod's meta
        game_domain = checkable[0].game_domain.strip().lower()
    if not game_domain:
        _log("No game domain available — cannot check updates.")
        return []

    # Deduplicate by mod_id (multiple mods could come from the same Nexus mod)
    by_mod_id: dict[int, list[NexusModMeta]] = {}
    for meta in checkable:
        by_mod_id.setdefault(meta.mod_id, []).append(meta)

    updates: list[UpdateInfo] = []
    checked = 0
    total = len(by_mod_id)

    for mod_id, metas in by_mod_id.items():
        checked += 1
        representative = metas[0]

        # Fetch the mod's file list
        try:
            files_resp = api.get_mod_files(game_domain, mod_id)
        except NexusAPIError as exc:
            _log(f"  [{checked}/{total}] {representative.mod_name}: "
                 f"could not fetch files ({exc})")
            continue

        # Find the latest MAIN file, or the newest file overall
        main_files = [f for f in files_resp.files
                      if f.category_name == "MAIN"]
        latest = None
        if main_files:
            latest = max(main_files, key=lambda f: f.uploaded_timestamp)
        elif files_resp.files:
            latest = max(files_resp.files, key=lambda f: f.uploaded_timestamp)

        if latest is None:
            continue

        # Check each local mod entry against this mod_id
        for meta in metas:
            if latest.file_id != meta.file_id:
                info = UpdateInfo(
                    mod_name=meta.mod_name,
                    mod_id=mod_id,
                    game_domain=game_domain,
                    installed_file_id=meta.file_id,
                    installed_version=meta.version,
                    latest_file_id=latest.file_id,
                    latest_version=latest.version or latest.mod_version,
                    latest_file_name=latest.file_name,
                    nexus_url=meta.nexus_page_url,
                )
                updates.append(info)
                _log(f"  ↑ {meta.mod_name}: "
                     f"{meta.version or '?'} → {info.latest_version or '?'}")

                if save_results:
                    meta.latest_file_id = latest.file_id
                    meta.latest_version = info.latest_version
                    meta.has_update = True
                    meta_path = staging_root / meta.mod_name / "meta.ini"
                    write_meta(meta_path, meta)
            else:
                # Up to date — clear flag if previously set
                if save_results and meta.has_update:
                    meta.has_update = False
                    meta_path = staging_root / meta.mod_name / "meta.ini"
                    write_meta(meta_path, meta)

        # Progress update every 10 mods
        if checked % 10 == 0:
            _log(f"  Checked {checked}/{total} mods...")

    _log(f"Update check complete: {len(updates)} update(s) available.")
    return updates
