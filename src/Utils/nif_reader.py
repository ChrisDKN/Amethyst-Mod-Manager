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
    and sometimes stale - recomputed here. SKINNED SSE meshes (bodies, armour,
    hair, facegen) leave the shape empty; geometry is in the NiSkinPartition
    reached via the skin instance.
  - NiTriShape/NiTriStrips (LE and earlier): separate float32 data block;
    strips are converted to triangles.
  - BSGeometry (Starfield, bsver 172/173): no geometry in the nif at all -
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
    tangents: list[tuple[float, float, float]] = field(default_factory=list)
    # Sign of the stored bitangent relative to cross(normal, tangent). Skyrim
    # uses both signs for mirrored UV islands; dropping it makes a normal map
    # light those islands in opposite directions.
    bitangent_signs: list[float] = field(default_factory=list)
    uvs: list[tuple[float, float]] = field(default_factory=list)
    # RGBA 0-1. Only modulates the diffuse when `vertex_colors` is set, which
    # is the engine's rule (SLSF2_Vertex_Colors); meshes routinely carry a
    # stale colour array with the flag off.
    colors: list[tuple[float, float, float, float]] = field(default_factory=list)
    vertex_colors: bool = False
    triangles: list[tuple[int, int, int]] = field(default_factory=list)
    # Vertex count as declared by the block, even when no geometry was decoded.
    num_vertices: int = 0
    # World transform, decomposed: translation, row-major 3x3 rotation, scale.
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, ...] = (1, 0, 0, 0, 1, 0, 0, 0, 1)
    scale: float = 1.0
    textures: list[str] = field(default_factory=list)
    shader_type: str = ""
    # Numeric Skyrim subtype from BSLightingShaderProperty. The block type
    # alone cannot distinguish ordinary fabric from runtime-tinted skin.
    lighting_shader_type: int = 0
    # Material file named by the shader; overrides `textures` when set.
    material: str = ""
    # Starfield external geometry path (under geometries/, no suffix).
    mesh_path: str = ""
    # BSLightingShaderProperty specular material (Skyrim/SSE; defaults else).
    spec_enabled: bool = True
    spec_color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    spec_strength: float = 1.0
    glossiness: float = 80.0
    # Non-zero only for environment-mapped shaders (type 1): the cubemap in
    # texture slot 4 is added at this strength, masked by slot 5.
    env_map_scale: float = 0.0
    # Community Shaders TruePBR (PGPatcher output). The classic specular and
    # texture-slot meanings do NOT apply to these.
    pbr: bool = False
    # Runtime colour multiply, filled in by callers (see Utils/facegen_tint).
    tint: tuple[float, float, float] = (1.0, 1.0, 1.0)
    # Runtime texture multiplied over the diffuse, filled in by callers: the
    # per-NPC FaceGen tint map carrying makeup, brows and skin tone.
    tint_overlay: str = ""
    # Skinning, for posing against a skeleton (see Utils.nif_skin). Bone names
    # and their skin->bone bind transforms, plus per-vertex (indices, weights)
    # when the shape is deformed by more than one bone; rigid shapes (every
    # FaceGen head part) carry no weights and ride their single bone.
    bones: list[str] = field(default_factory=list)
    binds: list[tuple] = field(default_factory=list)
    skin_weights: list = field(default_factory=list)
    # NiAlphaProperty: cut-out fur/hair/foliage need the test, glass the blend.
    alpha_test: bool = False
    alpha_blend: bool = False
    alpha_threshold: int = 128

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
    # Why a size-table-less file yielded no shapes, for the preview's log.
    walk_error: str = ""

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
    user_version = c.u32() if version >= 0x0A010000 else 0
    num_blocks = c.u32()

    bs_version = 0
    if version == 0x0A000102:
        # 10.0.1.2 carries the export info block alone, prefixed by a spare
        # int, with no user version fields at all.
        c.u32()                            # unknown int
        c.short_str()                      # author
        c.short_str()                      # process script
        c.short_str()                      # export script
    elif version >= 0x0A010000 and (
            user_version >= 10
            or (user_version == 1 and version != 0x0A020000)):
        # This is nif.xml's own gate for "User Version 2" and the export
        # strings. Testing user_version >= 11 instead misses 10.2.0.0 meshes
        # written at user version 10, which do carry both.
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

    # Empirical, not derived: 10.0.1.0 up to (not including) 10.2.0.0 puts one
    # more word here before the first block. Every such mesh vanilla Oblivion
    # ships (154 of them) has this word and the group count above both zero, so
    # the data cannot say which spec field is which - only that one more is
    # there. Without it block 0 starts 4 bytes early and the whole walk desyncs.
    if 0x0A000100 <= version < 0x0A020000:
        c.u32()

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
    shape._alpha_ref = c.i32()                     # type: ignore[attr-defined]
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
            shape.triangles = list(
                struct.iter_unpack("<3H", c.take(num_tris * 6)))

    if h.bs_version == 100:
        pdata = c.u32()                            # particle data size
        if pdata:
            c.skip(num_verts * 6 * 2 + num_tris * 6)

    if block_type == "BSDynamicTriShape":
        dyn = c.u32()
        if dyn:
            raw = c.take(dyn)
            if not shape.vertices:
                shape.vertices = list(struct.iter_unpack("<3f4x", raw))


