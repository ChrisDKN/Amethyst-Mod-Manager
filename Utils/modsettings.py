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

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from Utils.modlist import ModEntry, read_modlist
from Utils.pak_reader import extract_meta_lsx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# UUIDs for base-game / engine modules that should be ignored as dependencies
_SYSTEM_UUIDS: frozenset[str] = frozenset({
    "28ac9ce2-2aba-8cda-b3b5-6e922f71b6b8",   # GustavDev
    "cb555efe-2d9e-131f-8195-a89329d218ea",     # GustavX
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
                log.warning("Failed to read %s: %s", pak, exc)
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
) -> int:
    """End-to-end: scan paks, resolve order, write modsettings.lsx.

    Returns the number of mod entries written (excluding GustavDev).
    """
    _log = log_fn or (lambda _: None)

    entries = read_modlist(modlist_path)
    enabled = [e for e in entries if e.enabled and not e.is_separator]

    _log("Scanning .pak files for mod metadata ...")
    mod_infos = scan_mod_paks(staging_root, enabled)
    _log(f"  Found metadata for {len(mod_infos)} mod(s).")

    if not mod_infos:
        # No mods — write vanilla modsettings with just GustavDev
        _log("No mod metadata found — writing vanilla modsettings.lsx.")
        xml = build_modsettings_xml([])
        modsettings_path.parent.mkdir(parents=True, exist_ok=True)
        modsettings_path.write_text(xml, encoding="utf-8")
        return 0

    _log("Resolving load order with dependency sorting ...")
    ordered = resolve_load_order(enabled, mod_infos)
    _log(f"  Load order: {', '.join(m.name for m in ordered)}")

    # Check for missing dependencies
    all_uuids = set(mod_infos.keys()) | _SYSTEM_UUIDS
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
