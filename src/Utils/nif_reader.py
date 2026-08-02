"""
nif_reader.py
Read geometry and texture references out of Gamebryo/NetImmerse .nif meshes.

Parses only what a preview needs: header, block table, node transforms,
geometry and texture/material paths. Unknown block types are skipped whole via
the header's per-block size table, so they can never desync the parse.

Header layout (version 20.2.0.7, all supported games):
    ...B  header_string     line-terminated, "Gamebryo File Format, Version X"
     4B   version           0x14020007 for 20.2.0.7
     1B   endian            1 = little
     4B   user_version      11 = Fallout 3/NV, 12 = Skyrim/Fallout 4
     4B   num_blocks
     4B   bs_version        11 = Oblivion, 26/34 = FO3/FNV, 83 = Skyrim LE,
                            100 = Skyrim SE, 130 = FO4, 155 = FO76, 172 = Starfield
    ...   author            ShortString (1B length INCLUDING the trailing null)
     4B   unknown_int       only when bs_version > 130
    ...   process_script    ShortString
    ...   export_script     ShortString
    ...   max_filepath      ShortString, only when bs_version == 130
     2B   num_block_types
    ...   block_types       num_block_types × SizedString (4B length, no null)
     2B   block_type_index  num_blocks × ushort, index into block_types
     4B   block_sizes       num_blocks × uint32   (version >= 20.2.0.5 only)
     4B   num_strings
     4B   max_string_length
    ...   strings           num_strings × SizedString
     4B   num_groups
     4B   groups            num_groups × uint32

Blocks follow in order, each exactly block_sizes[i] bytes, so the reader seeks
by offset. Geometry, by era:
  - BSTriShape (SSE/FO4): inline interleaved vertex buffer. 64-bit descriptor:
    bits 0-3 vertex size /4, 8-11 UV offset /4, 16-19 normal, 20-23 tangent,
    24-27 colour, 44-55 VF_* flags. SSE positions are float32; FO4+ half
    unless VF_FULLPREC. Data Size = (desc&0xF)*verts*4 + tris*6, but is derived
    and sometimes stale — recomputed here. SKINNED SSE meshes (bodies, armour,
    hair, facegen) leave the shape empty; geometry is in the NiSkinPartition
    reached via the skin instance.
  - NiTriShape/NiTriStrips (LE and earlier): separate float32 data block;
    strips are converted to triangles.
  - BSGeometry (Starfield, bsver 172/173): no geometry in the nif at all —
    the block names geometries/<path>.mesh (see Utils.sf_mesh_reader).

Textures: BSShaderTextureSet slot 0 = diffuse, 1 = normal. Pre-Skyrim shaders
hang off the NiAVObject properties array. FO4/Starfield shaders instead name a
material file (.bgsm/.bgem/.mat) whose contents override the texture set;
BSEffectShaderProperty names its texture inline.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "NifError", "NifHeader", "NifShape", "NifModel",
    "read_nif", "read_nif_header",
]

# Vertex descriptor attribute flags (bits 44+ of BSTriShape.vertex_desc).
VF_VERTEX = 0x001
VF_UV = 0x002
VF_UV_2 = 0x004
VF_NORMAL = 0x008
VF_TANGENT = 0x010
VF_COLORS = 0x020
VF_SKINNED = 0x040
VF_LANDDATA = 0x080
VF_EYEDATA = 0x100
VF_FULLPREC = 0x400

_NODE_TYPES = {
    "NiNode", "BSFadeNode", "BSLeafAnimNode", "BSOrderedNode", "BSValueNode",
    "BSMultiBoundNode", "BSBlastNode", "BSDamageStage", "BSMasterParticleSystem",
    "NiBillboardNode", "NiSwitchNode", "NiBSAnimationNode", "NiBSParticleNode",
    "BSDebrisNode", "BSTreeNode", "NiLODNode", "BSRangeNode",
}

_BSTRISHAPE_TYPES = {
    "BSTriShape", "BSDynamicTriShape", "BSSubIndexTriShape", "BSMeshLODTriShape",
}

_SKIN_INSTANCE_TYPES = {
    "NiSkinInstance", "BSDismemberSkinInstance", "BSSkinInstance",
}

_SHADER_TYPES = {
    "BSLightingShaderProperty", "BSEffectShaderProperty",
    "BSShaderPPLightingProperty", "BSShaderNoLightingProperty",
    "SkyShaderProperty", "TallGrassShaderProperty", "Lighting30ShaderProperty",
    "WaterShaderProperty", "DistantLODShaderProperty", "BSSkyShaderProperty",
}


class NifError(Exception):
    """Raised when a file is not a readable NIF."""


class _Cur:
    """Little-endian cursor over a bytes buffer."""

    __slots__ = ("d", "p")

    def __init__(self, data: bytes, pos: int = 0):
        self.d = data
        self.p = pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.p + n > len(self.d):
            raise NifError(f"read past end of data (want {n} at {self.p})")
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def skip(self, n: int) -> None:
        self.p += n

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack_from("<H", self.d, self._adv(2))[0]

    def u32(self) -> int:
        return struct.unpack_from("<I", self.d, self._adv(4))[0]

    def i32(self) -> int:
        return struct.unpack_from("<i", self.d, self._adv(4))[0]

    def u64(self) -> int:
        return struct.unpack_from("<Q", self.d, self._adv(8))[0]

    def f32(self) -> float:
        return struct.unpack_from("<f", self.d, self._adv(4))[0]

    def _adv(self, n: int) -> int:
        if self.p + n > len(self.d):
            raise NifError(f"read past end of data (want {n} at {self.p})")
        off = self.p
        self.p += n
        return off

    def vec3(self) -> tuple[float, float, float]:
        return struct.unpack_from("<3f", self.d, self._adv(12))

    def mat33(self) -> tuple[float, ...]:
        return struct.unpack_from("<9f", self.d, self._adv(36))

    def sized_str(self) -> str:
        n = self.u32()
        return self.take(n).decode("latin-1")

    def short_str(self) -> str:
        n = self.u8()
        return self.take(n).decode("latin-1").rstrip("\x00")

    def refs(self) -> list[int]:
        """Read a uint32 count followed by that many int32 block refs."""
        n = self.u32()
        if n == 0:
            return []
        return list(struct.unpack_from(f"<{n}i", self.d, self._adv(4 * n)))


@dataclass
class NifHeader:
    version: int
    user_version: int
    bs_version: int
    num_blocks: int
    block_types: list[str]
    block_type_index: list[int]
    block_sizes: list[int]
    strings: list[str]
    body_offset: int
    header_string: str = ""

    def type_of(self, i: int) -> str:
        """Block type name of block *i*."""
        if 0 <= i < len(self.block_type_index):
            t = self.block_type_index[i]
            if 0 <= t < len(self.block_types):
                return self.block_types[t]
        return ""

    def string(self, idx: int) -> str:
        """Resolve a string-table index; -1 / out of range yields ''."""
        if 0 <= idx < len(self.strings):
            return self.strings[idx]
        return ""

    @property
    def version_name(self) -> str:
        return {
            11: "Oblivion", 26: "Fallout 3", 34: "Fallout 3/NV",
            83: "Skyrim", 100: "Skyrim SE", 130: "Fallout 4",
            155: "Fallout 76", 172: "Starfield",
        }.get(self.bs_version, f"BSVersion {self.bs_version}")


@dataclass
class NifShape:
    """One renderable mesh: triangles plus the transform that places it."""

    name: str
    block_index: int
    block_type: str
    vertices: list[tuple[float, float, float]] = field(default_factory=list)
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    uvs: list[tuple[float, float]] = field(default_factory=list)
    triangles: list[tuple[int, int, int]] = field(default_factory=list)
    # Vertex count as declared by the block, even when no geometry was decoded.
    num_vertices: int = 0
    # World transform, decomposed: translation, row-major 3x3 rotation, scale.
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, ...] = (1, 0, 0, 0, 1, 0, 0, 0, 1)
    scale: float = 1.0
    textures: list[str] = field(default_factory=list)
    shader_type: str = ""
    # Material file named by the shader; overrides `textures` when set.
    material: str = ""
    # Starfield external geometry path (under geometries/, no suffix).
    mesh_path: str = ""

    @property
    def diffuse(self) -> str:
        return self.textures[0] if self.textures else ""

    @property
    def normal_map(self) -> str:
        return self.textures[1] if len(self.textures) > 1 else ""


@dataclass
class NifModel:
    header: NifHeader
    shapes: list[NifShape] = field(default_factory=list)
    # Block indices this reader recognised but could not decode, by type name.
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def tri_count(self) -> int:
        return sum(len(s.triangles) for s in self.shapes)

    @property
    def vert_count(self) -> int:
        return sum(len(s.vertices) for s in self.shapes)

    def texture_paths(self) -> list[str]:
        """Every distinct non-empty texture path referenced, in first-seen order."""
        seen: dict[str, None] = {}
        for s in self.shapes:
            for t in s.textures:
                if t:
                    seen.setdefault(t, None)
        return list(seen)


def _parse_header(data: bytes) -> NifHeader:
    nl = data.find(b"\n", 0, 128)
    if nl < 0 or not data.startswith((b"Gamebryo File Format", b"NetImmerse File Format")):
        raise NifError("not a NIF file (bad header string)")
    header_string = data[:nl].decode("latin-1")
    c = _Cur(data, nl + 1)

    version = c.u32()
    if version >= 0x14000004:
        endian = c.u8()
        if endian != 1:
            raise NifError("big-endian NIF files are not supported")
    user_version = c.u32() if version >= 0x0A000108 else 0
    num_blocks = c.u32()

    bs_version = 0
    if user_version >= 11:
        bs_version = c.u32()
        c.short_str()                      # author
        if bs_version > 130:
            c.u32()                        # unknown int
        c.short_str()                      # process script
        c.short_str()                      # export script
        if bs_version == 130:
            c.short_str()                  # max filepath

    num_block_types = c.u16()
    block_types = [c.sized_str() for _ in range(num_block_types)]
    block_type_index = list(
        struct.unpack_from(f"<{num_blocks}H", c.d, c._adv(2 * num_blocks))
    ) if num_blocks else []

    block_sizes: list[int] = []
    if version >= 0x14020005:
        block_sizes = list(
            struct.unpack_from(f"<{num_blocks}I", c.d, c._adv(4 * num_blocks))
        ) if num_blocks else []

    strings: list[str] = []
    if version >= 0x14010003:
        num_strings = c.u32()
        c.u32()                            # max string length
        strings = [c.sized_str() for _ in range(num_strings)]

    if version >= 0x05000006:
        num_groups = c.u32()
        c.skip(4 * num_groups)

    return NifHeader(
        version=version, user_version=user_version, bs_version=bs_version,
        num_blocks=num_blocks, block_types=block_types,
        block_type_index=block_type_index, block_sizes=block_sizes,
        strings=strings, body_offset=c.p, header_string=header_string,
    )


def _source_bytes(source, limit: int | None = None) -> bytes:
    """Accept a path or raw bytes, so callers can hand us archive contents."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        return data[:limit] if limit is not None else data
    with open(source, "rb") as f:
        return f.read(limit) if limit is not None else f.read()


