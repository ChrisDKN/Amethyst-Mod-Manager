from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat as stat_types
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from Utils.filegraph.paths import EXCLUDE_NAMES, is_macos_junk


LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, str | None], None]
BOUNDARIES = ("overwrite", "root_folder")


class BoundaryError(RuntimeError):
    def __init__(self, name: str, message: str,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


@contextmanager
def _preserve_active_profile(game: Any, log: LogFn):
    previous = getattr(game, "_active_profile_dir", None)
    try:
        yield
    finally:
        if getattr(game, "_active_profile_dir", None) != previous:
            try:
                game.set_active_profile_dir(previous)
                game.load_paths()
            except Exception as exc:
                log(f"WARN previous profile context could not be restored: {exc}")


def is_hidden_name(name: str) -> bool:
    return name in EXCLUDE_NAMES or is_macos_junk(name)


def list_boundary_tree(
    root: Path,
    error_fn: Callable[[str], None] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Return visible files, empty folders, and watchable folders."""
    root = Path(root)
    files: list[str] = []
    empty_dirs: list[str] = []
    watch_dirs = [str(root)]
    stack = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        visible_child = False
        try:
            iterator = os.scandir(directory)
        except OSError as exc:
            if error_fn is not None:
                error_fn(f"{prefix or '.'}: {exc}")
            continue
        with iterator:
            for entry in iterator:
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                relative = relative.replace(os.sep, "/")
                path = Path(entry.path)
                try:
                    if entry.is_symlink() or entry.is_file(
                            follow_symlinks=False):
                        if is_hidden_name(entry.name):
                            continue
                        files.append(relative)
                        visible_child = True
                    elif entry.is_dir(follow_symlinks=False):
                        if is_macos_junk(entry.name):
                            continue
                        watch_dirs.append(str(path))
                        stack.append((path, relative))
                        visible_child = True
                    elif not is_hidden_name(entry.name):
                        visible_child = True
                except OSError as exc:
                    if error_fn is not None:
                        error_fn(f"{relative}: {exc}")
                    continue
        if prefix and not visible_child:
            empty_dirs.append(prefix)
    return files, empty_dirs, watch_dirs


def entry_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def safe_child(root: Path, relative: str) -> Path | None:
    path = Path(relative)
    if (not path.parts or path.is_absolute()
            or any(part in ("", ".", "..") for part in path.parts)):
        return None
    candidate = Path(root).joinpath(*path.parts)
    try:
        resolved_root = Path(root).resolve(strict=False)
        resolved_parent = candidate.parent.resolve(strict=False)
        resolved_parent.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def safe_mod_root(staging: Path, name: str) -> Path | None:
    path = Path(name)
    if path.is_absolute() or len(path.parts) != 1 or path.name != name:
        return None
    return safe_child(staging, name)


def move_relative(
    source_root: Path,
    relative: str,
    destination_root: Path,
) -> tuple[bool, list[str]]:
    source = safe_child(source_root, relative)
    destination = safe_child(destination_root, relative)
    if source is None or destination is None:
        return False, [f"{relative}: path is outside its managed folder"]
    if not entry_exists(source):
        return False, [f"{relative}: source no longer exists"]

    source_is_dir = source.is_dir() and not source.is_symlink()
    destination_exists = entry_exists(destination)
    destination_is_dir = (
        destination_exists
        and destination.is_dir()
        and not destination.is_symlink()
    )

    if source_is_dir and destination_is_dir:
        try:
            children = list(source.iterdir())
        except OSError as exc:
            return False, [f"{relative}: {exc}"]
        moved = False
        failed: list[str] = []
        for child in children:
            child_relative = f"{relative}/{child.name}"
            child_moved, child_failed = move_relative(
                source_root, child_relative, destination_root)
            moved = moved or child_moved
            failed.extend(child_failed)
        try:
            source.rmdir()
            moved = True
        except OSError:
            pass
        return moved, failed

    if source_is_dir and destination_exists:
        return False, [
            f"{relative}: target is not a directory; not replaced"]

    if not source_is_dir and destination_is_dir:
        try:
            if any(destination.iterdir()):
                return False, [
                    f"{relative}: target is a non-empty directory; "
                    "not replaced"]
            destination.rmdir()
        except OSError as exc:
            return False, [f"{relative}: {exc}"]
    elif destination_exists:
        try:
            source.replace(destination)
            return True, []
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                return False, [f"{relative}: {exc}"]
            temporary = None
            try:
                temporary = (
                    destination.parent / f".amethyst-move-{uuid4().hex}")
                if source.is_symlink():
                    temporary.symlink_to(
                        source.readlink(), target_is_directory=source.is_dir())
                else:
                    shutil.copy2(source, temporary)
                temporary.replace(destination)
            except (OSError, ValueError, shutil.Error) as copy_exc:
                try:
                    if temporary is not None and entry_exists(temporary):
                        temporary.unlink()
                except OSError:
                    pass
                return False, [f"{relative}: {copy_exc}"]
            try:
                source.unlink()
            except OSError as unlink_exc:
                return True, [
                    f"{relative}: copied, but source could not be removed: "
                    f"{unlink_exc}"]
            return True, []

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return True, []
    except (OSError, ValueError, shutil.Error) as exc:
        return False, [f"{relative}: {exc}"]


def delete_relative(root: Path, relative: str) -> tuple[bool, str | None]:
    target = safe_child(root, relative)
    if target is None:
        return False, f"{relative}: path is outside its managed folder"
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        return True, None
    except OSError as exc:
        return False, f"{relative}: {exc}"


def prune_empty_dirs(root: Path) -> None:
    root = Path(root)
    if not root.is_dir():
        return
    try:
        directories = sorted(
            (
                path for path in root.rglob("*")
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: len(path.parts),
            reverse=True,
        )
    except OSError:
        return
    for directory in directories:
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            continue


def _load_boundary(
    game: Any,
    profile_dir: Path,
    boundary: str,
) -> tuple[Path, Path]:
    profile_dir = Path(profile_dir)
    if boundary not in BOUNDARIES:
        raise BoundaryError(
            "boundary_not_supported",
            f"Boundary '{boundary}' is not supported.",
            {"boundary": boundary, "supported": list(BOUNDARIES)},
        )
    if not profile_dir.is_dir():
        raise BoundaryError(
            "profile_not_found",
            f"Profile '{profile_dir.name}' is no longer available.",
            {"profile": profile_dir.name},
        )
    try:
        game.set_active_profile_dir(profile_dir)
        game.load_paths()
        staging = Path(game.get_effective_mod_staging_path())
        root = Path(
            game.get_effective_overwrite_path()
            if boundary == "overwrite"
            else game.get_effective_root_folder_path()
        )
    except Exception as exc:
        raise BoundaryError(
            "boundary_unavailable",
            f"Could not resolve the profile's {boundary} folder.",
            {
                "boundary": boundary,
                "profile": profile_dir.name,
                "reason": str(exc),
            },
        ) from exc
    return root, staging


def _modified_at(seconds: float) -> str | None:
    try:
        return datetime.fromtimestamp(
            seconds, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return None


def _inventory_entries(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    files, empty_dirs, watch_dirs = list_boundary_tree(
        root, error_fn=warnings.append)
    empty = set(empty_dirs)
    paths = list(files)
    for directory in watch_dirs:
        path = Path(directory)
        if path == root:
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        paths.append(relative)

    entries: list[dict[str, Any]] = []
    for relative in paths:
        path = safe_child(root, relative)
        if path is None:
            warnings.append(f"{relative}: path is outside its managed folder")
            continue
        try:
            stat = path.lstat()
        except OSError as exc:
            warnings.append(f"{relative}: {exc}")
            continue
        if stat_types.S_ISLNK(stat.st_mode):
            kind = "symlink"
        elif stat_types.S_ISDIR(stat.st_mode):
            kind = "directory"
        elif stat_types.S_ISREG(stat.st_mode):
            kind = "file"
        else:
            warnings.append(f"{relative}: unsupported filesystem entry")
            continue
        size = int(stat.st_size) if kind == "file" else None
        entries.append({
            "path": relative,
            "kind": kind,
            "size": size,
            "modified_at": _modified_at(stat.st_mtime),
            "empty": relative in empty if kind == "directory" else None,
            "_modified_ns": int(stat.st_mtime_ns),
            "_changed_ns": int(stat.st_ctime_ns),
            "_device": int(stat.st_dev),
            "_inode": int(stat.st_ino),
        })
    entries.sort(key=lambda item: (item["path"].casefold(), item["path"]))
    return entries, warnings


def _inventory_revision(
    boundary: str,
    entries: list[dict[str, Any]],
    *,
    exists: bool,
) -> str:
    digest = hashlib.sha256()
    digest.update(boundary.encode("ascii"))
    digest.update(b"\0")
    digest.update(b"1\0" if exists else b"0\0")
    for entry in entries:
        for value in (
            entry["path"], entry["kind"], entry.get("size"),
            entry.get("empty"), entry["_modified_ns"], entry["_changed_ns"],
            entry["_device"], entry["_inode"],
        ):
            digest.update(str(value).encode("utf-8", "surrogatepass"))
            digest.update(b"\0")
    return "boundary_" + digest.hexdigest()


def list_profile_boundary(
    game: Any,
    profile_dir: Path,
    boundary: str,
    *,
    offset: int = 0,
    limit: int = 500,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    profile_dir = Path(profile_dir)
    with acquire_game_operation(
        game, "boundaries.list", owner=operation_owner or "boundaries.list",
    ), _preserve_active_profile(game, log):
        progress(0, 2, "Resolving managed folder")
        root, staging = _load_boundary(game, profile_dir, boundary)
        progress(1, 2, "Scanning managed folder")
        exists = root.is_dir()
        entries, warnings = (
            _inventory_entries(root) if exists else ([], []))
        revision = _inventory_revision(boundary, entries, exists=exists)
        total = len(entries)
        page = entries[offset:offset + limit]
        regular_files = [item for item in entries if item["kind"] == "file"]
        for item in entries:
            for key in ("_modified_ns", "_changed_ns", "_device", "_inode"):
                item.pop(key, None)
        progress(2, 2, "Managed folder inventory ready")
        next_offset = offset + len(page)
        return {
            "ok": True,
            "profile": {"name": profile_dir.name, "path": str(profile_dir)},
            "boundary": boundary,
            "path": str(root),
            "staging_path": str(staging),
            "scope": (
                "profile" if root.parent == profile_dir else "shared"),
            "exists": exists,
            "revision": revision,
            "counts": {
                "entries": total,
                "files": len(regular_files),
                "directories": sum(
                    item["kind"] == "directory" for item in entries),
                "symlinks": sum(
                    item["kind"] == "symlink" for item in entries),
                "bytes": sum(item["size"] or 0 for item in regular_files),
            },
            "page": {
                "offset": offset,
                "limit": limit,
                "returned": len(page),
                "total": total,
                "next_offset": next_offset if next_offset < total else None,
            },
            "entries": page,
            "warnings": warnings,
        }
