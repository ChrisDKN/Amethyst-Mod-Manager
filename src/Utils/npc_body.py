"""Which meshes make up an NPC's body, from the plugin records.

A FaceGen head is only the face. The rest of an actor is assembled by the
engine from the RACE's skin, so finding it is a record walk:

    NPC_ -> RNAM (race) -> RACE -> WNAM (skin ARMO)
                                -> ANAM (skeleton nifs, male and female)
    ARMO -> MODL (one ARMA per body part)
    ARMA -> MOD2 / MOD3 (male / female mesh), gated on the actor's race

An ARMA names its primary race in RNAM and every other race it also covers in
repeated MODL subrecords - the vanilla naked torso is filed under DefaultRace
and lists Nord, Imperial and the rest that way, so matching RNAM alone finds
nothing for most NPCs.

Sex comes from the NPC's ACBS flags (bit 0). Scope here is the NAKED body;
worn armour is a separate walk through WNAM/DOFT.
"""
from __future__ import annotations

import mmap
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path

from Utils.txst_lookup import _iter_subrecords, _record_payload

__all__ = ["BodyRecords", "BodyPart", "parse_body_records", "resolve_body",
           "load_order_records", "parse_cached"]

_ACBS_FEMALE = 0x00000001
_PLUGIN_EXTS = (".esp", ".esm", ".esl")

# Biped slots that the skin's own parts occupy: 32 body, 33 hands, 37 feet.
# A worn piece covering one of these replaces that naked part. Masking on the
# full slot list instead would let an amulet (slot 35, which the naked torso
# also lists) hide the entire body.
SLOT_BODY = 0x00000004
SLOT_HANDS = 0x00000008
SLOT_FEET = 0x00000080
CORE_SLOTS = SLOT_BODY | SLOT_HANDS | SLOT_FEET

# Slot 31, Hair. A hood or helmet claims it and the engine then hides the
# NPC's hair; without that the hair grows straight through the hat. A circlet
# does NOT claim it - it is worn ON visible hair - so this is the flag to key
# on rather than guessing from the item's name.
SLOT_HAIR = 0x00000002

# Plugin TX00..TX07 fields do not map monotonically onto a NIF texture set.
# This is Bethesda's TextureSet slot order (shared with Utils.txst_lookup).
_TX_TO_SET = {0: 0, 1: 1, 2: 5, 3: 2, 4: 3, 5: 4, 6: 6, 7: 7}


# Slots whose meshes are NOT skinned: the engine hangs them off a skeleton
# node instead. Without the node an unskinned piece keeps the position its own
# root gives it, which puts a shield on the floor by the actor's feet.
_SLOT_NODES = {
    0x00000200: "SHIELD",          # slot 39
}


@dataclass
class BodyPart:
    """One mesh the actor is built from."""

    rel: str                       # 'meshes/actors/.../femalebody_1.nif'
    editor_id: str = ""
    source: str = ""               # which plugin named it
    attach: str = ""               # skeleton node for unskinned meshes
    # ARMA NAM0/NAM1 can directly select the skin TXST for this actor. This is
    # how NPC replacers keep a custom head and body texture family together.
    textures: tuple[str, ...] = ()


@dataclass
class Arma:
    """An armature: the mesh for one body slot, per race and sex."""

    races: tuple = ()
    male: str = ""
    female: str = ""
    editor_id: str = ""
    slots: int = 0
    male_txst: int | None = None
    female_txst: int | None = None


