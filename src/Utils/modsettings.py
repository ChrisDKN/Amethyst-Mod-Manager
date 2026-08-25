"""
modsettings.py
Build and write modsettings.lsx for Baldur's Gate 3.

Workflow:
  1. For each enabled mod, open its .pak file(s) and extract meta.lsx.
  2. Parse the XML to collect UUID, Name, Folder, Version64, and dependencies.
  3. Topologically sort mods so dependencies always appear before dependents.
  4. Write the Patch 7+ modsettings.lsx (Mods node only, no ModOrder).

The campaign entry is always written first: GustavX by default, or an
installed Adventure (custom campaign) mod in its place.  Pure override paks
(only touching base-game module folders) are left out of the load order.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from Utils.modlist import ModEntry, read_modlist
from Utils.pak_reader import extract_meta_lsx, read_pak_info
from Utils.app_log import app_log, safe_log as _safe_log
from Utils.filegraph_constants import OVERWRITE_NAME as _OVERWRITE_NAME

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# UUIDs for base-game / engine modules that should be ignored as dependencies.
# Patch / DLC modules added in later game updates are discovered dynamically
# by scanning the game's Data/ directory (see scan_game_data_uuids).
_SYSTEM_UUIDS: frozenset[str] = frozenset({
    # Core engine / story modules
    "28ac9ce2-2aba-8cda-b3b5-6e922f71b6b8",   # GustavDev
    "991c9c7a-fb80-40cb-8f0d-b92d4e80e9b1",   # Gustav
    "cb555efe-2d9e-131f-8195-a89329d218ea",    # GustavX
    "ed539163-bb70-431b-96a7-f5b2eda5376b",   # Shared
    "3d0c5ff8-c95d-c907-ff3e-34b204f1c630",   # SharedDev
    "b77b6210-ac50-4cb1-a3d5-5702fb9c744c",   # Honour
    "767d0062-d82c-279c-e16b-dfee7fe94cdd",   # HonourX
    # DLC dice sets
    "e842840a-2449-588c-b0c4-22122cfce31b",   # DiceSet_01
    "b176a0ac-d79f-ed9d-5a87-5c2c80874e10",   # DiceSet_02
    "e0a4d990-7b9b-8fa9-d7c6-04017c6cf5b1",   # DiceSet_03
    "77a2155f-4b35-4f0c-e7ff-4338f91426a4",   # DiceSet_04
    "6efc8f44-cc2a-0273-d4b1-681d3faa411b",   # DiceSet_05
    "ee4989eb-aab8-968f-8674-812ea2f4bfd7",   # DiceSet_06
    "bf19bab4-4908-ef39-9065-ced469c0f877",   # DiceSet_07
    # UI / feature modules
    "630daa32-70f8-3da5-41b9-154fe8410236",   # MainUI
    "ee5a55ff-eb38-0b27-c5b0-f358dc306d34",   # ModBrowser
    "55ef175c-59e3-b44b-3fb2-8f86acc5d550",   # PhotoMode
    "e1ce736b-52e6-e713-e9e7-e6abbb15a198",   # CrossplayUI
    # Engine / patch 6 builtin
    "9dff4c3b-fda7-43de-a763-ce1383039999",   # Engine
})

# Folder names of base-game modules inside paks (Mods/<Folder>/ or
# Public/<Folder>/).  A pak that only writes into these folders is a pure
# override - the game loads it automatically, it needs no load-order entry.
# Mirrors BG3 Mod Manager's IgnoredMods.json.
_BUILTIN_FOLDERS: frozenset[str] = frozenset({
    "gustav", "gustavdev", "gustavx", "shared", "shareddev", "engine", "game",
    "diceset_01", "diceset_02", "diceset_03", "diceset_04", "diceset_05",
    "diceset_06", "diceset_07", "honour", "honourx",
    "modbrowser", "mainui", "crossplayui", "photomode",
})

# Builtin paths that don't count as "overriding base-game data" (BG3MM's
# IgnoreBuiltinPath) - custom UI mods legitimately add new files here.
_BUILTIN_IGNORE_PATH = "game/gui/assets"

_MOD_FOLDER_PATH_RE = re.compile(r"^(mods|public)/([^/]+)/(.+)$")

# Campaign / adventure base-game entry - varies by patch.  Values taken from
# the BG3MM tag that shipped for each patch:
#   Patch 8  → GustavX (BG3MM master)
#   Patch 7  → GustavDev (BG3MM 1.0.11.1 - MAIN_CAMPAIGN_UUID)
#   Patch 6  → Gustav (BG3MM 1.0.10.0)
# Version64 for patch 6/7 is a 1.0.0.0 placeholder - the engine doesn't
# reject "low" campaign versions, so we don't need to read it from the
# installed Gustav.pak.
_GUSTAV_X = {
    "Folder":        "GustavX",
    "MD5":           "ef3fcba3f3684b3088ad1f9874d4957c",
    "Name":          "GustavX",
    "PublishHandle":  "0",
    "UUID":          "cb555efe-2d9e-131f-8195-a89329d218ea",
    "Version64":     "145241946983300916",
}

_GUSTAV_DEV = {
    "Folder":        "GustavDev",
    "MD5":           "",
    "Name":          "GustavDev",
    "PublishHandle": "0",
    "UUID":          "28ac9ce2-2aba-8cda-b3b5-6e922f71b6b8",
    "Version64":     "36028797018963968",
}

_GUSTAV_CLASSIC = {
    "Folder":        "Gustav",
    "MD5":           "",
    "Name":          "Gustav",
    "PublishHandle": "0",
    "UUID":          "991c9c7a-fb80-40cb-8f0d-b92d4e80e9b1",
    "Version64":     "36028797018963968",
}

# Patch 8 modsettings.lsx template - LSX version stamp 4/8/0/100.
_MODSETTINGS_HEADER_P8 = """\
<?xml version="1.0" encoding="UTF-8"?>
<save>
  <version major="4" minor="8" revision="0" build="100"/>
  <region id="ModuleSettings">
    <node id="root">
      <children>
        <node id="Mods">
          <children>
