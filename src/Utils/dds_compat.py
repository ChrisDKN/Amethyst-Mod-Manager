"""
dds_compat.py
Make DDS blobs Pillow can't decode decodable, for preview purposes.

PBR texture packs (Community Shaders / PGPatcher output) encode as
BC1/BC2/BC3_UNORM_SRGB in a DX10 header; Pillow implements only the UNORM
twins. The pixel data is identical — sRGB vs UNORM is a sampling gamma hint —
so rewriting the DXGI format dword is lossless for display.
"""

from __future__ import annotations

__all__ = ["is_srgb_dds", "sanitise_dds"]

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
