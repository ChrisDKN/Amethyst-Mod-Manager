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
        except OSError as exc:
            failed.append(f"{relative}: {exc}")
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


def _entry_kind(path: Path) -> str | None:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return None
    if stat_types.S_ISLNK(mode):
        return "symlink"
    if stat_types.S_ISDIR(mode):
        return "directory"
    if stat_types.S_ISREG(mode):
        return "file"
    return None


def _move_inventory_entry(
    source_root: Path,
    relative: str,
    destination_root: Path,
    expected_kind: str,
) -> tuple[bool, list[str]]:
    source = safe_child(source_root, relative)
    destination = safe_child(destination_root, relative)
    if source is None or destination is None:
        return False, [f"{relative}: path is outside its managed folder"]
    actual_kind = _entry_kind(source)
    if actual_kind != expected_kind:
        return False, [
            f"{relative}: entry changed from {expected_kind} to "
            f"{actual_kind or 'missing'}"]

    if expected_kind == "directory":
        if entry_exists(destination):
            if not destination.is_dir() or destination.is_symlink():
                return False, [
                    f"{relative}: target is not a directory; not replaced"]
            try:
                source.rmdir()
                return True, []
            except OSError as exc:
                return False, [f"{relative}: {exc}"]
        try:
            source.rmdir()
        except OSError as exc:
            return False, [f"{relative}: {exc}"]
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.mkdir(exist_ok=False)
            return True, []
        except OSError as exc:
            rollback_error = None
            try:
                source.mkdir(exist_ok=False)
            except OSError as rollback_exc:
                rollback_error = str(rollback_exc)
            message = f"{relative}: {exc}"
            if rollback_error:
                message += f"; source restore failed: {rollback_error}"
            return True, [message]

    target_changed = False
    if (entry_exists(destination) and destination.is_dir()
            and not destination.is_symlink()):
        try:
            if any(destination.iterdir()):
                return False, [
                    f"{relative}: target is a non-empty directory; "
                    "not replaced"]
            destination.rmdir()
            target_changed = True
        except OSError as exc:
            return False, [f"{relative}: {exc}"]
    changed, errors = move_relative(
        source_root, relative, destination_root)
    return changed or target_changed, errors