def _decode_vertex_buffer(vbuf: bytes, desc: int, count: int, bpv: int,
                          fullprec: bool, shape: NifShape) -> None:
    """Scatter an interleaved BSVertexData buffer into a shape's attributes."""
    flags = (desc >> 44) & 0xFFF
    off_uv = ((desc >> 8) & 0xF) * 4
    off_norm = ((desc >> 16) & 0xF) * 4
    off_tan = ((desc >> 20) & 0xF) * 4
    off_col = ((desc >> 24) & 0xF) * 4

    if flags & VF_VERTEX:
        shape.vertices = _strided(vbuf, 0, bpv, count,
                                  "<3f" if fullprec else "<3e")
    if flags & VF_UV:
        shape.uvs = _strided(vbuf, off_uv, bpv, count, "<2e")
    if flags & VF_NORMAL:
        shape.normals = _signed_bytes(vbuf, off_norm, bpv, count)
    if flags & VF_TANGENT:
        # Needed to apply tangent-space normal maps.
        shape.tangents = _signed_bytes(vbuf, off_tan, bpv, count)
        # BSVertexData stores a packed bitangent alongside the normal and
        # tangent. Its full vector is redundant once the handedness is known,
        # so retain one float per vertex rather than widening the model by a
        # further vec3. ``off_uv`` is also the size of the position/extra-data
        # section; bitangent X is its last value.
        main_size = off_uv
        off_bx = ((main_size - 4) if main_size > 16
                  else (12 if fullprec else 6))
        bx_fmt = "<f" if fullprec or main_size > 16 else "<e"
        if (flags & VF_NORMAL and main_size > 0
                and off_bx + struct.calcsize(bx_fmt) <= bpv
                and off_norm + 4 <= bpv and off_tan + 4 <= bpv):
            bx = (v[0] for v in _strided(vbuf, off_bx, bpv, count, bx_fmt))
            end = bpv * count
            lut = _SNORM_LUT.__getitem__
            bitangents = zip(bx,
                             map(lut, vbuf[off_norm + 3:end:bpv]),
                             map(lut, vbuf[off_tan + 3:end:bpv]))
            shape.bitangent_signs = _bitangent_signs(
                shape.normals, shape.tangents, bitangents)
    if flags & VF_COLORS:
        # One RGBA byte quad per vertex, unlike NiTriShapeData's floats.
        end = off_col + bpv * count
        lut = _UNORM_LUT.__getitem__
        shape.colors = list(zip(map(lut, vbuf[off_col:end:bpv]),
                                map(lut, vbuf[off_col + 1:end:bpv]),
                                map(lut, vbuf[off_col + 2:end:bpv]),
                                map(lut, vbuf[off_col + 3:end:bpv])))


_SNORM_LUT = tuple(b / 127.5 - 1.0 for b in range(256))
_UNORM_LUT = tuple(b / 255.0 for b in range(256))


def _signed_bytes(vbuf: bytes, offset: int, stride: int, count: int) -> list:
    """Unpack a normalised 3-byte vector per vertex."""
    end = offset + stride * count
    lut = _SNORM_LUT.__getitem__
    # Stepped bytes slices + a table lookup keep the whole pass in C.
    return list(zip(map(lut, vbuf[offset:end:stride]),
                    map(lut, vbuf[offset + 1:end:stride]),
                    map(lut, vbuf[offset + 2:end:stride])))


def _bitangent_signs(normals, tangents, bitangents) -> list[float]:
    """Reduce stored bitangents to tangent-basis handedness.

    Bethesda calls the V direction ``tangent`` and the U direction
    ``bitangent``. The renderer reconstructs the latter from N x T, retaining
    this sign so mirrored UV islands keep the orientation authored in the NIF.
    """
    out = []
    for n, t, b in zip(normals, tangents, bitangents):
        cx = n[1] * t[2] - n[2] * t[1]
        cy = n[2] * t[0] - n[0] * t[2]
        cz = n[0] * t[1] - n[1] * t[0]
        out.append(-1.0 if cx * b[0] + cy * b[1] + cz * b[2] < 0.0
                   else 1.0)
    return out


