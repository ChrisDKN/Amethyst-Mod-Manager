from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import stat
import tarfile
import zipfile

from Utils.archives.identify import TAR_TYPES, identify_archive, may_be_zip
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
        raise RuntimeError("7-Zip is required to inspect this archive")
    result = run_listing(
        [tool, "l", "-slt", "-ba", "-sccUTF-8", "--", str(archive)], cancel)
    if result.returncode:
        output = result.stdout[-2000:] + result.stderr[-2000:]
        if failure_kind(output, result.returncode, tool) == "password":
            raise RuntimeError("A password is required to list this archive's contents")
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
