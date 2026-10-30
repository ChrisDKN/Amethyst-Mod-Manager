"""Resolve confirmed Nexus successors for Quick Update."""
from __future__ import annotations

from pathlib import Path

from Nexus.nexus_meta import normalise_game_domain, read_meta
from Nexus.nexus_updates import NexusUpdateResolver


def resolve_quick_update_target(api, staging_root: Path, mod_name: str,
                                fallback_domain: str, *, resolver=None,
                                files=None, log=None) -> tuple[str, object]:
    """Return a queued download payload or a reason to skip this mod."""
    meta_path = Path(staging_root) / mod_name / "meta.ini"
    if not meta_path.is_file():
        return ("skipped", "no Nexus metadata")
    try:
        meta = read_meta(meta_path)
    except Exception as exc:
        return ("skipped", f"could not read metadata ({exc})")
    if not meta.mod_id:
        return ("skipped", "no Nexus mod id in metadata")
    if meta.ignore_update:
        return ("skipped", "updates ignored")
    game_domain = (normalise_game_domain(meta.game_domain)
                   or normalise_game_domain(fallback_domain))
    try:
        if files is None:
            files = api.get_mod_files(game_domain, meta.mod_id).files
    except Exception as exc:
        return ("skipped", f"could not fetch file list ({exc})")
    resolver = resolver or NexusUpdateResolver(api, log)
    result = resolver.resolve(game_domain, meta, files)
    if result.error:
        return ("skipped", result.error)
    if not result.has_update:
        return ("skipped", "already up to date")
    if not result.automatic or result.file is None:
        return ("skipped", "no confirmed successor - use Change Version")
    return ("queued", (mod_name, game_domain, meta, result.file.file_id, result.file))