def _decode_bsgeometry(c: _Cur, h: NifHeader, shape: NifShape) -> None:
    """Decode a Starfield BSGeometry: ref triple, then {counts, path} entries."""
    c.skip(16)                                     # bounding sphere
    c.skip(24)                                     # bound min/max
    shape._skin_ref = c.i32()                      # type: ignore[attr-defined]
    shape._shader_ref = c.i32()                    # type: ignore[attr-defined]
    shape._alpha_ref = c.i32()                     # type: ignore[attr-defined]
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
            tris.extend(struct.iter_unpack("<3H", c.take(num_tris * 6)))
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
    if count <= 0:
        return []
    # Padding the format out to the full stride turns the per-record Python
    # loop into a single C-level iter_unpack pass.
    pad = stride - offset - struct.calcsize(fmt)
    padded = f"<{offset}x{fmt.lstrip('<')}{pad}x"
    return list(struct.iter_unpack(padded, memoryview(buf)[:stride * count]))


def _decode_geometry_data(c: _Cur, h: NifHeader, block_type: str) -> dict:
    """Decode NiTriShapeData / NiTriStripsData into vertices/normals/uvs/tris."""
    out: dict = {"vertices": [], "normals": [], "uvs": [], "triangles": [],
                 "tangents": [], "bitangent_signs": [], "colors": []}

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
            raw = c.take(num_verts * 12)
            out["tangents"] = list(struct.iter_unpack("<3f", raw))
            raw = c.take(num_verts * 12)
            bitangents = struct.iter_unpack("<3f", raw)
            out["bitangent_signs"] = _bitangent_signs(
                out["normals"], out["tangents"], bitangents)

    c.skip(16)                                     # bounding sphere
    has_colors = c.u8()
    if has_colors and num_verts:
        raw = c.take(num_verts * 16)
        out["colors"] = list(struct.iter_unpack("<4f", raw))

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
            out["triangles"] = list(
                struct.iter_unpack("<3H", c.take(num_tris * 6)))
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


def _decode_alpha_property(c: _Cur, h: NifHeader) -> "tuple[bool, bool, int]":
    """Return ``(test, blend, threshold)`` from a NiAlphaProperty block.

    Flags bit 0 enables blending, bit 9 alpha testing; the threshold byte
    follows. Cut-out foliage/fur/hair set testing and render as opaque cards
    without it.
    """
    if h.version >= 0x14010003:
        c.u32()                                    # name index
    c.refs()                                       # extra data
    c.i32()                                        # controller
    flags = c.u16()
    return bool(flags & 0x200), bool(flags & 0x1), c.u8()


def _decode_shader(c: _Cur, h: NifHeader,
                   block_type: str
                   ) -> "tuple[int, int, str, tuple | None, int]":
    """Return ``(texture_ref, name_index, source, spec, lighting_type)``.

    On Fallout 4 the name is the path to a .bgsm/.bgem material file, and THAT
    is where the real textures live - the block's own texture set is often
    stale, so callers should prefer the material. BSEffectShaderProperty has no
    texture set at all; it names its texture inline instead.

    *spec* is ``(enabled, color, strength, glossiness)`` from Skyrim/SSE
    BSLightingShaderProperty blocks, None elsewhere.
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
        return -1, name_idx, c.sized_str(), None, 0  # source texture
    if block_type == "BSLightingShaderProperty":
        shader_type = 0
        if 83 <= h.bs_version <= 130:
            shader_type = c.u32()
        name_idx = c.u32() if h.version >= 0x14010003 else -1
        # The name (FO4/Starfield material path) must survive a failed decode
        # of the rest - Starfield adds fields this reader does not model.
        try:
            c.refs()                               # extra data
            c.i32()                                # controller
            flags1 = c.u32()                       # shader flags 1
            flags2 = c.u32()                       # shader flags 2
            c.skip(8)                              # uv offset
            c.skip(8)                              # uv scale
            tref = c.i32()                         # texture set
        except (NifError, struct.error):
            return -1, name_idx, "", None, shader_type
        spec = None
        if h.bs_version <= 100:
            # Skyrim/SSE only: FO4 inserts a wet-material ref here and swaps
            # glossiness for 0-1 smoothness (its values come from .bgsm).
            try:
                c.skip(12)                         # emissive color
                c.f32()                            # emissive multiple
                c.u32()                            # texture clamp mode
                c.f32()                            # alpha
                c.f32()                            # refraction strength
                gloss = c.f32()
                color = (c.f32(), c.f32(), c.f32())
                strength = c.f32()
                # Soft-lighting and rim-light power are present for every
                # Skyrim shader. Conditional data follows them: vanilla
                # FaceGen stores its fallback skin/hair tint directly here.
                c.f32()                            # lighting effect 1
                c.f32()                            # lighting effect 2
                env = 0.0
                shader_tint = (1.0, 1.0, 1.0)
                if shader_type == 1:               # environment map
                    env = c.f32()
                elif shader_type in (5, 6):        # skin / hair tint
                    shader_tint = c.vec3()
                # Flags 2 bit 23 is nominally SLSF2_Unused01; Community
                # Shaders' TruePBR claims it, and PGPatcher stamps it on
                # every mesh it converts.
                pbr = bool(flags2 & 0x00800000)
                # Bit 0 of flags 1 is SLSF1_Specular, bit 5 of flags 2 is
                # SLSF2_Vertex_Colors.
                spec = (bool(flags1 & 1), color, strength, gloss, env, pbr,
                        bool(flags2 & 0x20), shader_tint)
            except (NifError, struct.error):
                pass
        return tref, name_idx, "", spec, shader_type
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
        return c.i32(), -1, "", None, 0           # texture set
    return -1, -1, "", None, 0


# Block types the pre-20.2.0.5 walk keeps array contents for. Everything else
# is stepped over for its size alone.
_SPEC_WANT = _NODE_TYPES | {
    "NiTriShape", "NiTriStrips", "NiTriShapeData", "NiTriStripsData",
    "NiTexturingProperty", "NiSourceTexture", "NiMaterialProperty",
    "NiAlphaProperty", "NiVertexColorProperty",
}

_SPEC_DATA_TYPES = ("NiTriShapeData", "NiTriStripsData")


def _spec_str(val, h: NifHeader) -> str:
    """Flatten nif.xml's `string` compound (inline text or a table index)."""
    if isinstance(val, dict):
        inner = val.get("String")
        if isinstance(inner, dict):
            return inner.get("Value", "") or ""
        if isinstance(inner, str):
            return inner
        idx = val.get("Index")
        if isinstance(idx, int):
            return h.string(idx)
    return ""