def read_nif_header(source: "str | Path | bytes") -> NifHeader:
    """Parse just the header of *source* (a path, or the file's bytes)."""
    return _parse_header(_source_bytes(source, 1 << 20))


def _block_offsets(h: NifHeader) -> list[int]:
    offs: list[int] = []
    p = h.body_offset
    for size in h.block_sizes:
        offs.append(p)
        p += size
    return offs


def _read_avobject(c: _Cur, h: NifHeader) -> dict:
    """Consume the NiObjectNET + NiAVObject prefix common to nodes and shapes."""
    name_idx = c.u32() if h.version >= 0x14010003 else -1
    c.refs()                                       # extra data list
    c.i32()                                        # controller
    # Flags widened to 32 bits in Fallout 3 and later.
    if h.bs_version > 26:
        c.u32()
    else:
        c.u16()
    translation = c.vec3()
    rotation = c.mat33()
    scale = c.f32()
    properties = c.refs() if h.bs_version <= 34 else []   # pre-Skyrim shaders
    c.i32()                                        # collision object
    return {
        "name": h.string(name_idx),
        "translation": translation,
        "rotation": rotation,
        "scale": scale,
        "properties": properties,
    }


def _decode_bstrishape(c: _Cur, h: NifHeader, av: dict, shape: NifShape,
                       block_type: str = "BSTriShape") -> None:
    """Decode the inline interleaved vertex buffer of a BSTriShape."""
    c.skip(16)                                     # bounding sphere
    if h.bs_version == 155:
        c.skip(24)                                 # bound min/max (Fallout 76)
    shape._skin_ref = c.i32()                      # type: ignore[attr-defined]
    shape._shader_ref = c.i32()                    # type: ignore[attr-defined]
    c.i32()                                        # alpha property
    desc = c.u64()

    num_tris = c.u32() if h.bs_version >= 130 else c.u16()
    num_verts = c.u16()
    data_size = c.u32()
    shape.num_vertices = num_verts

    flags = (desc >> 44) & 0xFFF
    bpv = (desc & 0xF) * 4

    # Data Size is derived and sometimes stale; prefer the descriptor's size.
    avail = len(c.d) - c.p
    tail = 4 if h.bs_version == 100 else 0      # particle data size
    calc = bpv * num_verts + num_tris * 6

    # Data Size 0 is authoritative: facegen keeps positions in the dynamic array.
    if not data_size or not calc or calc + tail > avail:
        if data_size and data_size + tail <= avail:
            c.skip(data_size)
    else:
        vbuf = c.take(num_verts * bpv)
        # Skyrim SE always stores full-precision positions; Fallout 4+ opts in.
        fullprec = h.bs_version == 100 or bool(flags & VF_FULLPREC)
        _decode_vertex_buffer(vbuf, desc, num_verts, bpv, fullprec, shape)

        if num_tris:
            flat = struct.unpack(f"<{num_tris * 3}H", c.take(num_tris * 6))
            shape.triangles = [
                (flat[i], flat[i + 1], flat[i + 2])
                for i in range(0, len(flat), 3)
            ]

    if h.bs_version == 100:
        pdata = c.u32()                            # particle data size
        if pdata:
            c.skip(num_verts * 6 * 2 + num_tris * 6)

    if block_type == "BSDynamicTriShape":
        dyn = c.u32()
        if dyn:
            raw = c.take(dyn)
            if not shape.vertices:
                shape.vertices = [
                    v[:3] for v in struct.iter_unpack("<4f", raw)
                ]


