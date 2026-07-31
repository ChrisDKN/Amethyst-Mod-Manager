"""
dao_chargen.py
Deploy-time generation of Dragon Age: Origins chargenmorphcfg.xml.

DAO's character creator only shows custom heads/hairs/beards/colours/tattoos/
skins that are registered in:

    packages/core/override/chargenmorphcfg.xml

A mod can drop the resource files on disk, but unless they are listed here the
creator never displays them. Worse, only ONE chargenmorphcfg.xml can win in the
override folder — so when several chargen mods are installed, deploying each
mod's own copy last-wins and silently drops everyone else's entries.

This module rebuilds a single merged chargenmorphcfg.xml at deploy time:

  1. Start from the vanilla baseline (the stock creator resources).
  2. Merge every installed mod's chargenmorphcfg.xml fragment — authors curate
     these, so they are the most reliable source. Resources are unioned by
     block path and de-duplicated by name.
  3. (Fallback) auto-register loose chargen files (.mop/.mmh/.tnt/.dds) found in
     the override tree that no fragment already covers, classified the same way
     the game/toolset names them.

The merged file is written to packages/core/override/chargenmorphcfg.xml. On
restore the generated file is removed so the folder returns to vanilla.
"""

from __future__ import annotations

import os
from pathlib import Path
from xml.etree import ElementTree as ET

_CHARGEN_REL = Path("packages/core/override/chargenmorphcfg.xml")

_RACE_GENDER_TAGS = {
    "hm": "human_male", "hf": "human_female",
    "dm": "dwarf_male", "df": "dwarf_female",
    "em": "elf_male",   "ef": "elf_female",
}

# --- vanilla baseline tables (ported from the MO2 DAO plugin's DAOChargen) ---

_V_HEADS = (
    "_cps_p01.mop", "_cps_p02.mop", "_cps_p03.mop", "_cps_p04.mop",
    "_cps_p05.mop", "_cps_p06.mop", "_cps_p07.mop", "_cps_p08.mop",
    "_pcc_b01.mop",
)
_V_HAIRS_MALE = (
    ("_har_blda_0", "0234"), ("_har_ha1a_0", "1"), ("_har_ha2a_0", "1"),
    ("_har_ha3a_0", "1"), ("_har_hb1a_0", "1"), ("_har_hb2a_0", "1"),
    ("_har_hb3a_0", "1"), ("_har_hb4a_0", "1"), ("_har_hc1a_0", "1"),
    ("_har_hc2a_0", "1"), ("_har_hc3a_0", "1"), ("_har_hc4a_0", "1"),
    ("_har_hd1a_0", "1"), ("_har_hd2a_0", "1"), ("_har_hd3a_0", "1"),
    ("_har_hd4a_0", "0"),
)
_V_HAIRS_FEMALE = (
    ("_har_blda_0", "02"), ("_har_ha1a_0", "1"), ("_har_ha2a_0", "1"),
    ("_har_ha3a_0", "1"), ("_har_ha4a_0", "1"), ("_har_hb1a_0", "1"),
    ("_har_hb2a_0", "1"), ("_har_hb3a_0", "1"), ("_har_hb4a_0", "1"),
    ("_har_hc1a_0", "1"), ("_har_hc2a_0", "1"), ("_har_hc3a_0", "1"),
    ("_har_hc4a_0", "1"), ("_har_hd1a_0", "1"), ("_har_hd2a_0", "1"),
    ("_har_hd3a_0", "1"), ("_har_hd4a_0", "1"),
)
_V_BEARDS = (
    "", "_brd_b1a_0", "_brd_b2a_0", "_brd_b3a_0",
    "_brd_b4a_0", "_brd_b5a_0", "_brd_b6a_0",
)
_V_HAIR_COLORS = ("t3_har_wht", "t3_har_bln", "t3_har_dbl", "t3_har_org",
                  "t3_har_red", "t3_har_lbr", "t3_har_rbr", "t3_har_dbr",
                  "t3_har_blk")
_V_SKIN_COLORS = ("t1_skn_001", "t1_skn_002", "t1_skn_003", "t1_skn_004",
                  "t1_skn_006", "t1_skn_005", "t1_skn_007")
