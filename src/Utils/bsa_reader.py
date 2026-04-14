"""
bsa_reader.py
Read file lists from Bethesda BSA archives (table-of-contents only).

Extracts the complete list of file paths stored inside a .bsa archive
without decompressing any file data — only the TOC headers are read.

BSA v104 (Oblivion / Skyrim LE) header layout (36 bytes):
     4B  magic           "BSA\x00" (0x00415342 LE)
     4B  version         104
     4B  folder_offset   offset to folder records (always 36)
     4B  archive_flags   bit 0: has dir names, bit 1: has file names,
                         bit 2: compressed by default, etc.
     4B  folder_count
     4B  file_count
     4B  total_folder_name_length
     4B  total_file_name_length
     4B  file_flags

Folder record (v104, 16 bytes each):
     8B  name_hash       (uint64)
     4B  count           number of files in this folder
     4B  offset          byte offset (from start of file) to this folder's
                         name + file records block

Folder record (v105, 24 bytes each):
     8B  name_hash       (uint64)
     4B  count           number of files in this folder
     4B  padding         (unused)
     8B  offset          byte offset (uint64)

After all folder records, for each folder in order:
     1B  name_length     (including the trailing null)
    NB   folder_name     null-terminated string (backslash-separated)

Then immediately, *count* file records (16 bytes each):
     8B  name_hash       (uint64)
     4B  size            (bit 30 may toggle compression for this file)
     4B  offset          byte offset to file data

After all folder+file-record blocks, the file name block:
     concatenated null-terminated file names, one per file record,
     in the same order as the file records were encountered.

BSA v103 (Morrowind) is a completely different flat format and is
not yet implemented (returns an empty list).

BA2 (Fallout 4 / Starfield, magic "BTDX") is not yet implemented.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

_BSA_MAGIC = b"BSA\x00"
_BTDX_MAGIC = b"BTDX"
_HEADER_SIZE = 36


def read_bsa_file_list(bsa_path: Path | str) -> list[str]:
    """Return all file paths inside a BSA as lowercase forward-slash strings.

    Dispatches to the correct parser based on the header version field.
    Returns an empty list on unrecognised formats or I/O errors.
    Never decompresses file data — only reads the TOC.
    """
    try:
        bsa_path = Path(bsa_path)
        with bsa_path.open("rb") as f:
            magic = f.read(4)
            if magic == _BSA_MAGIC:
                return _read_bsa_v104_v105(f)
            if magic == _BTDX_MAGIC:
                return []  # BA2 not yet supported
            return []  # unrecognised format
    except (OSError, struct.error, ValueError, OverflowError):
        return []


def _read_bsa_v104_v105(f) -> list[str]:
    """Parse BSA v104 or v105 TOC and return file path list."""
    header = f.read(_HEADER_SIZE - 4)  # magic already consumed
    if len(header) < _HEADER_SIZE - 4:
        return []

    (
        version,
        folder_offset,
        archive_flags,
        folder_count,
        file_count,
        total_folder_name_length,
        total_file_name_length,
        file_flags,
    ) = struct.unpack_from("<IIIIIIII", header, 0)

    if version == 103:
        return []  # Morrowind — not yet implemented

    if version not in (104, 105):
        return []

    has_dir_names = bool(archive_flags & 0x1)
    has_file_names = bool(archive_flags & 0x2)

    if not has_dir_names or not has_file_names:
        # Without names we cannot reconstruct paths.
        return []

    # Read folder records
    f.seek(folder_offset)
    if version == 105:
        # 24 bytes: hash(8) + count(4) + padding(4) + offset(8)
        folder_rec_size = 24
        folder_rec_fmt = "<QII"  # hash, count, padding — then read offset separately
    else:
        # v104: 16 bytes: hash(8) + count(4) + offset(4)
        folder_rec_size = 16

    folder_records: list[tuple[int, int]] = []  # (count, offset)
    raw = f.read(folder_rec_size * folder_count)
    if len(raw) < folder_rec_size * folder_count:
        return []

    for i in range(folder_count):
        base = i * folder_rec_size
        if version == 105:
            _hash, count, _pad = struct.unpack_from("<QII", raw, base)
            offset = struct.unpack_from("<Q", raw, base + 16)[0]
        else:
            _hash, count, offset = struct.unpack_from("<QII", raw, base)
        folder_records.append((count, offset))

    # Read folder names + file records for each folder.
    # They appear sequentially after the folder record table.
    folder_names: list[str] = []
    file_records_per_folder: list[int] = []  # just the count per folder

    for count, _offset in folder_records:
        # Folder name: 1-byte length (including null), then the string + null
        name_len_raw = f.read(1)
        if len(name_len_raw) < 1:
            return []
        name_len = name_len_raw[0]
        name_bytes = f.read(name_len)
        if len(name_bytes) < name_len:
            return []
        folder_name = name_bytes.rstrip(b"\x00").decode("utf-8", errors="replace")
        # Normalise: lowercase, backslash → forward slash
        folder_name = folder_name.replace("\\", "/").lower()
        folder_names.append(folder_name)
        file_records_per_folder.append(count)

        # Skip past file records (16 bytes each: hash + size + offset)
        f.seek(16 * count, os.SEEK_CUR)

    # Read file name block
    file_name_data = f.read(total_file_name_length)
    if not file_name_data:
        return []

    # Split on null bytes — last entry may have trailing null
    file_names = file_name_data.split(b"\x00")
    # Remove trailing empty string from final null
    if file_names and file_names[-1] == b"":
        file_names.pop()

    # Build full paths: pair file names with folder names
    result: list[str] = []
    name_idx = 0
    for folder_idx, count in enumerate(file_records_per_folder):
        folder = folder_names[folder_idx]
        for _ in range(count):
            if name_idx >= len(file_names):
                break
            fname = file_names[name_idx].decode("utf-8", errors="replace").lower()
            name_idx += 1
            if folder:
                result.append(folder + "/" + fname)
            else:
                result.append(fname)

    return result
