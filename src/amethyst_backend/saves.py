from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from Utils.saves.paths import matches_patterns, save_paths_for_game

from .jobs import JobError


@contextmanager
def _selected_profile(game: Any, profile_dir: Path) -> Iterator[None]:
    previous = getattr(game, "_active_profile_dir", None)
    game.set_active_profile_dir(profile_dir)
    try:
        game.load_paths(read_only=True)
        yield
    finally:
        game.set_active_profile_dir(previous)
        game.load_paths(read_only=True)


def _locations(game: Any, profile_dir: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if (getattr(game, "profile_saves", False)
            and callable(getattr(game, "_profile_saves_dir", None))):
        path = Path(game._profile_saves_dir(profile_dir.name))
        if path.is_dir():
            found.append({"path": path, "in_prefix": False,
                          "is_profile": True, "exists": True, "patterns": ()})
    for location in save_paths_for_game(game):
        path = Path(location.path)
        patterns = tuple(location.patterns)
        if path.is_file():
            patterns = (path.name,)
            path = path.parent
        found.append({"path": path,
                      "in_prefix": location.in_prefix,
                      "is_profile": False, "exists": location.exists,
                      "patterns": patterns})
    deduped: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for location in found:
        key = os.path.realpath(location["path"])
        if key in seen:
            previous = deduped[seen[key]]
            old, new = previous["patterns"], location["patterns"]
            previous["patterns"] = () if not old or not new else tuple(
                dict.fromkeys((*old, *new)))
            previous["exists"] = previous["exists"] or location["exists"]
            continue
        seen[key] = len(deduped)
        deduped.append(location)
    return deduped


def _location(game: Any, profile_dir: Path, selected: str) -> dict[str, Any]:
    for location in _locations(game, profile_dir):
        if str(location["path"]) == selected:
            return location
    raise JobError(
        "save_location_not_found",
        "The save location is no longer available for this game and profile.",
        {"location": selected},
    )


def _public_location(location: dict[str, Any]) -> dict[str, Any]:
    return {**location, "path": str(location["path"]),
            "patterns": list(location["patterns"])}


def _relative_parts(relative: str) -> tuple[str, ...]:
    if (not isinstance(relative, str) or not relative
            or relative.startswith("/") or "\\" in relative
            or "\x00" in relative):
        raise JobError("invalid_save_entry", "A relative save entry is required.")
    parts = tuple(relative.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise JobError("invalid_save_entry", "The save entry has an unsafe path.")
    return parts


def _entry(location: dict[str, Any], relative: str) -> Path:
    parts = _relative_parts(relative)
    if not matches_patterns(parts[0], location["patterns"]):
        raise JobError("save_entry_outside_pattern",
                       "The entry is outside this location's save pattern.",
                       {"entry": relative})
    root = location["path"].resolve()
    candidate = location["path"].joinpath(*parts)
    if not candidate.parent.resolve().is_relative_to(root):
        raise JobError("unsafe_save_path", "The save entry leaves its location.",
                       {"entry": relative})
    if not candidate.exists() and not candidate.is_symlink():
        raise JobError("save_entry_not_found", "The save entry is unavailable.",
                       {"entry": relative})
    return candidate


def _revision(path: Path) -> str:
    digest = hashlib.sha256()

    def include(entry: Path) -> None:
        info = entry.lstat()
        values = [str(entry), info.st_dev, info.st_ino, info.st_mode,
                  info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        digest.update(json.dumps(values).encode("utf-8"))

    def failed(error: OSError) -> None:
        raise error

    include(path)
    if path.is_dir() and not path.is_symlink():
        for parent, directories, files in os.walk(path, followlinks=False, onerror=failed):
            directories.sort()
            for name in sorted(directories + files):
                include(Path(parent) / name)
    return digest.hexdigest()


def _require_revision(path: Path, revision: str) -> None:
    if _revision(path) != revision:
        raise JobError("stale_save_entry",
                       "The save entry changed; list it again.",
                       {"entry": str(path)})


def list_locations(game: Any, profile_dir: Path) -> dict[str, Any]:
    try:
        with _selected_profile(game, profile_dir):
            locations = [_public_location(loc)
                         for loc in _locations(game, profile_dir)]
        return {"locations": locations}
    except JobError:
        raise
    except (OSError, ValueError) as exc:
        raise JobError("resource_unavailable",
                       "Could not resolve the game's save locations.",
                       {"reason": str(exc)}) from exc


def list_entries(game: Any, profile_dir: Path, selected: str,
                 relative: str, offset: int, limit: int) -> dict[str, Any]:
    try:
        with _selected_profile(game, profile_dir):
            location = _location(game, profile_dir, selected)
            root = location["path"]
            folder = _entry(location, relative) if relative else root
            if not relative and not folder.exists():
                return {"location": _public_location(location),
                        "directory": "", "total": 0, "offset": offset,
                        "limit": limit, "entries": []}
            if (relative and folder.is_symlink()) or not folder.is_dir():
                raise JobError("save_folder_not_found",
                               "The selected save folder is unavailable.",
                               {"folder": str(folder)})
            with os.scandir(folder) as children:
                rows = []
                for child in children:
                    if not relative and not matches_patterns(
                            child.name, location["patterns"]):
                        continue
                    try:
                        info = child.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    path = Path(child.path)
                    rows.append({
                        "name": child.name,
                        "entry": f"{relative}/{child.name}" if relative else child.name,
                        "kind": "link" if stat.S_ISLNK(info.st_mode) else
                                "directory" if stat.S_ISDIR(info.st_mode) else "file",
                        "size_bytes": None if stat.S_ISDIR(info.st_mode)
                                      else info.st_size,
                        "modified_ns": info.st_mtime_ns,
                        "revision": _revision(path),
                    })
            rows.sort(key=lambda row: (-row["modified_ns"], row["name"].casefold()))
            return {"location": _public_location(location),
                    "directory": relative, "total": len(rows),
                    "offset": offset, "limit": limit,
                    "entries": rows[offset:offset + limit]}
    except JobError:
        raise
    except OSError as exc:
        raise JobError("resource_unavailable", "Could not list the save folder.",
                       {"reason": str(exc)}) from exc


def export_location(game: Any, profile_dir: Path, selected: str,
                    destination: Path, overwrite: bool,
                    log: Callable[[str], None],
                    progress: Callable[[int, int, str | None], None]) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation
    from Utils.saves.transfer import SaveTransferError, export_saves

    try:
        with acquire_game_operation(game, "saves.export", owner="headless:saves.export"):
            with _selected_profile(game, profile_dir):
                location = _location(game, profile_dir, selected)
                source = location["path"]
                if not source.is_dir():
                    raise JobError("save_location_not_found",
                                   "The save folder does not exist.")
                if destination.exists() or destination.is_symlink():
                    if not overwrite:
                        raise JobError("save_archive_exists",
                                       "The export archive already exists.",
                                       {"path": str(destination)})
                    if destination.is_symlink() or not destination.is_file():
                        raise JobError("unsafe_save_path",
                                       "The export destination is not a regular file.")
                if destination.resolve().is_relative_to(source.resolve()):
                    raise JobError("unsafe_save_path",
                                   "Export outside the save folder.")
                files, size = export_saves(
                    source, destination, progress, location["patterns"])
        log(f"Exported {files} save file(s) to {destination}.")
        return {"files": files, "bytes": size, "archive_path": str(destination)}
    except JobError:
        raise
    except SaveTransferError as exc:
        raise JobError("save_export_failed", str(exc)) from exc
    except OSError as exc:
        raise JobError("save_export_failed", "Could not export saves.",
                       {"reason": str(exc)}) from exc


def import_location(game: Any, profile_dir: Path, selected: str,
                    archive: Path, log: Callable[[str], None],
                    progress: Callable[[int, int, str | None], None]) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation
    from Utils.saves.transfer import SaveTransferError, import_saves

    try:
        with acquire_game_operation(game, "saves.import", owner="headless:saves.import"):
            with _selected_profile(game, profile_dir):
                location = _location(game, profile_dir, selected)
                target = location["path"]
                if target.is_symlink() or (target.exists() and not target.is_dir()):
                    raise JobError("unsafe_save_path",
                                   "The save location is not a regular folder.")
                if archive.resolve().is_relative_to(target.resolve()):
                    raise JobError("unsafe_save_path",
                                   "Import an archive outside the save folder.")
                with zipfile.ZipFile(archive) as opened:
                    for item in opened.infolist():
                        if item.is_dir():
                            continue
                        top = item.filename.replace("\\", "/").split("/", 1)[0]
                        if not matches_patterns(top, location["patterns"]):
                            raise JobError(
                                "save_entry_outside_pattern",
                                "The archive contains files outside this save pattern.",
                                {"entry": item.filename},
                            )
                files, size, backup = import_saves(
                    archive, target, progress, backup=True,
                    patterns=location["patterns"])
        log(f"Imported {files} save file(s) into {target}.")
        return {"files": files, "bytes": size, "location": str(target),
                "backup_path": str(backup) if backup else None}
    except JobError:
        raise
    except SaveTransferError as exc:
        raise JobError("save_import_failed", str(exc)) from exc
    except (OSError, zipfile.BadZipFile) as exc:
        raise JobError("save_import_failed", "Could not import saves.",
                       {"reason": str(exc)}) from exc


def transfer_entry(game: Any, profile_dir: Path, selected: str,
                   relative: str, revision: str, target_profile: Path,
                   move: bool, overwrite: bool,
                   log: Callable[[str], None],
                   progress: Callable[[int, int, str | None], None]) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation
    from Utils.saves.transfer import SaveTransferError, transfer_save_entry

    try:
        with acquire_game_operation(game, "saves.transfer", owner="headless:saves.transfer"):
            with _selected_profile(game, profile_dir):
                location = _location(game, profile_dir, selected)
                source = _entry(location, relative)
                _require_revision(source, revision)
                getter = getattr(game, "_profile_saves_dir", None)
                if (not getattr(game, "supports_profile_saves", True)
                        or not callable(getter)):
                    raise JobError("profile_saves_unsupported",
                                   "This game does not support profile saves.")
                if not target_profile.is_dir() or target_profile.is_symlink():
                    raise JobError("profile_not_found",
                                   "The target profile is no longer available.",
                                   {"profile": target_profile.name})
                destination = Path(getter(target_profile.name))
                if destination.is_symlink():
                    raise JobError("unsafe_save_path",
                                   "The target profile saves folder is a symlink.")
                target = destination / source.name
                replaced = target.exists() or target.is_symlink()
                files, size, path = transfer_save_entry(
                    source, destination, move=move, overwrite=overwrite,
                    progress_fn=progress)
        log(f"{'Moved' if move else 'Copied'} {source} to {path}.")
        return {"files": files, "bytes": size, "destination": str(path),
                "moved": move, "overwrote": replaced}
    except JobError:
        raise
    except SaveTransferError as exc:
        raise JobError("save_transfer_failed", str(exc)) from exc
    except OSError as exc:
        raise JobError("save_transfer_failed", "Could not transfer the save.",
                       {"reason": str(exc)}) from exc


def remove_entry(game: Any, profile_dir: Path, selected: str,
                 relative: str, revision: str,
                 log: Callable[[str], None]) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation
    from Utils.saves.transfer import SaveTransferError, delete_save_entry

    try:
        with acquire_game_operation(game, "saves.remove", owner="headless:saves.remove"):
            with _selected_profile(game, profile_dir):
                location = _location(game, profile_dir, selected)
                source = _entry(location, relative)
                _require_revision(source, revision)
                files, size = delete_save_entry(source)
        log(f"Deleted save entry {source}.")
        return {"files": files, "bytes": size, "entry": relative}
    except JobError:
        raise
    except SaveTransferError as exc:
        raise JobError("save_remove_failed", str(exc)) from exc
    except OSError as exc:
        raise JobError("save_remove_failed", "Could not remove the save.",
                       {"reason": str(exc)}) from exc