def _decode_vertex_buffer(vbuf: bytes, desc: int, count: int, bpv: int,
                          fullprec: bool, shape: NifShape) -> None:
    """Scatter an interleaved BSVertexData buffer into a shape's attributes."""
    flags = (desc >> 44) & 0xFFF
    off_uv = ((desc >> 8) & 0xF) * 4
    off_norm = ((desc >> 16) & 0xF) * 4

    if flags & VF_VERTEX:
        shape.vertices = _strided(vbuf, 0, bpv, count,
                                  "<3f" if fullprec else "<3e")
    if flags & VF_UV:
        shape.uvs = _strided(vbuf, off_uv, bpv, count, "<2e")
    if flags & VF_NORMAL:
        norms = []
        for o in range(off_norm, off_norm + bpv * count, bpv):
            norms.append((vbuf[o] / 127.5 - 1.0,
                          vbuf[o + 1] / 127.5 - 1.0,
                          vbuf[o + 2] / 127.5 - 1.0))
        shape.normals = norms


def _decode_bsgeometry(c: _Cur, h: NifHeader, shape: NifShape) -> None:
    """Decode a Starfield BSGeometry: ref triple, then {counts, path} entries."""
    c.skip(16)                                     # bounding sphere
    c.skip(24)                                     # bound min/max
    shape._skin_ref = c.i32()                      # type: ignore[attr-defined]
    shape._shader_ref = c.i32()                    # type: ignore[attr-defined]
    c.i32()                                        # alpha property
    for _ in range(c.u8()):
        c.u32()                                    # index count
        vertex_count = c.u32()
        c.u32()                                    # flags
        path = c.sized_str()
        # The first entry is the full-detail mesh; later ones are LODs.
        if path and not shape.mesh_path:
            shape.mesh_path = path
            shape.num_vertices = vertex_count


