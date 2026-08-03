"""Texture-set overrides from plugin records (TXST / alternate textures).

Armour mods routinely bake dead texture paths into their meshes and swap in
the real ones at runtime via TXST records referenced from a model's
alternate-textures list (MODS/MO2S/... subrecords). A NIF-only preview shows
such meshes untextured, so this module recovers the mapping:

    model path -> [(3D name, 3D index, TXST formid)]   per plugin
    TXST formid -> texture paths (BSShaderTextureSet slot order)

Scope: the plugins shipped alongside the mesh (small files). Vanilla masters
are skipped by a size cap, so overrides that live in Skyrim.esm stay
unresolved — rare for the armour-pack case this exists for.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

_COMPRESSED = 0x00040000
# Data-dir plugins above this size are skipped (vanilla ESMs).
MAX_PLUGIN_BYTES = 32 * 1024 * 1024

# TXST slot -> BSShaderTextureSet slot. TX02 (env mask) and TX03 (glow) are
# swapped relative to their numbering.
_TX_TO_SET = {0: 0, 1: 1, 2: 5, 3: 2, 4: 3, 5: 4, 6: 6, 7: 7}

_MODEL_SUBS = {b"MODL": b"MODS", b"MOD2": b"MO2S", b"MOD3": b"MO3S",
               b"MOD4": b"MO4S", b"MOD5": b"MO5S"}
_ALT_SUBS = frozenset(_MODEL_SUBS.values())


@dataclass
class PluginTextures:
    """One plugin's texture-set records and alternate-texture lists."""

    name: str = ""
    masters: list[str] = field(default_factory=list)
    # formid low 24 bits -> texture paths in BSShaderTextureSet slot order.
    txst: dict[int, list[str]] = field(default_factory=dict)
    # normalised model path -> [(3D name, 3D index, raw formid)]
    alt: dict[str, list[tuple[str, int, int]]] = field(default_factory=dict)


def norm_model_path(path: str) -> str:
    """Plugin model paths vs mesh paths: lowercase, /, no meshes/ prefix."""
    p = path.replace("\\", "/").lower().strip("/")
    if p.startswith("data/"):
        p = p[5:]
    if p.startswith("meshes/"):
        p = p[7:]
    return p


def _iter_subrecords(data, pos, end):
    """Yield (sig, payload); handles XXXX extended-size prefixes."""
    big = None
    while pos + 6 <= end:
        sig = bytes(data[pos:pos + 4])
        size = struct.unpack_from("<H", data, pos + 4)[0]
        pos += 6
        if sig == b"XXXX" and size == 4:
            big = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            continue
        if big is not None:
            size, big = big, None
        if pos + size > end:
            return
        yield sig, data[pos:pos + size]
        pos += size


def _parse_alt_list(blob) -> list[tuple[str, int, int]]:
    """Decode MODS/MO*S: count, then (nameLen, name, TXST formid, 3D index)."""
    out = []
    try:
        count = struct.unpack_from("<I", blob, 0)[0]
        pos = 4
        for _ in range(min(count, 4096)):
            nlen = struct.unpack_from("<I", blob, pos)[0]
            if nlen > 1024:
                return []
            name = bytes(blob[pos + 4:pos + 4 + nlen]).decode(
                "cp1252", "replace")
            fid, idx = struct.unpack_from("<II", blob, pos + 4 + nlen)
            out.append((name, idx, fid))
            pos += 12 + nlen
    except struct.error:
        return []
    return out


def _record_payload(data, pos, size, flags):
    if not flags & _COMPRESSED:
        return data[pos:pos + size]
    try:
        want = struct.unpack_from("<I", data, pos)[0]
        if want > 100_000_000:
            return b""
        return zlib.decompress(data[pos + 4:pos + size], bufsize=want)
    except (zlib.error, struct.error):
        return b""


