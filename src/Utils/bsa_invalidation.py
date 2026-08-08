"""
Archive-invalidation helpers for Gamebryo-era Bethesda games.

Ports MO2's GamebryoBSAInvalidation/DummyBSA approach: write a tiny empty BSA,
register it at position 0 of SArchiveList, and let the engine's normal archive
priority take care of overriding vanilla BSA assets with loose files.

Exposed:
  write_dummy_bsa(path, version)            - write the empty archive bytes
  ensure_in_archive_list(list_str, name)    - return list with `name` first
  remove_from_archive_list(list_str, name)  - return list without `name`
  BSA_VERSION_OBLIVION / _FO3_FNV_SKYRIM / _SSE  - per-game version bytes
"""

from __future__ import annotations

import struct
from pathlib import Path

BSA_VERSION_OBLIVION = 0x67       # TES4: Oblivion
BSA_VERSION_FO3_FNV_SKYRIM = 0x68 # FO3, FNV, Skyrim LE
BSA_VERSION_SSE = 0x69            # Skyrim Special Edition

_HEADER_SIZE = 36
_FOLDER_RECORD_SIZE = 16
_FILE_RECORD_SIZE = 16
_DUMMY_FILE_NAME = "dummy.dds"
_DUMMY_FOLDER_NAME = ""

# Archive flags: bit0 = has folder names, bit1 = has file names. MO2 sets both.
_ARCHIVE_FLAGS = 0x01 | 0x02
# File flags: bit1 = DDS. The dummy's only asset is a .dds, so this is what the
# engine sees when it inspects the archive.
_FILE_FLAGS = 0x02


def _gen_hash_int(data: bytes) -> int:
    """The polynomial hash used for the middle-of-name and extension portions."""
    h = 0
    for b in data:
        h = (h * 0x1003F + b) & 0xFFFFFFFFFFFFFFFF
    return h


def _gen_hash(file_name: str) -> int:
    """Bethesda BSA name hash (64-bit). Mirrors DummyBSA::genHash exactly.

    The C++ source lowercases, swaps backslashes to forward slashes, then builds
    a 64-bit value: low 32 bits encode last-char + length + first-char + (a flag
    bit picked from the file extension); high 32 bits are a polynomial hash of
    the middle of the name plus the extension.
    """
    lowered = file_name.lower().replace("\\", "/").encode("ascii", "replace")

    dot = lowered.rfind(b".")
    if dot < 0:
        ext = b""
        stem = lowered
    else:
        ext = lowered[dot:]
        stem = lowered[:dot]

    length = len(stem)
    hash_val = 0
    if length > 0:
        last = stem[-1]
        second_last = stem[-2] if length > 2 else 0
        first = lowered[0]
        hash_val = last | (second_last << 8) | (length << 16) | (first << 24)

    if len(ext) > 0:
        ext_str = ext[1:]  # without the leading dot
        if ext_str == b"kf":
            hash_val |= 0x80
        elif ext_str == b"nif":
            hash_val |= 0x8000
        elif ext_str == b"dds":
            hash_val |= 0x8080
        elif ext_str == b"wav":
            hash_val |= 0x80000000

        # Middle portion in the C source: [fileNameLowerU+1, extU-2) - i.e.
        # lowered[1 : dot-2], NOT stem[1:-2]. (The end pointer is two before
        # the dot, but the start pointer is one *past* the very first byte.)
        middle_end = dot - 2 if dot >= 2 else 1
        middle = lowered[1:max(1, middle_end)]
        temp = _gen_hash_int(middle)
        temp = (temp + _gen_hash_int(ext)) & 0xFFFFFFFFFFFFFFFF
        hash_val |= (temp & 0xFFFFFFFF) << 32

    return hash_val & 0xFFFFFFFFFFFFFFFF


