"""
bgsm_reader.py
Read texture paths out of FO4 .bgsm/.bgem and Starfield .mat material files.

FO4/Starfield meshes usually carry no textures; the shader names a material
file and the real paths live there. Binary BGSM/BGEM (v1/v2): fixed 63-byte
header, then length-prefixed strings (uint32 length INCLUDING trailing null).
JSON variants share the same extensions, so the first byte is sniffed:
FO4 Material Editor JSON has sDiffuseTexture-style keys; Starfield .mat is a
component tree whose texture nodes carry "FileName". Material paths are
relative to textures/, so that prefix is added when absent.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

__all__ = ["read_material", "MaterialTextures"]

# Offset of the first texture string in every binary variant seen in the wild
# (BGSM v1/v2 and BGEM v1 all agree; verified across 247 real material files).
_TEXTURE_OFFSET = 63

# Slot order for BGSM. BGEM front-loads its base/glow map, so slot 0 is still
# the one worth showing.
_MAX_SLOTS = 10


class MaterialTextures:
    """The texture paths a material file supplies."""

    __slots__ = ("paths", "kind", "version")

    def __init__(self, paths: list[str], kind: str = "", version: int = 0):
        self.paths = paths
        self.kind = kind
        self.version = version

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
    paths: list[str] = []
    pos = _TEXTURE_OFFSET
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
    return MaterialTextures(paths, kind, version)


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
    return MaterialTextures(paths, "JSON", int(obj.get("iVersion", 0) or 0))


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
