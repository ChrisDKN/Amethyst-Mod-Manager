"""
Utils.nms.gcmodsettings
Build and write GCMODSETTINGS.MXML for No Man's Sky.

Workflow:
  1. Map each deployed GAMEDATA/MODS/<folder> to the mod(s) that supplied it.
  2. Walk modlist.txt highest-priority-first and emit each enabled mod's
     folders. NMS gives ModPriority 0 the highest precedence, so the modlist
     order maps straight across ([Overwrite] outranks everything).
  3. Append the original file's entries for folders Amethyst doesn't manage
     (mods installed by hand), keeping their order and enabled state.
  4. Write the file in the game's own format: UTF-8 BOM, CRLF, tabs, and
     no newline after the closing </Data>.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.atomic_write import write_atomic_text
from Utils.filegraph.constants import OVERWRITE_NAME as _OVERWRITE_NAME
from Utils.mods.modlist import ModEntry, read_modlist

_HEADER = '<?xml version="1.0" encoding="utf-8"?>'
_TEMPLATE = "GcModSettings"
_ENTRY_VALUE = "GcModSettingsInfo"
_NEWLINE = "\r\n"
_NO_PRIORITY = 1 << 31


def _xml_escape(value: str) -> str:
    """Escape &, <, >, and " for safe insertion into MXML attribute values."""
    return (
        value.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
    )


def parse_gcmodsettings(xml_text: str) -> ET.Element | None:
    """Parse GCMODSETTINGS.MXML text, or return None if it isn't one."""
    try:
        root = ET.fromstring(xml_text.lstrip("\ufeff"))
    except ET.ParseError:
        return None
    if root.tag != "Data" or root.get("template") != _TEMPLATE:
        return None
    return root


def _prop(entry: ET.Element, name: str) -> ET.Element | None:
    for child in entry:
        if child.get("name") == name:
            return child
    return None


def _container(root: ET.Element) -> ET.Element | None:
    for child in root:
        if child.get("name") == "Data" and child.get("value") is None:
            return child
    return None


def _priority(entry: ET.Element) -> int:
    prop = _prop(entry, "ModPriority")
    try:
        return int(prop.get("value", "")) if prop is not None else _NO_PRIORITY
    except ValueError:
        return _NO_PRIORITY


def mod_entries(root: ET.Element) -> list[ET.Element]:
    """Return the GcModSettingsInfo entries sorted by ModPriority."""
    container = _container(root)
    if container is None:
        return []
    items = [c for c in container if c.get("value") == _ENTRY_VALUE]
    return sorted(items, key=_priority)


def entry_name(entry: ET.Element) -> str:
    prop = _prop(entry, "Name")
    return prop.get("value", "") if prop is not None else ""


def disable_all_mods(root: ET.Element) -> str:
    prop = _prop(root, "DisableAllMods")
    return prop.get("value", "false") if prop is not None else "false"


def new_entry(folder: str) -> ET.Element:
    """A fresh entry in the exact shape the game writes for a new mod."""
    entry = ET.Element("Property", {
        "name": "Data", "value": _ENTRY_VALUE, "_index": "0"})
    for name, value in (
        ("Name", folder.upper()),
        ("Author", ""),
        ("ID", "0"),
        ("AuthorID", "0"),
        ("LastUpdated", "0"),
        ("ModPriority", "0"),
        ("Enabled", "true"),
        ("EnabledVR", "true"),
    ):
        ET.SubElement(entry, "Property", {"name": name, "value": value})
    ET.SubElement(entry, "Property", {"name": "Dependencies"})
    return entry


def set_enabled(entry: ET.Element, enabled: bool) -> None:
    value = "true" if enabled else "false"
    for name in ("Enabled", "EnabledVR"):
        prop = _prop(entry, name)
        if prop is not None:
            prop.set("value", value)


def _format_element(el: ET.Element, depth: int, lines: list[str]) -> None:
    indent = "\t" * depth
    attrs = "".join(f' {k}="{_xml_escape(v)}"' for k, v in el.attrib.items())
    children = list(el)
    if not children:
        lines.append(f"{indent}<{el.tag}{attrs} />")
        return
    lines.append(f"{indent}<{el.tag}{attrs}>")
    for child in children:
        _format_element(child, depth + 1, lines)
    lines.append(f"{indent}</{el.tag}>")


