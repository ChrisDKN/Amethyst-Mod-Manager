"""Runtime Fallout 4 face morphs for an already-baked FaceGeom head.

Fallout 4 NPC records can change appearance without shipping a replacement
FaceGeom NIF.  Their MSDK/MSDV values name morphs in the race's chargen TRI;
the game applies those records at runtime.  The viewer starts with the baked
head, so it applies only ``winning record - record baked into that head``.

Only the vertex-morph portion of FRTRI003 is needed here.  Animation morphs in
the ordinary (non-chargen) TRI and Face Morph bone transforms are separate.
"""
from __future__ import annotations

import struct

__all__ = ["apply_face_morphs", "tri_morph_names"]

_MAGIC = b"FRTRI003"
_HEADER_SIZE = 64
_MAX_VERTICES = 1_000_000
_MAX_MORPHS = 100_000


def _morph_offset(data: bytes):
    """Return ``(vertex count, morph count, byte offset)`` for FRTRI003."""
    if len(data) < _HEADER_SIZE or data[:8] != _MAGIC:
        raise ValueError("not a Fallout 4 FRTRI003 file")
    values = struct.unpack_from("<14I", data, 8)
    vertices, triangles, quads = values[:3]
    uv_vertices, morphs = values[5], values[7]
    if (vertices > _MAX_VERTICES or morphs > _MAX_MORPHS
            or triangles > _MAX_VERTICES * 4
            or quads > _MAX_VERTICES * 4
            or uv_vertices > _MAX_VERTICES):
        raise ValueError("implausible TRI counts")
    # Reference positions, topology, UVs, then UV topology.  FO4's face TRIs
    # normally have no quads, but their two index tables are accounted for.
    pos = (_HEADER_SIZE + vertices * 12 + triangles * 12 + quads * 16
           + uv_vertices * 8 + triangles * 12 + quads * 16)
    if pos > len(data):
        raise ValueError("truncated TRI geometry")
    return vertices, morphs, pos


def _iter_morphs(data: bytes):
    vertices, count, pos = _morph_offset(data)
    delta_bytes = vertices * 3 * 2
    for _index in range(count):
        if pos + 4 > len(data):
            raise ValueError("truncated TRI morph name")
        name_size = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if name_size > len(data) - pos or pos + name_size + 4 > len(data):
            raise ValueError("invalid TRI morph name size")
        name = data[pos:pos + name_size].split(b"\0", 1)[0].decode(
            "utf-8", "replace")
        pos += name_size
        multiplier = struct.unpack_from("<f", data, pos)[0]
        pos += 4
        if pos + delta_bytes > len(data):
            raise ValueError("truncated TRI morph deltas")
        yield name, multiplier, memoryview(data)[pos:pos + delta_bytes]
        pos += delta_bytes


def tri_morph_names(data: bytes) -> tuple[str, ...]:
    """Names exposed by a chargen TRI, primarily for diagnostics/tests."""
    return tuple(name for name, _multiplier, _deltas in _iter_morphs(data))


def apply_face_morphs(model, tri_data: bytes,
                      weights: dict[str, float]) -> tuple[int, int]:
    """Apply named TRI weights to the face shape in *model*.

    Returns ``(morphs applied, vertices changed)``.  Unknown names are ignored
    because races and mod-added chargen TRIs legitimately expose different
    sets.  The baked face and its chargen TRI must share vertex order.
    """
    wanted = {str(name): float(weight) for name, weight in weights.items()
              if abs(float(weight)) > 1e-7}
    if not wanted:
        return 0, 0

    from Utils.facegen_tint import head_shape

    shape = head_shape(model)
    if shape is None or not shape.vertices:
        return 0, 0
    vertex_count, _morph_count, _pos = _morph_offset(tri_data)
    if len(shape.vertices) != vertex_count:
        return 0, 0

    # Accumulate once, then rebuild the immutable vertex tuples once.  This is
    # much cheaper than copying 1,689 vertices after every individual slider.
    offsets = [[0.0, 0.0, 0.0] for _ in range(vertex_count)]
    applied = 0
    for name, multiplier, raw in _iter_morphs(tri_data):
        weight = wanted.get(name)
        if weight is None:
            continue
        scale = multiplier * weight
        deltas = struct.iter_unpack("<hhh", raw)
        for out, (dx, dy, dz) in zip(offsets, deltas):
            out[0] += dx * scale
            out[1] += dy * scale
            out[2] += dz * scale
        applied += 1

    if not applied:
        return 0, 0
    shape.vertices = [
        (vertex[0] + delta[0], vertex[1] + delta[1], vertex[2] + delta[2])
        for vertex, delta in zip(shape.vertices, offsets)
    ]
    return applied, vertex_count