@dataclass
class BodyRecords:
    """One plugin's actor-assembly records."""

    name: str = ""
    masters: list[str] = field(default_factory=list)
    # Staged mod the plugin came from; "" for the game's own data folder.
    mod: str = ""
    npc_race: dict = field(default_factory=dict)       # key -> raw race formid
    npc_female: dict = field(default_factory=dict)     # key -> bool
    npc_weight: dict = field(default_factory=dict)     # key -> 0.0 .. 1.0
    npc_outfit: dict = field(default_factory=dict)     # key -> raw OTFT formid
    npc_skin: dict = field(default_factory=dict)       # key -> raw WNAM ARMO
    # NPC_ QNAM texture-lighting colour, doubled/saturated as the game's skin
    # tint shader treats it (and as the FaceTint compositor treats its map).
    npc_skin_tint: dict = field(default_factory=dict)  # key -> RGB floats
    race_skin: dict = field(default_factory=dict)      # key -> raw ARMO formid
    race_skeleton: dict = field(default_factory=dict)  # key -> (male, female)
    armo_armatures: dict = field(default_factory=dict)  # key -> [raw ARMA ids]
    armo_slots: dict = field(default_factory=dict)     # key -> biped slot bits
    armo_name: dict = field(default_factory=dict)      # key -> editor id
    outfit_items: dict = field(default_factory=dict)   # key -> [raw item ids]
    lvli: dict = field(default_factory=dict)           # key -> [raw entry ids]
    arma: dict = field(default_factory=dict)           # key -> Arma
    txst: dict = field(default_factory=dict)           # key -> texture paths

    def owner(self, formid: int) -> str:
        """Which plugin a FormID belongs to, per this plugin's master list."""
        idx = (formid >> 24) & 0xFF
        return self.masters[idx] if idx < len(self.masters) else self.name

    def key(self, formid: int) -> tuple:
        return self.owner(formid), formid & 0xFFFFFF


def parse_body_records(path: Path) -> BodyRecords:
    """Collect NPC_/RACE/ARMO/ARMA assembly data from one plugin."""
    out = BodyRecords(name=Path(path).name.lower())
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
                if sig in (b"NPC_", b"RACE", b"ARMO", b"ARMA", b"OTFT",
                           b"LVLI", b"TXST"):
                    try:
                        _read_record(out, sig, formid,
                                     _record_payload(data, pos, size, flags))
                    except Exception:                    # noqa: BLE001
                        pass
                pos += size
    return out