def _spec_vec3(val) -> tuple[float, float, float]:
    if isinstance(val, dict):
        return (val.get("x", 0.0), val.get("y", 0.0), val.get("z", 0.0))
    if isinstance(val, (list, tuple)) and len(val) >= 3:
        return tuple(val[:3])                       # type: ignore[return-value]
    return (0.0, 0.0, 0.0)


def _spec_mat33(val) -> tuple[float, ...]:
    if isinstance(val, dict):
        return tuple(val.get(f"m{r}{c}", 1.0 if r == c else 0.0)
                     for r in (1, 2, 3) for c in (1, 2, 3))
    return (1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0)


def _spec_normal_map(diffuse: str) -> str:
    """Oblivion's implicit normal map: the '_n' sibling of the diffuse."""
    if not diffuse:
        return ""
    stem, dot, ext = diffuse.rpartition(".")
    return f"{stem}_n{dot}{ext}" if dot else ""


def _spec_fill_geometry(sh: NifShape, gv: dict, block_type: str) -> None:
    """Copy vertices/normals/uvs/colours/triangles out of a decoded data block."""
    sh.num_vertices = gv.get("Num Vertices", 0) or 0
    verts = gv.get("Vertices")
    if isinstance(verts, list):
        sh.vertices = verts
    norms = gv.get("Normals")
    if isinstance(norms, list):
        sh.normals = norms
    cols = gv.get("Vertex Colors")
    if isinstance(cols, list):
        sh.colors = cols
    # UV Sets is a 2-D array: one row per set, each Num Vertices long.
    uv_sets = gv.get("UV Sets")
    if isinstance(uv_sets, list) and uv_sets and isinstance(uv_sets[0], list):
        sh.uvs = uv_sets[0]

    if block_type == "NiTriShapeData":
        tris = gv.get("Triangles")
        if isinstance(tris, list):
            sh.triangles = [t for t in tris if isinstance(t, tuple)]
        return

    # NiTriStripsData stores one strip per row; stitch them into triangles.
    tris: list[tuple[int, int, int]] = []
    for strip in gv.get("Points") or []:
        if isinstance(strip, list) and len(strip) >= 3:
            tris.extend(_strip_to_tris(strip))
    sh.triangles = tris


def _read_nif_spec_walk(data: bytes, h: NifHeader,
                        want_geometry: bool) -> NifModel:
    """Build a NifModel from a nif.xml-driven walk (no block size table).

    Raises when the walk does not land exactly on a valid footer - a desync
    anywhere would otherwise surface as convincing but wrong geometry.
    """
    from Utils.nif_xml import load_spec

    spec = load_spec()
    model = NifModel(header=h)

    blocks: dict[int, dict] = {}
    end = h.body_offset
    for idx, offset, size, values in spec.walk(data, h, want=_SPEC_WANT):
        blocks[idx] = values
        end = offset + size

    # Integrity gate: the last block must abut the footer, and the footer must
    # account for the rest of the file exactly.
    if end + 4 > len(data):
        raise NifError("walk ran past the end of the file")
    num_roots = struct.unpack_from("<I", data, end)[0]
    if num_roots > h.num_blocks or end + 4 + 4 * num_roots != len(data):
        raise NifError(
            f"walk ended at {end} but the footer does not close the file")

    n = h.num_blocks
    local: dict[int, dict] = {}
    parent: dict[int, int] = {}
    shapes: list[NifShape] = []

    def refs(values: dict, key: str) -> list[int]:
        got = values.get(key)
        return [r for r in got if isinstance(r, int)] if isinstance(got, list) else []

    for idx, values in blocks.items():
        bt = h.type_of(idx)
        if bt not in _NODE_TYPES and bt not in ("NiTriShape", "NiTriStrips"):
            continue
        local[idx] = {
            "translation": _spec_vec3(values.get("Translation")),
            "rotation": _spec_mat33(values.get("Rotation")),
            "scale": values.get("Scale", 1.0) or 1.0,
        }
        if bt in _NODE_TYPES:
            for child in refs(values, "Children"):
                if child >= 0:
                    parent[child] = idx
            continue

        sh = NifShape(name=_spec_str(values.get("Name"), h),
                      block_index=idx, block_type=bt)
        _spec_fill_shape(sh, values, blocks, h, n, want_geometry)
        shapes.append(sh)

    for sh in shapes:
        sh.translation, sh.rotation, sh.scale = _world_transform(
            sh.block_index, local, parent)

    model.shapes = shapes
    return model


