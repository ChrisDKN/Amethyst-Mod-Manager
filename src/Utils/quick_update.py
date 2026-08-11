"""Quick Update - resolve the latest name-matched Nexus file for each
update-flagged mod (tkinter-free, shared by the Qt and Tk front-ends).

"Quick Update" auto-installs the newest file whose name still matches the
installed one (the non-orange file in Change Version). Mods whose latest file
is NOT a name match are skipped - the user updates those manually via Change
Version. This module holds only the pure resolve/skip logic; the front-end
owns the download + install pipeline and all UI.
"""
from __future__ import annotations

from pathlib import Path

from Nexus.nexus_meta import normalise_game_domain, read_meta
from Utils.mod_files_versions import resolve_latest_name_match


def resolve_quick_update_target(api, staging_root: Path, mod_name: str,
                                fallback_domain: str) -> tuple[str, object]:
    """Resolve one mod's name-matched update.

    Returns ``("queued", payload)`` where *payload* is
    ``(mod_name, game_domain, meta, file_id, file_info)`` ready for download,
    or ``("skipped", reason)`` with a human-readable reason string.

    Mirrors Tk ``_quick_update_mods._resolve_one`` exactly:
    - no meta.ini / unreadable / no mod_id  → skipped
    - the latest file isn't a name match (or is the installed one) → skipped
      ("no name-matched update - use Change Version")
    """
    meta_path = Path(staging_root) / mod_name / "meta.ini"
    if not meta_path.is_file():
        return ("skipped", "no Nexus metadata")
    try:
        meta = read_meta(meta_path)
    except Exception as exc:
        return ("skipped", f"could not read metadata ({exc})")
    if not meta.mod_id:
        return ("skipped", "no Nexus mod id in metadata")
    game_domain = (normalise_game_domain(meta.game_domain)
                   or normalise_game_domain(fallback_domain))
    try:
        files = api.get_mod_files(game_domain, meta.mod_id).files
    except Exception as exc:
        return ("skipped", f"could not fetch file list ({exc})")
    fid, _old = resolve_latest_name_match(files, meta.file_id, mod_name)
    if fid <= 0 or fid == meta.file_id:
        return ("skipped", "no name-matched update - use Change Version")
    file_info = next((f for f in files if f.file_id == fid), None)
    return ("queued", (mod_name, game_domain, meta, fid, file_info))