def _read_record(out: BodyRecords, sig: bytes, formid: int, body) -> None:
    key = out.key(formid)
    if sig == b"NPC_":
        for ssig, sdata in _iter_subrecords(body, 0, len(body)):
            if ssig == b"ACBS" and len(sdata) >= 4:
                flags = struct.unpack_from("<I", sdata, 0)[0]
                out.npc_female[key] = bool(flags & _ACBS_FEMALE)
            elif ssig == b"RNAM" and len(sdata) >= 4:
                out.npc_race[key] = struct.unpack_from("<I", sdata, 0)[0]
            elif ssig == b"NAM7" and len(sdata) >= 4:
                raw_weight = struct.unpack_from("<f", sdata, 0)[0] / 100.0
                out.npc_weight[key] = min(
                    1.0, max(0.0, raw_weight))
            elif ssig == b"DOFT" and len(sdata) >= 4:
                out.npc_outfit[key] = struct.unpack_from("<I", sdata, 0)[0]
            elif ssig == b"WNAM" and len(sdata) >= 4:
                out.npc_skin[key] = struct.unpack_from("<I", sdata, 0)[0]
            elif ssig == b"QNAM" and len(sdata) >= 12:
                rgb = struct.unpack_from("<3f", sdata, 0)
                out.npc_skin_tint[key] = tuple(
                    min(1.0, max(0.0, channel * 2.0)) for channel in rgb)
        return
    if sig == b"TXST":
        paths = [""] * 8
        for ssig, sdata in _iter_subrecords(body, 0, len(body)):
            if len(ssig) != 4 or not ssig.startswith(b"TX"):
                continue
            try:
                field = int(ssig[2:4], 16)
            except ValueError:
                continue
            slot = _TX_TO_SET.get(field)
            if slot is not None:
                paths[slot] = _text(sdata)
        if any(paths):
            out.txst[key] = paths
        return
    if sig == b"LVLI":
        # LVLO is 12 bytes: level, form id, count. An outfit routinely names a
        # list rather than an item ("wear some farm clothes"), and a quarter of
        # vanilla NPCs would otherwise preview naked.
        entries = []
        for ssig, sdata in _iter_subrecords(body, 0, len(body)):
            if ssig == b"LVLO" and len(sdata) >= 12:
                entries.append(struct.unpack_from("<I", bytes(sdata), 4)[0])
        if entries:
            out.lvli[key] = entries
        return
    if sig == b"OTFT":
        items = []
        for ssig, sdata in _iter_subrecords(body, 0, len(body)):
            if ssig == b"INAM":
                raw = bytes(sdata)
                items += list(struct.unpack_from(
                    f"<{len(raw) // 4}I", raw, 0)) if len(raw) >= 4 else []
        if items:
            out.outfit_items[key] = items
        return
    if sig == b"RACE":
        skeleton = []
        for ssig, sdata in _iter_subrecords(body, 0, len(body)):
            if ssig == b"WNAM" and len(sdata) >= 4:
                out.race_skin[key] = struct.unpack_from("<I", sdata, 0)[0]
            elif ssig == b"ANAM":
                skeleton.append(_text(sdata))
        if skeleton:
            male = skeleton[0]
            female = skeleton[1] if len(skeleton) > 1 else male
            out.race_skeleton[key] = (male, female)
        return
    if sig == b"ARMO":
        refs = []
        for ssig, sdata in _iter_subrecords(body, 0, len(body)):
            if ssig == b"MODL" and len(sdata) == 4:
                refs.append(struct.unpack_from("<I", sdata, 0)[0])
            elif ssig in (b"BOD2", b"BODT") and len(sdata) >= 4:
                out.armo_slots[key] = struct.unpack_from("<I", sdata, 0)[0]
            elif ssig == b"EDID":
                out.armo_name[key] = _text(sdata)
        if refs:
            out.armo_armatures[key] = refs
        return
    # ARMA
    entry = Arma()
    races = []
    for ssig, sdata in _iter_subrecords(body, 0, len(body)):
        if ssig == b"EDID":
            entry.editor_id = _text(sdata)
        elif ssig == b"RNAM" and len(sdata) >= 4:
            races.append(struct.unpack_from("<I", sdata, 0)[0])
        elif ssig == b"MODL" and len(sdata) == 4:
            # On ARMA this is an ADDITIONAL race, not a model path.
            races.append(struct.unpack_from("<I", sdata, 0)[0])
        elif ssig == b"MOD2":
            entry.male = _text(sdata)
        elif ssig == b"MOD3":
            entry.female = _text(sdata)
        elif ssig == b"NAM0" and len(sdata) >= 4:
            entry.male_txst = struct.unpack_from("<I", sdata, 0)[0]
        elif ssig == b"NAM1" and len(sdata) >= 4:
            entry.female_txst = struct.unpack_from("<I", sdata, 0)[0]
        elif ssig in (b"BOD2", b"BODT") and len(sdata) >= 4:
            entry.slots = struct.unpack_from("<I", sdata, 0)[0]
    entry.races = tuple(races)
    if entry.male or entry.female:
        out.arma[key] = entry


def _text(payload) -> str:
    return bytes(payload).split(b"\0")[0].decode("cp1252", "replace")


def mesh_key(model_path: str) -> str:
    """A plugin model path as a data-relative asset key."""
    p = model_path.replace("\\", "/").lower().strip("/")
    if p.startswith("data/"):
        p = p[5:]
    if not p.startswith("meshes/"):
        p = "meshes/" + p
    return p


def _npc_key(plugin: str, formid: int, records):
    """The record key for a FaceGen path's (plugin, formid).

    A FaceGeom folder is named for the plugin that OWNS the NPC, but mods get
    it wrong: several ship Lucia - a HearthFires NPC - under a `Skyrim.esm/`
    folder. Trusting the folder finds no record at all and the actor renders
    as a bare head, so when the named plugin knows nothing about the FormID,
    fall back to whichever plugin does define it.
    """
    exact = (plugin.lower(), formid & 0xFFFFFF)
    if _first(records, "npc_race", exact) is not None:
        return exact
    low = formid & 0xFFFFFF
    for rec in records:
        for key in rec.npc_race:
            if key[1] == low:
                return key
    return exact


