from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import stat
import tarfile
import zipfile

from Utils.archives.identify import ArchiveType, TAR_TYPES, identify_archive, may_be_zip
from Utils.archives.process import failure_kind, failure_message, run_listing


@dataclass(frozen=True)
class ArchiveMember:
    path: str
    size: int | None
    is_dir: bool = False
    is_link: bool = False
    encrypted: bool = False


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Archive inspection stopped")


def _rar_members(archive, cancel):
    try:
        import rarfile
    except ImportError as exc:
        raise RuntimeError("RAR inspection requires rarfile; update Amethyst's Python requirements") from exc
    rows = []
    try:
        def inspected(_):
            _check_cancel(cancel)
        with rarfile.RarFile(archive, errors="strict", info_callback=inspected) as source:
            if source.needs_password():
                raise RuntimeError("Password-protected RAR archives are unsupported")
            for item in source.infolist():
                _check_cancel(cancel)
                rows.append(ArchiveMember(
                    item.filename, item.file_size, item.is_dir(),
                    item.is_symlink() or bool(getattr(item, "file_redir", None))
                    or not (item.is_file() or item.is_dir())
                    or (item.host_os == rarfile.RAR_OS_UNIX and stat.S_IFMT(item.mode) != 0
                        and not (stat.S_ISREG(item.mode) or stat.S_ISDIR(item.mode))),
                    item.needs_password()))
    except rarfile.Error as exc:
        raise RuntimeError(f"Cannot inspect RAR archive {archive.name}: {exc}") from exc
    return rows


def list_archive(archive, cancel=None) -> list[ArchiveMember]:
    """Read member metadata without extracting or validating installation paths."""
    archive = Path(archive)
    _check_cancel(cancel)
    kind = identify_archive(archive)
    try:
        source = zipfile.ZipFile(archive) if may_be_zip(archive, kind) else None
    except zipfile.BadZipFile:
        source = None
    rows = []
    if source is not None:
        with source:
            for item in source.infolist():
                _check_cancel(cancel)
                mode = item.external_attr >> 16
                rows.append(ArchiveMember(
                    item.orig_filename, item.file_size,
                    item.is_dir() or item.filename.endswith("\\")
                    or stat.S_ISDIR(mode) or bool(item.external_attr & 0x10),
                    stat.S_ISLNK(mode), bool(item.flag_bits & 1)))
        return rows
    if kind in TAR_TYPES and tarfile.is_tarfile(archive):
        with tarfile.open(archive) as source:
            for item in source:
                _check_cancel(cancel)
                rows.append(ArchiveMember(
                    item.name, item.size, item.isdir(),
                    item.issym() or item.islnk()))
        return rows
    tool = next((path for name in ("7zzs", "7zz", "7z", "7za")
                 if (path := shutil.which(name))), None)
    if tool is None:
        if kind == ArchiveType.RAR:
            return _rar_members(archive, cancel)
        raise RuntimeError("7-Zip is required to inspect this archive")
    result = run_listing(
        [tool, "l", "-slt", "-ba", "-sccUTF-8", "--", str(archive)], cancel)
    if result.returncode:
        output = result.stdout[-2000:] + result.stderr[-2000:]
        if failure_kind(output, result.returncode, tool) == "password":
            raise RuntimeError("A password is required to list this archive's contents")
        if kind == ArchiveType.RAR and failure_kind(output, result.returncode, tool) == "archive":
            return _rar_members(archive, cancel)
        raise RuntimeError(failure_message(tool, result.returncode, output))
    entry = {}
    for line in [*result.stdout.splitlines(), ""]:
        _check_cancel(cancel)
        key, sep, value = line.partition(" = ")
        if sep:
            entry[key] = value
        elif not line.strip() and entry:
            if "Path" in entry:
                size = entry.get("Size")
                rows.append(ArchiveMember(
                    entry["Path"], int(size) if size else None,
                    entry.get("Folder") == "+"
                    or entry.get("Attributes", "").startswith("D"),
                    bool(entry.get("Symbolic Link") or entry.get("Hard Link")),
                    entry.get("Encrypted") == "+"))
            entry = {}
    return rows
