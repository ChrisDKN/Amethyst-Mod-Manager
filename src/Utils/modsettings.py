"""
modsettings.py
Build and write modsettings.lsx for Baldur's Gate 3.

Workflow:
  1. For each enabled mod, open its .pak file(s) and extract meta.lsx.
  2. Parse the XML to collect UUID, Name, Folder, Version64, and dependencies.
  3. Topologically sort mods so dependencies always appear before dependents.
  4. Write the Patch 7+ modsettings.lsx (Mods node only, no ModOrder).

The GustavDev base-game entry is always written first and never removed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from Utils.modlist import ModEntry, read_modlist
from Utils.pak_reader import extract_meta_lsx
from Utils.app_log import app_log

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
})

_GUSTAV_DEV = {
    "Folder":        "GustavDev",
    "MD5":           "",
    "Name":          "GustavDev",
    "PublishHandle":  "0",
    "UUID":          "28ac9ce2-2aba-8cda-b3b5-6e922f71b6b8",
    "Version64":     "36028797018963968",
}

# Patch 7+ modsettings.lsx template
_MODSETTINGS_HEADER = """\
<?xml version="1.0" encoding="UTF-8"?>
<save>
  <version major="4" minor="7" revision="1" build="3"/>
  <region id="ModuleSettings">
    <node id="root">
      <children>
        <node id="Mods">
          <children>
"""

_MODSETTINGS_FOOTER = """\
          </children>
        </node>
      </children>
    </node>
  </region>
</save>
"""

_MOD_ENTRY_TEMPLATE = """\
            <node id="ModuleShortDesc">
              <attribute id="Folder" type="LSString" value="{Folder}"/>
              <attribute id="MD5" type="LSString" value="{MD5}"/>
              <attribute id="Name" type="LSString" value="{Name}"/>
              <attribute id="PublishHandle" type="uint64" value="{PublishHandle}"/>
              <attribute id="UUID" type="guid" value="{UUID}"/>
              <attribute id="Version64" type="int64" value="{Version64}"/>
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
    # UUIDs of mods this mod depends on
    dependencies: list[str] = field(default_factory=list)
    # The mod-list name (staging folder name) this came from
    source_mod: str = ""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _attr_value(node: ET.Element, attr_id: str) -> str:
    """Find <attribute id="attr_id" ... value="X"/> and return X, or ""."""
    for attr in node.iter("attribute"):
        if attr.get("id") == attr_id:
            return attr.get("value", "")
    return ""


def parse_meta_lsx(xml_text: str) -> BG3ModInfo | None:
    """Parse a meta.lsx XML string and return a BG3ModInfo, or None on failure."""
    try:
        root = ET.fromstring(xml_text)
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
    md5 = _attr_value(module_info, "MD5")

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
        dependencies=deps,
    )


# ---------------------------------------------------------------------------
# .pak scanning
# ---------------------------------------------------------------------------

def scan_mod_paks(
    staging_root: Path,
    enabled_mods: list[ModEntry],
) -> dict[str, BG3ModInfo]:
    """Scan .pak files for all enabled mods and return {uuid: BG3ModInfo}.

    Each mod's staging folder may contain one or more .pak files.  We extract
    meta.lsx from each and collect the metadata.  If a mod folder contains
    multiple .pak files, each one that has a meta.lsx is recorded.
    """
    by_uuid: dict[str, BG3ModInfo] = {}

    for entry in enabled_mods:
        mod_dir = staging_root / entry.name
        if not mod_dir.is_dir():
            continue
        for pak in mod_dir.rglob("*.pak"):
            try:
                xml_text = extract_meta_lsx(pak)
            except Exception as exc:
                app_log(f"Failed to read {pak}: {exc}")
                continue
            if xml_text is None:
                continue
            info = parse_meta_lsx(xml_text)
            if info is None:
                continue
            info.source_mod = entry.name
            by_uuid[info.uuid] = info

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

    The user's modlist order is respected as much as possible — the resolver
    only reorders when a dependency must be loaded before a dependent.

    Algorithm (mirrors BG3 Mod Manager):
      For each mod in the user's order, recursively insert its dependencies
      first, then insert the mod itself.  A visited set prevents duplicates.
    """
    # Build a lookup: source_mod name → BG3ModInfo
    by_source: dict[str, BG3ModInfo] = {}
    for info in mod_infos.values():
        if info.source_mod:
            by_source[info.source_mod] = info

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
        info = by_source.get(entry.name)
        if info is not None:
            _insert(info)

    return result


# ---------------------------------------------------------------------------
# modsettings.lsx generation
# ---------------------------------------------------------------------------

def _format_entry(info: dict[str, str]) -> str:
    return _MOD_ENTRY_TEMPLATE.format(**info)


def build_modsettings_xml(ordered_mods: list[BG3ModInfo]) -> str:
    """Build the full modsettings.lsx XML string."""
    parts = [_MODSETTINGS_HEADER]

    # GustavDev always first
    parts.append(_format_entry(_GUSTAV_DEV))

    # Then each mod in resolved order
    for mod in ordered_mods:
        parts.append(_format_entry({
            "Folder":        mod.folder,
            "MD5":           mod.md5,
            "Name":          mod.name,
            "PublishHandle": mod.publish_handle,
            "UUID":          mod.uuid,
            "Version64":     mod.version64,
        }))

    parts.append(_MODSETTINGS_FOOTER)
    return "".join(parts)


def write_modsettings(
    modsettings_path: Path,
    modlist_path: Path,
    staging_root: Path,
    log_fn=None,
    game_data_path: Path | None = None,
) -> int:
    """End-to-end: scan paks, resolve order, write modsettings.lsx.

    *game_data_path* — optional path to the game's ``Data/`` directory.
    When provided, .pak files there are scanned so that base-game / DLC /
    patch module UUIDs are recognised during the dependency check and don't
    produce false "not installed" warnings.

    Returns the number of mod entries written (excluding GustavDev).
    """
    _log = log_fn or (lambda _: None)

    entries = read_modlist(modlist_path)
    enabled = [e for e in entries if e.enabled and not e.is_separator]

    _log("Scanning .pak files for mod metadata ...")
    mod_infos = scan_mod_paks(staging_root, enabled)
    _log(f"  Found metadata for {len(mod_infos)} mod(s).")

    if not mod_infos:
        _log("No mod metadata found — writing vanilla modsettings.lsx.")
        xml = build_modsettings_xml([])
        modsettings_path.parent.mkdir(parents=True, exist_ok=True)
        modsettings_path.write_text(xml, encoding="utf-8")
        return 0

    _log("Resolving load order with dependency sorting ...")
    ordered = resolve_load_order(enabled, mod_infos)
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

    xml = build_modsettings_xml(ordered)
    modsettings_path.parent.mkdir(parents=True, exist_ok=True)
    modsettings_path.write_text(xml, encoding="utf-8")

    _log(f"Wrote modsettings.lsx with {len(ordered)} mod(s).")
    return len(ordered)


def write_vanilla_modsettings(modsettings_path: Path, log_fn=None) -> None:
    """Write a clean modsettings.lsx with only the GustavDev entry."""
    _log = log_fn or (lambda _: None)
    xml = build_modsettings_xml([])
    modsettings_path.parent.mkdir(parents=True, exist_ok=True)
    modsettings_path.write_text(xml, encoding="utf-8")
    _log("Reset modsettings.lsx to vanilla (GustavDev only).")
