from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from Utils.downloads.core import (
    build_installed_index_for_staging,
    file_extension,
    parse_archive_mod_file_ids,
    scan_download_dirs,
)
from Utils.downloads.locations import (
    archive_path_key, load_hidden_archive_paths,
)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path.absolute()


def _absolute(path: Path) -> Path:
    try:
        return path.expanduser().absolute()
    except (OSError, RuntimeError):
        return path


def _opaque_id(prefix: str, path: Path) -> str:
    digest = hashlib.sha256(str(_absolute(path)).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def _timestamp(value: float) -> str | None:
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace(
            "+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return None


def _same_path(left: Path, right: Path) -> bool:
    return _resolved(left) == _resolved(right)


def _location_kind(path: Path, game_name: str) -> str:
    from Utils.downloads.locations import get_default_downloads_dir

    if _same_path(path, get_default_downloads_dir()):
        return "downloads"
    try:
        from Utils.config_paths import get_download_cache_dir_for_game
        if _same_path(path, get_download_cache_dir_for_game(game_name)):
            return "cache"
    except Exception:
        pass
    return "extra"


def _nexus_identity(path: Path) -> dict[str, int | str | None] | None:
    parsed = None if _modio_identity(path) is not None else parse_archive_mod_file_ids(path.name)
    mod_id = parsed[0] if parsed is not None else None
    file_id = parsed[1] if parsed is not None else None
    domain: str | None = None
    try:
        sidecar = path.with_suffix(path.suffix + ".nexus.json")
        identity = json.loads(sidecar.read_text(encoding="utf-8"))
        if isinstance(identity, dict) and identity.get("version") == 1:
            saved_mod_id = int(identity.get("mod_id") or 0)
            saved_file_id = int(identity.get("file_id") or 0)
            saved_domain = str(identity.get("game_domain") or "").strip()
            if saved_mod_id > 0:
                mod_id = saved_mod_id
            if saved_file_id > 0:
                file_id = saved_file_id
            if saved_domain:
                domain = saved_domain
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        sidecar = path.with_suffix(path.suffix + ".fileid")
        sidecar_id = int(sidecar.read_text(encoding="utf-8").strip())
        if sidecar_id > 0:
            file_id = sidecar_id
    except (OSError, TypeError, ValueError):
        pass
    if mod_id is None and file_id is None:
        return None
    return {"domain": domain, "mod_id": mod_id, "file_id": file_id}


def _staging_dir(game: Any, profile_dir: Path) -> Path:
    from Utils.profiles.state import profile_uses_specific_mods

    if profile_uses_specific_mods(profile_dir):
        return profile_dir / "mods"
    return Path(game.get_mod_staging_path())


def _thunderstore_identity(path: Path) -> dict | None:
    sidecar = path.with_suffix(path.suffix + ".thunderstore.json")
    try:
        if sidecar.is_symlink():
            return None
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return None
        keys = ("namespace", "package", "package_version", "community")
        if not all(isinstance(raw.get(key), str) and raw[key] for key in keys):
            return None
        return {key: raw[key] for key in keys}
    except (OSError, ValueError):
        return None


def _modio_identity(path: Path) -> dict | None:
    sidecar = path.with_suffix(path.suffix + ".modio.json")
    try:
        if sidecar.is_symlink():
            return None
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
        keys = ("game_id", "mod_id", "file_id", "size_bytes")
        if (not isinstance(raw, dict) or raw.get("version") != 1
                or any(type(raw.get(key)) is not int or raw[key] <= 0 for key in keys)
                or not isinstance(raw.get("md5"), str) or re.fullmatch(r"[a-f0-9]{32}", raw["md5"]) is None):
            return None
        return {key: raw[key] for key in (*keys, "md5")}
    except (OSError, ValueError):
        return None


def hidden_revision(paths: set[str]) -> str:
    return hashlib.sha256(json.dumps(
        sorted(paths), ensure_ascii=True).encode("ascii")).hexdigest()


def build_inventory(game: Any, profile_dir: Path) -> dict[str, Any]:
    game_name = str(getattr(game, "name", "") or "")
    staging = _staging_dir(game, profile_dir)
    installed = build_installed_index_for_staging(staging)
    hidden_paths = load_hidden_archive_paths()
    scanned = scan_download_dirs(game_name)
    locations: list[dict[str, Any]] = []
    archives: list[dict[str, Any]] = []
    current_location: dict[str, Any] | None = None

    for entry in scanned:
        if entry.is_section_header:
            if entry.src_dir is None:
                continue
            source = Path(entry.src_dir)
            current_location = {
                "id": _opaque_id("location", source),
                "label": entry.section_name,
                "kind": _location_kind(source, game_name),
                "path": str(_absolute(source)),
                "exists": source.is_dir(),
                "archive_count": 0,
                "total_bytes": 0,
            }
            locations.append(current_location)
            continue
        if entry.path is None or entry.src_dir is None:
            continue
        path = Path(entry.path)
        location = current_location
        if (location is None
                or not _same_path(Path(location["path"]), entry.src_dir)):
            continue
        from amethyst_plugins.downloads import SIDECAR, read_source
        provider = read_source(path, strict=False)
        provider_sidecar = path.with_suffix(path.suffix + SIDECAR)
        identity = None if provider_sidecar.exists() or provider_sidecar.is_symlink() else _nexus_identity(path)
        archive = {
            "id": _opaque_id("archive", path),
            "name": path.name,
            "path": str(_absolute(path)),
            "location_id": location["id"],
            "location_kind": location["kind"],
            "extension": file_extension(path.name),
            "size_bytes": entry.size,
            "downloaded_at": _timestamp(entry.mtime),
            "modified_time": entry.mtime,
            "installed": installed.is_archive_installed(path.name),
            "is_symlink": path.is_symlink(),
            "hidden": archive_path_key(path) in hidden_paths,
            "nexus": identity,
            "thunderstore": _thunderstore_identity(path),
            "modio": _modio_identity(path),
            "provider": ({key: provider[key] for key in (
                "plugin_id", "provider_id", "provider_label", "identity", "sha256")} if provider else None),
        }
        archives.append(archive)
        location["archive_count"] += 1
        location["total_bytes"] += entry.size

    return {
        "staging_path": str(_absolute(staging)),
        "hidden_revision": hidden_revision(hidden_paths),
        "locations": locations,
        "archives": archives,
        "counts": {
            "locations": len(locations),
            "archives": len(archives),
            "installed": sum(bool(item["installed"]) for item in archives),
            "not_installed": sum(not item["installed"] for item in archives),
            "total_bytes": sum(int(item["size_bytes"]) for item in archives),
        },
    }