def _spec_fill_shape(sh: NifShape, values: dict, blocks: dict, h: NifHeader,
                     n: int, want_geometry: bool) -> None:
    """Attach geometry and Oblivion-era shading to one NiTriShape/NiTriStrips."""
    if want_geometry:
        dref = values.get("Data", -1)
        if isinstance(dref, int) and 0 <= dref < n and dref in blocks:
            if h.type_of(dref) in _SPEC_DATA_TYPES:
                _spec_fill_geometry(sh, blocks[dref], h.type_of(dref))

    # Pre-Skyrim shading hangs off the NiAVObject properties array.
    props = values.get("Properties")
    if not isinstance(props, list):
        return
    has_vertex_colour_prop = False
    for pref in props:
        if not isinstance(pref, int) or not 0 <= pref < n or pref not in blocks:
            continue
        pt = h.type_of(pref)
        pv = blocks[pref]
        if pt == "NiTexturingProperty":
            sh.shader_type = pt
            _spec_fill_textures(sh, pv, blocks, h, n)
        elif pt == "NiMaterialProperty":
            sh.spec_color = _spec_vec3(pv.get("Specular Color"))
            sh.glossiness = pv.get("Glossiness", 80.0) or 80.0
        elif pt == "NiAlphaProperty":
            flags = pv.get("Flags", 0) or 0
            sh.alpha_blend = bool(flags & 0x1)
            sh.alpha_test = bool(flags & 0x200)
            sh.alpha_threshold = pv.get("Threshold", 128)
        elif pt == "NiVertexColorProperty":
            has_vertex_colour_prop = True
    # Oblivion only modulates by vertex colour when the property is present.
    sh.vertex_colors = has_vertex_colour_prop and bool(sh.colors)


def _spec_fill_textures(sh: NifShape, prop: dict, blocks: dict, h: NifHeader,
                        n: int) -> None:
    """Resolve NiTexturingProperty slots to their NiSourceTexture paths."""

    def slot(name: str) -> str:
        desc = prop.get(name)
        if not isinstance(desc, dict):
            return ""
        src = desc.get("Source")
        if not isinstance(src, int) or not 0 <= src < n or src not in blocks:
            return ""
        if h.type_of(src) != "NiSourceTexture":
            return ""
        return _spec_str(blocks[src].get("File Name"), h).replace("\\", "/")

    diffuse = slot("Base Texture")
    # Oblivion has no normal-map slot: the engine loads the '_n' sibling.
    normal = slot("Bump Map Texture") or _spec_normal_map(diffuse)
    sh.textures = [diffuse, normal]
    glow = slot("Glow Texture")
    if glow:
        sh.textures += ["", glow]


