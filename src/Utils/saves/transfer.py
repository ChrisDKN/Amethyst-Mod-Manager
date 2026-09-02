"""Zip export / import of a game's save folder, plus single-save file ops.
Toolkit-neutral.

Archives are flat-rooted (names relative to the save folder), so one is
portable between machines whose prefixes live in different places. Import
refuses any member that would escape the destination. ``progress_fn(done,
total, phase)`` counts bytes, not files -one 30 MB save dwarfs a hundred
small ones.

``transfer_save_entry`` / ``delete_save_entry`` back the Saves tab's
right-click actions and work on ONE save (a file, or a folder of them).
"""

from __future__ import annotations

import os
import shutil
import time
import zipfile
from pathlib import Path

from Utils.saves.paths import matches_patterns

#: Files never worth carrying between machines.
_SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}

#: Ceiling on what an import will write. A compression-ratio bomb guard would
#: reject our OWN exports -save data beats 200:1 -so free space plus this cap
#: covers the real harm (filling the disk) instead.
_MAX_UNCOMPRESSED = 64 * 1024 ** 3
#: Leave this much headroom rather than filling the volume exactly.
_FREE_SPACE_MARGIN = 128 * 1024 ** 2


class SaveTransferError(Exception):
    """Export/import failed for a reason worth showing the user verbatim."""


def _walk_files(root: Path, patterns=()) -> "list[tuple[Path, str, int]]":
    """(absolute path, archive name, size) under *root*. Symlinks are skipped
    -a prefix save dir can hold dosdevices links to the whole filesystem.

    *patterns* limits the TOP level to the location's save files (see
    Utils.saves.paths.SaveLocation.patterns); a matching folder is taken whole."""
    out: list[tuple[Path, str, int]] = []
    top = str(root)
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        if patterns and dirpath == top:
            dirnames[:] = [d for d in dirnames if matches_patterns(d, patterns)]
        for name in filenames:
            if name in _SKIP_NAMES:
                continue
            if patterns and dirpath == top and not matches_patterns(name, patterns):
                continue
            full = Path(dirpath) / name
            if full.is_symlink():
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            out.append((full, str(full.relative_to(root)), size))
    return out


def export_saves(source: Path, dest_zip: Path, progress_fn=None,
                 patterns=()) -> tuple[int, int]:
    """Zip *source* into *dest_zip*, returning (files, bytes). Writes a temp
    file and renames, so a failed run leaves no half-written archive."""
    source = Path(source)
    dest_zip = Path(dest_zip)
    if not source.is_dir():
        raise SaveTransferError(f"Save folder not found: {source}")

    files = _walk_files(source, patterns)
    if not files:
        raise SaveTransferError(
            f"Nothing to export -no saves in {source}." if patterns
            else f"Nothing to export -{source} is empty.")
    total = sum(size for _f, _n, size in files)

    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_zip.with_name(dest_zip.name + f".part{os.getpid()}")
    done = 0
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as zf:
            for full, name, size in files:
                if progress_fn is not None:
                    progress_fn(done, total, name)
                try:
                    zf.write(full, name)
                except OSError as exc:
                    raise SaveTransferError(f"Could not read {name}: {exc}") from exc
                done += size
        os.replace(tmp, dest_zip)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if progress_fn is not None:
        progress_fn(total, total, "")
    return len(files), total


def _safe_members(zf: zipfile.ZipFile, dest: Path) -> list[zipfile.ZipInfo]:
    """Members that are plain files inside the destination, or raise."""
    members: list[zipfile.ZipInfo] = []
    uncompressed = 0
    for info in zf.infolist():
        name = info.filename
        if info.is_dir() or not name.strip("/\\ "):
            continue
        # Zip-slip: absolute paths, drive letters and ".." all escape the root.
        if name.startswith("/") or name.startswith("\\") or ":" in name.split("/")[0]:
            raise SaveTransferError(f"Refusing archive with an absolute path: {name}")
        if any(part == ".." for part in name.replace("\\", "/").split("/")):
            raise SaveTransferError(f"Refusing archive with a parent path: {name}")
        uncompressed += info.file_size
        members.append(info)
    if not members:
        raise SaveTransferError("The archive contains no files.")
    if uncompressed > _MAX_UNCOMPRESSED:
        raise SaveTransferError("The archive expands to an implausible size.")
    free = _free_space(dest)
    if free is not None and uncompressed + _FREE_SPACE_MARGIN > free:
        raise SaveTransferError(
            f"Not enough free space: the archive needs {uncompressed // 1024 ** 2} MB "
            f"but only {free // 1024 ** 2} MB is available.")
    return members


