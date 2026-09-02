"""Localised plugin strings (.STRINGS / .ILSTRINGS / .DLSTRINGS).

A plugin with the localised flag set in its TES4 header stores no text: FULL,
DESC and friends hold a 4-byte string ID resolved against a side table. Read
the subrecord as text and Skyrim.esm's NPC names come out as ``x&``, ``Š&``.

    NPC_ -> FULL (uint32 id) -> Strings/<plugin>_<language>.STRINGS -> "Nazeem"

The tables live beside the plugin under ``Strings/`` or, for the vanilla game,
inside an archive (Skyrim - Interface.bsa) - so lookup goes through the same
archive-aware read the rest of the asset path uses.

Three suffixes share one container format, differing only in how the payload
is stored: .STRINGS entries are bare null-terminated, while .ILSTRINGS and
.DLSTRINGS prefix each with a uint32 byte length.
"""
from __future__ import annotations

import struct
from pathlib import Path

# TES4 header flag marking a plugin's strings as externalised.
LOCALISED = 0x80

_SUFFIXES = ("strings", "ilstrings", "dlstrings")
# Bare null-terminated payloads; the other two carry a uint32 length prefix.
_UNPREFIXED = "strings"

_MAX_ENTRIES = 1 << 20


def is_localised(flags: int | None) -> bool:
    """Whether a TES4 header flags word marks the plugin as localised."""
    return bool((flags or 0) & LOCALISED)


def parse_strings(data: bytes, prefixed: bool) -> dict[int, str]:
    """Decode one strings table: ``{string id: text}``.

    *prefixed* selects the .ILSTRINGS/.DLSTRINGS layout, whose payloads carry a
    uint32 byte length instead of relying on a terminator.
    """
    out: dict[int, str] = {}
    if len(data) < 8:
        return out
    count, _data_size = struct.unpack_from("<II", data, 0)
    if count > _MAX_ENTRIES:
        return out
    directory = 8
    payload = directory + count * 8
    if payload > len(data):
        return out
    for i in range(count):
        try:
            sid, offset = struct.unpack_from("<II", data, directory + i * 8)
        except struct.error:
            break
        start = payload + offset
        if start >= len(data):
            continue
        if prefixed:
            try:
                length = struct.unpack_from("<I", data, start)[0]
            except struct.error:
                continue
            start += 4
            end = min(start + max(length - 1, 0), len(data))
        else:
            end = data.find(b"\0", start)
            if end < 0:
                end = len(data)
        out[sid] = data[start:end].decode("cp1252", "replace")
    return out


def table_names(plugin_name: str, language: str = "english") -> list[str]:
    """The archive-relative table paths for a plugin, in lookup order."""
    stem = Path(plugin_name).stem.lower()
    return [f"strings/{stem}_{language.lower()}.{sfx}" for sfx in _SUFFIXES]


def load_tables(plugin_name: str, search_dirs, read_archive=None,
                archives=(), language: str = "english") -> dict[int, str]:
    """Merge every strings table for one plugin into ``{string id: text}``.

    Loose files under any of *search_dirs* win over archive copies, matching
    how the engine resolves them. *read_archive* is called as
    ``read_archive(archive, inner_path)`` for each entry of *archives*; pass
    ``mesh_catalog.read_archive_member`` to reuse the warm archive index.
    """
    merged: dict[int, str] = {}
    for rel in reversed(table_names(plugin_name, language)):
        prefixed = not rel.endswith("." + _UNPREFIXED)
        # Archives first, then the loose copy overwrites: a loose table wins
        # per id, but ids it omits still resolve from the archive.
        if read_archive is not None:
            for archive in archives:
                try:
                    data = read_archive(archive, rel)
                except Exception:                        # noqa: BLE001
                    data = None
                if data:
                    merged.update(parse_strings(bytes(data), prefixed))
                    break
        loose = _read_loose(rel, search_dirs)
        if loose:
            merged.update(parse_strings(bytes(loose), prefixed))
    return merged


def _read_loose(rel: str, search_dirs) -> bytes | None:
    """The first case-insensitive match for *rel* under *search_dirs*.

    Every component is matched case-insensitively: the tables live under
    ``Strings/`` on a case-sensitive filesystem but are referenced lowercase.
    """
    parts = [p for p in rel.split("/") if p]
    for base in search_dirs or ():
        found = _walk_ci(Path(base), parts)
        if found is not None:
            try:
                return found.read_bytes()
            except OSError:
                continue
    return None


def _walk_ci(base: Path, parts: list[str]) -> "Path | None":
    """Resolve *parts* under *base*, one case-insensitive component at a time."""
    current = base
    for i, want in enumerate(parts):
        last = i == len(parts) - 1
        direct = current / want
        if (direct.is_file() if last else direct.is_dir()):
            current = direct
            continue
        try:
            entries = list(current.iterdir())
        except OSError:
            return None
        for entry in entries:
            if entry.name.lower() == want.lower():
                if entry.is_file() if last else entry.is_dir():
                    current = entry
                    break
        else:
            return None
    return current


def resolve(value, strings: dict[int, str] | None) -> str:
    """Text for a FULL-style value: a string id, decoded text, or raw bytes."""
    if isinstance(value, int):
        return (strings or {}).get(value, "")
    if isinstance(value, str):
        return value
    raw = bytes(value or b"")
    if strings is not None and len(raw) == 4:
        return strings.get(struct.unpack_from("<I", raw, 0)[0], "")
    return raw.split(b"\0")[0].decode("cp1252", "replace")
