"""
dds_compat.py
Make DDS blobs Pillow can't decode decodable, for preview purposes.

PBR texture packs (Community Shaders / PGPatcher output) encode as
BC1/BC2/BC3_UNORM_SRGB in a DX10 header; Pillow implements only the UNORM
twins. The pixel data is identical — sRGB vs UNORM is a sampling gamma hint —
so rewriting the DXGI format dword is lossless for display.
"""

from __future__ import annotations

import struct

__all__ = ["is_srgb_dds", "sanitise_dds", "skip_dds_mips"]

# DXGI *_UNORM_SRGB -> *_UNORM for the BC formats Pillow lacks the sRGB
# variant of (BC7_UNORM_SRGB it handles natively).
_SRGB_TO_UNORM = {72: 71, 75: 74, 78: 77}

# Every DXGI format whose values are sRGB-encoded rather than linear.
_SRGB_FORMATS = frozenset({29, 30, 72, 75, 78, 91, 93, 99})

_MAGIC = b"DDS "
_DX10 = b"DX10"
_FOURCC_OFF = 84          # fourcc within the legacy header
_DXGI_OFF = 128           # dxgiFormat within the DX10 extension header


def is_srgb_dds(data: bytes) -> bool:
    """True if the DDS DECLARES an sRGB-encoded format.

    Only DX10-header files say so. Legacy (DXT1/DXT5 fourcc) files carry no
    such flag — Skyrim's own textures are sRGB content but never declare it,
    so a caller must not infer 'linear' from a False here.
    """
    if (len(data) < _DXGI_OFF + 4 or not data.startswith(_MAGIC)
            or data[_FOURCC_OFF:_FOURCC_OFF + 4] != _DX10):
        return False
    return int.from_bytes(data[_DXGI_OFF:_DXGI_OFF + 4], "little") in _SRGB_FORMATS


def sanitise_dds(data: bytes) -> bytes:
    """Return *data* with an unsupported sRGB DXGI format swapped to its
    UNORM twin; anything else passes through untouched."""
    if (len(data) < _DXGI_OFF + 4 or not data.startswith(_MAGIC)
            or data[_FOURCC_OFF:_FOURCC_OFF + 4] != _DX10):
        return data
    fmt = int.from_bytes(data[_DXGI_OFF:_DXGI_OFF + 4], "little")
    swap = _SRGB_TO_UNORM.get(fmt)
    if swap is None:
        return data
    out = bytearray(data)
    out[_DXGI_OFF:_DXGI_OFF + 4] = swap.to_bytes(4, "little")
    return bytes(out)


# Bytes per 4x4 block for block-compressed formats, by legacy fourcc...
_FOURCC_BLOCK = {b"DXT1": 8, b"DXT2": 16, b"DXT3": 16, b"DXT4": 16,
                 b"DXT5": 16, b"ATI1": 8, b"BC4U": 8, b"BC4S": 8,
                 b"ATI2": 16, b"BC5U": 16, b"BC5S": 16}
# ...and by DXGI format (BC1/BC4 are 8, the rest 16).
_DXGI_BLOCK = {}
for _f in (*range(70, 73), *range(79, 82)):              # BC1, BC4
    _DXGI_BLOCK[_f] = 8
for _f in (*range(73, 79), *range(82, 85), *range(94, 100)):  # BC2/3/5/6/7
    _DXGI_BLOCK[_f] = 16
# Common 32-bit uncompressed DXGI formats (bytes per pixel).
_DXGI_BPP = {f: 4 for f in (*range(27, 33), *range(87, 94))}

_DDSD_MIPMAPCOUNT = 0x20000
_CUBEMAP_CAPS2 = 0xFE00                                  # any cubemap face bit
_DX10_MISC_CUBE = 0x4


def _mip_size(w: int, h: int, block: int | None, bpp: int) -> int:
    if block is not None:
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * block
    return w * h * bpp


def skip_dds_mips(data: bytes, max_dim: int) -> bytes:
    """Rewrite *data* so its top mip is the first one <= *max_dim* wide/tall.

    DDS files carry a full mip chain, and decoding a 4K BC7 level just to
    shrink it afterwards costs ~400ms; pointing the decoder at the 1K mip
    reads a sixteenth of the data. Files without usable mips (or cubemaps,
    arrays, volumes, exotic formats) pass through untouched.
    """
    if len(data) < 148 or not data.startswith(_MAGIC):
        return data
    (height, width, _pitch, depth, mipcount) = struct.unpack_from("<5I", data, 12)
    flags = int.from_bytes(data[8:12], "little")
    caps2 = int.from_bytes(data[112:116], "little")
    if (not (flags & _DDSD_MIPMAPCOUNT) or mipcount <= 1
            or (caps2 & _CUBEMAP_CAPS2) or depth > 1
            or max(width, height) <= max_dim):
        return data

    fourcc = data[_FOURCC_OFF:_FOURCC_OFF + 4]
    hdr_end = 128
    block = bpp = None
    if fourcc == _DX10:
        hdr_end = 148
        dxgi, _dim, misc, arr_size = struct.unpack_from("<4I", data, _DXGI_OFF)
        if (misc & _DX10_MISC_CUBE) or arr_size > 1:
            return data
        block = _DXGI_BLOCK.get(dxgi)
        bpp = _DXGI_BPP.get(dxgi)
    else:
        pf_flags = int.from_bytes(data[80:84], "little")
        if pf_flags & 0x4:                               # DDPF_FOURCC
            block = _FOURCC_BLOCK.get(fourcc)
        elif pf_flags & 0x40:                            # DDPF_RGB
            bits = int.from_bytes(data[88:92], "little")
            bpp = bits // 8 if bits in (8, 16, 24, 32) else None
    if block is None and bpp is None:
        return data

    # Walk down the chain to the first mip that fits.
    skip = offset = 0
    w, h = width, height
    while max(w, h) > max_dim and skip < mipcount - 1:
        offset += _mip_size(w, h, block, bpp or 0)
        w, h = max(1, w // 2), max(1, h // 2)
        skip += 1
    if not skip or len(data) < hdr_end + offset + _mip_size(w, h, block, bpp or 0):
        return data

    out = bytearray(data[:hdr_end])
    struct.pack_into("<5I", out, 12, h, w,
                     _mip_size(w, h, block, bpp or 0), depth, mipcount - skip)
    out += data[hdr_end + offset:]
    return bytes(out)