def parse_plugin(path: Path) -> PluginTextures:
    """Extract TXST records and alternate-texture lists from one plugin."""
    out = PluginTextures(name=path.name.lower())
    data = path.read_bytes()
    if len(data) < 24 or data[:4] != b"TES4":
        return out
    tes4_size = struct.unpack_from("<I", data, 4)[0]
    for sig, payload in _iter_subrecords(data, 24, min(24 + tes4_size,
                                                       len(data))):
        if sig == b"MAST":
            out.masters.append(bytes(payload).split(b"\0")[0]
                               .decode("cp1252", "replace").lower())
    pos = 24 + tes4_size
    end = len(data)
    while pos + 24 <= end:
        sig = bytes(data[pos:pos + 4])
        if sig == b"GRUP":
            # Enter the group: records follow its 24-byte header.
            pos += 24
            continue
        size, flags, formid = struct.unpack_from("<III", data, pos + 4)
        pos += 24
        if pos + size > end:
            break
        if sig == b"TXST":
            paths = [""] * 9
            payload = _record_payload(data, pos, size, flags)
            for ssig, sdata in _iter_subrecords(payload, 0, len(payload)):
                if ssig[:2] == b"TX" and ssig[2:].isdigit():
                    slot = _TX_TO_SET.get(int(ssig[2:]))
                    if slot is not None:
                        paths[slot] = bytes(sdata).split(b"\0")[0].decode(
                            "cp1252", "replace")
            if any(paths):
                out.txst[formid & 0xFFFFFF] = paths
        else:
            # Any record type may pair model paths with alternate textures.
            payload = _record_payload(data, pos, size, flags)
            model = None
            for ssig, sdata in _iter_subrecords(payload, 0, len(payload)):
                if ssig in _MODEL_SUBS:
                    model = (ssig, bytes(sdata).split(b"\0")[0].decode(
                        "cp1252", "replace"))
                elif ssig in _ALT_SUBS and model is not None:
                    if _MODEL_SUBS[model[0]] == ssig:
                        entries = _parse_alt_list(sdata)
                        if entries:
                            out.alt.setdefault(
                                norm_model_path(model[1]), []).extend(entries)
                    model = None
        pos += size
    return out


# path -> (mtime, size, PluginTextures); plugins re-parse when they change.
_cache: dict[str, tuple[float, int, PluginTextures]] = {}


def _cached_parse(path: Path) -> PluginTextures | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    hit = _cache.get(key)
    if hit is not None and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        parsed = parse_plugin(path)
    except Exception:                                    # noqa: BLE001
        parsed = PluginTextures(name=path.name.lower())
    _cache[key] = (st.st_mtime, st.st_size, parsed)
    return parsed


def alt_textures_for_mesh(mesh_rel: str,
                          plugin_dirs: list[Path]) -> dict[object, list[str]]:
    """Texture-set overrides for one mesh: {3D name lower / 3D index: paths}.

    *mesh_rel* is the mesh path relative to the data folder (or meshes/).
    Scans top-level plugins of each dir in order; first plugin naming the
    mesh wins. Big data-dir masters are skipped via MAX_PLUGIN_BYTES.
    """
    rel = norm_model_path(mesh_rel)
    if not rel:
        return {}
    plugins: list[PluginTextures] = []
    seen: set[str] = set()
    for d in plugin_dirs:
        try:
            files = sorted(p for p in Path(d).iterdir()
                           if p.suffix.lower() in (".esp", ".esm", ".esl"))
        except OSError:
            continue
        for p in files:
            if p.name.lower() in seen:
                continue
            seen.add(p.name.lower())
            try:
                if p.stat().st_size > MAX_PLUGIN_BYTES:
                    continue
            except OSError:
                continue
            parsed = _cached_parse(p)
            if parsed is not None:
                plugins.append(parsed)
    by_name = {p.name: p for p in plugins}
    for plug in plugins:
        entries = plug.alt.get(rel)
        if not entries:
            continue
        out: dict[object, list[str]] = {}
        for name, idx, fid in entries:
            src = (fid >> 24) & 0xFF
            if src >= len(plug.masters):
                owner = plug                     # the plugin's own record
            else:
                owner = by_name.get(plug.masters[src])
                if owner is None:                # unparsed master (vanilla)
                    continue
            paths = owner.txst.get(fid & 0xFFFFFF)
            if not paths:
                continue
            if name:
                out[name.lower()] = paths
            out[idx] = paths
        if out:
            return out
    return {}


def apply_alt_textures(model, mesh_rel: str, plugin_dirs) -> int:
    """Swap plugin-overridden texture sets into *model*'s shapes in place.

    The game replaces the mesh's whole baked set, so we do too. Returns how
    many shapes were overridden.
    """
    over = alt_textures_for_mesh(mesh_rel, list(plugin_dirs or []))
    if not over:
        return 0
    hits = 0
    for i, shape in enumerate(model.shapes):
        paths = over.get(shape.name.lower()) or over.get(i)
        if paths:
            shape.textures = list(paths)
            hits += 1
    return hits
