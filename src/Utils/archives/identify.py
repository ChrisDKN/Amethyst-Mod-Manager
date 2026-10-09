from __future__ import annotations

import tarfile
import zipfile
from enum import StrEnum


class ArchiveType(StrEnum):
    ZIP = "zip"
    RAR = "rar"
    SEVEN_ZIP = "7z"
    TAR = "tar"
    GZIP = "gzip"
    BZIP2 = "bzip2"
    XZ = "xz"
    ZSTD = "zstd"
    UNKNOWN = "unknown"


TAR_TYPES = {ArchiveType.TAR, ArchiveType.GZIP, ArchiveType.BZIP2, ArchiveType.XZ}


def identify_archive(path) -> ArchiveType:
    """Identify the outer header; unknown formats remain eligible for native extraction."""
    with open(path, "rb") as source:
        header = source.read(512)
    for signatures, kind in (
        ((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"PK\x06\x06"), ArchiveType.ZIP),
        ((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00"), ArchiveType.RAR),
        ((b"7z\xbc\xaf\x27\x1c",), ArchiveType.SEVEN_ZIP),
        ((b"\x1f\x8b\x08",), ArchiveType.GZIP),
        ((b"BZh1", b"BZh2", b"BZh3", b"BZh4", b"BZh5", b"BZh6", b"BZh7", b"BZh8", b"BZh9"), ArchiveType.BZIP2),
        ((b"\xfd7zXZ\x00",), ArchiveType.XZ),
        ((b"\x28\xb5\x2f\xfd",), ArchiveType.ZSTD),
    ):
        if header.startswith(signatures):
            return kind
    try:
        tarfile.TarInfo.frombuf(header, "utf-8", "surrogateescape")
    except tarfile.HeaderError:
        return ArchiveType.UNKNOWN
    return ArchiveType.TAR


def may_be_zip(path, archive_type: ArchiveType) -> bool:
    return (archive_type == ArchiveType.ZIP
            or archive_type == ArchiveType.UNKNOWN and zipfile.is_zipfile(path))