def build_gcmodsettings_xml(
    entries: list[ET.Element],
    disable_all: str = "false",
) -> str:
    """Return GCMODSETTINGS.MXML text (no BOM) with *entries* in order.

    ``_index`` and ``ModPriority`` are renumbered 0..n-1 from list order.
    Encode with ``utf-8-sig`` to match the game's BOM.
    """
    lines = [
        _HEADER,
        f'<Data template="{_TEMPLATE}">',
        f'\t<Property name="DisableAllMods" value="{_xml_escape(disable_all)}" />',
    ]
    if not entries:
        lines.append('\t<Property name="Data" />')
    else:
        lines.append('\t<Property name="Data">')
        for i, entry in enumerate(entries):
            entry = copy.deepcopy(entry)
            entry.set("_index", str(i))
            prio = _prop(entry, "ModPriority")
            if prio is not None:
                prio.set("value", str(i))
            _format_element(entry, 2, lines)
        lines.append('\t</Property>')
    lines.append("</Data>")
    # The game ends the file at </Data> with no trailing newline.
    return _NEWLINE.join(lines)


# ---------------------------------------------------------------------------
# Load order
# ---------------------------------------------------------------------------

def resolve_mod_order(
    enabled_mods: list[ModEntry],
    folder_owners: dict[str, set[str]],
) -> list[str]:
    """Return NMS mod folders in ModPriority order (index 0 wins).

    *enabled_mods* is highest-priority-first (modlist.txt order). Each mod
    contributes the folders it supplied, sorted by name; a folder supplied
    by several mods belongs to the highest-priority one. Folders whose owner
    isn't in the list are appended so a deployed folder is never dropped.
    """
    by_mod: dict[str, list[str]] = {}
    for folder, owners in folder_owners.items():
        for owner in owners:
            by_mod.setdefault(owner.casefold(), []).append(folder)

    seen: set[str] = set()
    result: list[str] = []

    def _emit(folders) -> None:
        for folder in sorted(folders, key=str.casefold):
            key = folder.casefold()
            if key not in seen:
                seen.add(key)
                result.append(folder)

    for entry in enabled_mods:
        _emit(by_mod.get(entry.name.casefold(), ()))
    _emit(folder_owners)
    return result


# ---------------------------------------------------------------------------
# GCMODSETTINGS.MXML generation
# ---------------------------------------------------------------------------

def write_gcmodsettings(
    settings_path: Path,
    modlist_path: Path,
    folder_owners: dict[str, set[str]],
    log_fn=None,
    preserved_settings: Path | None = None,
    unmanaged_folders: set[str] = frozenset(),
) -> int:
    """End-to-end: order the deployed folders and write GCMODSETTINGS.MXML.

    *folder_owners* - deployed GAMEDATA/MODS folder → the mods that supplied it.

    *preserved_settings* - the user's original file (the per-profile backup).
    Its entries for *unmanaged_folders* (folders present that Amethyst didn't
    deploy - mods installed by hand) are kept after Amethyst's entries, in
    their original order and enabled state. Entries for any other folder are
    dropped.

    Returns the number of mod entries written.
    """
    _log = _safe_log(log_fn)

    entries = read_modlist(modlist_path)
    # modlist.txt is highest-priority-first, which is NMS's order too
    # (ModPriority 0 wins). Overwrite outranks every staged mod.
    enabled = [ModEntry(name=_OVERWRITE_NAME, enabled=True, locked=False)]
    enabled += [e for e in entries if e.enabled and not e.is_separator]
    managed = resolve_mod_order(enabled, folder_owners)
    managed_keys = {f.casefold() for f in managed}

    originals: list[ET.Element] = []
    disable_all = "false"
    if preserved_settings is not None and preserved_settings.is_file():
        root = parse_gcmodsettings(
            preserved_settings.read_text(encoding="utf-8-sig", errors="replace"))
        if root is None:
            _log("  WARNING: could not parse the original GCMODSETTINGS.MXML - "
                 "pre-existing entries were not preserved.")
        else:
            originals = mod_entries(root)
            disable_all = disable_all_mods(root)
    by_key = {entry_name(e).casefold(): e for e in originals}

    result: list[ET.Element] = []
    for folder in managed:
        original = by_key.get(folder.casefold())
        entry = copy.deepcopy(original) if original is not None else new_entry(folder)
        set_enabled(entry, True)
        result.append(entry)

    unmanaged_keys = {f.casefold() for f in unmanaged_folders} - managed_keys
    preserved = [e for e in originals
                 if entry_name(e).casefold() in unmanaged_keys]
    if preserved:
        _log(f"  Preserving {len(preserved)} pre-existing mod entry/entries.")
    result.extend(preserved)

    _log("  Mod priority: "
         + (", ".join(entry_name(e) for e in result) or "(none)"))
    write_atomic_text(settings_path,
                      build_gcmodsettings_xml(result, disable_all),
                      encoding="utf-8-sig")
    _log(f"Wrote GCMODSETTINGS.MXML with {len(result)} mod(s).")
    return len(result)