def resolve_body(plugin: str, formid: int, records, outfit: bool = True) -> dict:
    """The meshes and skeleton that make up one NPC.

    *records* is an ordered list of BodyRecords, highest priority first, so a
    mod's override of a race, armature or outfit wins the same way its meshes
    do. With *outfit* the NPC's DEFAULT outfit (DOFT) is worn and the skin
    parts it covers are dropped.

    Returns ``{'parts', 'worn', 'skeleton', 'female', 'weight', 'skin_tint'}``.
    """
    npc_key = _npc_key(plugin, formid, records)
    race_raw = _first(records, "npc_race", npc_key)
    female = bool(_first(records, "npc_female", npc_key))
    weight = _first(records, "npc_weight", npc_key)
    # NAM7 is present on normal NPC records. Preserve the old `_1.nif`
    # behaviour for unusual records that omit it instead of guessing a morph.
    weight = 1.0 if weight is None else weight
    skin_tint = _first(records, "npc_skin_tint", npc_key)
    if race_raw is None:
        return {"parts": [], "worn": [], "skeleton": [], "female": female,
                "weight": weight, "skin_tint": skin_tint}

    race_key = _resolve_key(records, "npc_race", npc_key, race_raw)
    # An NPC-specific WNAM skin overrides the race's generic naked skin. NPC
    # replacers use this to bind their face to a matching body texture set.
    skin_raw = _first(records, "npc_skin", npc_key)
    skin_table, skin_holder = "npc_skin", npc_key
    if skin_raw is None:
        skin_raw = _first(records, "race_skin", race_key)
        skin_table, skin_holder = "race_skin", race_key
    skeleton = []
    got = _first(records, "race_skeleton", race_key)
    if got:
        skeleton = [mesh_key(got[1] if female else got[0])]

    skin_armatures = []
    skin_textures: dict[int, tuple[str, ...]] = {}
    armo_key = None
    if skin_raw is not None:
        armo_key = _resolve_key(records, skin_table, skin_holder, skin_raw)
        skin_armatures = _armatures(records, armo_key, race_key)
        for entry, slots, arma_key in skin_armatures:
            textures = _textures_for(entry, female, records, arma_key)
            if not textures:
                continue
            for bit in (SLOT_BODY, SLOT_HANDS, SLOT_FEET):
                if slots & bit:
                    skin_textures[bit] = textures

    worn: list[BodyPart] = []
    covered = 0
    if outfit:
        worn, covered = _outfit_parts(
            records, npc_key, race_key, female, skin_textures)

    naked: list[BodyPart] = []
    if armo_key is not None:
        for entry, _slots, arma_key in skin_armatures:
            # A worn piece replaces the skin's part for the slot it fills, so
            # a cuirass hides the naked torso rather than clipping through it.
            if entry.slots & covered & CORE_SLOTS:
                continue
            model = _model_for(entry, female)
            if model:
                naked.append(BodyPart(rel=mesh_key(model),
                                      editor_id=entry.editor_id,
                                      textures=_textures_for(
                                          entry, female, records, armo_key)))
    return {"parts": _dedupe(naked + worn), "skeleton": skeleton,
            "female": female, "worn": _dedupe(worn),
            "weight": weight, "skin_tint": skin_tint,
            # A hood or helmet claims the hair slot and the engine hides the
            # hair; the FaceGen head still carries it, so it must be dropped
            # or it grows straight through the hat.
            "hide_hair": bool(covered & SLOT_HAIR)}