def _decode_skin_partition(c: _Cur, h: NifHeader, shape: NifShape) -> None:
    """Read geometry out of a NiSkinPartition (where skinned SSE meshes keep it)."""
    num_partitions = c.u32()

    if h.bs_version == 100:
        data_size = c.u32()
        vertex_size = c.u32()
        desc = c.u64()
        if data_size and vertex_size:
            count = data_size // vertex_size
            vbuf = c.take(data_size)
            shape.num_vertices = count
            _decode_vertex_buffer(vbuf, desc, count, vertex_size, True, shape)

    tris: list[tuple[int, int, int]] = []
    for _ in range(num_partitions):
        num_verts = c.u16()
        num_tris = c.u16()
        num_bones = c.u16()
        num_strips = c.u16()
        num_weights = c.u16()
        c.skip(2 * num_bones)                      # bones

        has_vertex_map = c.u8() if h.version >= 0x0A010000 else 1
        if has_vertex_map:
            c.skip(2 * num_verts)                  # vertex map
        has_weights = c.u8() if h.version >= 0x0A010000 else 1
        if has_weights:
            c.skip(4 * num_verts * num_weights)    # vertex weights
        strip_lengths = list(struct.unpack_from(
            f"<{num_strips}H", c.d, c._adv(2 * num_strips))) if num_strips else []
        has_faces = c.u8() if h.version >= 0x0A010000 else 1
        if has_faces and num_strips:
            for n in strip_lengths:
                tris.extend(_strip_to_tris(struct.unpack(f"<{n}H", c.take(n * 2))))
        elif has_faces and num_tris:
            flat = struct.unpack(f"<{num_tris * 3}H", c.take(num_tris * 6))
            tris.extend((flat[i], flat[i + 1], flat[i + 2])
                        for i in range(0, len(flat), 3))
        has_bone_indices = c.u8()
        if has_bone_indices:
            c.skip(num_verts * num_weights)        # bone indices
        if h.bs_version > 34:
            c.u8()                                 # LOD level
            c.u8()                                 # global VB
        if h.bs_version == 100:
            c.u64()                                # per-partition vertex desc
            c.skip(num_tris * 6)                   # triangles copy

    if tris:
        shape.triangles = tris


