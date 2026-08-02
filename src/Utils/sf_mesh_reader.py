"""
sf_mesh_reader.py
Read Starfield external geometry (.mesh) files, named by nif BSGeometry blocks.

No published spec (nif.xml stops at FO76); layout derived from real files:
     4B  version        1 so far
     4B  index_count    then index_count x uint16 indices
     4B  scale          float, multiplies positions
     4B  flags          stream mask (7 on everything seen)
     4B  vertex_count   then vertex_count x 3 x int16 positions
     4B  uv_count       then uv_count x 2 x half UVs; more arrays follow
Positions decode as int16/32767*scale (metres); half-float decodes to
infinities, which is how the encoding was pinned.
"""

from __future__ import annotations

import struct
from pathlib import Path

__all__ = ["SfMesh", "read_sf_mesh"]

_I16_SCALE = 1.0 / 32767.0


class SfMesh:
    """Geometry read out of a Starfield .mesh."""

    __slots__ = ("vertices", "uvs", "triangles", "version", "scale")

    def __init__(self, vertices, uvs, triangles, version=0, scale=1.0):
        self.vertices = vertices
        self.uvs = uvs
        self.triangles = triangles
        self.version = version
        self.scale = scale

    def __repr__(self) -> str:
        return (f"SfMesh(v{self.version}, {len(self.vertices)} verts, "
                f"{len(self.triangles)} tris)")


def read_sf_mesh(source: "str | Path | bytes") -> SfMesh | None:
    """Parse a Starfield .mesh from a path or raw bytes; None if unreadable."""
    try:
        if isinstance(source, (bytes, bytearray, memoryview)):
            data = bytes(source)
        else:
            data = Path(source).read_bytes()
    except OSError:
        return None
    if len(data) < 16:
        return None
    try:
        return _parse(data)
    except (struct.error, ValueError, IndexError):
        return None


def _parse(data: bytes) -> SfMesh | None:
    n = len(data)
    pos = 0

    version = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    index_count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    if index_count % 3 or pos + index_count * 2 > n:
        return None

    flat = struct.unpack_from(f"<{index_count}H", data, pos)
    pos += index_count * 2
    triangles = [(flat[i], flat[i + 1], flat[i + 2])
                 for i in range(0, index_count, 3)]

    if pos + 12 > n:
        return None
    scale = struct.unpack_from("<f", data, pos)[0]
    pos += 4
    pos += 4                                    # stream/attribute flags
    vertex_count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    if vertex_count == 0 or pos + vertex_count * 6 > n:
        return None

    raw = struct.unpack_from(f"<{vertex_count * 3}h", data, pos)
    pos += vertex_count * 6
    f = _I16_SCALE * (scale if scale else 1.0)
    vertices = [(raw[i] * f, raw[i + 1] * f, raw[i + 2] * f)
                for i in range(0, len(raw), 3)]

    uvs: list = []
    if pos + 4 <= n:
        uv_count = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if uv_count == vertex_count and pos + uv_count * 4 <= n:
            flat_uv = struct.unpack_from(f"<{uv_count * 2}e", data, pos)
            uvs = [(flat_uv[i], flat_uv[i + 1])
                   for i in range(0, len(flat_uv), 2)]

    # Drop triangles that index past the vertex array rather than handing a
    # renderer indices it cannot use.
    if triangles:
        limit = len(vertices)
        if max(max(t) for t in triangles) >= limit:
            triangles = [t for t in triangles if max(t) < limit]

    return SfMesh(vertices, uvs, triangles, version, scale)
