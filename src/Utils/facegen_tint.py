"""Hair colour for FaceGen head meshes, from the NPC record in a plugin.

Skyrim ships hair textures as GREYSCALE and tints them at runtime, so a
NIF-only preview shows white/grey hair. The colour is not a texture-set
override (see [[txst_lookup]]) but a colour form:

    FaceGeom/<master>/<formid>.nif -> NPC_ record -> HCLF -> CLFM -> CNAM

CNAM is RGBA byte order, confirmed against Skyrim.esm's own named colours
(RedTintBright = 213,0,0; reading it as BGRA would make that blue).

Only the plugins actually needed are walked — the mod's own, plus the one or
two masters named by the FormIDs — because the masters are large.
"""
from __future__ import annotations

import mmap
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from Utils.txst_lookup import _iter_subrecords, _record_payload

# FaceGen meshes are named for the NPC's FormID, under a folder named for the
# plugin that owns the record.
_FACEGEN_RE = re.compile(
    r"facegeom/(?P<master>[^/]+\.es[pml])/(?P<formid>[0-9a-f]{8})\.nif$")

_PLUGIN_EXTS = (".esp", ".esm", ".esl")


@dataclass
class PluginForms:
    """One plugin's NPC hair references and colour records."""

    name: str = ""
    masters: list[str] = field(default_factory=list)
    # (owning plugin, formid low 24) -> raw HCLF FormID
    npc_hair: dict[tuple[str, int], int] = field(default_factory=dict)
    # (owning plugin, formid low 24) -> (r, g, b) in 0-1
    clfm: dict[tuple[str, int], tuple] = field(default_factory=dict)

    def owner(self, formid: int) -> str:
        """Which plugin a FormID belongs to, per this plugin's master list."""
        idx = (formid >> 24) & 0xFF
        return self.masters[idx] if idx < len(self.masters) else self.name


def facegen_npc(mesh_rel: str) -> "tuple[str, int] | None":
    """``('dawnguard.esm', 0x002B6C)`` for a FaceGen path, else None."""
    m = _FACEGEN_RE.search(mesh_rel.replace("\\", "/").lower())
    if not m:
        return None
    return m.group("master"), int(m.group("formid"), 16) & 0xFFFFFF


def parse_plugin_forms(path: Path) -> PluginForms:
    """Collect NPC_ hair references and CLFM colours from one plugin."""
    out = PluginForms(name=path.name.lower())
    with open(path, "rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as data:
            if len(data) < 24 or data[:4] != b"TES4":
                return out
            tes4_size = struct.unpack_from("<I", data, 4)[0]
            for sig, payload in _iter_subrecords(
                    data, 24, min(24 + tes4_size, len(data))):
                if sig == b"MAST":
                    out.masters.append(bytes(payload).split(b"\0")[0]
                                       .decode("cp1252", "replace").lower())
            pos = 24 + tes4_size
            end = len(data)
            while pos + 24 <= end:
                sig = bytes(data[pos:pos + 4])
                if sig == b"GRUP":
                    pos += 24
                    continue
                size, flags, formid = struct.unpack_from("<III", data, pos + 4)
                pos += 24
                if pos + size > end:
                    break
                if sig in (b"NPC_", b"CLFM"):
                    key = (out.owner(formid), formid & 0xFFFFFF)
                    try:
                        body = _record_payload(data, pos, size, flags)
                        for ssig, sdata in _iter_subrecords(body, 0, len(body)):
                            if sig == b"NPC_" and ssig == b"HCLF" and len(sdata) >= 4:
                                out.npc_hair[key] = struct.unpack_from(
                                    "<I", sdata, 0)[0]
                            elif sig == b"CLFM" and ssig == b"CNAM" and len(sdata) >= 3:
                                out.clfm[key] = (sdata[0] / 255.0,
                                                 sdata[1] / 255.0,
                                                 sdata[2] / 255.0)
                    except Exception:                    # noqa: BLE001
                        pass
                pos += size
    return out


_cache: dict[str, tuple[float, int, PluginForms]] = {}


def _parse_cached(path: Path) -> "PluginForms | None":
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    hit = _cache.get(key)
    if hit is not None and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        parsed = parse_plugin_forms(path)
    except Exception:                                    # noqa: BLE001
        parsed = PluginForms(name=path.name.lower())
    _cache[key] = (st.st_mtime, st.st_size, parsed)
    return parsed


def _find_plugin(name: str, dirs) -> "Path | None":
    low = name.lower()
    for d in dirs:
        try:
            for p in Path(d).iterdir():
                if p.name.lower() == low and p.is_file():
                    return p
        except OSError:
            continue
    return None


def _small_plugins(dirs) -> list[Path]:
    """Top-level plugins of the mod folders — masters are fetched by name."""
    out: list[Path] = []
    for d in dirs:
        try:
            out += sorted(p for p in Path(d).iterdir()
                          if p.suffix.lower() in _PLUGIN_EXTS and p.is_file())
        except OSError:
            continue
    return out


def hair_color(mesh_rel: str, plugin_dirs) -> "tuple[float, float, float] | None":
    """The NPC's hair colour for a FaceGen mesh path, or None.

    *plugin_dirs* should list the mesh's own mod first, then the data folder;
    the first plugin defining the NPC wins, which is what makes a mod's
    override beat the master it came from.
    """
    got = facegen_npc(mesh_rel)
    if got is None:
        return None
    master, low = got
    dirs = [Path(d) for d in plugin_dirs or ()]
    if not dirs:
        return None

    loaded: list[PluginForms] = []
    for p in _small_plugins(dirs[:-1] if len(dirs) > 1 else dirs):
        parsed = _parse_cached(p)
        if parsed is not None:
            loaded.append(parsed)
    # The master that owns the record, wherever it lives.
    own = _find_plugin(master, dirs)
    if own is not None:
        parsed = _parse_cached(own)
        if parsed is not None:
            loaded.append(parsed)

    hclf = None
    for pl in loaded:
        raw = pl.npc_hair.get((master, low))
        if raw is not None:
            hclf = (pl, raw)
            break
    if hclf is None:
        return None
    src, raw = hclf
    key = (src.owner(raw), raw & 0xFFFFFF)

    for pl in loaded:
        rgb = pl.clfm.get(key)
        if rgb is not None:
            return rgb
    # The colour usually lives in a master the mod does not ship.
    owner = _find_plugin(key[0], dirs)
    if owner is not None:
        parsed = _parse_cached(owner)
        if parsed is not None:
            return parsed.clfm.get(key)
    return None


def _is_hair(shape) -> bool:
    """Which shapes the hair colour applies to.

    A heuristic — the authoritative answer is the head part's type, which a
    baked FaceGen mesh no longer carries. Scoped to FaceGen meshes by the
    caller, so a stray match cannot affect ordinary models.
    """
    name = (shape.name or "").lower()
    if "hair" in name or "scalp" in name:
        return True
    diffuse = (shape.diffuse or "").replace("\\", "/").lower()
    return "/hair/" in diffuse


def apply_hair_tint(model, mesh_rel: str, plugin_dirs) -> int:
    """Set ``shape.tint`` on a FaceGen mesh's hair shapes. Returns how many."""
    if "facegeom" not in mesh_rel.replace("\\", "/").lower():
        return 0
    rgb = hair_color(mesh_rel, plugin_dirs)
    if rgb is None:
        return 0
    hits = 0
    for shape in model.shapes:
        if _is_hair(shape):
            shape.tint = rgb
            hits += 1
    return hits