"""

# Patch 7 modsettings.lsx template - LSX version stamp 4/7/1/3 (from
# BG3MM 1.0.11.1, the last release targeting patch 7).  Same structure
# as patch 8 otherwise (no ModOrder block).
_MODSETTINGS_HEADER_P7 = """\
<?xml version="1.0" encoding="UTF-8"?>
<save>
  <version major="4" minor="7" revision="1" build="3"/>
  <region id="ModuleSettings">
    <node id="root">
      <children>
        <node id="Mods">
          <children>
"""

_MODSETTINGS_FOOTER_P7 = """\
          </children>
        </node>
      </children>
    </node>
  </region>
</save>
"""

# Patch 6 modsettings.lsx template - includes a ModOrder node and
# uses the LSX version stamp shipped by BG3MM 1.0.10.0 (the last release
# targeting patch 6): major=4, minor=0, revision=9, build=331.
_MODSETTINGS_HEADER_P6 = """\
<?xml version="1.0" encoding="UTF-8"?>
<save>
  <version major="4" minor="0" revision="9" build="331"/>
  <region id="ModuleSettings">
    <node id="root">
      <children>
        <node id="ModOrder">
          <children>
{MOD_ORDER}\
          </children>
        </node>
        <node id="Mods">
          <children>
"""

_MODSETTINGS_FOOTER_P6 = """\
          </children>
        </node>
      </children>
    </node>
  </region>
</save>
"""

# Patch 7/8 mod entry - uses PublishHandle + Version64
_MOD_ENTRY_TEMPLATE_P7 = """\
            <node id="ModuleShortDesc">
              <attribute id="Folder" type="LSString" value="{Folder}"/>
              <attribute id="MD5" type="LSString" value="{MD5}"/>
              <attribute id="Name" type="LSString" value="{Name}"/>
              <attribute id="PublishHandle" type="uint64" value="{PublishHandle}"/>
              <attribute id="UUID" type="guid" value="{UUID}"/>
              <attribute id="Version64" type="int64" value="{Version64}"/>
            </node>
"""

# Patch 6 mod entry - no PublishHandle; UUID is FixedString instead of guid.
# Based verbatim on BG3MM 1.0.10.0's XML_MODULE_SHORT_DESC (the last BG3MM
# release that targeted patch 6).
_MOD_ENTRY_TEMPLATE_P6 = """\
            <node id="ModuleShortDesc">
              <attribute id="Folder" value="{Folder}" type="LSString"/>
              <attribute id="MD5" value="{MD5}" type="LSString"/>
              <attribute id="Name" value="{Name}" type="LSString"/>
              <attribute id="UUID" value="{UUID}" type="FixedString"/>
              <attribute id="Version64" value="{Version64}" type="int64"/>
            </node>