def read_nif(source: "str | Path | bytes", *,
             want_geometry: bool = True) -> NifModel:
    """Parse *source* (path or raw bytes) into a NifModel of world-space shapes."""
    data = _source_bytes(source)
    h = _parse_header(data)
    model = NifModel(header=h)

    if not h.block_sizes:
        # Pre-20.2.0.5 files (Oblivion and older) have no size table, so every
        # block has to be stepped over by its real layout - see Utils.nif_xml.
        try:
            return _read_nif_spec_walk(data, h, want_geometry)
        except Exception as exc:                             # noqa: BLE001
            # Never guess: a desynced walk would render as garbage geometry.
            # Report the header alone, as this reader always has.
            model.skipped["<unwalkable>"] = 1
            model.walk_error = str(exc)
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
    spec_of_shader: dict[int, tuple] = {}
    lighting_type_of_shader: dict[int, int] = {}
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
                    sh._alpha_ref = c.i32()        # type: ignore[attr-defined]
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
                # Skyrim LE keeps geometry in NiTriShape, and its FaceGen
                # heads are skinned exactly like the SSE BSTriShape ones.
                # Dropping the reference here left every LE shape looking
                # unskinned, so nothing could be posed: a Bijin head rendered
                # with the hair already at neck height while the face, brows
                # and eyes sat at the origin, a metre below it.
                sh._skin_ref = c.i32()             # type: ignore[attr-defined]
                if h.version >= 0x14020005:        # MaterialData
                    nm = c.u32()
                    c.skip(4 * nm)                 # material names
                    c.skip(4 * nm)                 # material extra data
                    c.i32()                        # active material
                    if h.version >= 0x14020007:
                        c.u8()                     # material needs update
                if h.bs_version > 34:
                    shader_of_block[i] = c.i32()
                    sh._alpha_ref = c.i32()        # alpha property
                else:
                    # Pre-Skyrim: shader and alpha hang off the properties array.
                    for pref in av["properties"]:
                        if not 0 <= pref < n:
                            continue
                        pt = h.type_of(pref)
                        if pt in _SHADER_TYPES and i not in shader_of_block:
                            shader_of_block[i] = pref
                        elif pt == "NiAlphaProperty":
                            sh._alpha_ref = pref
                data_of_shape[i] = data_ref
                shapes.append(sh)
            elif bt == "BSShaderTextureSet":
                tex_of_shader[i] = _decode_texture_set(_Cur(blob))
            elif bt in _SHADER_TYPES:
                ref, name_idx, src_tex, spec, lighting_type = _decode_shader(
                    _Cur(blob), h, bt)
                lighting_type_of_shader[i] = lighting_type
                if src_tex:
                    source_of_shader[i] = src_tex
                if spec is not None:
                    spec_of_shader[i] = spec
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
            sh.tangents = got["tangents"]
            sh.bitangent_signs = got["bitangent_signs"]
            sh.uvs = got["uvs"]
            sh.triangles = got["triangles"]
            sh.colors = got["colors"]

    # Pass 3: skinned shapes - follow skin instance to the NiSkinPartition.
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

    # Resolve each shape's alpha property (cut-out fur/hair/foliage).
    for sh in shapes:
        aref = getattr(sh, "_alpha_ref", -1)
        if aref is None or not 0 <= aref < n:
            continue
        if h.type_of(aref) != "NiAlphaProperty":
            continue
        try:
            sh.alpha_test, sh.alpha_blend, sh.alpha_threshold = (
                _decode_alpha_property(
                    _Cur(data[offs[aref]:offs[aref] + h.block_sizes[aref]]), h))
        except (NifError, struct.error):
            model.skipped["NiAlphaProperty"] = (
                model.skipped.get("NiAlphaProperty", 0) + 1)

    # Resolve shader -> texture set for each shape.
    for sh in shapes:
        sref = shader_of_block.get(sh.block_index, -1)
        if sref is None or sref < 0 or sref >= n:
            continue
        sh.shader_type = h.type_of(sref)
        sh.lighting_shader_type = lighting_type_of_shader.get(sref, 0)
        sh.material = material_of_shader.get(sref, "")
        if sref in spec_of_shader:
            (sh.spec_enabled, sh.spec_color, sh.spec_strength,
             sh.glossiness, sh.env_map_scale, sh.pbr,
             sh.vertex_colors, sh.tint) = spec_of_shader[sref]
        tref = shader_of_block.get(-1000 - sref, -1)
        if tref >= 0:
            sh.textures = tex_of_shader.get(tref, [])
        if not sh.textures:
            # BSEffectShaderProperty names its texture inline.
            src = source_of_shader.get(sref, "")
            if src:
                sh.textures = [src]

    # Compose world transforms down the node graph.
    #
    # A SKINNED shape is the exception: its vertices are already in skeleton
    # space, so its node transform has been baked in and applying it again
    # displaces the shape. FaceGen heads are the visible case - the head node
    # sits at the skeleton's neck height (z~120) while the brows, eyes, mouth
    # and hair sit at zero, so the head alone flies off up the screen.
    for sh in shapes:
        if _is_skinned(sh, h, n):
            if want_geometry:
                # Kept for Utils.nif_skin, which can pose the shape against a
                # real skeleton; without one the bind below is the best there
                # is, and it is what a head-only preview uses.
                got = _read_skin(data, offs, h, n, sh)
                if got is not None:
                    sh.bones, binds, weights = got
                    sh.binds = [(t, r, s) for t, r, s, _v in binds]
                    sh.skin_weights = weights or []
            bind = _skin_bind(data, offs, h, n, sh)
            if bind is not None:
                sh.translation, sh.rotation, sh.scale = _invert_bind(bind)
            else:
                sh.translation, sh.rotation, sh.scale = (
                    (0.0, 0.0, 0.0), (1, 0, 0, 0, 1, 0, 0, 0, 1), 1.0)
            continue
        t, r, s = _world_transform(sh.block_index, local, parent)
        sh.translation, sh.rotation, sh.scale = t, r, s

    model.shapes = shapes
    return model


