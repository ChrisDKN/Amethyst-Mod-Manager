from __future__ import annotations

import errno
import shutil
from pathlib import Path
from uuid import uuid4

from Utils.filegraph.paths import EXCLUDE_NAMES, is_macos_junk


def is_hidden_name(name: str) -> bool:
    return name in EXCLUDE_NAMES or is_macos_junk(name)


def list_boundary_tree(
    root: Path,
) -> tuple[list[str], list[str], list[str]]:
    """Return visible files, empty folders, and watchable folders."""
    root = Path(root)
    files: list[str] = []
    empty_dirs: list[str] = []
    watch_dirs = [str(root)]
    try:
        for path in root.rglob("*"):
            try:
                if path.is_symlink() or path.is_file():
                    if is_hidden_name(path.name):
                        continue
                    files.append(path.relative_to(root).as_posix())
                elif path.is_dir():
                    if is_macos_junk(path.name):
                        continue
                    watch_dirs.append(str(path))
                    if not any(
                        child for child in path.iterdir()
                        if not is_hidden_name(child.name)
                    ):
                        empty_dirs.append(path.relative_to(root).as_posix())
            except OSError:
                continue
    except OSError:
        pass
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