"""

# Patch 6 ModOrder entry (just UUID references, in load order)
_MOD_ORDER_ENTRY_P6 = """\
            <node id="Module">
              <attribute id="UUID" value="{UUID}" type="FixedString"/>
            </node>
"""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class BG3ModInfo:
    """Metadata extracted from a mod's meta.lsx inside its .pak file."""
    uuid: str
    name: str
    folder: str
    version64: str
    md5: str = ""
    publish_handle: str = "0"
    # Legacy "Version" attribute - same 64-bit packed layout as Version64,
    # apart from two historic 1.0.0.0 encodings (see _version64_or_default).
    version: str = ""
    # ModuleInfo "Type" attribute - "Adventure" marks a custom campaign that
    # should replace GustavX as the top modsettings entry.
    mod_type: str = ""
    # UUIDs of mods this mod depends on
    dependencies: list[str] = field(default_factory=list)
    # The mod-list name (staging folder name) this came from
    source_mod: str = ""
    # True when the pak only overrides base-game module folders - the game
    # loads it automatically; it must not get a load-order entry.
    is_override_only: bool = False
    # True when the pak ships a ScriptExtender/Config.json with RequiredVersion.
    requires_script_extender: bool = False


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _attr_value(node: ET.Element, attr_id: str) -> str:
    """Find <attribute id="attr_id" ... value="X"/> and return X, or ""."""
    for attr in node.iter("attribute"):
        if attr.get("id") == attr_id:
            return attr.get("value", "")
    return ""


# Bare ampersand: "&" not already starting a character/entity reference.
_BARE_AMP_RE = re.compile(r"&(?!amp;|apos;|quot;|gt;|lt;|#\d+;|#x[0-9A-Fa-f]+;)")
_ATTR_VALUE_RE = re.compile(r'value="([^"]*)"')


def _repair_meta_xml(xml_text: str) -> str:
    """Escape raw &/</> in attribute values so malformed meta.lsx still parses.

    Mod authors routinely ship meta.lsx with unescaped characters in Name or
    Description.  BG3 Mod Manager pre-escapes attribute values before parsing;
    we do the equivalent as a retry path.
    """
    xml_text = _BARE_AMP_RE.sub("&amp;", xml_text)
    return _ATTR_VALUE_RE.sub(
        lambda m: 'value="' + m.group(1).replace("<", "&lt;").replace(">", "&gt;") + '"',
        xml_text,
    )


def parse_meta_lsx(xml_text: str) -> BG3ModInfo | None:
    """Parse a meta.lsx XML string and return a BG3ModInfo, or None on failure."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        try:
            root = ET.fromstring(_repair_meta_xml(xml_text))
        except ET.ParseError:
            return None

    # Find the ModuleInfo node
    module_info = None
    for node in root.iter("node"):
        if node.get("id") == "ModuleInfo":
            module_info = node
            break
    if module_info is None:
        return None

    uuid = _attr_value(module_info, "UUID")
    name = _attr_value(module_info, "Name")
    folder = _attr_value(module_info, "Folder")
    version64 = _attr_value(module_info, "Version64")
    version32 = _attr_value(module_info, "Version")
    md5 = _attr_value(module_info, "MD5")
    publish_handle = _attr_value(module_info, "PublishHandle") or "0"
    mod_type = _attr_value(module_info, "Type")

    if not uuid:
        return None

    # Parse dependencies
    deps: list[str] = []
    for node in root.iter("node"):
        if node.get("id") == "Dependencies":
            for child in node.iter("node"):
                if child.get("id") == "ModuleShortDesc":
                    dep_uuid = _attr_value(child, "UUID")
                    if dep_uuid and dep_uuid not in _SYSTEM_UUIDS:
                        deps.append(dep_uuid)
            break

    return BG3ModInfo(
        uuid=uuid,
        name=name,
        folder=folder,
        version64=version64,
        md5=md5,
        publish_handle=publish_handle,
        version=version32,
        mod_type=mod_type,
        dependencies=deps,
    )


def _classify_pak_files(file_names: list[str]) -> tuple[bool, bool]:
    """Return (overrides_builtin, has_own_data) for a pak's file list."""
    overrides_builtin = False
    has_own_data = False
    for name in file_names:
        nl = name.replace("\\", "/").lower()
        m = _MOD_FOLDER_PATH_RE.match(nl)
        if m is None:
            continue
        if nl.endswith("/meta.lsx"):
            continue
        if m.group(2) in _BUILTIN_FOLDERS:
            if _BUILTIN_IGNORE_PATH not in nl:
                overrides_builtin = True
        else:
            has_own_data = True
    return overrides_builtin, has_own_data


