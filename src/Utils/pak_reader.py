"""
pak_reader.py
Read metadata from Baldur's Gate 3 .pak files (Larian LSPK v18 format).

Extracts the meta.lsx XML from inside a .pak archive without needing
lslib or any external tools — only the ``lz4`` Python package is required.

LSPK v18 header layout (40 bytes):
    4B  signature   ("LSPK" = 0x4B50534C)
    4B  version     (18 for current BG3)
    8B  file_list_offset
    4B  file_list_size
    1B  flags
    1B  priority
   16B  md5
    2B  num_parts

File entry layout (272 bytes each):
  256B  name (null-terminated UTF-8)
    4B  offset_low   (uint32)
    2B  offset_high  (uint16)  → full offset = offset_low | (offset_high << 32)
    1B  archive_part
    1B  flags        (lower nibble: 0=None, 1=Zlib, 2=LZ4)
    4B  size_on_disk
    4B  uncompressed_size
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

try:
    import lz4.block as _lz4
except ImportError:
    _lz4 = None  # type: ignore[assignment]

_LSPK_SIGNATURE = 0x4B50534C  # "LSPK" little-endian
_HEADER_SIZE = 40
_ENTRY_SIZE = 272


def _require_lz4() -> None:
    if _lz4 is None:
        raise ImportError(
            "The 'lz4' package is required to read BG3 .pak files.\n"
            "Install it with:  pip install lz4"
        )


def _decompress(data: bytes, flags: int, uncompressed_size: int) -> bytes:
    """Decompress a chunk according to LSPK compression flags."""
    method = flags & 0x0F
    if method == 0:
        return data
    if method == 1:
        return zlib.decompress(data)
    if method == 2:
        _require_lz4()
        return _lz4.decompress(data, uncompressed_size=uncompressed_size)
    raise ValueError(f"Unknown LSPK compression method: {method}")


def extract_meta_lsx(pak_path: Path | str) -> str | None:
    """Open a BG3 .pak and return the contents of meta.lsx as a string.

    Returns None if the archive does not contain a meta.lsx file.
    Raises on format errors or missing dependencies.
    """
    _require_lz4()
    pak_path = Path(pak_path)

    with pak_path.open("rb") as f:
        # -- Header ----------------------------------------------------------
        header = f.read(_HEADER_SIZE)
        if len(header) < _HEADER_SIZE:
            raise ValueError(f"File too small to be an LSPK archive: {pak_path}")

        sig, version, file_list_offset, file_list_size, flags, priority = (
            struct.unpack_from("<IIQIBB", header, 0)
        )
        if sig != _LSPK_SIGNATURE:
            raise ValueError(
                f"Not an LSPK file (bad signature 0x{sig:08X}): {pak_path}"
            )

        # -- File list --------------------------------------------------------
        f.seek(file_list_offset)
        num_files = struct.unpack("<I", f.read(4))[0]
        compressed_size = struct.unpack("<I", f.read(4))[0]
        compressed_data = f.read(compressed_size)

        uncompressed_size = num_files * _ENTRY_SIZE
        file_list = _lz4.decompress(
            compressed_data, uncompressed_size=uncompressed_size
        )

        # -- Scan entries for meta.lsx ----------------------------------------
        for i in range(num_files):
            base = i * _ENTRY_SIZE
            name_bytes = file_list[base : base + 256]
            nul = name_bytes.find(b"\x00")
            name = name_bytes[:nul].decode("utf-8") if nul >= 0 else name_bytes.decode("utf-8")

            if not name.endswith("meta.lsx"):
                continue

            offset_low = struct.unpack_from("<I", file_list, base + 256)[0]
            offset_high = struct.unpack_from("<H", file_list, base + 260)[0]
            file_offset = offset_low | (offset_high << 32)
            # archive_part = file_list[base + 262]
            entry_flags = file_list[base + 263]
            size_on_disk = struct.unpack_from("<I", file_list, base + 264)[0]
            unc_size = struct.unpack_from("<I", file_list, base + 268)[0]

            f.seek(file_offset)
            raw = f.read(size_on_disk)
            content = _decompress(raw, entry_flags, unc_size)
            return content.decode("utf-8")

    return None
