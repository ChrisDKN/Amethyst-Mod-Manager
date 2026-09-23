from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any, Callable

from .jobs import JobError, _validated_archive_path


def _token(path: Path, *, required: bool) -> list[int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not required:
            return None
        raise JobError("archive_not_found",
                       "The selected archive is no longer available.",
                       {"path": str(path)})
    except OSError as exc:
        raise JobError("resource_unavailable",
                       "Could not inspect an archive file.",
                       {"path": str(path), "reason": str(exc)}) from exc
    if not stat.S_ISREG(info.st_mode):
        raise JobError("unsafe_archive",
                       "The archive or its sidecar is not a regular file.",
                       {"path": str(path)})
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns]


def _removal_plan(
    game: Any, profile_dir: Path, archive_ids: list[str],
) -> tuple[dict[str, Any], list[tuple[Path, list[Path], list[int], list[list[int] | None]]]]:
    from Utils.downloads.inventory import build_inventory

    if not profile_dir.is_dir() or profile_dir.is_symlink():
        raise JobError("profile_not_found",
                       "The selected profile is no longer available.",
                       {"profile": profile_dir.name})
    inventory = build_inventory(game, profile_dir)
    by_id = {archive["id"]: archive for archive in inventory["archives"]}
    public: list[dict[str, Any]] = []
    fingerprints: list[dict[str, Any]] = []
    paths: list[tuple[Path, list[Path], list[int], list[list[int] | None]]] = []
    for archive_id in archive_ids:
        archive = by_id.get(archive_id)
        if archive is None:
            raise JobError("archive_not_found",
                           "The selected archive is no longer in download inventory.",
                           {"archive_id": archive_id})
        path = _validated_archive_path(inventory, archive)
        token = _token(path, required=True)
        sidecars = [path.with_suffix(path.suffix + suffix)
                    for suffix in (".fileid", ".nexus.json")]
        sidecar_tokens = [_token(sidecar, required=False)
                          for sidecar in sidecars]
        present_sidecars = [sidecar for sidecar, value
                            in zip(sidecars, sidecar_tokens) if value is not None]
        public.append({
            "id": archive_id,
            "name": archive["name"],
            "path": str(path),
            "size_bytes": token[3],
            "installed": archive["installed"],
            "sidecars": [str(sidecar) for sidecar in present_sidecars],
        })
        fingerprints.append({
            "id": archive_id, "path": str(path),
            "installed": archive["installed"], "archive": token,
            "sidecars": sidecar_tokens,
        })
        paths.append((path, sidecars, token, sidecar_tokens))
    payload = {"profile": str(profile_dir.resolve(strict=False)),
               "archives": fingerprints}
    revision = hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()
    return ({"revision": revision, "profile": profile_dir.name,
             "archives": public,
             "total_bytes": sum(item["size_bytes"] for item in public)},
            paths)


def plan_removal(
    game: Any, profile_dir: Path, archive_ids: list[str],
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    with acquire_game_operation(
        game, "downloads.remove.plan", owner="headless:downloads.remove.plan",
    ):
        plan, _paths = _removal_plan(game, profile_dir, archive_ids)
        return plan


def plan_runner(
    game: Any, profile_dir: Path, archive_ids: list[str],
) -> Callable:
    def run(_log: Callable[..., None], progress: Callable[..., None]) -> dict:
        progress(0, 1, "Inspecting downloaded archives")
        plan = plan_removal(game, profile_dir, archive_ids)
        progress(1, 1, "Archive removal plan ready")
        return {"plan": plan}
    return run


def remove_runner(
    game: Any, profile_dir: Path, archive_ids: list[str],
    revision: str,
) -> Callable:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict:
        from Nexus.nexus_download import delete_archive_and_sidecar
        from Utils.operations import acquire_game_operation

        with acquire_game_operation(
            game, "downloads.remove", owner="headless:downloads.remove",
        ):
            plan, paths = _removal_plan(game, profile_dir, archive_ids)
            if plan["revision"] != revision:
                raise JobError("download_removal_plan_changed",
                               "The selected archives changed; inspect the plan again.",
                               {"plan": plan})
            removed: list[str] = []
            try:
                for index, (path, sidecars, token, sidecar_tokens) in enumerate(paths):
                    progress(index, len(paths), "Removing downloaded archives")
                    if (_token(path, required=True) != token
                            or [_token(sidecar, required=False)
                                for sidecar in sidecars] != sidecar_tokens):
                        raise JobError("download_removal_plan_changed",
                                       "An archive changed during removal.",
                                       {"path": str(path)})
                    delete_archive_and_sidecar(path)
                    remaining = [str(item) for item in (path, *sidecars)
                                 if item.exists() or item.is_symlink()]
                    if remaining:
                        raise JobError("download_remove_failed",
                                       "Could not remove an archive and all its sidecars.",
                                       {"incomplete_archive": str(path),
                                        "remaining": remaining})
                    removed.append(str(path))
                    log(f"Removed downloaded archive: {path.name}")
            except JobError as exc:
                if removed:
                    raise JobError(exc.name, str(exc),
                                   {**exc.details, "removed": removed}) from exc
                raise
            progress(len(paths), len(paths), "Downloaded archives removed")
            return {"removed": removed, "count": len(removed),
                    "total_bytes": plan["total_bytes"]}
    return run