_V_EYES_COLORS = ("t3_eye_ice", "t3_eye_lbl", "t3_eye_dbl", "t3_eye_tea",
                  "t3_eye_grn", "t3_eye_hzl", "t3_eye_lbr", "t3_eye_amb",
                  "t3_eye_dbr", "t3_eye_gry", "t3_eye_blk")
_V_EYES_MAKEUP = ("", "t1_mue_bl1", "t1_mue_bl2", "t1_mue_bl3", "t1_mue_gn1",
                  "t1_mue_gn2", "t1_mue_gn3", "t1_mue_gr1", "t1_mue_gr2",
                  "t1_mue_gr3", "t1_mue_or1", "t1_mue_or2", "t1_mue_or3",
                  "t1_mue_pi1", "t1_mue_pi2", "t1_mue_pi3", "t1_mue_pu1",
                  "t1_mue_pu2", "t1_mue_pu3", "t1_mue_re1", "t1_mue_re2",
                  "t1_mue_re3", "t1_mue_ro1", "t1_mue_ro2", "t1_mue_ro3",
                  "t1_mue_te1", "t1_mue_te2", "t1_mue_te3", "t1_mue_ye1",
                  "t1_mue_ye2", "t1_mue_ye3")
_V_BLUSH_MAKEUP = ("", "t1_mub_br1", "t1_mub_br2", "t1_mub_br3", "t1_mub_or1",
                   "t1_mub_or2", "t1_mub_or3", "t1_mub_pi1", "t1_mub_pi2",
                   "t1_mub_pi3", "t1_mub_pu1", "t1_mub_pu2", "t1_mub_pu3",
                   "t1_mub_re1", "t1_mub_re2", "t1_mub_re3", "t1_mub_ro1",
                   "t1_mub_ro2", "t1_mub_ro3", "t1_mub_ta1", "t1_mub_ta2",
                   "t1_mub_ta3", "t1_mub_te1", "t1_mub_te2", "t1_mub_te3")
_V_LIP_MAKEUP = ("", "t1_mul_bk1", "t1_mul_bk2", "t1_mul_bk3", "t1_mul_br1",
                 "t1_mul_br2", "t1_mul_br3", "t1_mul_pi1", "t1_mul_pi2",
                 "t1_mul_pi3", "t1_mul_pu1", "t1_mul_pu2", "t1_mul_pu3",
                 "t1_mul_re1", "t1_mul_re2", "t1_mul_re3", "t1_mul_ro1",
                 "t1_mul_ro2", "t1_mul_ro3", "t1_mul_ta1", "t1_mul_ta2",
                 "t1_mul_ta3", "t1_mul_te1", "t1_mul_te2", "t1_mul_te3")
_V_BROW_STUBBLE = ("t1_stb_wht", "t1_stb_bln", "t1_stb_dbl", "t1_stb_org",
                   "t1_stb_red", "t1_stb_lbr", "t1_stb_rbr", "t1_stb_dbr",
                   "t1_stb_blk")
_V_TATTOO_COLORS = ("T1_TAT_BLK", "T1_TAT_GRY", "T1_TAT_BRN", "T1_TAT_DBR",
                    "T1_TAT_GRN", "T1_TAT_DGN", "T1_TAT_BLU", "T1_TAT_DBL",
                    "T1_TAT_PUR", "T1_TAT_DPU", "T1_TAT_RED", "T1_TAT_DRD",
                    "T1_TAT_ORG", "T1_TAT_YEL", "T1_TAT_PNK")
_V_TATTOOS = ("uh_tat_av1_0t", "uh_tat_av2_0t", "uh_tat_av3_0t",
              "uh_tat_da1_0t", "uh_tat_da2_0t", "uh_tat_da3_0t",
              "uh_tat_dw1_0t", "uh_tat_dw2_0t", "uh_tat_p01_0t")
_V_SKINS = ("uh_hed_fema_0d", "uh_hed_elfa_0d", "uh_hed_kida_0d",
            "uh_hed_masa_0d", "uh_hed_dwfa_0d", "uh_hed_quna_0d")

