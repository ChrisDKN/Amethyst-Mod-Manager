from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable

from .jobs import JobError, _validated_archive_path

_SIDECAR_SUFFIXES = (".fileid", ".nexus.json", ".thunderstore.json", ".modio.json", ".tool.json", ".plugin.json")


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
                    for suffix in _SIDECAR_SUFFIXES]
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


def _move_plan(
    game: Any, profile_dir: Path, archive_ids: list[str], destination_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from Utils.downloads.inventory import build_inventory
    from .download_settings import locations_snapshot

    if not profile_dir.is_dir() or profile_dir.is_symlink():
        raise JobError("profile_not_found",
                       "The selected profile is no longer available.",
                       {"profile": profile_dir.name})
    inventory = build_inventory(game, profile_dir)
    location = next((item for item in inventory["locations"]
                     if item["id"] == destination_id), None)
    if location is None:
        raise JobError("download_location_not_found",
                       "The destination is not an enabled download location.",
                       {"location_id": destination_id})
    try:
        destination = Path(location["path"]).resolve(strict=True)
        if not destination.is_dir():
            raise OSError("Destination is not a directory")
    except (OSError, RuntimeError, ValueError) as exc:
        raise JobError("download_location_unavailable",
                       "The destination download location is unavailable.",
                       {"location_id": destination_id, "reason": str(exc)}) from exc

    by_id = {item["id"]: item for item in inventory["archives"]}
    public: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    fingerprints: list[dict[str, Any]] = []
    target_names: set[str] = set()
    for archive_id in archive_ids:
        archive = by_id.get(archive_id)
        if archive is None:
            raise JobError("archive_not_found",
                           "The selected archive is no longer in download inventory.",
                           {"archive_id": archive_id})
        source = _validated_archive_path(inventory, archive)
        target = destination / source.name
        if source.name in target_names:
            raise JobError("download_move_target_conflict",
                           "Selected archives share a destination filename.",
                           {"name": source.name})
        target_names.add(source.name)
        source_paths = [source, *(
            source.with_suffix(source.suffix + suffix)
            for suffix in _SIDECAR_SUFFIXES)]
        target_paths = [target, *(
            target.with_suffix(target.suffix + suffix)
            for suffix in _SIDECAR_SUFFIXES)]
        source_tokens = [_token(path, required=index == 0)
                         for index, path in enumerate(source_paths)]
        same_location = source == target
        target_tokens = (source_tokens if same_location else
                         [_token(path, required=False) for path in target_paths])
        collisions = ([] if same_location else
                      [str(path) for path, token in zip(target_paths, target_tokens)
                       if token is not None])
        public.append({
            "id": archive_id, "name": source.name,
            "source": str(source), "destination": str(target),
            "size_bytes": source_tokens[0][3],
            "sidecars": [str(path) for path, token in
                         zip(source_paths[1:], source_tokens[1:])
                         if token is not None],
            "replaced_paths": collisions,
            "already_at_destination": same_location,
        })
        fingerprints.append({
            "id": archive_id, "source": str(source),
            "source_tokens": source_tokens, "target": str(target),
            "target_tokens": target_tokens,
        })
        entries.append({
            "source_paths": source_paths, "source_tokens": source_tokens,
            "target_paths": target_paths, "target_tokens": target_tokens,
            "same_location": same_location,
        })
    payload = {
        "profile": str(profile_dir.resolve(strict=False)),
        "destination_id": destination_id,
        "locations_revision": locations_snapshot()["revision"],
        "hidden_revision": inventory["hidden_revision"],
        "archives": fingerprints,
    }
    revision = hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()
    return ({
        "revision": revision, "profile": profile_dir.name,
        "destination": {"id": destination_id, "path": str(destination),
                        "label": location["label"]},
        "archives": public,
        "total_bytes": sum(item["size_bytes"] for item in public
                           if not item["already_at_destination"]),
        "collisions": [path for item in public for path in item["replaced_paths"]],
    }, entries)


def _stage_copy(source: Path, target: Path, expected: list[int]) -> Path:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    staged = None
    try:
        descriptor = os.open(source, flags)
        info = os.fstat(descriptor)
        current = [info.st_dev, info.st_ino, info.st_mode, info.st_size,
                   info.st_mtime_ns, info.st_ctime_ns]
        if current != expected:
            raise JobError("download_move_plan_changed",
                           "An archive changed while preparing its move.",
                           {"path": str(source)})
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".amm-move-", dir=target.parent,
            delete=False,
        ) as output:
            staged = Path(output.name)
            with os.fdopen(descriptor, "rb") as input_file:
                descriptor = None
                shutil.copyfileobj(input_file, output)
                if os.fstat(input_file.fileno()).st_size != expected[3]:
                    raise JobError("download_move_plan_changed",
                                   "An archive changed during copying.",
                                   {"path": str(source)})
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), stat.S_IMODE(expected[2]) & 0o777)
        os.utime(staged, ns=(info.st_atime_ns, info.st_mtime_ns))
        if _token(source, required=False) != expected:
            raise JobError("download_move_plan_changed",
                           "An archive changed during copying.",
                           {"path": str(source)})
        return staged
    except BaseException:
        if staged is not None:
            staged.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _move_one(entry: dict[str, Any]) -> dict[str, Any]:
    from Nexus.nexus_download import _md5_cache_forget

    source_paths = entry["source_paths"]
    target_paths = entry["target_paths"]
    source_tokens = entry["source_tokens"]
    target_tokens = entry["target_tokens"]
    staged: list[tuple[Path, Path, bool]] = []
    linked_sources: set[Path] = set()
    backups: list[tuple[Path, Path]] = []
    placed: list[Path] = []
    source_deleted: list[Path] = []
    try:
        for source, target, token in zip(source_paths, target_paths, source_tokens):
            if token is not None:
                same_device = source.stat().st_dev == target.parent.stat().st_dev
                if same_device:
                    linked_sources.add(source)
                staged.append((source if same_device else
                               _stage_copy(source, target, token),
                               target, not same_device))
        if ([_token(path, required=False) for path in source_paths] != source_tokens
                or [_token(path, required=False) for path in target_paths] != target_tokens):
            raise JobError("download_move_plan_changed",
                           "An archive or destination changed during the move.",
                           {"path": str(source_paths[0])})
        for target, token in zip(target_paths, target_tokens):
            if token is None:
                continue
            if _token(target, required=False) != token:
                raise JobError("download_move_plan_changed",
                               "A destination changed during the move.",
                               {"path": str(target)})
            with tempfile.NamedTemporaryFile(
                prefix=".amm-backup-", dir=target.parent, delete=False,
            ) as placeholder:
                backup = Path(placeholder.name)
            try:
                target.replace(backup)
            except BaseException:
                backup.unlink(missing_ok=True)
                raise
            backups.append((backup, target))
        for staged_path, target, _temporary in staged:
            os.link(staged_path, target)
            placed.append(target)
        for source, token in zip(source_paths, source_tokens):
            if token is not None:
                current = _token(source, required=False)
                unchanged = (current is not None and current[:5] == token[:5]
                             if source in linked_sources else current == token)
                if not unchanged:
                    raise JobError("download_move_plan_changed",
                                   "An archive changed before source cleanup.",
                                   {"path": str(source)})
                source.unlink()
                source_deleted.append(source)
        for backup, _target in backups:
            backup.unlink(missing_ok=True)
        _md5_cache_forget(source_paths[0])
        _md5_cache_forget(target_paths[0])
        return {"source": str(source_paths[0]),
                "destination": str(target_paths[0])}
    except BaseException as exc:
        if source_deleted:
            raise JobError("download_move_incomplete",
                           "The destination was written but final cleanup failed.",
                           {"source": str(source_paths[0]),
                            "destination": str(target_paths[0]),
                            "reason": str(exc),
                            "deleted_sources": [str(path) for path in source_deleted],
                            "backups": [str(path) for path, _ in backups]}) from exc
        rollback_errors: list[str] = []
        for target in placed:
            try:
                target.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        for backup, target in reversed(backups):
            try:
                os.link(backup, target)
                backup.unlink()
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        if rollback_errors:
            raise JobError("download_move_rollback_failed",
                           "Could not restore the destination after a failed move.",
                           {"source": str(source_paths[0]),
                            "destination": str(target_paths[0]),
                            "rollback_errors": rollback_errors,
                            "backups": [str(path) for path, _ in backups]}) from exc
        raise
    finally:
        for staged_path, _target, temporary in staged:
            if temporary:
                staged_path.unlink(missing_ok=True)