def write_dummy_bsa(path: Path, version: int) -> None:
    """Write an empty, structurally-valid dummy BSA to `path`.

    Contains a single folder (empty name) holding a single zero-byte file
    `dummy.dds`. The archive's mtime ends up at "now" (file write time), which
    is the whole point: when registered at SArchiveList[0], it makes every real
    BSA "older" under bInvalidateOlderFiles=1.
    """
    folder_name = _DUMMY_FOLDER_NAME
    file_name = _DUMMY_FILE_NAME
    total_file_name_length = len(file_name) + 1  # the trailing NUL

    buf = bytearray()

    # ---- header (36 bytes) ----
    buf += b"BSA\x00"
    buf += struct.pack("<I", version)
    buf += struct.pack("<I", _HEADER_SIZE)            # offset to folder records
    buf += struct.pack("<I", _ARCHIVE_FLAGS)
    buf += struct.pack("<I", 1)                       # folder count
    buf += struct.pack("<I", 1)                       # file count
    buf += struct.pack("<I", len(folder_name) + 1)    # total folder name length
    buf += struct.pack("<I", total_file_name_length)  # total file name length
    buf += struct.pack("<I", _FILE_FLAGS)

    # ---- folder record (16 bytes) ----
    # offset to folder block = total file name length + 0x34 (matches MO2's literal)
    buf += struct.pack("<Q", _gen_hash(folder_name))
    buf += struct.pack("<I", 1)                       # file count in folder
    buf += struct.pack("<I", 0x34 + total_file_name_length)

    # ---- folder block: prefixed length byte + folder name + NUL ----
    folder_bytes = folder_name.encode("ascii", "replace") + b"\x00"
    buf += folder_bytes

    # ---- file record (16 bytes) ----
    # offset to file data = file_name_length + 1 + 4 (size field) + 0x44
    buf += struct.pack("<Q", _gen_hash(file_name))
    buf += struct.pack("<I", 0)                       # file size
    buf += struct.pack("<I", 0x44 + total_file_name_length + 4)

    # ---- file-name table + zero-byte file data ----
    buf += file_name.encode("ascii", "replace") + b"\x00"
    buf += struct.pack("<I", 0)                       # 4-byte file size sentinel

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(buf))


# ---------------------------------------------------------------------------
# SArchiveList helpers
# ---------------------------------------------------------------------------

def _split_archive_list(list_str: str | None) -> list[str]:
    if not list_str:
        return []
    return [s.strip() for s in list_str.split(",") if s.strip()]


def _join_archive_list(items: list[str]) -> str:
    return ", ".join(items)


def ensure_in_archive_list(list_str: str | None, bsa_name: str) -> str:
    """Return SArchiveList with `bsa_name` first, deduped (case-insensitive).

    Idempotent: if `bsa_name` is already first, the result is byte-identical.
    """
    items = _split_archive_list(list_str)
    lname = bsa_name.lower()
    items = [s for s in items if s.lower() != lname]
    items.insert(0, bsa_name)
    return _join_archive_list(items)


def remove_from_archive_list(list_str: str | None, bsa_name: str) -> str:
    """Return SArchiveList with all case-insensitive copies of `bsa_name` removed."""
    items = _split_archive_list(list_str)
    lname = bsa_name.lower()
    return _join_archive_list([s for s in items if s.lower() != lname])


def append_to_archive_list(list_str: str | None, bsa_names: list[str]) -> str:
    """Return SArchiveList with each of `bsa_names` appended at the end, deduped.

    Used for FO3/FNV mod-provided BSAs: those engines only read files from BSAs
    that appear in SArchiveList (plugin-name matching is unreliable), so every
    deployed mod BSA must be listed. Appended (not prepended) so the dummy
    invalidation BSA stays first. Idempotent and order-stable for names already
    present.
    """
    items = _split_archive_list(list_str)
    have = {s.lower() for s in items}
    for name in bsa_names:
        if name.lower() not in have:
            items.append(name)
            have.add(name.lower())
    return _join_archive_list(items)


def remove_many_from_archive_list(list_str: str | None, bsa_names: list[str]) -> str:
    """Return SArchiveList with all case-insensitive copies of each name removed."""
    items = _split_archive_list(list_str)
    drop = {n.lower() for n in bsa_names}
    return _join_archive_list([s for s in items if s.lower() not in drop])