def _is_skinned(shape, h: "NifHeader", n: int) -> bool:
    """Whether a shape's vertices are already in skeleton space.

    Keyed on a REAL skin instance block, not merely a stored reference: a
    stale or out-of-range index must not strand an ordinary shape at the
    origin.
    """
    skin = getattr(shape, "_skin_ref", -1)
    if skin is None or not 0 <= skin < n:
        return False
    return h.type_of(skin) in _SKIN_INSTANCE_TYPES


def _skin_bone_names(data: bytes, offs, h: "NifHeader", n: int, skin: int,
                     num_bones: int) -> list[str]:
    """The node names a skin instance's bone list points at, in bone order."""
    out: list[str] = []
    for i in range(num_bones):
        try:
            ref = struct.unpack_from("<i", data, offs[skin] + 16 + i * 4)[0]
        except struct.error:
            break
        name = ""
        if 0 <= ref < n:
            try:
                name = h.string(struct.unpack_from("<i", data, offs[ref])[0])
            except struct.error:
                name = ""
        out.append(name)
    return out


def _le_vertex_weights(data: bytes, offs, h: "NifHeader", part: int,
                       count: int):
    """Per-vertex ``(bone indices, weights)`` from a pre-SSE skin partition.

    Skyrim LE keeps skinning in the partition's PARALLEL ARRAYS rather than
    SSE's interleaved vertex buffer, and its bone indices are LOCAL to each
    partition - they index that partition's own bone list, which in turn
    indexes the skin instance's. Without this an LE hand mesh reports 36 bones
    and no weights, so it is placed rigidly on ONE of them and the fingers
    collapse into a flat paddle.
    """
    base = offs[part]
    end = base + h.block_sizes[part]
    cur = _Cur(data[base:end])
    try:
        num_partitions = cur.u32()
        out: list = [None] * count
        for _ in range(num_partitions):
            num_verts = cur.u16()
            num_tris = cur.u16()
            num_bones = cur.u16()
            num_strips = cur.u16()
            num_weights = cur.u16()
            bones = struct.unpack_from(f"<{num_bones}H", cur.d,
                                       cur._adv(2 * num_bones))
            has_map = cur.u8() if h.version >= 0x0A010000 else 1
            vmap = (struct.unpack_from(f"<{num_verts}H", cur.d,
                                       cur._adv(2 * num_verts))
                    if has_map else range(num_verts))
            has_weights = cur.u8() if h.version >= 0x0A010000 else 1
            weights = (struct.unpack_from(
                f"<{num_verts * num_weights}f", cur.d,
                cur._adv(4 * num_verts * num_weights))
                if has_weights else ())
            if num_strips:
                lengths = struct.unpack_from(f"<{num_strips}H", cur.d,
                                             cur._adv(2 * num_strips))
            else:
                lengths = ()
            has_faces = cur.u8() if h.version >= 0x0A010000 else 1
            if has_faces and num_strips:
                for length in lengths:
                    cur.skip(length * 2)
            elif has_faces and num_tris:
                cur.skip(num_tris * 6)
            has_indices = cur.u8()
            local = (struct.unpack_from(f"<{num_verts * num_weights}B", cur.d,
                                        cur._adv(num_verts * num_weights))
                     if has_indices else ())
            if h.bs_version > 34:
                cur.u8()                           # LOD level
                cur.u8()                           # global VB
            if not (has_weights and has_indices):
                continue
            for i in range(num_verts):
                vert = vmap[i] if has_map else i
                if not 0 <= vert < count:
                    continue
                lo = i * num_weights
                idx = tuple(bones[b] if b < num_bones else 0
                            for b in local[lo:lo + num_weights])
                out[vert] = (idx, weights[lo:lo + num_weights])
        if not any(out):
            return None
        blank = ((0,) * 4, (0.0,) * 4)
        return [v if v is not None else blank for v in out]
    except (NifError, struct.error, IndexError):
        return None


