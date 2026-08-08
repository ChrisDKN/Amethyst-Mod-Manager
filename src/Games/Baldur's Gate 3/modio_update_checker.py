"""
modio_update_checker.py  (Baldur's Gate 3)

Check installed mod.io mods for available updates.

Workflow:
  1. For each staging folder, read its mod.io meta.ini keys.  Folders with no
     mod.io id yet (e.g. installed before a key was entered) are resolved on
     demand from the pak's PublishHandle via ``modio_meta.resolve_modio_meta``.
  2. Compare the stored ``modioFileId`` against the newest released file's id.
  3. ``modioLatestFileId`` / ``modioLatestVersion`` are refreshed in meta.ini
     and updated mods are returned to the caller.

Update detection is a straight file-id comparison.  A mod whose installed
file id was never identified (``modioFileId == 0``) is reported as "unknown".

Loaded by file path (BG3 folder has spaces), so it imports only from
``Utils.*`` and loads its siblings via :func:`_load_sibling`.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from Utils.app_log import app_log

ProgressCallback = Callable[[str], None]


def _load_sibling(stem: str):
    mod_name = f"{stem}_bg3"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    sibling = Path(__file__).resolve().parent / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(mod_name, str(sibling))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class ModioUpdateInfo:
    """One mod's update status."""

    mod_name: str = ""              # staging folder name
    mod_id: int = 0
    installed_file_id: int = 0
    installed_version: str = ""
    latest_file_id: int = 0
    latest_version: str = ""
    has_update: bool = False
    # True when we never identified the installed file (file_id == 0), so the
    # update state is genuinely unknown rather than up-to-date.
    unknown: bool = False


def check_for_updates(
    staging_root: Path,
    api_key: str,
    progress_cb: Optional[ProgressCallback] = None,
    only_names: Optional[set[str]] = None,
) -> list[ModioUpdateInfo]:
    """Return update info for every mod.io-tracked mod under *staging_root*.

    Only mods with a newer file (or unknown state) are returned; up-to-date
    mods are omitted.  meta.ini is refreshed with the latest file id/version.
    If *only_names* is given, restrict the check to those staging folder names
    (used by the right-click "Check Updates" on specific mods).
    """
    _log = progress_cb or (lambda m: None)
    results: list[ModioUpdateInfo] = []

    if not api_key:
        _log("mod.io: no API key - skipping update check.")
        return results

    modio_meta = _load_sibling("modio_meta")
    modio_api = _load_sibling("modio_api")
    try:
        api = modio_api.ModioAPI(api_key)
    except Exception as e:
        _log(f"mod.io: cannot init API - {e}")
        return results

    # Phase 1: gather each mod.io folder's meta, resolving any that lack an id.
    # Resolve-on-demand (per-mod /files for the size/md5 match) is unavoidably
    # individual, but it only runs for mods installed before a key was entered.
    targets: list[tuple[Path, Path, "object"]] = []  # (folder, meta_path, ModioMeta)
    for folder in sorted(p for p in staging_root.iterdir() if p.is_dir()):
        if only_names is not None and folder.name not in only_names:
            continue
        meta_path = folder / "meta.ini"
        meta = (modio_meta.read_modio_meta(meta_path)
                if meta_path.is_file() else modio_meta.ModioMeta())

        if meta.mod_id <= 0:
            mod_id, _name, _pak = modio_meta.read_publish_handle_from_staging(folder)
            if mod_id <= 0:
                continue  # not a mod.io pak
            _log(f"mod.io: resolving '{folder.name}' (mod {mod_id})...")
            try:
                resolved = modio_meta.resolve_modio_meta(
                    archive_path=(_pak if _pak is not None else folder),
                    staging_dir=folder, api_key=api_key, log_fn=_log,
                )
            except Exception as e:
                _log(f"mod.io: resolve failed for '{folder.name}' - {e}")
                continue
            if resolved is None or resolved.mod_id <= 0:
                continue
            try:
                modio_meta.write_modio_meta(meta_path, resolved)
            except OSError as e:
                app_log(f"mod.io: could not write meta.ini for '{folder.name}': {e}")
            meta = resolved
        targets.append((folder, meta_path, meta))

    if not targets:
        _log("mod.io: no mod.io mods to check.")
        return results

    # Phase 2: one batched request for every mod's latest file + page URL.
    _log(f"mod.io: checking {len(targets)} mod(s)...")
    try:
        summaries = api.get_mods_latest_batch([m.mod_id for _, _, m in targets])
    except Exception as e:
        _log(f"mod.io: batch lookup failed - {e}")
        return results

    for folder, meta_path, meta in targets:
        s = summaries.get(meta.mod_id)
        if s is None or s.latest_file_id <= 0:
            continue

        info = ModioUpdateInfo(
            mod_name=folder.name, mod_id=meta.mod_id,
            installed_file_id=meta.file_id, installed_version=meta.version,
            latest_file_id=s.latest_file_id, latest_version=s.latest_version,
        )
        if meta.file_id <= 0:
            info.unknown = True
            results.append(info)
        elif s.latest_file_id != meta.file_id:
            info.has_update = True
            results.append(info)

        # Persist refreshed latest-file fields + backfill the page URL (the
        # batch already fetched it, so this is free).
        url = meta.profile_url or s.profile_url
        if (meta.latest_file_id != s.latest_file_id
                or meta.latest_version != s.latest_version
                or meta.profile_url != url):
            meta.latest_file_id = s.latest_file_id
            meta.latest_version = s.latest_version
            meta.profile_url = url
            try:
                modio_meta.write_modio_meta(meta_path, meta)
            except OSError as e:
                app_log(f"mod.io: could not update meta.ini for '{folder.name}': {e}")

    _log(f"mod.io: {len(results)} mod(s) need attention.")
    return results
