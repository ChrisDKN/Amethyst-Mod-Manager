"""Random-access index for Morrowind's uncompressed BSA format."""

from __future__ import annotations

import struct
from pathlib import Path


def index_tes3_bsa(path: Path | str) -> dict[str, tuple[int, int]]:
    path = Path(path)
    with path.open("rb") as stream:
        stream.seek(0, 2)
        total = stream.tell()
        stream.seek(0)
        head = stream.read(12)
        if len(head) != 12:
            raise ValueError("truncated Morrowind BSA header")
        magic, directory_size, count = struct.unpack("<III", head)
        if magic != 0x100 or directory_size < count * 12:
            raise ValueError("invalid Morrowind BSA directory")
        data_start = 12 + directory_size + count * 8
        if data_start > total:
            raise ValueError("Morrowind BSA directory exceeds file size")
        directory = stream.read(directory_size)
        if len(directory) != directory_size:
            raise ValueError("truncated Morrowind BSA directory")

    names = directory[count * 12:]
    out: dict[str, tuple[int, int]] = {}
    for index in range(count):
        size, relative_offset = struct.unpack_from("<II", directory, index * 8)
        name_offset = struct.unpack_from("<I", directory, count * 8 + index * 4)[0]
        if name_offset >= len(names):
            raise ValueError("Morrowind BSA name offset exceeds directory")
        end = names.find(b"\0", name_offset)
        if end < 0:
            raise ValueError("unterminated Morrowind BSA member name")
        offset = data_start + relative_offset
        if offset + size > total:
            raise ValueError("Morrowind BSA member exceeds file size")
        name = names[name_offset:end].decode("latin-1").replace("\\", "/").lower()
        out[name] = size, offset
    return out


def read_tes3_bsa_entry(path: Path | str, record: tuple[int, int]) -> bytes:
    size, offset = record
    with Path(path).open("rb") as stream:
        stream.seek(offset)
        data = stream.read(size)
    if len(data) != size:
        raise ValueError("truncated Morrowind BSA member")
    return data