def _free_space(path: Path) -> "int | None":
    """Free bytes on the volume that will hold *path*, or None if unknown."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def backup_saves(location: Path, patterns=()) -> "Path | None":
    """Move the save folder aside before an import, returning its new path.
    A rename is atomic and free even on a multi-GB folder.

    With *patterns*, only the entries they claim move into a fresh backup
    folder -the location is then shared with files that are not saves (the
    game's own data), and moving the whole thing would take those too."""
    location = Path(location)
    if not location.is_dir():
        return None
    doomed = [e for e in location.iterdir() if matches_patterns(e.name, patterns)]
    if not doomed:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = location.with_name(f"{location.name}.before-import-{stamp}")
    n = 1
    while backup.exists():
        n += 1
        backup = location.with_name(f"{location.name}.before-import-{stamp}-{n}")
    if not patterns:
        os.replace(location, backup)
        location.mkdir(parents=True, exist_ok=True)
        return backup
    backup.mkdir(parents=True)
    for entry in doomed:
        os.replace(entry, backup / entry.name)
    return backup


def _restore_backup(location: Path, backup: Path, patterns, created) -> None:
    """Undo a failed import: bin what was extracted, put the originals back."""
    if not patterns:
        shutil.rmtree(location, ignore_errors=True)
        os.replace(backup, location)
        return
    for name in created:
        target = location / name
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        except OSError:
            pass
    for entry in list(backup.iterdir()):
        os.replace(entry, location / entry.name)
    try:
        backup.rmdir()
    except OSError:
        pass


def import_saves(src_zip: Path, location: Path, progress_fn=None,
                 backup: bool = True, patterns=()) -> tuple[int, int, "Path | None"]:
    """Extract *src_zip* into *location*, returning (files, bytes, backup).
    The old saves are moved aside (unless *backup* is False) and restored if
    extraction fails, so a bad archive can't cost the user saves."""
    src_zip = Path(src_zip)
    location = Path(location)
    if not zipfile.is_zipfile(src_zip):
        raise SaveTransferError(f"Not a zip archive: {src_zip.name}")

    with zipfile.ZipFile(src_zip, "r") as zf:
        members = _safe_members(zf, location)
        total = sum(m.file_size for m in members)
        moved = backup_saves(location, patterns) if backup else None
        location.mkdir(parents=True, exist_ok=True)
        root = location.resolve()
        done = 0
        created: list[str] = []
        try:
            for info in members:
                if progress_fn is not None:
                    progress_fn(done, total, info.filename)
                parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
                dest = location.joinpath(*parts)
                # Re-check after joining: a symlink among the parents can still
                # land outside. is_relative_to, not a string prefix -that would
                # accept a sibling "Saves2" next to "Saves".
                if not dest.resolve().is_relative_to(root):
                    raise SaveTransferError(
                        f"Refusing archive member outside the save folder: {info.filename}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Noted BEFORE the write: a member that fails halfway still
                # left a partial file for the rollback to clear.
                if parts[0] not in created:
                    created.append(parts[0])
                with zf.open(info) as srcf, open(dest, "wb") as dstf:
                    shutil.copyfileobj(srcf, dstf)
                done += info.file_size
        except BaseException:
            # Put the originals back -the half-extracted folder is worthless.
            if moved is not None:
                _restore_backup(location, moved, patterns, created)
            raise
    if progress_fn is not None:
        progress_fn(total, total, "")
    return len(members), total, moved


# ---- single-save copy / move / delete ------------------------------------
# The Saves tab's right-click actions. Everything above moves a whole save
# folder through a zip; these act on one entry inside one.


def _prune_links(dirpath: str, dirnames: list) -> None:
    """Drop symlinked subfolders from a walk -a save dir can link out to /."""
    dirnames[:] = [d for d in dirnames
                   if not os.path.islink(os.path.join(dirpath, d))]


def _entry_files(entry: Path) -> "list[tuple[Path, str, int]]":
    """(absolute path, name relative to *entry*, size) for one file or folder.

    A symlink counts as itself, never as what it points at."""
    entry = Path(entry)
    if entry.is_symlink() or not entry.is_dir():
        try:
            size = entry.lstat().st_size
        except OSError:
            size = 0
        return [(entry, entry.name, size)]
    out: list[tuple[Path, str, int]] = []
    for dirpath, dirnames, filenames in os.walk(entry, followlinks=False):
        _prune_links(dirpath, dirnames)
        for name in filenames:
            full = Path(dirpath) / name
            try:
                size = full.lstat().st_size
            except OSError:
                continue
            out.append((full, str(full.relative_to(entry)), size))
    return out


def _remove_entry(path: Path) -> None:
    """Delete a file, symlink or folder; a missing path is not an error."""
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=False)
    else:
        path.unlink(missing_ok=True)


def _copy_one(src: Path, dest: Path) -> None:
    """Copy a file, or recreate a symlink as a symlink rather than its target."""
    try:
        if src.is_symlink():
            dest.unlink(missing_ok=True)
            os.symlink(os.readlink(src), dest)
        else:
            shutil.copy2(src, dest)
    except OSError as exc:
        raise SaveTransferError(f"Could not copy {src.name}: {exc}") from exc


def _copy_entry(src: Path, dest: Path, total: int, progress_fn=None) -> None:
    """Copy one file/folder to *dest*, counting bytes for *progress_fn*."""
    if src.is_symlink() or not src.is_dir():
        if progress_fn is not None:
            progress_fn(0, total, src.name)
        _copy_one(src, dest)
        return
    dest.mkdir(parents=True, exist_ok=True)
    done = 0
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        _prune_links(dirpath, dirnames)
        rel = os.path.relpath(dirpath, src)
        # Empty subfolders are part of the save's shape -make them too.
        here = dest if rel == "." else dest / rel
        here.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            full = Path(dirpath) / name
            if progress_fn is not None:
                progress_fn(done, total, name)
            _copy_one(full, here / name)
            try:
                done += full.lstat().st_size
            except OSError:
                pass


def _same_filesystem(src: Path, dest_dir: Path) -> bool:
    """Whether *src* can be renamed into *dest_dir* instead of copied."""
    try:
        return src.lstat().st_dev == dest_dir.stat().st_dev
    except OSError:
        return False


def _stash(dest: Path) -> Path:
    """Rename an existing destination aside, so a failed overwrite can undo."""
    stash = dest.with_name(f"{dest.name}.replaced{os.getpid()}")
    n = 1
    while stash.exists() or stash.is_symlink():
        n += 1
        stash = dest.with_name(f"{dest.name}.replaced{os.getpid()}-{n}")
    os.replace(dest, stash)
    return stash


def transfer_save_entry(src: Path, dest_dir: Path, move: bool = False,
                        overwrite: bool = False,
                        progress_fn=None) -> "tuple[int, int, Path]":
    """Copy -or *move* -one save file/folder into *dest_dir*.

    Returns (files, bytes, destination). An entry of the same name is only
    touched with *overwrite*, and even then it is renamed aside until the
    transfer has succeeded: a half-copied save must never be all that is left.
    """
    src = Path(src)
    dest_dir = Path(dest_dir)
    if not src.exists() and not src.is_symlink():
        raise SaveTransferError(f"Not found: {src.name}")
    if os.path.realpath(src.parent) == os.path.realpath(dest_dir):
        raise SaveTransferError(f"{src.name} is already in that folder.")
    dest = dest_dir / src.name
    if src.is_dir() and not src.is_symlink():
        # A folder cannot be copied into its own subtree -that recurses.
        root = Path(os.path.realpath(src))
        target = Path(os.path.realpath(dest_dir))
        if target == root or root in target.parents:
            raise SaveTransferError(f"Cannot put {src.name} inside itself.")
    replacing = dest.exists() or dest.is_symlink()
    if replacing and not overwrite:
        raise SaveTransferError(f"{dest.name} already exists in that folder.")

    files = _entry_files(src)
    total = sum(size for _f, _n, size in files)
    dest_dir.mkdir(parents=True, exist_ok=True)
    rename = move and _same_filesystem(src, dest_dir)
    if not rename:
        free = _free_space(dest_dir)
        if free is not None and total + _FREE_SPACE_MARGIN > free:
            raise SaveTransferError(
                f"Not enough free space: {src.name} needs "
                f"{total // 1024 ** 2} MB but only {free // 1024 ** 2} MB "
                f"is available.")

    stash = _stash(dest) if replacing else None
    try:
        if rename:
            os.replace(src, dest)
        else:
            _copy_entry(src, dest, total, progress_fn)
    except BaseException:
        # The destination is not complete yet: bin whatever landed, then put
        # the replaced entry back.
        try:
            _remove_entry(dest)
        except OSError:
            pass
        if stash is not None:
            os.replace(stash, dest)
        raise

    if move and not rename:
        try:
            _remove_entry(src)
        except BaseException as exc:
            # Copying has completed, so *dest* is now the only known-complete
            # copy.  Source cleanup can fail after rmtree has already removed
            # some files; rolling the destination back here would then lose
            # data from both sides.  Commit the destination and report only
            # that the source could not be fully removed.
            if stash is not None:
                try:
                    _remove_entry(stash)
                except OSError:
                    pass
            if isinstance(exc, OSError):
                raise SaveTransferError(
                    f"{src.name} was copied safely, but the original could "
                    f"not be completely removed: {exc}") from exc
            raise
    if stash is not None:
        _remove_entry(stash)
    if progress_fn is not None:
        progress_fn(total, total, "")
    return len(files), total, dest


def delete_save_entry(path: Path) -> "tuple[int, int]":
    """Delete one save file or folder, returning (files, bytes) removed."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        raise SaveTransferError(f"Not found: {path.name}")
    files = _entry_files(path)
    total = sum(size for _f, _n, size in files)
    try:
        _remove_entry(path)
    except OSError as exc:
        raise SaveTransferError(f"Could not delete {path.name}: {exc}") from exc
    return len(files), total
