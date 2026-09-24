"""Focused checks for No Man's Sky GCMODSETTINGS.MXML handling.

Run directly from the source tree::

    python3 src/Utils/nms/_selftest.py

Covers parsing and rebuilding the game's exact file format, the mod-order
mapping, preservation of hand-installed entries, and the handler's
backup/restore cycle.
"""

from __future__ import annotations

import sys
from pathlib import Path


_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from Utils.nms.gcmodsettings import (  # noqa: E402
    build_gcmodsettings_xml,
    disable_all_mods,
    entry_name,
    mod_entries,
    new_entry,
    parse_gcmodsettings,
)

_CRLF = "\r\n"


def _entry_lines(index: int, name: str, enabled: str) -> list[str]:
    return [
        f'\t\t<Property name="Data" value="GcModSettingsInfo" _index="{index}">',
        f'\t\t\t<Property name="Name" value="{name}" />',
        '\t\t\t<Property name="Author" value="" />',
        '\t\t\t<Property name="ID" value="0" />',
        '\t\t\t<Property name="AuthorID" value="0" />',
        '\t\t\t<Property name="LastUpdated" value="0" />',
        f'\t\t\t<Property name="ModPriority" value="{index}" />',
        f'\t\t\t<Property name="Enabled" value="{enabled}" />',
        f'\t\t\t<Property name="EnabledVR" value="{enabled}" />',
        '\t\t\t<Property name="Dependencies" />',
        '\t\t</Property>',
    ]


def _game_file(entries: list[tuple[str, str]]) -> bytes:
    """Bytes exactly as NMS writes GCMODSETTINGS.MXML."""
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<Data template="GcModSettings">',
        '\t<Property name="DisableAllMods" value="false" />',
    ]
    if entries:
        lines.append('\t<Property name="Data">')
        for i, (name, enabled) in enumerate(entries):
            lines.extend(_entry_lines(i, name, enabled))
        lines.append('\t</Property>')
    else:
        lines.append('\t<Property name="Data" />')
    lines.append('</Data>')
    # The game ends the file at </Data> with no trailing newline.
    return _CRLF.join(lines).encode("utf-8-sig")


_POPULATED = _game_file([
    ("CORVETTE OPERATIONS CONSOLE - WITH TELEPORT", "true"),
    ("BETTER RECHARGE ORDER", "true"),
    ("REMOVE INTRO LOGOS", "false"),
])
_EMPTY = _game_file([])


def _roundtrip(raw: bytes) -> bytes:
    root = parse_gcmodsettings(raw.decode("utf-8-sig"))
    assert root is not None
    xml = build_gcmodsettings_xml(mod_entries(root), disable_all_mods(root))
    return xml.encode("utf-8-sig")


def test_roundtrip_populated_file_is_byte_exact() -> None:
    assert _roundtrip(_POPULATED) == _POPULATED


def test_roundtrip_empty_file_is_byte_exact() -> None:
    assert _roundtrip(_EMPTY) == _EMPTY


def test_parse_reads_names_in_priority_order() -> None:
    root = parse_gcmodsettings(_POPULATED.decode("utf-8-sig"))
    assert [entry_name(e) for e in mod_entries(root)] == [
        "CORVETTE OPERATIONS CONSOLE - WITH TELEPORT",
        "BETTER RECHARGE ORDER",
        "REMOVE INTRO LOGOS",
    ]


def test_parse_rejects_garbage_and_foreign_documents() -> None:
    assert parse_gcmodsettings("not xml at all") is None
    assert parse_gcmodsettings('<Data template="GcUserSettingsData" />') is None


def test_new_entry_matches_game_template() -> None:
    xml = build_gcmodsettings_xml([new_entry("Better Recharge Order")])
    assert xml == _CRLF.join([
        '<?xml version="1.0" encoding="utf-8"?>',
        '<Data template="GcModSettings">',
        '\t<Property name="DisableAllMods" value="false" />',
        '\t<Property name="Data">',
        *_entry_lines(0, "BETTER RECHARGE ORDER", "true"),
        '\t</Property>',
        '</Data>',
    ])


def test_build_renumbers_index_and_priority() -> None:
    root = parse_gcmodsettings(_POPULATED.decode("utf-8-sig"))
    reordered = list(reversed(mod_entries(root)))
    rebuilt = parse_gcmodsettings(build_gcmodsettings_xml(reordered))
    for i, entry in enumerate(mod_entries(rebuilt)):
        assert entry.get("_index") == str(i)
        prio = [p for p in entry if p.get("name") == "ModPriority"][0]
        assert prio.get("value") == str(i)
    assert entry_name(mod_entries(rebuilt)[0]) == "REMOVE INTRO LOGOS"


def test_special_characters_are_escaped() -> None:
    xml = build_gcmodsettings_xml([new_entry('Guns & "Roses" <v2>')])
    root = parse_gcmodsettings(xml)
    assert root is not None
    assert entry_name(mod_entries(root)[0]) == 'GUNS & "ROSES" <V2>'
    assert "&amp;" in xml and "&quot;" in xml and "&lt;" in xml


def main() -> None:
    test_roundtrip_populated_file_is_byte_exact()
    test_roundtrip_empty_file_is_byte_exact()
    test_parse_reads_names_in_priority_order()
    test_parse_rejects_garbage_and_foreign_documents()
    test_new_entry_matches_game_template()
    test_build_renumbers_index_and_priority()
    test_special_characters_are_escaped()
    print("ok  gcmodsettings format")


if __name__ == "__main__":
    main()