def _strided(buf: bytes, offset: int, stride: int, count: int, fmt: str) -> list:
    """Unpack *count* tuples of *fmt* spaced *stride* bytes apart."""
    s = struct.Struct(fmt)
    unpack = s.unpack_from
    return [unpack(buf, o) for o in range(offset, offset + stride * count, stride)]


def _decode_geometry_data(c: _Cur, h: NifHeader, block_type: str) -> dict:
    """Decode NiTriShapeData / NiTriStripsData into vertices/normals/uvs/tris."""
    out: dict = {"vertices": [], "normals": [], "uvs": [], "triangles": []}

    # Bethesda 20.2.0.7 files use BSGeometryDataFlags, where only bit 0 counts
    # UV sets; everyone else packs the count into the low 6 bits.
    bs202 = h.version == 0x14020007 and h.bs_version > 0

    if h.version >= 0x0A010072:
        c.i32()                                    # group id
    num_verts = c.u16()
    if h.version >= 0x0A010000:
        c.u8()                                     # keep flags
        c.u8()                                     # compress flags
    has_verts = c.u8()
    if has_verts and num_verts:
        raw = c.take(num_verts * 12)
        out["vertices"] = list(struct.iter_unpack("<3f", raw))
    data_flags = c.u16() if h.version >= 0x0A000100 else 0
    if bs202 and h.bs_version > 34:
        c.u32()                                    # material CRC

    has_normals = c.u8()
    if has_normals and num_verts:
        raw = c.take(num_verts * 12)
        out["normals"] = list(struct.iter_unpack("<3f", raw))
        if data_flags & 0x1000:
            c.skip(num_verts * 24)                 # tangents + bitangents

    c.skip(16)                                     # bounding sphere
    has_colors = c.u8()
    if has_colors and num_verts:
        c.skip(num_verts * 16)

    num_uv_sets = (data_flags & 1) if bs202 else (data_flags & 0x3F)
    if num_uv_sets and num_verts:
        raw = c.take(num_verts * 8)
        out["uvs"] = list(struct.iter_unpack("<2f", raw))
        if num_uv_sets > 1:
            c.skip(num_verts * 8 * (num_uv_sets - 1))

    if h.version >= 0x0A000100:
        c.u16()                                    # consistency flags
    if h.version >= 0x14000004:
        c.i32()                                    # additional data

    num_tris = c.u16()                             # NiTriBasedGeomData

    if block_type == "NiTriShapeData":
        c.u32()                                    # num triangle points
        has_tris = c.u8() if h.version >= 0x0A010000 else 1
        if has_tris and num_tris:
            flat = struct.unpack(f"<{num_tris * 3}H", c.take(num_tris * 6))
            out["triangles"] = [
                (flat[i], flat[i + 1], flat[i + 2]) for i in range(0, len(flat), 3)
            ]
        num_match = c.u16()
        for _ in range(num_match):
            c.skip(2 * c.u16())                    # shared-normal groups
    elif block_type == "NiTriStripsData":
        num_strips = c.u16()
        lengths = list(struct.unpack_from(
            f"<{num_strips}H", c.d, c._adv(2 * num_strips))) if num_strips else []
        has_points = c.u8() if h.version >= 0x0A000103 else 1
        tris: list[tuple[int, int, int]] = []
        if has_points:
            for n in lengths:
                tris.extend(_strip_to_tris(struct.unpack(f"<{n}H", c.take(n * 2))))
        out["triangles"] = tris

    return out


