"""Hair colour for FaceGen head meshes, from the NPC record in a plugin.

Skyrim ships hair textures as GREYSCALE and tints them at runtime, so a
NIF-only preview shows white/grey hair. The colour is not a texture-set
override (see [[txst_lookup]]) but a colour form:

    FaceGeom/<master>/<formid>.nif -> NPC_ record -> HCLF -> CLFM -> CNAM

CNAM is RGBA byte order, confirmed against Skyrim.esm's own named colours
(RedTintBright = 213,0,0; reading it as BGRA would make that blue).

Only the plugins actually needed are walked - the mod's own, plus the one or
two masters named by the FormIDs - because the masters are large.
"""
from __future__ import annotations

import mmap
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from Utils.strings_table import is_localised
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
    # (owning plugin, formid low 24) -> name, or a string id when localised.
    npc_name: dict[tuple[str, int], "str | int"] = field(default_factory=dict)
    # (owning plugin, formid low 24) -> editor id
    npc_edid: dict[tuple[str, int], str] = field(default_factory=dict)
    # FULL payloads are string ids when set; resolved via Utils.strings_table.
    localised: bool = False

    def owner(self, formid: int) -> str:
        """Which plugin a FormID belongs to, per this plugin's master list."""
        idx = (formid >> 24) & 0xFF
        return self.masters[idx] if idx < len(self.masters) else self.name


def _full_value(sdata, localised: bool):
    """A FULL payload: text, or the string id a localised plugin stores."""
    if localised and len(sdata) == 4:
        return struct.unpack_from("<I", sdata, 0)[0]
    return bytes(sdata).split(b"\0")[0].decode("cp1252", "replace")


def facegen_npc(mesh_rel: str) -> "tuple[str, int] | None":
    """``('dawnguard.esm', 0x002B6C)`` for a FaceGen path, else None."""
    m = _FACEGEN_RE.search(mesh_rel.replace("\\", "/").lower())
    if not m:
        return None
    return m.group("master"), int(m.group("formid"), 16) & 0xFFFFFF


