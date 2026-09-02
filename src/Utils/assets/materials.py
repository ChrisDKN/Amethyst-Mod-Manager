"""
Utils.assets.materials
Read texture paths out of FO4 .bgsm/.bgem and Starfield .mat material files.

FO4/Starfield meshes usually carry no textures; the shader names a material
file and the real paths live there. Binary BGSM/BGEM (v1/v2): fixed 63-byte
header, then length-prefixed strings (uint32 length INCLUDING trailing null).
JSON variants share the same extensions, so the first byte is sniffed:
FO4 Material Editor JSON has sDiffuseTexture-style keys; Starfield .mat is a
component tree whose texture nodes carry "FileName". Material version 6 adds
a write-mask byte before its texture table. Material paths are relative to
textures/, so that prefix is added when absent.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

__all__ = ["read_material", "MaterialTextures"]

# Offset of the first texture string in the original binary layout. Version 6
# added one mask-writes byte immediately before it.
_TEXTURE_OFFSET = 63

# Slot order for BGSM. BGEM front-loads its base/glow map, so slot 0 is still
# the one worth showing.
_MAX_SLOTS = 10


class MaterialTextures:
    """Texture paths and shared render state supplied by a material file."""

    __slots__ = ("paths", "kind", "version", "uv_offset", "uv_scale",
                 "texture_clamp_mode", "alpha", "alpha_blend",
                 "alpha_source", "alpha_destination", "alpha_test",
                 "alpha_threshold", "depth_write", "depth_test",
                 "double_sided")

    def __init__(self, paths: list[str], kind: str = "", version: int = 0,
                 *, uv_offset=(0.0, 0.0), uv_scale=(1.0, 1.0),
                 texture_clamp_mode=3, alpha=1.0, alpha_blend=False,
                 alpha_source=6, alpha_destination=7, alpha_test=False,
                 alpha_threshold=255, depth_write=True, depth_test=True,
                 double_sided=False):
        self.paths = paths
        self.kind = kind
        self.version = version
        self.uv_offset = uv_offset
        self.uv_scale = uv_scale
        self.texture_clamp_mode = texture_clamp_mode
        self.alpha = alpha
        self.alpha_blend = alpha_blend
        self.alpha_source = alpha_source
        self.alpha_destination = alpha_destination
        self.alpha_test = alpha_test
        self.alpha_threshold = alpha_threshold
        self.depth_write = depth_write
        self.depth_test = depth_test
        self.double_sided = double_sided

    @property
    def diffuse(self) -> str:
        """The base colour map, already prefixed with ``textures/``."""
        for p in self.paths:
            if p:
                return p
        return ""

    def __repr__(self) -> str:
        return f"MaterialTextures({self.kind} v{self.version}, {self.paths!r})"


def _normalise(path: str) -> str:
    """Material paths are relative to textures/; make them data-relative."""
    s = path.replace("\\", "/").strip().lstrip("/")
    if not s:
        return ""
    low = s.lower()
    if low.startswith("data/"):
        s = s[5:]
        low = s.lower()
    if not low.startswith("textures/"):
        s = "textures/" + s
    return s


def _read_binary(data: bytes) -> MaterialTextures:
    kind = data[:4].decode("latin-1")
    version = struct.unpack_from("<I", data, 4)[0]
    tile_flags = struct.unpack_from("<I", data, 8)[0]
    uv_offset = struct.unpack_from("<2f", data, 12)
    uv_scale = struct.unpack_from("<2f", data, 20)
    alpha = struct.unpack_from("<f", data, 28)[0]
    alpha_blend = bool(data[32])
    alpha_source = struct.unpack_from("<I", data, 33)[0]
    alpha_destination = struct.unpack_from("<I", data, 37)[0]
    alpha_threshold = data[41]
    alpha_test = bool(data[42])
    depth_write = bool(data[43])
    depth_test = bool(data[44])
    double_sided = bool(data[48])
    clamp_mode = (2 if tile_flags & 2 else 0) | (1 if tile_flags & 1 else 0)
    paths: list[str] = []
    pos = _TEXTURE_OFFSET + (1 if version >= 6 else 0)
    for _ in range(_MAX_SLOTS):
        if pos + 4 > len(data):
            break
        n = struct.unpack_from("<I", data, pos)[0]
        # A sane slot is a short, in-bounds string; anything else means we have
        # run past the texture block into the shading parameters.
        if n > 1024 or pos + 4 + n > len(data):
            break
        raw = data[pos + 4:pos + 4 + n]
        pos += 4 + n
        text = raw.decode("latin-1").rstrip("\x00")
        if text and not text.lower().endswith(
                (".dds", ".png", ".tga", ".bmp")):
            break
        paths.append(_normalise(text))
    return MaterialTextures(
        paths, kind, version, uv_offset=uv_offset, uv_scale=uv_scale,
        texture_clamp_mode=clamp_mode, alpha=alpha,
        alpha_blend=alpha_blend, alpha_source=alpha_source,
        alpha_destination=alpha_destination, alpha_test=alpha_test,
        alpha_threshold=alpha_threshold, depth_write=depth_write,
        depth_test=depth_test, double_sided=double_sided)


def _read_json(data: bytes) -> MaterialTextures:
    obj = json.loads(data.decode("utf-8", errors="replace"))

    # Starfield .mat: a component tree whose texture nodes carry "FileName".
    # Nothing like the flat Fallout 4 layout, so detect it by shape.
    if isinstance(obj, dict) and "Objects" in obj:
        found: list[str] = []
        seen: set[str] = set()

        def walk(node):
            if isinstance(node, dict):
                name = node.get("FileName")
                if isinstance(name, str) and name.lower().endswith(
                        (".dds", ".png", ".tga")):
                    rel = _normalise(name)
                    if rel not in seen:
                        seen.add(rel)
                        found.append(rel)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(obj)
        return MaterialTextures(found, "MAT", 0)

    order = ("sDiffuseTexture", "sNormalTexture", "sSmoothSpecTexture",
             "sGreyscaleTexture", "sEnvmapTexture", "sGlowTexture",
             "sInnerLayerTexture", "sWrinklesTexture",
             "sDisplacementTexture", "sBaseTexture")
    paths = [_normalise(str(obj.get(k, "") or "")) for k in order]
    tile_u = bool(obj.get("bTileU", True))
    tile_v = bool(obj.get("bTileV", True))
    clamp_mode = (2 if tile_u else 0) | (1 if tile_v else 0)
    return MaterialTextures(
        paths, "JSON", int(obj.get("iVersion", 0) or 0),
        uv_offset=(float(obj.get("fUOffset", 0.0) or 0.0),
                   float(obj.get("fVOffset", 0.0) or 0.0)),
        uv_scale=(float(obj.get("fUScale", 1.0) or 1.0),
                  float(obj.get("fVScale", 1.0) or 1.0)),
        texture_clamp_mode=clamp_mode,
        alpha=float(obj.get("fAlpha", 1.0) or 0.0),
        alpha_blend=bool(obj.get("bAlphaBlend", False)),
        alpha_source=int(obj.get("iAlphaSrc", 6) or 0),
        alpha_destination=int(obj.get("iAlphaDst", 7) or 0),
        alpha_test=bool(obj.get("bAlphaTest", False)),
        alpha_threshold=int(obj.get("iAlphaTestRef", 255) or 0),
        depth_write=bool(obj.get("bZBufferWrite", True)),
        depth_test=bool(obj.get("bZBufferTest", True)),
        double_sided=bool(obj.get("bTwoSided", False)))


def read_material(source: "str | Path | bytes") -> MaterialTextures | None:
    """Parse a .bgsm/.bgem from a path or raw bytes; None if unreadable."""
    try:
        if isinstance(source, (bytes, bytearray, memoryview)):
            data = bytes(source)
        else:
            data = Path(source).read_bytes()
    except OSError:
        return None
    if len(data) < 8:
        return None
    try:
        head = data.lstrip()[:1]
        if head == b"{":
            return _read_json(data)
        if data[:4] in (b"BGSM", b"BGEM"):
            return _read_binary(data)
    except Exception:                                    # noqa: BLE001
        return None
    return None