def _outfit_parts(records, npc_key, race_key, female, skin_textures=None):
    """(meshes, covered slot mask) for the NPC's default outfit."""
    raw = _first(records, "npc_outfit", npc_key)
    if raw is None:
        return [], 0
    otft_key = _resolve_key(records, "npc_outfit", npc_key, raw)
    # Decode each item against the plugin the ITEM LIST came from. The winning
    # OTFT and some other holder of the same key routinely have different
    # master lists, and resolving against the wrong one silently yields an
    # unrelated record.
    items, owner = _first_with_owner(records, "outfit_items", otft_key)
    items = items or []
    parts: list[BodyPart] = []
    covered = 0
    for item_raw in items:
        item_key = (owner.key(item_raw) if owner is not None
                    else _resolve_key(records, "outfit_items", otft_key,
                                      item_raw))
        for armo_key in _as_armour(records, item_key):
            slots = _first(records, "armo_slots", armo_key) or 0
            label = _first(records, "armo_name", armo_key) or ""
            attach = next((node for bit, node in _SLOT_NODES.items()
                           if slots & bit), "")
            worn_here = False
            for entry, _slots, arma_key in _armatures(
                    records, armo_key, race_key):
                model = _model_for(entry, female)
                if model:
                    worn_here = True
                    parts.append(BodyPart(rel=mesh_key(model),
                                          editor_id=entry.editor_id or label,
                                          attach=attach,
                                          textures=(
                                              _textures_for(
                                                  entry, female, records,
                                                  arma_key)
                                              or _skin_textures_for_slots(
                                                  skin_textures, entry.slots))))
            # Only a piece that actually PRODUCED a mesh hides the skin under
            # it. An armour with no armature for this race contributes nothing
            # to render, and counting its slots as covered strips the body
            # away and leaves the actor as a floating head - vanilla beggar
            # robes have no child armature, so every RS child lost their body.
            if worn_here:
                covered |= slots
    return parts, covered


def _as_armour(records, key, depth: int = 0) -> list:
    """An outfit entry as armour keys: itself, or a pick from a levelled list.

    A levelled list is a set of ALTERNATIVES for ONE slot ("some farm
    dress"), but an OUTFIT list bundles a whole wardrobe - Carlotta's has five
    entries covering earrings, shirt, skirt and shoes. Taking a single item
    from the whole list dressed her in earrings and nothing else; taking every
    item stacks five dresses on one body.

    So: one item PER BIPED SLOT. The first entry to fill a slot wins it, and
    entries whose slots are already taken are skipped. Lists nest, hence the
    depth cap.
    """
    if _first(records, "armo_armatures", key) is not None:
        return [key]
    if depth >= 4:
        return []
    entries, owner = _first_with_owner(records, "lvli", key)
    picked: list = []
    taken = 0
    for raw in entries or []:
        sub = (owner.key(raw) if owner is not None
               else _resolve_key(records, "lvli", key, raw))
        for armo_key in _as_armour(records, sub, depth + 1):
            slots = _first(records, "armo_slots", armo_key) or 0
            # A slotless entry cannot conflict, so it always comes along.
            if slots and slots & taken:
                continue
            taken |= slots
            picked.append(armo_key)
    return picked


def _armatures(records, armo_key, race_key):
    """Every armature of an ARMO that applies to this race."""
    out = []
    for raw in _first(records, "armo_armatures", armo_key) or []:
        arma_key = _resolve_key(records, "armo_armatures", armo_key, raw)
        entry = _first(records, "arma", arma_key)
        if entry is None or not _covers(entry, records, arma_key, race_key):
            continue
        out.append((entry, entry.slots, arma_key))
    return out


def _model_for(entry, female: bool) -> str:
    model = entry.female if female else entry.male
    return model or entry.male or entry.female


def _textures_for(entry, female: bool, records, arma_key) -> tuple[str, ...]:
    """Direct skin TXST selected by an ARMA's NAM0/NAM1, if present."""
    raw = entry.female_txst if female else entry.male_txst
    raw = raw if raw is not None else (
        entry.male_txst if female else entry.female_txst)
    if raw is None:
        return ()
    txst_key = _resolve_key(records, "arma", arma_key, raw)
    return tuple(_first(records, "txst", txst_key) or ())