def _se_config_requires_extender(se_config: str | None) -> bool:
    """True when a pak's ScriptExtender/Config.json declares RequiredVersion."""
    if not se_config:
        return False
    try:
        cfg = json.loads(se_config.lstrip("\ufeff"))
    except (ValueError, TypeError):
        return False
    rv = cfg.get("RequiredVersion", -1) if isinstance(cfg, dict) else -1
    return isinstance(rv, (int, float)) and rv > -1


# ---------------------------------------------------------------------------
# .pak scanning
# ---------------------------------------------------------------------------

def scan_mod_paks(
    staging_root: Path,
    enabled_mods: list[ModEntry],
    no_metadata: list[str] | None = None,
    excluded: dict[str, set[str]] | None = None,
    extra_dirs: dict[str, Path] | None = None,
) -> dict[str, BG3ModInfo]:
    """Scan .pak files for all enabled mods and return {uuid: BG3ModInfo}.

    Each mod's staging folder may contain one or more .pak files.  We extract
    meta.lsx from each and collect the metadata.  If a mod folder contains
    multiple .pak files, each one that has a meta.lsx is recorded.

    *no_metadata* - optional list; enabled mods that contain at least one .pak
    but yielded no usable meta.lsx are appended (with the reason) for the
    caller to surface as a diagnostic.

    *excluded* - optional {mod_name: {rel_path_lower, ...}} of files the user
    excluded in the Mod Files tab; those paks are not deployed, so they must
    not get a modsettings entry either.

    *extra_dirs* - {mod_name: folder} for pseudo-mods outside *staging_root*
    (the overwrite folder). Duplicate UUIDs: last scanned wins, and callers pass
    *enabled_mods* lowest-priority-first, so that matches what deploy picks.
    """
    by_uuid: dict[str, BG3ModInfo] = {}
    _excluded = excluded or {}
    _extra = extra_dirs or {}

    for entry in enabled_mods:
        mod_dir = _extra.get(entry.name) or (staging_root / entry.name)
        if not mod_dir.is_dir():
            continue
        paks = list(mod_dir.rglob("*.pak"))
        _mod_excl = _excluded.get(entry.name)
        if _mod_excl:
            paks = [p for p in paks
                    if p.relative_to(mod_dir).as_posix().lower() not in _mod_excl]
        got_meta = False
        for pak in paks:
            try:
                pak_info = read_pak_info(pak)
            except Exception as exc:
                app_log(f"Failed to read {pak}: {exc}")
                continue
            if pak_info.meta_xml is None:
                continue
            info = parse_meta_lsx(pak_info.meta_xml)
            if info is None:
                continue
            if info.uuid in _SYSTEM_UUIDS:
                # Override of a base-game module (e.g. a party-size mod's
                # Mods/GustavX/meta.lsx copy, or a vanilla pak replacement) -
                # never emit a base module as a user mod entry.
                got_meta = True
                continue
            overrides_builtin, has_own_data = _classify_pak_files(pak_info.file_names)
            info.is_override_only = overrides_builtin and not has_own_data
            info.requires_script_extender = _se_config_requires_extender(pak_info.se_config)
            info.source_mod = entry.name
            by_uuid[info.uuid] = info
            got_meta = True

        if paks and not got_meta and no_metadata is not None:
            reason = "no meta.lsx in any .pak (likely a loose-file / data mod)"
            no_metadata.append(f"{entry.name} ({len(paks)} pak(s): {reason})")

    return by_uuid


# ---------------------------------------------------------------------------
# Base-game module discovery
# ---------------------------------------------------------------------------

def scan_game_data_uuids(game_data_path: Path) -> set[str]:
    """Scan .pak files in the game's Data/ directory and return their UUIDs.

    BG3 ships base-game, DLC, and patch modules as .pak files under
    ``<game_root>/Data/``.  Mods that override Gustav often inherit
    dependencies on these modules; without knowing their UUIDs the
    dependency checker would emit false "not installed" warnings.

    Only the file-list header of each .pak is read (a few KB regardless
    of total file size), so this is fast even for multi-GB archives.
    """
    uuids: set[str] = set()
    if not game_data_path.is_dir():
        return uuids
    for pak in game_data_path.glob("*.pak"):
        try:
            xml_text = extract_meta_lsx(pak)
        except Exception:
            continue
        if xml_text is None:
            continue
        info = parse_meta_lsx(xml_text)
        if info is not None and info.uuid:
            uuids.add(info.uuid)
    return uuids


