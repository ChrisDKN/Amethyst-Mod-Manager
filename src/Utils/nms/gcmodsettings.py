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
        root = ET.fromstring(xml_text.lstrip("﻿"))
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
