from __future__ import annotations

import struct
import sys
from array import array
from pathlib import Path

from Utils.atomic_write import atomic_writer

_LARGE_ADDRESS_AWARE = 0x20
_BLOCK = 1024 * 1024


class PEFormatError(ValueError):
    pass


def _layout(path: Path) -> tuple[int, int, int, int]:
    size = path.stat().st_size
    if size < 64:
        raise PEFormatError("Executable is too small to contain a PE header")
    with path.open("rb") as stream:
        header = stream.read(64)
        if header[:2] != b"MZ":
            raise PEFormatError("Executable has no DOS header")
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        if pe_offset > size - 24:
            raise PEFormatError("Executable has an invalid PE header offset")
        stream.seek(pe_offset)
        coff = stream.read(24)
        if len(coff) != 24 or coff[:4] != b"PE\0\0":
            raise PEFormatError("Executable has no PE header")
        optional_size = struct.unpack_from("<H", coff, 20)[0]
        characteristics = struct.unpack_from("<H", coff, 22)[0]
        if optional_size < 68 or pe_offset + 24 + optional_size > size:
            raise PEFormatError("Executable has an invalid optional header")
        stream.seek(pe_offset + 24)
        optional = stream.read(68)
        if len(optional) != 68 or struct.unpack_from("<H", optional)[0] not in {0x10B, 0x20B}:
            raise PEFormatError("Executable uses an unsupported PE format")
    return size, pe_offset + 22, pe_offset + 24 + 64, characteristics


def _replace(block: bytes, offset: int, replacements) -> bytes:
    end = offset + len(block)
    changed = None
    for position, value in replacements:
        left = max(position, offset)
        right = min(position + len(value), end)
        if left >= right:
            continue
        if changed is None:
            changed = bytearray(block)
        changed[left - offset:right - offset] = value[left - position:right - position]
    return bytes(changed) if changed is not None else block


def _checksum(path: Path, size: int, replacements, stop=None) -> int:
    total = 0
    offset = 0
    with path.open("rb") as stream:
        while block := stream.read(_BLOCK):
            if stop is not None and stop.is_set():
                raise InterruptedError("Executable patching stopped")
            block = _replace(block, offset, replacements)
            odd = len(block) & 1
            words = array("H", block[:-1] if odd else block)
            if sys.byteorder != "little":
                words.byteswap()
            total += sum(words)
            if odd:
                total += block[-1]
            offset += len(block)
    total = (total & 0xFFFF) + (total >> 16)
    total = (total & 0xFFFF) + (total >> 16)
    return (total & 0xFFFF) + size


def is_large_address_aware(path: Path) -> bool:
    return bool(_layout(Path(path))[3] & _LARGE_ADDRESS_AWARE)


def write_large_address_aware(source: Path, target: Path, stop=None) -> Path:
    source, target = Path(source), Path(target)
    source_mode = source.stat().st_mode & 0o777
    size, characteristics_offset, checksum_offset, characteristics = _layout(source)
    header_changes = (
        (characteristics_offset, struct.pack("<H", characteristics | _LARGE_ADDRESS_AWARE)),
        (checksum_offset, b"\0\0\0\0"),
    )
    checksum = _checksum(source, size, header_changes, stop)
    replacements = (header_changes[0], (checksum_offset, struct.pack("<I", checksum)))
    with source.open("rb") as incoming, atomic_writer(target, "wb", encoding=None) as output:
        offset = 0
        while block := incoming.read(_BLOCK):
            if stop is not None and stop.is_set():
                raise InterruptedError("Executable patching stopped")
            output.write(_replace(block, offset, replacements))
            offset += len(block)
    target.chmod(source_mode)
    return target