# flat colour/tattoo/skin blocks (tag → name list)
_V_FLAT = {
    "hair_colors": _V_HAIR_COLORS,
    "skin_colors": _V_SKIN_COLORS,
    "eyes_colors": _V_EYES_COLORS,
    "eyes_makeup_colors": _V_EYES_MAKEUP,
    "blush_makeup_colors": _V_BLUSH_MAKEUP,
    "lip_makeup_colors": _V_LIP_MAKEUP,
    "brow_stubble_colors": _V_BROW_STUBBLE,
    "crew_cut_colors": _V_BROW_STUBBLE,  # same table as brow stubble
    "tattoo_colors": _V_TATTOO_COLORS,
    "tattoos": _V_TATTOOS,
    "skins": _V_SKINS,
}


def _build_vanilla() -> ET.Element:
    """Construct the stock chargenmorphcfg morph_config tree."""
    root = ET.Element("morph_config")

    heads = ET.SubElement(root, "heads")
    for prefix, race in _RACE_GENDER_TAGS.items():
        block = ET.SubElement(heads, race)
        for item in _V_HEADS:
            ET.SubElement(block, "resource", {"name": f"{prefix}{item}"})

    hairs = ET.SubElement(root, "hairs")
    for prefix, race in _RACE_GENDER_TAGS.items():
        table = _V_HAIRS_FEMALE if prefix.endswith("f") else _V_HAIRS_MALE
        block = ET.SubElement(hairs, race)
        for name, cut in table:
            ET.SubElement(block, "resource", {"name": f"{prefix}{name}", "cut": cut})

    beards = ET.SubElement(root, "beards")
    for prefix, race in _RACE_GENDER_TAGS.items():
        if prefix.endswith("f") or prefix.startswith("e"):
            continue
        block = ET.SubElement(beards, race)
        for item in _V_BEARDS:
            ET.SubElement(block, "resource", {"name": f"{prefix.upper()}{item}"})

    for tag, names in _V_FLAT.items():
        block = ET.SubElement(root, tag)
        for name in names:
            ET.SubElement(block, "resource", {"name": name})

    return root


def _existing_names(block: ET.Element) -> set[str]:
    """Lowercased set of resource names directly under a block element."""
    return {
        (r.get("name") or "").casefold()
        for r in block.findall("resource")
    }


# Map a chargen block tag to the file extension(s) its resources need on disk.
# A resource is only registered if a matching file is present in the override —
# otherwise the creator shows an empty/invisible slot. Blocks whose "resources"
# are colour tints (.tnt) are matched by basename too.
_BLOCK_EXTS = {
    "heads": (".mop",),
    "hairs": (".mmh",),
    "beards": (".mmh",),
    "hair_colors": (".tnt",),
    "skin_colors": (".tnt",),
    "eyes_colors": (".tnt",),
    "eyes_makeup_colors": (".tnt",),
    "blush_makeup_colors": (".tnt",),
    "lip_makeup_colors": (".tnt",),
    "brow_stubble_colors": (".tnt",),
    "crew_cut_colors": (".tnt",),
    "tattoo_colors": (".tnt",),
    "tattoos": (".dds",),
    "skins": (".dds",),
}


def _resource_present(name: str, exts: tuple[str, ...],
                      present: set[str]) -> bool:
    """True if the resource's file (name + one of exts) exists in override.

    ``present`` is a set of lowercased basenames actually on disk. Head names
    already include the .mop extension; others are bare and need one appended.
    """
    n = name.casefold()
    if n in present:  # name already carries its extension (e.g. heads .mop)
        return True
    return any((n + e) in present for e in exts)


def _block_exts_for(block_tag: str) -> tuple[str, ...]:
    return _BLOCK_EXTS.get(block_tag, ())


def _merge_block(dst_parent: ET.Element, src_block: ET.Element,
                 present: "set[str] | None" = None,
                 block_exts: "tuple[str, ...] | None" = None) -> int:
    """Merge src_block into the matching child of dst_parent, recursively.

    Resources are unioned and de-duplicated by name. Nested race/gender groups
    (under heads/hairs/beards) are matched by tag and merged in turn. Returns
    the number of resources added.
    """
    tag = src_block.tag
    dst_block = dst_parent.find(tag)
    if dst_block is None:
        dst_block = ET.SubElement(dst_parent, tag)

    # Determine which extension(s) this block's resources need. Top-level blocks
    # carry the type; nested race/gender groups inherit from their parent.
    exts = _block_exts_for(tag) if tag in _BLOCK_EXTS else (block_exts or ())

    added = 0
    have = _existing_names(dst_block)
    for res in src_block.findall("resource"):
        name = (res.get("name") or "")
        if not name:
            continue
        if name.casefold() in have:
            continue
        # Drop phantom entries whose mesh/texture isn't actually installed —
        # otherwise the creator shows an invisible slot. When present is None
        # (no override scan), keep everything (vanilla-only baseline build).
        if present is not None and exts and not _resource_present(name, exts, present):
            continue
        dst_block.append(ET.Element("resource", dict(res.attrib)))
        have.add(name.casefold())
        added += 1

    # Recurse into nested groups (e.g. human_male under heads), inheriting exts.
    for child in src_block:
        if child.tag == "resource":
            continue
        added += _merge_block(dst_block, child, present=present, block_exts=exts)
    return added