def _strip_to_tris(strip) -> list[tuple[int, int, int]]:
    """Expand one triangle strip, dropping the degenerate stitching triangles."""
    tris = []
    for i in range(len(strip) - 2):
        a, b, cc = strip[i], strip[i + 1], strip[i + 2]
        if a == b or b == cc or a == cc:
            continue
        tris.append((a, cc, b) if (i & 1) else (a, b, cc))
    return tris


def _decode_texture_set(c: _Cur) -> list[str]:
    n = c.u32()
    return [c.sized_str() for _ in range(n)]


def _decode_shader(c: _Cur, h: NifHeader,
                   block_type: str) -> "tuple[int, int, str]":
    """Return ``(texture_set_ref, name_string_index, source_texture)``.

    On Fallout 4 the name is the path to a .bgsm/.bgem material file, and THAT
    is where the real textures live — the block's own texture set is often
    stale, so callers should prefer the material. BSEffectShaderProperty has no
    texture set at all; it names its texture inline instead.
    """
    if block_type == "BSEffectShaderProperty":
        name_idx = c.u32() if h.version >= 0x14010003 else -1
        c.refs()                                   # extra data
        c.i32()                                    # controller
        c.u32()                                    # shader flags 1
        c.u32()                                    # shader flags 2
        if h.bs_version >= 132:
            c.skip(4 * c.u32())                    # SF1 CRCs
            if h.bs_version >= 152:
                c.skip(4 * c.u32())                # SF2 CRCs
        c.skip(8)                                  # uv offset
        c.skip(8)                                  # uv scale
        return -1, name_idx, c.sized_str()         # source texture
    if block_type == "BSLightingShaderProperty":
        if 83 <= h.bs_version <= 130:
            c.u32()                                # shader type
        name_idx = c.u32() if h.version >= 0x14010003 else -1
        # The name (FO4/Starfield material path) must survive a failed decode
        # of the rest — Starfield adds fields this reader does not model.
        try:
            c.refs()                               # extra data
            c.i32()                                # controller
            c.u32()                                # shader flags 1
            c.u32()                                # shader flags 2
            c.skip(8)                              # uv offset
            c.skip(8)                              # uv scale
            return c.i32(), name_idx, ""          # texture set
        except (NifError, struct.error):
            return -1, name_idx, ""
    if block_type == "BSShaderPPLightingProperty":
        c.u32()                                    # name
        c.refs()                                   # extra data
        c.i32()                                    # controller
        c.u16()                                    # NiShadeProperty flags
        c.u32()                                    # shader type
        c.u32()                                    # shader flags 1
        c.u32()                                    # shader flags 2
        c.f32()                                    # env map scale
        c.u32()                                    # texture clamp mode
        return c.i32(), -1, ""                    # texture set
    return -1, -1, ""


