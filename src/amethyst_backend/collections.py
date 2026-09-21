from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable


_CARD_FIELDS = (
    "id", "name", "summary", "description", "user_name", "author",
    "tile_image_url", "image_url", "thumbnail_url", "total_downloads",
    "endorsements", "adult",
)


def _state_revision(record: dict[str, Any]) -> str:
    value = {key: item for key, item in record.items() if key != "path"}
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _scalar(value: Any) -> Any:
    return value if value is None or isinstance(value, (str, int, float, bool)) else None


def _record_summary(record: dict[str, Any], profile_dir: Path, locked: bool) -> dict[str, Any]:
    from Utils.collections.installed import collection_title
    from Utils.collections.manifest import is_collection_manifest

    manifest = record.get("manifest") if isinstance(record.get("manifest"), dict) else {}
    info = manifest.get("info") if isinstance(manifest.get("info"), dict) else {}
    card = record.get("card") if isinstance(record.get("card"), dict) else {}
    members = record.get("members") if isinstance(record.get("members"), dict) else {}
    mods = manifest.get("mods") if isinstance(manifest.get("mods"), list) else []
    plugins = manifest.get("plugins") if isinstance(manifest.get("plugins"), list) else []
    previous = record.get("previous") if isinstance(record.get("previous"), dict) else {}
    kind = str(record.get("kind") or "appended")
    install_id = record.get("install_id")
    if not isinstance(install_id, str) or not install_id or len(install_id) > 128:
        install_id = None
    slug = str(record.get("slug") or "")
    domain = str(record.get("domain") or info.get("domainName") or "") or None
    status = str(record.get("status") or "unknown")
    return {
        "id": install_id,
        "kind": kind,
        "profile": profile_dir.name,
        "title": collection_title(record, profile_dir.name),
        "slug": slug,
        "domain": domain,
        "revision": _scalar(record.get("revision")),
        "previous_revision": _scalar(previous.get("revision")),
        "status": status,
        "saved_at": _scalar(record.get("saved")),
        "state_revision": _state_revision(record),
        "locked": bool(locked),
        "mods_enabled": (record.get("mods_enabled")
                         if isinstance(record.get("mods_enabled"), bool) else None),
        "member_count": len(members),
        "manifest": {
            "available": is_collection_manifest(manifest),
            "mod_count": len(mods),
            "plugin_count": len(plugins),
        },
        "card": {
            key: value for key in _CARD_FIELDS
            if (value := _scalar(card.get(key))) is not None
        },
        "author": str(card.get("user_name") or card.get("author")
                      or info.get("author") or "") or None,
        "can_toggle": kind == "appended" and not locked,
        "can_remove": kind in {"appended", "standalone"} and not locked,
    }


def installed_inventory(
    game: Any,
    game_summary: dict[str, str],
    profile_dir: Path | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    from Utils.collections.installed import list_appended_collections, primary_collection
    from Utils.profiles.groups import profile_is_locked

    log = log_fn or (lambda _message: None)
    if profile_dir is not None:
        profiles = [Path(profile_dir)]
    else:
        root = Path(game.get_profile_root()) / "profiles"
        profiles = [] if not root.is_dir() else [
            path for path in sorted(root.iterdir(), key=lambda value: value.name.casefold())
            if path.is_dir() and not path.is_symlink()
        ]

    installations = []
    warnings = []
    for profile in profiles:
        messages = []
        try:
            locked = profile_is_locked(profile)
            primary = primary_collection(profile)
            if primary is not None:
                installations.append(_record_summary(primary, profile, locked))
            for record in list_appended_collections(profile, messages.append):
                installations.append(_record_summary(record, profile, locked))
        except (OSError, TypeError, ValueError) as exc:
            messages.append(f"Could not scan collections in {profile.name}: {exc}")
        for message in messages:
            log(message)
            warnings.append({"profile": profile.name, "message": message})

    installations.sort(key=lambda value: (
        value["profile"].casefold(), value["title"].casefold(),
        value["kind"], str(value["revision"]),
    ))
    return {
        "game": game_summary,
        "profile": profile_dir.name if profile_dir is not None else None,
        "installations": installations,
        "counts": {
            "installations": len(installations),
            "standalone": sum(item["kind"] == "standalone" for item in installations),
            "appended": sum(item["kind"] == "appended" for item in installations),
            "incomplete": sum(item["status"] != "complete" for item in installations),
        },
        "warnings": warnings,
    }