def _skin_vertex_weights(data: bytes, offs, h: "NifHeader", n: int, part: int,
                         count: int):
    """Per-vertex ``(bone indices, weights)`` from an SSE skin partition.

    Skyrim SE packs skinning into the partition's interleaved vertex buffer
    rather than the old parallel arrays. Returns None when the buffer carries
    no skinning data - a rigidly attached shape (every FaceGen head part) has
    one bone and no per-vertex weights at all, and reading the descriptor's
    zero offset would decode POSITIONS as weights.
    """
    if not 0 <= part < n or h.type_of(part) != "NiSkinPartition":
        return None
    if h.bs_version != 100:
        # Only SSE puts an interleaved vertex buffer at the head of the
        # partition; earlier games go straight into the per-partition arrays,
        # so the data_size/vertex_size read below would be garbage.
        return _le_vertex_weights(data, offs, h, part, count)
    base = offs[part]
    try:
        data_size, vertex_size = struct.unpack_from("<II", data, base + 4)
        desc = struct.unpack_from("<Q", data, base + 12)[0]
    except struct.error:
        return None
    if not data_size or not vertex_size:
        return None
    if not (desc >> 44) & VF_SKINNED:
        return None
    off_skin = ((desc >> 28) & 0xF) * 4
    if off_skin <= 0 or off_skin + 12 > vertex_size:
        return None
    vbuf_at = base + 20
    have = min(count, data_size // vertex_size)
    out = []
    for i in range(have):
        p = vbuf_at + i * vertex_size + off_skin
        try:
            w = struct.unpack_from("<4e", data, p)
            idx = struct.unpack_from("<4B", data, p + 8)
        except struct.error:
            break
        out.append((idx, w))
    return out or None


def _read_skin(data: bytes, offs, h: "NifHeader", n: int, shape):
    """Bone names, bind transforms and per-vertex weights for a skinned shape.

    Returns ``(names, binds, weights|None)``; *weights* is None for a rigidly
    attached shape, which is placed by its single bone instead.
    """
    skin = getattr(shape, "_skin_ref", -1)
    if skin is None or not 0 <= skin < n:
        return None
    try:
        sd, part = struct.unpack_from("<ii", data, offs[skin])
        num_bones = struct.unpack_from("<I", data, offs[skin] + 12)[0]
    except struct.error:
        return None
    if not 0 <= sd < n or h.type_of(sd) != "NiSkinData":
        return None
    if num_bones <= 0 or num_bones > 4096:
        return None
    binds = _skin_binds(data, offs, h, sd)
    if not binds:
        return None
    names = _skin_bone_names(data, offs, h, n, skin, num_bones)
    weights = None
    if num_bones > 1:
        weights = _skin_vertex_weights(data, offs, h, n, part,
                                       len(shape.vertices))
    return names, binds, weights


def _skin_binds(data: bytes, offs, h: "NifHeader", sd: int):
    """Every bone's skin->bone bind transform, in bone order.

    The count comes from the NiSkinData being walked, not the skin instance:
    the two agree in practice, but a mismatch would desynchronise this walk
    from the variable-length per-bone records.
    """
    base = offs[sd]
    end = base + h.block_sizes[sd]
    try:
        num_bones = struct.unpack_from("<I", data, base + 52)[0]
        has_weights = struct.unpack_from("<B", data, base + 56)[0]
    except struct.error:
        return []
    if num_bones <= 0 or num_bones > 4096:
        return []
    pos = base + 57
    out = []
    for _ in range(num_bones):
        if pos + 70 > end:
            break
        try:
            rot = struct.unpack_from("<9f", data, pos)
            tr = struct.unpack_from("<3f", data, pos + 36)
            scale = struct.unpack_from("<f", data, pos + 48)[0]
            verts = struct.unpack_from("<H", data, pos + 68)[0]
        except struct.error:
            break
        out.append((tr, rot, scale or 1.0, verts))
        pos += 70 + (verts * 6 if has_weights else 0)
    return out


def _skin_bind(data: bytes, offs, h: "NifHeader", n: int, shape):
    """The dominant bone's bind transform, as (rotation, T, scale).

    Used when no skeleton is available: a skinned shape's vertices sit in ITS
    BONE's space, and a FaceGen head mixes two - the mod's own parts bound to
    the head bone (T z=-120.34) while untouched vanilla parts like the mouth
    and brows are bound at the origin, so the raw vertices scatter the face
    over 120 units. Applying the bind puts every part in one shared frame.

    The bone holding the most vertices wins - correct whenever a shape is
    rigidly attached, which is the case for every part of a face.
    """
    skin = getattr(shape, "_skin_ref", -1)
    if skin is None or not 0 <= skin < n:
        return None
    try:
        sd = struct.unpack_from("<i", data, offs[skin])[0]
        num_bones = struct.unpack_from("<I", data, offs[skin] + 12)[0]
    except struct.error:
        return None
    if not 0 <= sd < n or h.type_of(sd) != "NiSkinData":
        return None
    if num_bones <= 0 or num_bones > 4096:
        return None
    best = None
    best_verts = -1
    for tr, rot, scale, verts in _skin_binds(data, offs, h, sd):
        if verts > best_verts:
            best_verts, best = verts, (rot, tr, scale)
    return best


def _invert_bind(bind):
    """A bind transform as the (translation, rotation, scale) to place by.

    NiSkinData stores the SKIN->BONE transform, so it applies directly:
    a part bound at the origin (an untouched vanilla mouth) stays put, while
    one bound to the head bone is carried up to the head. Inverting it instead
    moves the head parts a second time, to double the neck height.
    """
    rot, tr, scale = bind
    return tuple(tr), tuple(rot), (scale or 1.0)


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