def read_nif(source: "str | Path | bytes", *,
             want_geometry: bool = True) -> NifModel:
    """Parse *source* (path or raw bytes) into a NifModel of world-space shapes."""
    data = _source_bytes(source)
    h = _parse_header(data)
    model = NifModel(header=h)

    if not h.block_sizes:
        # Pre-20.2.0.5 files have no size table; sequential parsing of unknown
        # blocks is not safe, so report the header only.
        return model

    offs = _block_offsets(h)
    n = min(len(offs), h.num_blocks)

    # Pass 1: node transforms and the child graph, so shapes can be placed.
    local: dict[int, dict] = {}
    parent: dict[int, int] = {}
    shapes: list[NifShape] = []
    tex_of_shader: dict[int, list[str]] = {}
    material_of_shader: dict[int, str] = {}
    source_of_shader: dict[int, str] = {}
    shader_of_block: dict[int, int] = {}
    data_of_shape: dict[int, int] = {}

    for i in range(n):
        bt = h.type_of(i)
        size = h.block_sizes[i]
        if size <= 0 or offs[i] + size > len(data):
            continue
        blob = data[offs[i]:offs[i] + size]
        try:
            if bt in _NODE_TYPES:
                c = _Cur(blob)
                av = _read_avobject(c, h)
                local[i] = av
                for ch in c.refs():
                    if ch >= 0:
                        parent[ch] = i
            elif bt in _BSTRISHAPE_TYPES:
                c = _Cur(blob)
                av = _read_avobject(c, h)
                local[i] = av
                sh = NifShape(name=av["name"], block_index=i, block_type=bt)
                if want_geometry:
                    _decode_bstrishape(c, h, av, sh, bt)
                else:
                    c.skip(16)
                    if h.bs_version >= 155:
                        c.skip(32)
                    sh._skin_ref = c.i32()         # type: ignore[attr-defined]
                    sh._shader_ref = c.i32()       # type: ignore[attr-defined]
                shader_of_block[i] = getattr(sh, "_shader_ref", -1)
                shapes.append(sh)
            elif bt == "BSGeometry":
                c = _Cur(blob)
                av = _read_avobject(c, h)
                local[i] = av
                sh = NifShape(name=av["name"], block_index=i, block_type=bt)
                _decode_bsgeometry(c, h, sh)
                shader_of_block[i] = getattr(sh, "_shader_ref", -1)
                shapes.append(sh)
            elif bt in ("NiTriShape", "NiTriStrips"):
                c = _Cur(blob)
                av = _read_avobject(c, h)
                local[i] = av
                sh = NifShape(name=av["name"], block_index=i, block_type=bt)
                data_ref = c.i32()
                c.i32()                            # skin instance
                if h.version >= 0x14020005:        # MaterialData
                    nm = c.u32()
                    c.skip(4 * nm)                 # material names
                    c.skip(4 * nm)                 # material extra data
                    c.i32()                        # active material
                    if h.version >= 0x14020007:
                        c.u8()                     # material needs update
                if h.bs_version > 34:
                    shader_of_block[i] = c.i32()
                    c.i32()                        # alpha property
                else:
                    # Pre-Skyrim: shader hangs off the properties array.
                    for pref in av["properties"]:
                        if 0 <= pref < n and h.type_of(pref) in _SHADER_TYPES:
                            shader_of_block[i] = pref
                            break
                data_of_shape[i] = data_ref
                shapes.append(sh)
            elif bt == "BSShaderTextureSet":
                tex_of_shader[i] = _decode_texture_set(_Cur(blob))
            elif bt in _SHADER_TYPES:
                ref, name_idx, src_tex = _decode_shader(_Cur(blob), h, bt)
                if src_tex:
                    source_of_shader[i] = src_tex
                if ref >= 0:
                    shader_of_block[-1000 - i] = ref
                name = h.string(name_idx)
                if name and name.lower().endswith((".bgsm", ".bgem", ".mat")):
                    material_of_shader[i] = name
            else:
                model.skipped[bt] = model.skipped.get(bt, 0) + 1
        except (NifError, struct.error):
            model.skipped[bt] = model.skipped.get(bt, 0) + 1

    # Pass 2: NiTriShape geometry lives in a separate data block.
    if want_geometry:
        for sh in shapes:
            dref = data_of_shape.get(sh.block_index, -1)
            if dref < 0 or dref >= n:
                continue
            dt = h.type_of(dref)
            if dt not in ("NiTriShapeData", "NiTriStripsData"):
                continue
            size = h.block_sizes[dref]
            try:
                got = _decode_geometry_data(
                    _Cur(data[offs[dref]:offs[dref] + size]), h, dt)
            except (NifError, struct.error):
                model.skipped[dt] = model.skipped.get(dt, 0) + 1
                continue
            sh.vertices = got["vertices"]
            sh.normals = got["normals"]
            sh.uvs = got["uvs"]
            sh.triangles = got["triangles"]

    # Pass 3: skinned shapes — follow skin instance to the NiSkinPartition.
    if want_geometry:
        for sh in shapes:
            if sh.vertices and sh.triangles:
                continue
            skin = getattr(sh, "_skin_ref", -1)
            if skin is None or skin < 0 or skin >= n:
                continue
            if h.type_of(skin) not in _SKIN_INSTANCE_TYPES:
                continue
            try:
                sc = _Cur(data[offs[skin]:offs[skin] + h.block_sizes[skin]])
                sc.i32()                           # skin data
                part = sc.i32()                    # skin partition
            except (NifError, struct.error):
                continue
            if part < 0 or part >= n or h.type_of(part) != "NiSkinPartition":
                continue
            try:
                _decode_skin_partition(
                    _Cur(data[offs[part]:offs[part] + h.block_sizes[part]]),
                    h, sh)
            except (NifError, struct.error):
                model.skipped["NiSkinPartition"] = (
                    model.skipped.get("NiSkinPartition", 0) + 1)

    # Resolve shader -> texture set for each shape.
    for sh in shapes:
        sref = shader_of_block.get(sh.block_index, -1)
        if sref is None or sref < 0 or sref >= n:
            continue
        sh.shader_type = h.type_of(sref)
        sh.material = material_of_shader.get(sref, "")
        tref = shader_of_block.get(-1000 - sref, -1)
        if tref >= 0:
            sh.textures = tex_of_shader.get(tref, [])
        if not sh.textures:
            # BSEffectShaderProperty names its texture inline.
            src = source_of_shader.get(sref, "")
            if src:
                sh.textures = [src]

    # Compose world transforms down the node graph.
    for sh in shapes:
        t, r, s = _world_transform(sh.block_index, local, parent)
        sh.translation, sh.rotation, sh.scale = t, r, s

    model.shapes = shapes
    return model


def _mat_mul(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(
        a[row * 3 + 0] * b[0 + col] + a[row * 3 + 1] * b[3 + col]
        + a[row * 3 + 2] * b[6 + col]
        for row in range(3) for col in range(3)
    )


def _world_transform(idx: int, local: dict[int, dict], parent: dict[int, int]):
    """Compose translation/rotation/scale from *idx* up to the root node."""
    chain: list[int] = []
    cur = idx
    seen = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        cur = parent.get(cur)

    t = (0.0, 0.0, 0.0)
    r = (1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0)
    s = 1.0
    for node in reversed(chain):
        av = local.get(node)
        if av is None:
            continue
        lt, lr, ls = av["translation"], av["rotation"], av["scale"]
        # world = parent_world ∘ local
        rt = (
            r[0] * lt[0] + r[1] * lt[1] + r[2] * lt[2],
            r[3] * lt[0] + r[4] * lt[1] + r[5] * lt[2],
            r[6] * lt[0] + r[7] * lt[1] + r[8] * lt[2],
        )
        t = (t[0] + s * rt[0], t[1] + s * rt[1], t[2] + s * rt[2])
        r = _mat_mul(r, lr)
        s = s * ls
    return t, r, s