# ---------------------------------------------------------------------------
# Dependency-aware ordering
# ---------------------------------------------------------------------------

def resolve_load_order(
    enabled_mods: list[ModEntry],
    mod_infos: dict[str, BG3ModInfo],
) -> list[BG3ModInfo]:
    """Return BG3ModInfo entries in dependency-correct load order.

    The user's modlist order is respected as much as possible - the resolver
    only reorders when a dependency must be loaded before a dependent.

    Algorithm (mirrors BG3 Mod Manager):
      For each mod in the user's order, recursively insert its dependencies
      first, then insert the mod itself.  A visited set prevents duplicates.
    """
    # Build a lookup: source_mod name → list of BG3ModInfo.
    # A single staging folder can contain many .pak files (e.g. load-order
    # divider packs with 30+ paks), each with its own UUID/meta.lsx - they
    # all need to be emitted into modsettings.lsx.
    by_source: dict[str, list[BG3ModInfo]] = {}
    for info in mod_infos.values():
        if info.source_mod:
            by_source.setdefault(info.source_mod, []).append(info)

    added: set[str] = set()
    result: list[BG3ModInfo] = []

    def _insert(info: BG3ModInfo) -> None:
        if info.uuid in added:
            return
        # Recursively insert dependencies first
        for dep_uuid in info.dependencies:
            dep = mod_infos.get(dep_uuid)
            if dep is not None:
                _insert(dep)
        added.add(info.uuid)
        result.append(info)

    # Walk mods in the user's listed order (modlist.txt order)
    for entry in enabled_mods:
        for info in by_source.get(entry.name, ()):
            _insert(info)

    return result


# ---------------------------------------------------------------------------
# modsettings.lsx generation
# ---------------------------------------------------------------------------

def _xml_escape(value: str) -> str:
    """Escape &, <, >, and " for safe insertion into LSX attribute values."""
    return (
        value.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
    )


def _format_entry_p7(info: dict[str, str]) -> str:
    escaped = {k: _xml_escape(v) for k, v in info.items()}
    return _MOD_ENTRY_TEMPLATE_P7.format(**escaped)


def _format_entry_p6(info: dict[str, str]) -> str:
    escaped = {k: _xml_escape(v) for k, v in info.items()}
    return _MOD_ENTRY_TEMPLATE_P6.format(**escaped)


def _campaign_entry(patch_version: int) -> dict[str, str]:
    """Return the base-game campaign entry appropriate for the given patch."""
    if patch_version >= 8:
        return _GUSTAV_X
    if patch_version == 7:
        return _GUSTAV_DEV
    return _GUSTAV_CLASSIC


def _version64_or_default(info: BG3ModInfo) -> str:
    """Return a Version64 string, falling back to a 1.0.0.0 placeholder.

    Mirrors BG3 Mod Manager: the legacy ``Version`` attribute holds the same
    64-bit packed value as ``Version64`` and is used verbatim, except the two
    historic 1.0.0.0 encodings (1 and 268435456) which map to 1<<55.
    """
    raw = info.version64 or info.version
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = 0
    if v in (0, 1, 268435456):
        return "36028797018963968"
    return str(v)


def _campaign_dict(patch_version: int, campaign: BG3ModInfo | None) -> dict[str, str]:
    """Return the campaign entry dict - an Adventure mod or the stock one."""
    if campaign is None:
        return _campaign_entry(patch_version)
    return {
        "Folder":        campaign.folder,
        "MD5":           campaign.md5,
        "Name":          campaign.name,
        "PublishHandle": campaign.publish_handle or "0",
        "UUID":          campaign.uuid,
        "Version64":     _version64_or_default(campaign),
    }


def build_modsettings_xml(
    ordered_mods: list[BG3ModInfo],
    patch_version: int = 8,
    campaign: BG3ModInfo | None = None,
) -> str:
    """Build the full modsettings.lsx XML string for the given patch.

    *campaign* - optional Adventure mod that replaces the stock campaign
    entry at the top (custom campaigns).  None keeps GustavX/GustavDev/Gustav.
    """
    if patch_version <= 6:
        return _build_modsettings_xml_p6(ordered_mods, campaign)
    return _build_modsettings_xml_p7(ordered_mods, patch_version, campaign)