def _merge_fragment(root: ET.Element, fragment_path: Path,
                    present: "set[str] | None", log_fn) -> int:
    try:
        frag = ET.parse(fragment_path).getroot()
    except ET.ParseError as exc:
        log_fn(f"  [DAO] skipping bad chargen fragment {fragment_path.name}: {exc}")
        return 0
    if frag.tag != "morph_config":
        return 0
    added = 0
    for block in frag:
        added += _merge_block(root, block, present=present)
    return added


def build_chargenmorph(data_root: Path, mod_staging: "Path | None" = None,
                       log_fn=None) -> int:
    """Build a merged chargenmorphcfg.xml in the override folder.

    data_root   — DAO data folder (the deployed override lives here)
    mod_staging — optional staging root; fragments are read from staging so the
                  merge sees every enabled mod even before the per-mod copies are
                  collapsed in the override. Falls back to scanning the deployed
                  override when not given.
    Returns the number of mod resources merged on top of vanilla.
    """
    _log = log_fn or (lambda _: None)
    root = _build_vanilla()

    # Scan the DEPLOYED override for files actually present. A fragment may
    # register hairs/heads/etc. that belong to mods the user doesn't have
    # installed (mega "compatibility" fragments do this). Registering a resource
    # with no mesh on disk yields an invisible slot in the creator — so we only
    # keep resources whose file is present here.
    deployed_override = data_root / _CHARGEN_REL.parent
    present: set[str] = set()
    if deployed_override.is_dir():
        for dirpath, _dn, fns in os.walk(deployed_override):
            for fn in fns:
                present.add(fn.casefold())
    _log(f"  [DAO] chargen: {len(present)} file(s) present in deployed override.")

    # Collect fragments from staging (one per mod, before the override copies
    # collapse) so every enabled mod's curated list is seen.
    fragments: list[Path] = []
    search_root = mod_staging if mod_staging and mod_staging.is_dir() else data_root
    # A Profile Group's staging is a farm of per-mod symlinks; os.walk never
    # descends into those, so expand them to their real targets first.
    from Utils.profile_groups import staging_walk_roots
    _seen_frag: set[str] = set()
    for base in staging_walk_roots(search_root):
        for dirpath, _dn, fns in os.walk(base):
            for fn in fns:
                if fn.casefold() != "chargenmorphcfg.xml":
                    continue
                frag = Path(dirpath) / fn
                if str(frag) in _seen_frag:
                    continue
                _seen_frag.add(str(frag))
                fragments.append(frag)

    merged = 0
    for frag in sorted(fragments):
        merged += _merge_fragment(root, frag, present, _log)

    out_path = data_root / _CHARGEN_REL
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="    ")  # pretty-print to match author convention
    xml_str = ET.tostring(root, encoding="unicode")
    out_path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n' + xml_str + "\n",
        encoding="utf-8",
    )
    _log(f"  [DAO] chargenmorphcfg.xml: merged {merged} resource(s) "
         f"from {len(fragments)} fragment(s) onto vanilla.")
    return merged


def reset_chargenmorph(data_root: Path, log_fn=None) -> None:
    """Remove the generated chargenmorphcfg.xml on restore."""
    _log = log_fn or (lambda _: None)
    out_path = data_root / _CHARGEN_REL
    try:
        if out_path.exists():
            out_path.unlink()
            _log("  [DAO] removed generated chargenmorphcfg.xml.")
    except OSError as exc:
        _log(f"  [DAO] could not remove chargenmorphcfg.xml: {exc}")