def _skin_textures_for_slots(skin_textures, slots: int) -> tuple[str, ...]:
    """NPC skin texture inherited by exposed skin inside an outfit mesh."""
    skin_textures = skin_textures or {}
    for bit in (SLOT_BODY, SLOT_HANDS, SLOT_FEET):
        if slots & bit and skin_textures.get(bit):
            return skin_textures[bit]
    return ()


def _covers(entry: Arma, records, arma_key, race_key) -> bool:
    """Whether an armature applies to this race.

    An ARMA files its primary race in RNAM and every other race it covers in
    repeated MODL subrecords: the vanilla naked torso sits under DefaultRace
    and reaches Nord that way, so RNAM alone matches almost nobody.
    """
    owner = arma_key[0]
    for raw in entry.races:
        if _resolve_key(records, "arma", arma_key, raw) == race_key:
            return True
        if (owner, raw & 0xFFFFFF) == race_key:
            return True
    return False


def _first(records, table: str, key):
    for rec in records:
        got = getattr(rec, table).get(key)
        if got is not None:
            return got
    return None


def _resolve_key(records, table: str, holder_key, raw: int):
    """Map a raw FormID to (owning plugin, id) using its holder's masters.

    A FormID's high byte indexes the MASTER LIST OF THE PLUGIN IT WAS READ
    FROM, so it can only be decoded against that plugin. `_first` returns the
    winning plugin's value, and this must agree with it - resolving against
    some other plugin that merely also holds the key reads the index into the
    wrong master list and lands on an unrelated record. Carlotta's outfit item
    `5A000822` came from `gts patches - owl.esp`; decoding it against a
    different holder turned her clothes into an earring.
    """
    for rec in records:
        if holder_key in getattr(rec, table):
            return rec.key(raw)
    return (holder_key[0], raw & 0xFFFFFF)


def _first_with_owner(records, table: str, key):
    """(value, record) for the winning definition of *key*, or (None, None)."""
    for rec in records:
        got = getattr(rec, table).get(key)
        if got is not None:
            return got, rec
    return None, None


def _dedupe(parts: list[BodyPart]) -> list[BodyPart]:
    seen = set()
    out = []
    for p in parts:
        key = (p.rel, p.textures)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def load_order_records(profile_dir, staging, data_dir, cancel=None) -> list:
    """Every enabled plugin's assembly records, HIGHEST PRIORITY FIRST.

    A mod can replace an NPC's outfit with a plugin alone - "Serana Lustmord
    Armor Outfit Patch" ships no meshes of its own, it just points her DOFT at
    another mod's armour. Reading only the face's mod and its masters cannot
    see that, so the whole load order is read.

    The list is REVERSED against load order because the engine's rule for
    record overrides is last-loaded-wins, while `resolve_body` takes the first
    record it finds. Load order comes from loadorder.txt (which includes the
    implicit masters that plugins.txt omits), enabled state from plugins.txt.
    """
    order = _load_order(profile_dir)
    if not order:
        return []
    index = _plugin_index(staging, profile_dir, data_dir)
    records = []
    for name in reversed(order):
        if cancel and cancel():
            return []
        path = index.get(name.lower())
        if path is None:
            continue
        parsed = parse_cached(path)
        if parsed is not None:
            # Stamped on the shared cached object rather than copied: which
            # mod a plugin came from cannot change while the file does not.
            parsed.mod = _mod_of(path, staging)
            records.append(parsed)
    return records


def _mod_of(path: Path, staging) -> str:
    """The staged mod a plugin file sits in, or '' for the data folder."""
    if staging is None:
        return ""
    try:
        rel = Path(path).relative_to(Path(staging))
    except ValueError:
        return ""
    return rel.parts[0] if rel.parts else ""


def scope_records(records, mod: str) -> list:
    """Records as the copy from *mod* would see them, that mod winning.

    The viewer shows every mod's version of a face, not just the winner's, and
    a body has to follow the head that is on screen: rendering a VANILLA face
    on the winning replacer's tanned body puts a seam at the neck. Passing the
    selected copy's mod promotes its plugins; passing "" (a vanilla archive
    copy) drops the staged plugins entirely, leaving the game's own records.
    """
    if not records:
        return records
    if not mod:
        return [r for r in records if not r.mod]
    own = [r for r in records if r.mod == mod]
    if not own:
        return records
    return own + [r for r in records if r.mod != mod]