def _build_modsettings_xml_p7(
    ordered_mods: list[BG3ModInfo],
    patch_version: int,
    campaign: BG3ModInfo | None = None,
) -> str:
    header = _MODSETTINGS_HEADER_P8 if patch_version >= 8 else _MODSETTINGS_HEADER_P7
    parts = [header]
    parts.append(_format_entry_p7(_campaign_dict(patch_version, campaign)))

    for mod in ordered_mods:
        parts.append(_format_entry_p7({
            "Folder":        mod.folder,
            "MD5":           mod.md5,
            "Name":          mod.name,
            "PublishHandle": mod.publish_handle or "0",
            "UUID":          mod.uuid,
            "Version64":     _version64_or_default(mod),
        }))

    parts.append(_MODSETTINGS_FOOTER_P7)
    return "".join(parts)


def _build_modsettings_xml_p6(
    ordered_mods: list[BG3ModInfo],
    campaign_mod: BG3ModInfo | None = None,
) -> str:
    campaign = _campaign_dict(6, campaign_mod)

    # ModOrder block: campaign first, then each mod in load order.
    mod_order_parts: list[str] = []
    mod_order_parts.append(
        _MOD_ORDER_ENTRY_P6.format(UUID=_xml_escape(campaign["UUID"]))
    )
    for mod in ordered_mods:
        mod_order_parts.append(
            _MOD_ORDER_ENTRY_P6.format(UUID=_xml_escape(mod.uuid))
        )
    mod_order_block = "".join(mod_order_parts)

    parts = [_MODSETTINGS_HEADER_P6.format(MOD_ORDER=mod_order_block)]

    # Mods block: campaign entry first, then each mod.
    parts.append(_format_entry_p6({
        "Folder":    campaign["Folder"],
        "MD5":       campaign["MD5"],
        "Name":      campaign["Name"],
        "UUID":      campaign["UUID"],
        "Version64": campaign["Version64"],
    }))
    for mod in ordered_mods:
        parts.append(_format_entry_p6({
            "Folder":    mod.folder,
            "MD5":       mod.md5,
            "Name":      mod.name,
            "UUID":      mod.uuid,
            "Version64": _version64_or_default(mod),
        }))

    parts.append(_MODSETTINGS_FOOTER_P6)
    return "".join(parts)


def _apply_manifest_pak_order(
    enabled: list[ModEntry],
    mod_infos: dict[str, BG3ModInfo],
    manifest_load_order: list[dict],
    log_fn,
) -> list[BG3ModInfo]:
    """Order paks by the collection manifest's loadOrder array.

    A single mod folder can ship multiple paks that the collection author
    intends to be interleaved with paks from other mods (e.g. load-order
    divider packs whose 30+ entries are spread throughout the LO). Walking
    by mod folder and emitting all paks per folder destroys this intent.

    Strategy: build pak-filename → BG3ModInfo from on-disk scan, then walk
    ``manifest_load_order`` in order. Manifest entries point at pak files via
    their ``id`` field. Paks present on disk but missing from the manifest
    (user-added patches, mods installed outside the collection) are appended
    at the end - that maps to "top of modlist.txt" = highest priority for
    overrides, which matches how this manager treats user-added content.

    Returns BG3ModInfo entries in lowest-priority-first order (ready for
    modsettings.lsx, where later entries override earlier ones).
    """

    # mod_infos is keyed by uuid. Build a casefold lookup once.
    uuid_to_info_cf: dict[str, BG3ModInfo] = {
        u.lower(): info for u, info in mod_infos.items()
    }

    # Walk manifest in order, matching by data.uuid. Only emit if the uuid
    # corresponds to an actually-installed pak.
    ordered: list[BG3ModInfo] = []
    seen_uuids: set[str] = set()
    for manifest_entry in manifest_load_order:
        data = manifest_entry.get("data") or {}
        uuid = (data.get("uuid") or "").strip().lower()
        if not uuid:
            continue
        info = uuid_to_info_cf.get(uuid)
        if info is None:
            continue
        if info.uuid in seen_uuids:
            continue
        seen_uuids.add(info.uuid)
        ordered.append(info)

    # Append any installed paks not covered by the manifest.
    # Walk in modlist order (already lowest-priority-first) so user-added
    # mods land in the same relative order they appear in modlist.txt.
    by_source: dict[str, list[BG3ModInfo]] = {}
    for info in mod_infos.values():
        if info.source_mod:
            by_source.setdefault(info.source_mod, []).append(info)
    for entry in enabled:
        for info in by_source.get(entry.name, ()):
            if info.uuid in seen_uuids:
                continue
            seen_uuids.add(info.uuid)
            ordered.append(info)

    # Dependency sweep: the manifest already orders deps before dependents
    # (curators verify their collections boot), but user-added trailing
    # paks may reference deps that were emitted later by the manifest.
    # Walk the result and pull each pak's missing deps to immediately
    # before the pak itself, preserving the manifest's pak-level interleave.
    final: list[BG3ModInfo] = []
    placed: set[str] = set()

    def _emit_with_deps(info: BG3ModInfo) -> None:
        if info.uuid in placed:
            return
        for dep_uuid in info.dependencies:
            dep = mod_infos.get(dep_uuid) or uuid_to_info_cf.get(dep_uuid.lower())
            if dep is not None and dep.uuid not in placed:
                _emit_with_deps(dep)
        placed.add(info.uuid)
        final.append(info)

    for info in ordered:
        _emit_with_deps(info)
    return final