def parse_plugin_forms(path: Path) -> PluginForms:
    """Collect NPC_ hair references, names and CLFM colours from one plugin."""
    out = PluginForms(name=path.name.lower())
    with open(path, "rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as data:
            if len(data) < 24 or data[:4] != b"TES4":
                return out
            out.localised = is_localised(
                struct.unpack_from("<I", data, 8)[0])
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
                            elif sig == b"NPC_" and ssig == b"FULL":
                                out.npc_name[key] = _full_value(
                                    sdata, out.localised)
                            elif sig == b"NPC_" and ssig == b"EDID":
                                out.npc_edid[key] = bytes(sdata).split(
                                    b"\0")[0].decode("cp1252", "replace")
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


def _small_plugins(dirs) -> list[Path]:
    """Top-level plugins of the mod folders - masters are fetched by name."""
    out: list[Path] = []
    for d in dirs:
        try:
            out += sorted(p for p in Path(d).iterdir()
                          if p.suffix.lower() in _PLUGIN_EXTS and p.is_file())
        except OSError:
            continue
    return out


class FormsContext:
    """Plugin forms for one set of directories, loaded once and reused.

    ``hair_color`` re-lists and re-parses every plugin on each call, which is
    invisible for a single preview but quadratic over a whole catalogue (2020
    plugins x 1274 FaceGen heads on a real profile). A browser builds ONE of
    these per scan and passes it in.
    """

    def __init__(self, plugin_dirs):
        self.dirs = [Path(d) for d in plugin_dirs or ()]
        self._loaded: "list[PluginForms] | None" = None
        self._by_name: "dict[str, Path] | None" = None

    def loaded(self) -> list[PluginForms]:
        """The mod-side plugins, parsed once."""
        if self._loaded is None:
            search = self.dirs[:-1] if len(self.dirs) > 1 else self.dirs
            out = []
            for p in _small_plugins(search):
                parsed = _parse_cached(p)
                if parsed is not None:
                    out.append(parsed)
            self._loaded = out
        return self._loaded

    def find(self, name: str) -> "Path | None":
        """A plugin by name, from a directory listing taken once."""
        if self._by_name is None:
            index: dict[str, Path] = {}
            # Reversed so the FIRST directory wins: the mesh's own mod beats
            # the data folder, same precedence the per-call lookup had.
            for d in reversed(self.dirs):
                try:
                    for p in d.iterdir():
                        if p.is_file():
                            index[p.name.lower()] = p
                except OSError:
                    continue
            self._by_name = index
        return self._by_name.get(name.lower())


def hair_color(mesh_rel: str, plugin_dirs,
               ctx: "FormsContext | None" = None
               ) -> "tuple[float, float, float] | None":
    """The NPC's hair colour for a FaceGen mesh path, or None.

    *plugin_dirs* should list the mesh's own mod first, then the data folder;
    the first plugin defining the NPC wins, which is what makes a mod's
    override beat the master it came from. Pass a shared *ctx* to reuse one
    load of those directories across many meshes.
    """
    got = facegen_npc(mesh_rel)
    if got is None:
        return None
    master, low = got
    if ctx is None:
        ctx = FormsContext(plugin_dirs)
    if not ctx.dirs:
        return None

    loaded = list(ctx.loaded())
    # The master that owns the record, wherever it lives.
    own = ctx.find(master)
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
    owner = ctx.find(key[0])
    if owner is not None:
        parsed = _parse_cached(owner)
        if parsed is not None:
            return parsed.clfm.get(key)
    return None


# Head parts the engine tints with the NPC's HAIR colour. Eyebrows and facial
# hair follow the hair, which is why every brow texture ships near-greyscale
# (saturation ~11 against ~25 for hair) - untinted they all render the same
# flat grey regardless of who the NPC is.
#
# 'brow(?!n)' is load-bearing: vanilla eye shapes are named MaleEyesHumanBrown,
# and a bare 'brow' would tint every brown-eyed NPC's EYES with their hair
# colour. Matching 'eye' as an exclusion instead cannot work - 'eyebrow' is
# both.
_HAIR_TINTED = re.compile(
    r"hair|scalp|brow(?!n)|beard|moustache|mustache|sideburn|stubble|goatee")
# Diffuse folders that mean the same thing when the shape name does not say so.
_HAIR_TINTED_DIRS = ("/hair/", "/beards/", "brows/")


def _is_hair(shape) -> bool:
    """Whether the NPC's hair colour tints this shape.

    Covers eyebrows and facial hair as well as hair itself - the engine tints
    all of them from the one colour. A heuristic: the authoritative answer is
    the head part's type, which a baked FaceGen mesh no longer carries. Scoped
    to FaceGen meshes by the caller, so a stray match cannot affect ordinary
    models.
    """
    name = (shape.name or "").lower()
    if _HAIR_TINTED.search(name):
        return True
    diffuse = (shape.diffuse or "").replace("\\", "/").lower()
    return any(d in diffuse for d in _HAIR_TINTED_DIRS)


# Scalp hair only. Deliberately NARROWER than _HAIR_TINTED: a hood claims the
# hair slot and hides the hair, but the NPC keeps their eyebrows and beard, so
# the set that takes the hair COLOUR is not the set a hat removes.
_SCALP_HAIR = re.compile(r"hair|scalp")


def hair_shapes(model) -> list:
    """The shapes a hood or helmet hides: scalp hair, not brows or beards."""
    direct = [s for s in model.shapes
              if _SCALP_HAIR.search((s.name or "").lower())
              or "/hair/" in (s.diffuse or "").replace("\\", "/").lower()]
    return _with_shared_textures(model, direct)


def remove_hair(model) -> int:
    """Drop the hair from a FaceGen head. Returns how many shapes went."""
    doomed = {id(s) for s in hair_shapes(model)}
    if not doomed:
        return 0
    model.shapes = [s for s in model.shapes if id(s) not in doomed]
    return len(doomed)


def _with_shared_textures(model, direct: list) -> list:
    """*direct* plus any shape sharing one of their diffuse textures.

    Hair mods ship the strands twice - the hair and a highlight or alpha-sorted
    duplicate ('_dbHair' + '_dbHL') - sharing ONE texture, and only the obvious
    one matches by name. Leaving the twin behind means an untinted copy drawn
    over the tinted one, or a hood with half a hairstyle still poking through.
    """
    keys = {(s.diffuse or "").replace("\\", "/").lower()
            for s in direct if s.diffuse}
    if not keys:
        return direct
    picked = {id(s) for s in direct}
    return [s for s in model.shapes
            if id(s) in picked
            or (s.diffuse or "").replace("\\", "/").lower() in keys]


def _hair_shapes(model) -> list:
    """Every hair shape, including highlight layers named nothing like hair.

    Hair mods routinely ship the same strands twice - the hair and a highlight
    or alpha-sorted duplicate ('_dbHair' + '_dbHL') - sharing ONE texture. Only
    the obvious one matches by name, so tinting by name alone leaves an
    untinted copy drawn straight over the tinted one and the hair keeps the
    texture's own colour. Anything sharing a matched shape's diffuse is hair
    too.
    """
    return _with_shared_textures(model, [s for s in model.shapes
                                         if _is_hair(s)])


# Head parts that are never the face, however they are named.
_NOT_HEAD = ("beard", "brow", "hair", "scalp", "mouth", "eye", "lash",
             "tongue", "teeth")


def face_tint_path(mesh_rel: str) -> str:
    """The per-NPC FaceTint texture for a FaceGeom mesh path, else ''."""
    got = facegen_npc(mesh_rel)
    if got is None:
        return ""
    plugin, formid = got
    return ("textures/actors/character/facegendata/facetint/"
            f"{plugin}/{formid:08x}.dds")


def head_shape(model):
    """The face shape of a FaceGen head, or None.

    The FaceTint map is the FACE's UV, so it must not land on the brows, eyes
    or a beard - and a beard is routinely named '..._Headpart', which is why
    matching 'head' alone is not enough. Where several still qualify the
    largest wins; the face always carries the most geometry.
    """
    best = None
    for shape in model.shapes:
        name = (shape.name or "").lower()
        if any(word in name for word in _NOT_HEAD):
            continue
        diffuse = (shape.diffuse or "").replace("\\", "/").lower()
        base = diffuse.rsplit("/", 1)[-1]
        if "head" not in name and "head" not in base:
            continue
        if best is None or len(shape.vertices) > len(best.vertices):
            best = shape
    return best


def apply_face_tint(model, mesh_rel: str) -> bool:
    """Point the face at its FaceTint map: makeup, brows, freckles, skin tone.

    The baked mesh names only a generic head texture; the engine multiplies
    the per-NPC tint map over it at runtime, which is where an NPC's makeup,
    war paint, complexion and eyebrow shading actually live. Without it every
    face previews as bare untinted skin.

    The path is set optimistically - plenty of NPCs ship no tint map, so
    whether one EXISTS is settled by the texture loader, which can read it
    without reporting a miss for something optional.
    """
    if "facegeom" not in mesh_rel.replace("\\", "/").lower():
        return False
    rel = face_tint_path(mesh_rel)
    if not rel:
        return False
    shape = head_shape(model)
    if shape is None:
        return False
    shape.tint_overlay = rel
    return True


def apply_hair_tint(model, mesh_rel: str, plugin_dirs,
                    ctx: "FormsContext | None" = None) -> int:
    """Set ``shape.tint`` on a FaceGen mesh's hair shapes. Returns how many."""
    if "facegeom" not in mesh_rel.replace("\\", "/").lower():
        return 0
    rgb = hair_color(mesh_rel, plugin_dirs, ctx)
    if rgb is None:
        return 0
    hits = 0
    for shape in _hair_shapes(model):
        shape.tint = rgb
        hits += 1
    return hits