def _remove_inventory_entry(
    root: Path,
    relative: str,
    expected_kind: str,
) -> tuple[bool, str | None]:
    target = safe_child(root, relative)
    if target is None:
        return False, f"{relative}: path is outside its managed folder"
    actual_kind = _entry_kind(target)
    if actual_kind != expected_kind:
        return (
            False,
            f"{relative}: entry changed from {expected_kind} to "
            f"{actual_kind or 'missing'}",
        )
    try:
        if expected_kind == "directory":
            target.rmdir()
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
    files, _empty_dirs, watch_dirs = list_boundary_tree(
        root, error_fn=warnings.append)
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
        empty = None
        if kind == "directory":
            try:
                empty = not any(path.iterdir())
            except OSError as exc:
                warnings.append(f"{relative}: {exc}")
                empty = False
        entries.append({
            "path": relative,
            "kind": kind,
            "size": size,
            "modified_at": _modified_at(stat.st_mtime),
            "empty": empty,
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


def _boundary_state(
    root: Path,
    boundary: str,
) -> tuple[bool, list[dict[str, Any]], list[str], str]:
    exists = root.is_dir()
    entries, warnings = _inventory_entries(root) if exists else ([], [])
    revision = _inventory_revision(boundary, entries, exists=exists)
    return exists, entries, warnings, revision


def _selected_entries(
    root: Path,
    boundary: str,
    paths: list[str],
    revision: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not paths or len(set(paths)) != len(paths):
        raise BoundaryError(
            "invalid_boundary_selection",
            "Boundary paths must be non-empty and unique.",
        )
    exists, entries, warnings, current = _boundary_state(root, boundary)
    if current != revision:
        raise BoundaryError(
            "boundary_revision_changed",
            "The managed folder changed after it was listed.",
            {
                "boundary": boundary,
                "expected_revision": revision,
                "current_revision": current,
            },
        )
    if not exists:
        raise BoundaryError(
            "boundary_unavailable",
            f"The profile's {boundary} folder does not exist.",
            {"boundary": boundary, "revision": current},
        )
    by_path = {entry["path"]: entry for entry in entries}
    missing = [path for path in paths if path not in by_path]
    if missing:
        raise BoundaryError(
            "boundary_entry_not_found",
            "One or more selected boundary entries are no longer available.",
            {
                "boundary": boundary,
                "paths": missing[:100],
                "missing_count": len(missing),
                "revision": current,
            },
        )
    non_empty = [
        path for path in paths
        if (by_path[path]["kind"] == "directory"
            and not by_path[path]["empty"])
    ]
    if non_empty:
        raise BoundaryError(
            "boundary_directory_not_empty",
            "Non-empty directories must be submitted as their visible files "
            "and empty subdirectories.",
            {
                "boundary": boundary,
                "paths": non_empty[:100],
                "count": len(non_empty),
                "revision": current,
            },
        )
    return [by_path[path] for path in paths], warnings


def _target_mod(
    profile_dir: Path,
    staging: Path,
    boundary: str,
    name: str,
    *,
    create: bool,
) -> Path:
    from Utils.mods.modlist import modlist_lock, read_modlist
    from Utils.mods.names import sanitize_mod_folder_name

    if not name:
        raise BoundaryError(
            "invalid_mod_name",
            f"'{name}' is not a safe mod folder name.",
            {"mod": name},
        )
    modlist_path = profile_dir / "modlist.txt"
    with modlist_lock(modlist_path):
        entries = read_modlist(modlist_path)
    names = {
        value.casefold()
        for entry in entries
        for value in (entry.name, entry.display_name)
    }
    try:
        disk_names = {
            entry.name.casefold() for entry in staging.iterdir()
        }
    except OSError:
        disk_names = set()
    destination = safe_mod_root(staging, name)
    if destination is None:
        raise BoundaryError(
            "invalid_mod_name",
            f"'{name}' is not a safe mod folder name.",
            {"mod": name},
        )

    if create:
        if (name.casefold().endswith("_separator")
                or sanitize_mod_folder_name(name) != name):
            raise BoundaryError(
                "invalid_mod_name",
                f"'{name}' is not a safe mod folder name.",
                {"mod": name, "suggested": sanitize_mod_folder_name(name)},
            )
        if name.casefold() in names or name.casefold() in disk_names:
            raise BoundaryError(
                "mod_already_exists",
                f"A mod or separator named '{name}' already exists.",
                {"mod": name},
            )
        return destination

    matches = [
        entry for entry in entries
        if not entry.is_separator and entry.name == name
    ]
    if not matches:
        casing = [
            entry.name for entry in entries
            if not entry.is_separator and entry.name.casefold() == name.casefold()
        ]
        raise BoundaryError(
            "mod_not_found",
            f"Mod '{name}' was not found in the profile.",
            {"mod": name, "casing_match": casing[0] if len(casing) == 1 else None},
        )
    if len(matches) > 1:
        raise BoundaryError(
            "ambiguous_mod",
            f"Mod name '{name}' appears more than once in the profile.",
            {"mod": name, "matches": len(matches)},
        )
    if destination.is_symlink():
        try:
            from Utils.profiles.groups import entry_owner_profile, is_group
            safe_group_link = (
                is_group(profile_dir)
                and entry_owner_profile(profile_dir, name) is not None
            )
        except Exception:
            safe_group_link = False
        if not safe_group_link:
            raise BoundaryError(
                "boundary_target_unsafe",
                f"The staging folder for '{name}' is an unmanaged symlink.",
                {"mod": name},
            )
    if not destination.is_dir():
        raise BoundaryError(
            "mod_files_unavailable",
            f"The staging folder for '{name}' is not available.",
            {"mod": name},
        )
    try:
        from Nexus.nexus_meta import read_meta
        root_folder = bool(read_meta(destination / "meta.ini").root_folder)
    except Exception:
        root_folder = False
    expected_root = boundary == "root_folder"
    if root_folder != expected_root:
        raise BoundaryError(
            "boundary_target_incompatible",
            f"Mod '{name}' does not use the same deployment root as "
            f"{boundary}.",
            {
                "mod": name,
                "boundary": boundary,
                "target_root_folder": root_folder,
                "required_root_folder": expected_root,
            },
        )
    return destination


def _create_target_mod(
    profile_dir: Path,
    staging: Path,
    boundary: str,
    name: str,
) -> Path:
    from Nexus.nexus_meta import NexusModMeta, write_meta
    from Utils.mods.modlist import (
        ModEntry,
        modlist_lock,
        read_modlist,
        write_modlist,
    )

    staging.mkdir(parents=True, exist_ok=True)
    destination = _target_mod(
        profile_dir, staging, boundary, name, create=True)
    modlist_path = profile_dir / "modlist.txt"
    created = False
    original = []
    rollback_error = None
    try:
        with modlist_lock(modlist_path):
            original = read_modlist(modlist_path)
            conflicts = {
                value.casefold()
                for entry in original
                for value in (entry.name, entry.display_name)
            }
            try:
                disk_conflict = any(
                    entry.name.casefold() == name.casefold()
                    for entry in staging.iterdir()
                )
            except OSError:
                disk_conflict = entry_exists(destination)
            if name.casefold() in conflicts or disk_conflict:
                raise BoundaryError(
                    "mod_already_exists",
                    f"A mod or separator named '{name}' already exists.",
                    {"mod": name},
                )
            destination.mkdir(exist_ok=False)
            created = True
            write_meta(
                destination / "meta.ini",
                NexusModMeta(
                    mod_name=name,
                    version="1.0",
                    installed=datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S"),
                    root_folder=boundary == "root_folder",
                ),
            )
            write_modlist(
                modlist_path,
                [ModEntry(name, True, False), *original],
            )
    except BoundaryError:
        raise
    except Exception as exc:
        if created:
            try:
                with modlist_lock(modlist_path):
                    write_modlist(modlist_path, original)
            except Exception as rollback_exc:
                rollback_error = str(rollback_exc)
            try:
                shutil.rmtree(destination)
            except OSError as rollback_exc:
                rollback_error = rollback_error or str(rollback_exc)
        raise BoundaryError(
            "mod_create_failed",
            f"Could not create target mod '{name}'.",
            {
                "mod": name,
                "reason": str(exc),
                "rollback_error": rollback_error,
            },
        ) from exc
    return destination


def _check_separate_roots(source: Path, destination: Path) -> None:
    try:
        source_root = source.resolve(strict=False)
        destination_root = destination.resolve(strict=False)
        overlaps = (
            source_root == destination_root
            or source_root.is_relative_to(destination_root)
            or destination_root.is_relative_to(source_root)
        )
    except (OSError, RuntimeError, ValueError):
        overlaps = True
    if overlaps:
        raise BoundaryError(
            "boundary_target_unsafe",
            "The boundary and target mod folders overlap.",
        )


def _prepare_filegraph(
    game: Any,
    profile_dir: Path,
    log: LogFn,
):
    try:
        from Utils.filegraph.service import FileGraphService
        library = FileGraphService.open_library(
            game, profile_dir, log_fn=log)
        library.ensure_ready(profile_dir)
        return library, library.open_profile(profile_dir)
    except Exception as exc:
        raise BoundaryError(
            "boundary_sync_unavailable",
            "Filegraph could not be prepared; no boundary files were changed.",
            {"reason": str(exc), "disk_changed": False},
        ) from exc


def _sync_filegraph(
    library,
    profile,
    profile_dir: Path,
    mod_names: list[str],
    *,
    kind: str,
    details: dict[str, Any],
    disk_changed: bool,
) -> tuple[bool, int]:
    try:
        library.refresh(profile_dir, mod_names=mod_names)
        reconciled = profile.ensure_reconciled(operation_hint={
            "kind": kind,
            "mods": list(mod_names),
        })
        return bool(reconciled), int(profile.snapshot().generation)
    except Exception as exc:
        message = (
            "The boundary files changed, but Filegraph synchronization failed."
            if disk_changed else
            "Filegraph synchronization failed after the boundary operation."
        )
        raise BoundaryError(
            "boundary_sync_failed",
            message,
            {
                **details,
                "stage": "filegraph",
                "disk_changed": disk_changed,
                "reason": str(exc),
            },
        ) from exc


def _boundary_catalog_name(boundary: str) -> str:
    from Utils.filegraph.constants import OVERWRITE_NAME, ROOT_FOLDER_NAME
    return OVERWRITE_NAME if boundary == "overwrite" else ROOT_FOLDER_NAME


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
        exists, entries, warnings, revision = _boundary_state(root, boundary)
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


def move_profile_boundary(
    game: Any,
    profile_dir: Path,
    boundary: str,
    paths: list[str],
    revision: str,
    target_mod: str,
    *,
    create_mod: bool = False,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    profile_dir = Path(profile_dir)
    with acquire_game_operation(
        game, "boundaries.move", owner=operation_owner or "boundaries.move",
    ), _preserve_active_profile(game, log):
        progress(0, 4, "Validating boundary revision")
        root, staging = _load_boundary(game, profile_dir, boundary)
        _selected_entries(root, boundary, paths, revision)
        destination = _target_mod(
            profile_dir, staging, boundary, target_mod, create=create_mod)
        _check_separate_roots(root, destination)

        progress(1, 4, "Preparing Filegraph")
        library, profile = _prepare_filegraph(game, profile_dir, log)
        selected, warnings = _selected_entries(
            root, boundary, paths, revision)
        destination = (
            _create_target_mod(
                profile_dir, staging, boundary, target_mod)
            if create_mod else
            _target_mod(
                profile_dir, staging, boundary, target_mod, create=False)
        )
        _check_separate_roots(root, destination)

        progress(2, 4, "Moving boundary files")
        moved: list[str] = []
        changed: list[str] = []
        failures: list[dict[str, str]] = []
        for entry in selected:
            relative = str(entry["path"])
            did_change, errors = _move_inventory_entry(
                root, relative, destination, str(entry["kind"]))
            if did_change:
                changed.append(relative)
            source = safe_child(root, relative)
            if (did_change and source is not None
                    and not entry_exists(source)):
                moved.append(relative)
            for message in errors:
                failures.append({"path": relative, "message": message})
        prune_empty_dirs(root)

        _exists, _entries, final_warnings, current_revision = (
            _boundary_state(root, boundary))
        details: dict[str, Any] = {
            "profile": profile_dir.name,
            "boundary": boundary,
            "revision_before": revision,
            "revision": current_revision,
            "target_mod": {
                "name": target_mod,
                "created": create_mod,
                "root_folder": boundary == "root_folder",
            },
            "requested": list(paths),
            "moved": moved,
            "changed": changed,
            "failed": failures,
            "warnings": [*warnings, *final_warnings],
        }

        progress(3, 4, "Synchronizing Filegraph")
        reconciled, generation = _sync_filegraph(
            library,
            profile,
            profile_dir,
            [_boundary_catalog_name(boundary), target_mod],
            kind="boundary_move",
            details=details,
            disk_changed=(
                create_mod or bool(changed) or current_revision != revision),
        )
        details.update({
            "filegraph_reconciled": reconciled,
            "filegraph_generation": generation,
        })
        progress(4, 4, "Boundary files moved")
        if failures or len(moved) != len(paths):
            raise BoundaryError(
                "boundary_move_incomplete",
                "Not every selected boundary entry could be moved.",
                details,
            )
        log(
            f"Moved {len(moved)} {boundary} entr"
            f"{'y' if len(moved) == 1 else 'ies'} into '{target_mod}'."
        )
        return {"ok": True, **details}


def remove_profile_boundary(
    game: Any,
    profile_dir: Path,
    boundary: str,
    paths: list[str],
    revision: str,
    *,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    profile_dir = Path(profile_dir)
    with acquire_game_operation(
        game, "boundaries.remove", owner=operation_owner or "boundaries.remove",
    ), _preserve_active_profile(game, log):
        progress(0, 4, "Validating boundary revision")
        root, _staging = _load_boundary(game, profile_dir, boundary)
        _selected_entries(root, boundary, paths, revision)

        progress(1, 4, "Preparing Filegraph")
        library, profile = _prepare_filegraph(game, profile_dir, log)
        selected, warnings = _selected_entries(
            root, boundary, paths, revision)

        progress(2, 4, "Removing boundary files")
        removed: list[str] = []
        failures: list[dict[str, str]] = []
        for entry in selected:
            relative = str(entry["path"])
            did_remove, error = _remove_inventory_entry(
                root, relative, str(entry["kind"]))
            if did_remove:
                removed.append(relative)
            elif error:
                failures.append({"path": relative, "message": error})
        prune_empty_dirs(root)

        _exists, _entries, final_warnings, current_revision = (
            _boundary_state(root, boundary))
        details: dict[str, Any] = {
            "profile": profile_dir.name,
            "boundary": boundary,
            "revision_before": revision,
            "revision": current_revision,
            "requested": list(paths),
            "removed": removed,
            "failed": failures,
            "warnings": [*warnings, *final_warnings],
        }
        progress(3, 4, "Synchronizing Filegraph")
        reconciled, generation = _sync_filegraph(
            library,
            profile,
            profile_dir,
            [_boundary_catalog_name(boundary)],
            kind="boundary_remove",
            details=details,
            disk_changed=current_revision != revision,
        )
        details.update({
            "filegraph_reconciled": reconciled,
            "filegraph_generation": generation,
        })
        progress(4, 4, "Boundary files removed")
        if failures or len(removed) != len(paths):
            raise BoundaryError(
                "boundary_remove_incomplete",
                "Not every selected boundary entry could be removed.",
                details,
            )
        log(
            f"Removed {len(removed)} {boundary} entr"
            f"{'y' if len(removed) == 1 else 'ies'}."
        )
        return {"ok": True, **details}