def write_modsettings(
    modsettings_path: Path,
    modlist_path: Path,
    staging_root: Path,
    log_fn=None,
    game_data_path: Path | None = None,
    patch_version: int = 8,
    manifest_load_order: list[dict] | None = None,
    script_extender_dll: Path | None = None,
    overwrite_root: Path | None = None,
) -> int:
    """End-to-end: scan paks, resolve order, write modsettings.lsx.

    *script_extender_dll* - optional path to the game's ``bin/DWrite.dll``.
    When given and missing on disk, a warning is logged for every mod whose
    pak declares a Script Extender requirement.

    *game_data_path* - optional path to the game's ``Data/`` directory.
    When provided, .pak files there are scanned so that base-game / DLC /
    patch module UUIDs are recognised during the dependency check and don't
    produce false "not installed" warnings.

    *patch_version* - 6, 7, or 8.  Controls the modsettings.lsx schema:
      - 8: GustavX campaign, Mods node only, Version64 + PublishHandle
      - 7: Gustav campaign, Mods node only, Version64 + PublishHandle
      - 6: Gustav campaign, ModOrder + Mods nodes, 32-bit Version

    *manifest_load_order* - optional list of entries from a collection
    manifest's ``loadOrder`` array. When provided, paks are emitted in the
    manifest's exact order (curators interleave paks from different mods -
    e.g. load-order divider packs - which the default folder-walk order
    destroys). Paks installed but not in the manifest are appended.

    *overwrite_root* - paks the in-game mod manager wrote there deploy at the
    highest priority, so they need load-order entries too (else they never load).

    Returns the number of mod entries written (excluding the campaign entry).
    """
    _log = _safe_log(log_fn)

    entries = read_modlist(modlist_path)
    enabled = [e for e in entries if e.enabled and not e.is_separator]
    # modlist.txt is highest-priority-first; modsettings.lsx needs
    # lowest-priority-first (later entries override earlier ones in BG3).
    enabled = list(reversed(enabled))
    extra_dirs: dict[str, Path] = {}
    if overwrite_root is not None and overwrite_root.is_dir():
        # Overwrite outranks every staged mod - append it last (= highest).
        enabled.append(ModEntry(name=_OVERWRITE_NAME, enabled=True, locked=False))
        extra_dirs[_OVERWRITE_NAME] = overwrite_root

    _log(f"Scanning .pak files for mod metadata (patch {patch_version}) ...")
    no_metadata: list[str] = []
    try:
        from Utils.profile_state import read_excluded_mod_files
        excluded = {m: set(v) for m, v in
                    read_excluded_mod_files(modlist_path.parent, None).items()}
    except Exception:
        excluded = {}
    mod_infos = scan_mod_paks(staging_root, enabled, no_metadata=no_metadata,
                              excluded=excluded, extra_dirs=extra_dirs or None)
    _log(f"  Found metadata for {len(mod_infos)} mod(s).")
    if no_metadata:
        _log(f"  {len(no_metadata)} enabled mod(s) had .pak files but no "
             "modsettings metadata (won't appear in load order):")
        for desc in no_metadata:
            _log(f"    - {desc}")

    if not mod_infos:
        _log("No mod metadata found - writing vanilla modsettings.lsx.")
        xml = build_modsettings_xml([], patch_version=patch_version)
        modsettings_path.parent.mkdir(parents=True, exist_ok=True)
        modsettings_path.write_text(xml, encoding="utf-8")
        return 0

    # Script Extender requirement check: warn when mods declare a required
    # SE version but the loader DLL isn't installed in the game's bin/.
    se_mods = sorted(i.name for i in mod_infos.values()
                     if i.requires_script_extender)
    if se_mods and script_extender_dll is not None \
            and not script_extender_dll.is_file():
        _log(f"  WARNING: {len(se_mods)} mod(s) require the BG3 Script "
             f"Extender, but it is not installed ({script_extender_dll} "
             f"missing): {', '.join(se_mods)}")

    # Pure override paks (only touch base-game module folders) are loaded by
    # the game automatically and must stay out of the load order - same as
    # BG3 Mod Manager's "Overrides" section.
    override_only = sorted((i for i in mod_infos.values() if i.is_override_only),
                           key=lambda i: i.name)
    eligible = {u: i for u, i in mod_infos.items() if not i.is_override_only}
    if override_only:
        _log(f"  {len(override_only)} pak(s) only override base-game files - "
             "loaded automatically, left out of the load order:")
        for i in override_only:
            _log(f"    - {i.name} ({i.source_mod})")

    if manifest_load_order:
        _log(f"Resolving load order from collection manifest ({len(manifest_load_order)} entries) ...")
        ordered = _apply_manifest_pak_order(enabled, eligible, manifest_load_order, _log)
    else:
        _log("Resolving load order with dependency sorting ...")
        ordered = resolve_load_order(enabled, eligible)

    # Adventure (custom campaign) mods replace the stock campaign entry at
    # the top of modsettings rather than appearing as regular mod entries.
    campaign_mod: BG3ModInfo | None = None
    adventures = [m for m in ordered if m.mod_type == "Adventure"]
    if adventures:
        campaign_mod = adventures[-1]  # highest priority wins
        if len(adventures) > 1:
            _log("  WARNING: multiple Adventure (custom campaign) mods "
                 f"installed: {', '.join(m.name for m in adventures)} - "
                 f"using '{campaign_mod.name}'.")
        ordered = [m for m in ordered if m.uuid != campaign_mod.uuid]
        _log(f"  Adventure mod '{campaign_mod.name}' set as the campaign "
             f"(replaces {_campaign_entry(patch_version)['Name']}).")
    _log(f"  Load order: {', '.join(m.name for m in ordered)}")

    # Build the set of UUIDs that are known to exist (installed mods +
    # base-game engine modules).  Scanning the game's Data/ directory
    # catches patch, DLC, and hotfix modules that ship with the game.
    all_uuids = set(mod_infos.keys()) | _SYSTEM_UUIDS
    if game_data_path is not None:
        _log("Scanning game Data/ for base-game module UUIDs ...")
        game_uuids = scan_game_data_uuids(game_data_path)
        all_uuids |= game_uuids
        _log(f"  Found {len(game_uuids)} base-game module(s).")

    for mod in ordered:
        for dep_uuid in mod.dependencies:
            if dep_uuid not in all_uuids:
                _log(f"  WARNING: {mod.name} requires a mod (UUID {dep_uuid}) "
                     f"that is not installed.")

    xml = build_modsettings_xml(ordered, patch_version=patch_version,
                                campaign=campaign_mod)
    modsettings_path.parent.mkdir(parents=True, exist_ok=True)
    modsettings_path.write_text(xml, encoding="utf-8")

    _log(f"Wrote modsettings.lsx with {len(ordered)} mod(s).")
    return len(ordered)


def write_vanilla_modsettings(
    modsettings_path: Path,
    log_fn=None,
    patch_version: int = 8,
) -> None:
    """Write a clean modsettings.lsx with only the campaign entry."""
    _log = _safe_log(log_fn)
    xml = build_modsettings_xml([], patch_version=patch_version)
    modsettings_path.parent.mkdir(parents=True, exist_ok=True)
    modsettings_path.write_text(xml, encoding="utf-8")
    campaign_name = _campaign_entry(patch_version)["Name"]
    _log(f"Reset modsettings.lsx to vanilla ({campaign_name} only, patch {patch_version}).")
