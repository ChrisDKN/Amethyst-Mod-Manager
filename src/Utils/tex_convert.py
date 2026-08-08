"""
tex_convert.py
Convert RE Engine TEX files between pre-RTX (v10) and post-RTX (v34) header formats.

Pre-RTX games (RE7, RE2, RE3 before RTX update) use `.tex.10` with a 28-byte header.
Post-RTX uses `.tex.34` with a 36-byte header (8 extra bytes, swapped mipCount/imgCount
fields, mipCount stored as mipCount*16).

The pixel data is identical - only the header and mip offset table change.
"""

from __future__ import annotations

import struct
from pathlib import Path

# TEX\0 magic
TEX_MAGIC = 0x00584554  # 5784916

# Pre-RTX header: 32 bytes (pack=1)
#   magic(4) extension(4) width(2) height(2) unk1(1) unk2(1) mipCount(1) imgCount(1)
#   type(4) unk6(4) unk7(4) flags(4)
_V10_HEADER = struct.Struct("<II HH BBBB I i I I")  # 32 bytes

# Post-RTX header: 40 bytes (pack=1)
#   magic(4) extension(4) width(2) height(2) unk1(1) unk2(1) imgCount(1) mipCount(1)
#   type(4) unk6(4) unk7(4) flags(4) unk8(8)
_V34_HEADER = struct.Struct("<II HH BBBB I i I I Q")  # 40 bytes

# Mip header: 16 bytes each
#   offsetForImageData(8) pitch(4) imageDataSize(4)
_MIP_HEADER = struct.Struct("<Q I I")  # 16 bytes

# Extensions that use the old (v10-style) header format
_OLD_FORMAT_EXTENSIONS = {8, 10, 11, 190820018}


def tex_needs_conversion(src: Path | str, target_extension: int = 34) -> bool:
    """Check if a TEX file uses the old format and needs conversion to target."""
    src = Path(src)
    if src.stat().st_size < _V10_HEADER.size:
        return False
    with open(src, "rb") as f:
        header_bytes = f.read(_V10_HEADER.size)
    if len(header_bytes) < _V10_HEADER.size:
        return False
    magic, extension = struct.unpack_from("<II", header_bytes)
    if magic != TEX_MAGIC:
        return False
    return extension in _OLD_FORMAT_EXTENSIONS


def convert_tex_v10_to_v34(
    src: Path | str,
    dst: Path | str,
    target_extension: int = 34,
) -> bool:
    """Convert a TEX file from v10 (pre-RTX) to v34 (post-RTX) format.

    Reads *src*, rewrites header to v30 format, adjusts mip offsets by +8,
    and writes the result to *dst*.  Returns True on success.
    """
    src, dst = Path(src), Path(dst)
    data = src.read_bytes()

    if len(data) < _V10_HEADER.size:
        return False

    # Parse old header
    (
        magic, extension,
        width, height,
        unk1, unk2,
        mip_count, img_count,
        tex_type, unk6, unk7, flags,
    ) = _V10_HEADER.unpack_from(data)

    if magic != TEX_MAGIC:
        return False
    if extension not in _OLD_FORMAT_EXTENSIONS:
        return False

    # Parse mip headers (immediately after old header)
    old_header_size = _V10_HEADER.size  # 28
    new_header_size = _V34_HEADER.size  # 36
    offset_delta = new_header_size - old_header_size  # +8

    mip_data_start = old_header_size
    mip_headers: list[tuple[int, int, int]] = []
    for i in range(mip_count):
        off = mip_data_start + i * _MIP_HEADER.size
        if off + _MIP_HEADER.size > len(data):
            return False
        offset_img, pitch, img_size = _MIP_HEADER.unpack_from(data, off)
        # Adjust offset: header grew by 8 bytes, so all offsets shift +8
        mip_headers.append((offset_img + offset_delta, pitch, img_size))

    # Everything after old header + mip headers is image data
    payload_start = old_header_size + mip_count * _MIP_HEADER.size
    payload = data[payload_start:]

    # Build new header (v30 format)
    # Note: mipCount and imgCount are swapped in v30, and mipCount is stored * 16
    new_header = _V34_HEADER.pack(
        magic,
        target_extension,
        width, height,
        unk1, unk2,
        img_count,           # imgCount (was after mipCount in v10, now before)
        mip_count * 16,      # mipCount * 16
        tex_type,
        unk6, unk7, flags,
        0,                   # unknown8 - zero
    )

    # Build new mip headers
    new_mips = b"".join(
        _MIP_HEADER.pack(off, pitch, size)
        for off, pitch, size in mip_headers
    )

    # Write output
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(new_header + new_mips + payload)
    return True