def plan_move_runner(
    game: Any, profile_dir: Path, archive_ids: list[str], destination_id: str,
) -> Callable:
    def run(_log: Callable[..., None], progress: Callable[..., None]) -> dict:
        from Utils.operations import acquire_game_operation, acquire_resource_operation

        with acquire_game_operation(
            game, "downloads.move.plan", owner="headless:downloads.move.plan",
        ), acquire_resource_operation(
            "downloads.settings", "downloads.move.plan",
            owner="headless:downloads.move.plan",
        ):
            progress(0, 1, "Inspecting archive move")
            plan, _entries = _move_plan(
                game, profile_dir, archive_ids, destination_id)
            progress(1, 1, "Archive move plan ready")
            return {"plan": plan}
    return run


def move_runner(
    game: Any, profile_dir: Path, archive_ids: list[str],
    destination_id: str, revision: str, if_exists: str,
) -> Callable:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict:
        from Utils.downloads.locations import (
            archive_path_key, load_hidden_archive_paths,
            save_hidden_archive_paths,
        )
        from Utils.operations import acquire_game_operation, acquire_resource_operation

        with acquire_game_operation(
            game, "downloads.move", owner="headless:downloads.move",
        ), acquire_resource_operation(
            "downloads.settings", "downloads.move",
            owner="headless:downloads.move",
        ):
            plan, entries = _move_plan(
                game, profile_dir, archive_ids, destination_id)
            if plan["revision"] != revision:
                raise JobError("download_move_plan_changed",
                               "The archive move changed; inspect the plan again.",
                               {"plan": plan})
            if plan["collisions"] and if_exists == "fail":
                raise JobError("download_move_target_exists",
                               "The destination contains files that would be replaced.",
                               {"collisions": plan["collisions"]})
            moved: list[dict[str, str]] = []
            try:
                for index, entry in enumerate(entries):
                    progress(index, len(entries), "Moving downloaded archives")
                    if entry["same_location"]:
                        continue
                    result = _move_one(entry)
                    moved.append(result)
                    hidden_paths = load_hidden_archive_paths()
                    old_key = archive_path_key(result["source"])
                    new_key = archive_path_key(result["destination"])
                    if old_key in hidden_paths:
                        hidden_paths.discard(old_key)
                        hidden_paths.add(new_key)
                        save_hidden_archive_paths(hidden_paths)
                    log(f"Moved downloaded archive: {Path(result['source']).name}")
            except (JobError, OSError) as exc:
                if isinstance(exc, JobError):
                    name, details = exc.name, exc.details
                else:
                    name, details = "download_move_failed", {"reason": str(exc)}
                raise JobError(name, str(exc),
                               {**details, "moved": moved}) from exc
            progress(len(entries), len(entries), "Downloaded archives moved")
            return {"moved": moved, "count": len(moved),
                    "skipped": len(entries) - len(moved),
                    "replaced_paths": plan["collisions"] if if_exists == "replace" else []}
    return run