def _load_order(profile_dir) -> list[str]:
    """Enabled plugin names in load order, first loaded first."""
    if profile_dir is None:
        return []
    profile_dir = Path(profile_dir)
    from Utils.plugins import read_loadorder, read_plugins
    try:
        order = read_loadorder(profile_dir / "loadorder.txt")
    except Exception:                                    # noqa: BLE001
        order = []
    try:
        listed = read_plugins(profile_dir / "plugins.txt")
    except Exception:                                    # noqa: BLE001
        listed = []
    # plugins.txt carries the enabled flags but omits the implicit masters,
    # which are always loaded; loadorder.txt has the masters but no flags.
    # NEITHER is a superset: a freshly installed mod can be enabled in
    # plugins.txt while loadorder.txt has not caught up, and dropping it would
    # lose exactly the plugin that defines the NPC being looked at.
    disabled = {e.name.lower() for e in listed if not e.enabled}
    if not order:
        return [e.name for e in listed if e.enabled]
    out = [n for n in order if n.lower() not in disabled]
    seen = {n.lower() for n in out}
    # Enabled but unordered: append as last-loaded, which is where a plugin
    # missing from loadorder.txt would sit anyway.
    out += [e.name for e in listed
            if e.enabled and e.name.lower() not in seen]
    return out


def _plugin_index(staging, profile_dir, data_dir) -> dict:
    """{plugin name: winning file}, mods in modlist priority then the data dir.

    Built in one pass: probing each of a few hundred plugin names against a
    thousand mod folders individually is minutes of stat() calls.
    """
    index: dict[str, Path] = {}
    if data_dir is not None:
        try:
            for p in Path(data_dir).iterdir():
                if p.suffix.lower() in _PLUGIN_EXTS and p.is_file():
                    index.setdefault(p.name.lower(), p)
        except OSError:
            pass
    if staging is None:
        return index
    mods = _enabled_mods(profile_dir)
    # Reversed so the highest-priority mod overwrites the rest.
    for mod in reversed(mods):
        mod_dir = Path(staging) / mod
        try:
            entries = list(mod_dir.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.suffix.lower() in _PLUGIN_EXTS and p.is_file():
                index[p.name.lower()] = p
    return index


def _enabled_mods(profile_dir) -> list[str]:
    if profile_dir is None:
        return []
    try:
        from Utils.modlist import read_modlist
        return [e.name for e in read_modlist(Path(profile_dir) / "modlist.txt")
                if e.enabled and not e.is_separator]
    except Exception:                                    # noqa: BLE001
        return []


_cache: dict[str, tuple] = {}
_cache_lock = threading.Lock()
_parse_locks: dict[str, threading.Lock] = {}


def parse_cached(path: Path):
    """parse_body_records, memoised on (mtime, size), one parse per file."""
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    stamp = st.st_mtime_ns, st.st_size
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[:2] == stamp:
            return hit[2]
        build_lock = _parse_locks.setdefault(key, threading.Lock())

    # The NPC list pre-warms Skyrim.esm in the background. If somebody clicks
    # immediately, its mesh worker waits for that same parse instead of doing
    # a second 1+ second record walk in parallel.
    with build_lock:
        try:
            st = path.stat()
        except OSError:
            with _cache_lock:
                _parse_locks.pop(key, None)
            return None
        stamp = st.st_mtime_ns, st.st_size
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and hit[:2] == stamp:
                _parse_locks.pop(key, None)
                return hit[2]
        try:
            parsed = parse_body_records(path)
        except Exception:                                # noqa: BLE001
            parsed = BodyRecords(name=path.name.lower())
        with _cache_lock:
            _cache[key] = (*stamp, parsed)
            _parse_locks.pop(key, None)
        return parsed
