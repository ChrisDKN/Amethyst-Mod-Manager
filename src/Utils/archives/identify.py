from enum import Enum, auto
from pathlib import Path


class ArchiveType(Enum):
    ZIP = auto()
    RAR = auto()
    SEVEN_ZIP = auto()
    UNKNOWN = auto()


EXTENSION_MAP: dict[str, ArchiveType] = {
    ".zip": ArchiveType.ZIP,
    ".rar": ArchiveType.RAR,
    ".7z": ArchiveType.SEVEN_ZIP,
}


def identify_archive(file: Path) -> ArchiveType:
    """Identify archive type based on magic header."""
    # 8 bytes is enough to identify ZIP, 7z, and both RAR4/RAR5 formats.
    try:
        with file.open("rb") as f:
            header = f.read(8)
    except (OSError, PermissionError):
        return ArchiveType.UNKNOWN

    # 7-Zip: 37 7A BC AF 27 1C.
    if header.startswith(b"7z\xbc\xaf'\x1c"):
        return ArchiveType.SEVEN_ZIP

    # RAR:
    # - RAR 1.5 to 4.x: 52 61 72 21 1A 07 00.
    # - RAR 5.0+:       52 61 72 21 1A 07 01 00.
    if header.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return ArchiveType.RAR

    # ZIP:
    # - Standard header: PK\x03\x04.
    # - Empty archive: PK\x05\x06.
    # - Spanned: PK\x07\x08.
    if header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return ArchiveType.ZIP

    return ArchiveType.UNKNOWN


def identify_extractor(file: Path) -> ArchiveType:
    """Determine which extractor to use for the archive file."""
    magic_type = identify_archive(file)

    # Trust the extension if we couldn't identify the type.
    if magic_type is ArchiveType.UNKNOWN:
        ext = file.suffix.lower()
        magic_type = EXTENSION_MAP.get(ext, ArchiveType.UNKNOWN)

    return magic_type
