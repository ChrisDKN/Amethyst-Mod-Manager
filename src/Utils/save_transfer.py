"""Zip export / import of a game's save folder. Toolkit-neutral.

Archives are flat-rooted (names relative to the save folder), so one is
portable between machines whose prefixes live in different places. Import
refuses any member that would escape the destination. ``progress_fn(done,
total, phase)`` counts bytes, not files — one 30 MB save dwarfs a hundred
small ones.
"""

from __future__ import annotations

import os
import shutil
import time
import zipfile
from pathlib import Path

#: Files never worth carrying between machines.
_SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}

#: Ceiling on what an import will write. A compression-ratio bomb guard would
#: reject our OWN exports — save data beats 200:1 — so free space plus this cap
#: covers the real harm (filling the disk) instead.
_MAX_UNCOMPRESSED = 64 * 1024 ** 3
#: Leave this much headroom rather than filling the volume exactly.
_FREE_SPACE_MARGIN = 128 * 1024 ** 2


class SaveTransferError(Exception):
    """Export/import failed for a reason worth showing the user verbatim."""


def _walk_files(root: Path) -> "list[tuple[Path, str, int]]":
    """(absolute path, archive name, size) under *root*. Symlinks are skipped
    — a prefix save dir can hold dosdevices links to the whole filesystem."""
    out: list[tuple[Path, str, int]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            if name in _SKIP_NAMES:
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


def export_saves(source: Path, dest_zip: Path, progress_fn=None) -> tuple[int, int]:
    """Zip *source* into *dest_zip*, returning (files, bytes). Writes a temp
    file and renames, so a failed run leaves no half-written archive."""
    source = Path(source)
    dest_zip = Path(dest_zip)
    if not source.is_dir():
        raise SaveTransferError(f"Save folder not found: {source}")

    files = _walk_files(source)
    if not files:
        raise SaveTransferError(f"Nothing to export — {source} is empty.")
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


def backup_saves(location: Path) -> "Path | None":
    """Move the save folder aside before an import, returning its new path.
    A rename is atomic and free even on a multi-GB folder."""
    location = Path(location)
    if not location.is_dir() or not any(location.iterdir()):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = location.with_name(f"{location.name}.before-import-{stamp}")
    n = 1
    while backup.exists():
        n += 1
        backup = location.with_name(f"{location.name}.before-import-{stamp}-{n}")
    os.replace(location, backup)
    location.mkdir(parents=True, exist_ok=True)
    return backup


def import_saves(src_zip: Path, location: Path, progress_fn=None,
                 backup: bool = True) -> tuple[int, int, "Path | None"]:
    """Extract *src_zip* into *location*, returning (files, bytes, backup).
    The old folder is moved aside (unless *backup* is False) and restored if
    extraction fails, so a bad archive can't cost the user saves."""
    src_zip = Path(src_zip)
    location = Path(location)
    if not zipfile.is_zipfile(src_zip):
        raise SaveTransferError(f"Not a zip archive: {src_zip.name}")

    with zipfile.ZipFile(src_zip, "r") as zf:
        members = _safe_members(zf, location)
        total = sum(m.file_size for m in members)
        moved = backup_saves(location) if backup else None
        location.mkdir(parents=True, exist_ok=True)
        root = location.resolve()
        done = 0
        try:
            for info in members:
                if progress_fn is not None:
                    progress_fn(done, total, info.filename)
                parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
                dest = location.joinpath(*parts)
                # Re-check after joining: a symlink among the parents can still
                # land outside. is_relative_to, not a string prefix — that would
                # accept a sibling "Saves2" next to "Saves".
                if not dest.resolve().is_relative_to(root):
                    raise SaveTransferError(
                        f"Refusing archive member outside the save folder: {info.filename}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as srcf, open(dest, "wb") as dstf:
                    shutil.copyfileobj(srcf, dstf)
                done += info.file_size
        except BaseException:
            # Put the originals back — the half-extracted folder is worthless.
            if moved is not None:
                shutil.rmtree(location, ignore_errors=True)
                os.replace(moved, location)
            raise
    if progress_fn is not None:
        progress_fn(total, total, "")
    return len(members), total, moved
