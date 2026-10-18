from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .jobs import JobError


def locations_snapshot() -> dict[str, Any]:
    from Utils.downloads.locations import get_default_downloads_dir, read_config

    extras, default_disabled, cache_disabled = read_config()
    settings = {
        "extras": extras,
        "default_enabled": not default_disabled,
        "cache_enabled": not cache_disabled,
    }
    payload = {"settings": settings,
               "default_path": str(get_default_downloads_dir())}
    revision = hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()
    return {"revision": revision, **payload}


def _prepare_locations(settings: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(settings, dict) or set(settings) != {
            "extras", "default_enabled", "cache_enabled"}:
        raise JobError("invalid_download_locations",
                       "Supply extras, default_enabled, and cache_enabled.")
    extras = settings["extras"]
    if not isinstance(extras, list) or len(extras) > 64:
        raise JobError("invalid_download_locations",
                       "'extras' must be an array of at most 64 folders.")
    for key in ("default_enabled", "cache_enabled"):
        if not isinstance(settings[key], bool):
            raise JobError("invalid_download_locations",
                           f"'{key}' must be a boolean.")
    values: list[str] = []
    seen: set[Path] = set()
    for raw in extras:
        if (not isinstance(raw, str) or not raw or raw != raw.strip()
                or len(raw) > 4096 or any(ord(char) < 32 for char in raw)):
            raise JobError("invalid_download_locations",
                           "Each extra location must be an absolute folder path.")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise JobError("invalid_download_locations",
                           "Each extra location must be an absolute folder path.",
                           {"path": raw})
        try:
            identity = path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise JobError("invalid_download_locations",
                           "Could not resolve an extra download location.",
                           {"path": raw, "reason": str(exc)}) from exc
        if identity in seen:
            raise JobError("invalid_download_locations",
                           "Extra download locations must be distinct.",
                           {"path": raw})
        seen.add(identity)
        values.append(str(path))
    return {"extras": values,
            "default_enabled": settings["default_enabled"],
            "cache_enabled": settings["cache_enabled"]}


def set_locations(
    revision: str, settings: dict[str, Any],
    log_fn: Callable[[str], None],
) -> dict[str, Any]:
    from Utils.downloads.locations import write_config
    from Utils.operations import acquire_resource_operation

    with acquire_resource_operation(
        "downloads.settings", "downloads.locations.set",
        owner="headless:downloads.locations.set",
    ):
        current = locations_snapshot()
        if current["revision"] != revision:
            raise JobError("stale_download_locations",
                           "Download locations changed; get them again.",
                           {"locations": current})
        proposed = _prepare_locations(settings)
        if proposed == current["settings"]:
            return {"changed": False, "locations": current}
        latest = locations_snapshot()
        if latest["revision"] != revision:
            raise JobError("stale_download_locations",
                           "Download locations changed; get them again.",
                           {"locations": latest})
        try:
            write_config(proposed["extras"],
                         not proposed["default_enabled"],
                         not proposed["cache_enabled"])
        except (OSError, ValueError) as exc:
            raise JobError("download_locations_save_failed",
                           "Could not save download locations.",
                           {"reason": str(exc)}) from exc
        log_fn("Saved download scan locations.")
        return {"changed": True, "locations": locations_snapshot()}


def set_hidden(
    game: Any, profile_dir: Path, archive_ids: list[str], hidden: bool,
    revision: str, log_fn: Callable[[str], None],
) -> dict[str, Any]:
    from Utils.downloads.inventory import build_inventory, hidden_revision
    from Utils.downloads.locations import (
        archive_path_key, load_hidden_archive_paths,
        save_hidden_archive_paths,
    )
    from Utils.operations import acquire_resource_operation

    with acquire_resource_operation(
        "downloads.settings", "downloads.hidden.set",
        owner="headless:downloads.hidden.set",
    ):
        current = load_hidden_archive_paths()
        current_revision = hidden_revision(current)
        if current_revision != revision:
            raise JobError("stale_hidden_archives",
                           "Hidden archives changed; list downloads again.",
                           {"revision": current_revision})
        if not profile_dir.is_dir() or profile_dir.is_symlink():
            raise JobError("profile_not_found",
                           "The selected profile is no longer available.",
                           {"profile": profile_dir.name})
        inventory = build_inventory(game, profile_dir)
        by_id = {item["id"]: item for item in inventory["archives"]}
        missing = [archive_id for archive_id in archive_ids
                   if archive_id not in by_id]
        if missing:
            raise JobError("archive_not_found",
                           "One or more selected archives are no longer available.",
                           {"archive_ids": missing})
        keys = {archive_path_key(by_id[archive_id]["path"])
                for archive_id in archive_ids}
        updated = current | keys if hidden else current - keys
        if updated == current:
            return {"changed": False, "hidden_revision": current_revision,
                    "archive_ids": archive_ids, "hidden": hidden}
        if hidden_revision(load_hidden_archive_paths()) != revision:
            raise JobError("stale_hidden_archives",
                           "Hidden archives changed; list downloads again.")
        try:
            save_hidden_archive_paths(updated)
        except (OSError, ValueError) as exc:
            raise JobError("hidden_archives_save_failed",
                           "Could not save hidden archives.",
                           {"reason": str(exc)}) from exc
        log_fn(f"Updated visibility for {len(keys)} download archives.")
        return {"changed": True,
                "hidden_revision": hidden_revision(updated),
                "archive_ids": archive_ids, "hidden": hidden}
