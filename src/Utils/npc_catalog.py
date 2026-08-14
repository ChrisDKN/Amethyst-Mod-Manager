"""NPCs with a baked FaceGen head, named from the plugin that defines them.

The 3D side is solved: a ``FaceGeom/<plugin>/<formid>.nif`` is a self-contained
head mesh the existing viewport already renders. What it lacks is identity - a
file called ``00013BBF.nif`` tells a user nothing. This module joins the mesh
catalogue to NPC_ records so the row reads "Nazeem" instead:

    meshes/actors/character/facegendata/facegeom/<plugin>/<formid>.nif
        -> NPC_ record in <plugin> (or a mod overriding it)
        -> FULL (localised: a string id, see Utils.strings_table) -> name

Every copy is listed, winner-flagged by Utils.mesh_catalog, so a contested head
shows which NPC overhaul actually wins. An NPC whose record exists but has no
FaceGen mesh is reported separately: that is exactly the grey-face bug.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from Utils.facegen_tint import _parse_cached, facegen_npc
from Utils.mesh_catalog import (DEFAULT_EXTS, MOD_ARCHIVE, MOD_LOOSE,
                                _enabled_mods, build_catalog, source_label)
from Utils.strings_table import load_tables, resolve

__all__ = ["NpcEntry", "FACEGEN_PREFIX", "build_npc_catalog", "npc_label"]

# Bounding the scan here (rather than all of meshes/) is what keeps a browse
# cheap: ~1.3k heads instead of ~23k meshes.
FACEGEN_PREFIX = "meshes/actors/character/facegendata/facegeom/"

_PLUGIN_EXTS = (".esp", ".esm", ".esl")


@dataclass(frozen=True)
class NpcEntry:
    """One NPC's head mesh, with whatever identity could be recovered."""

    entry: object                   # the MeshEntry this was built from
    plugin: str                     # owning plugin, from the mesh path
    formid: int                     # low 24 bits
    name: str = ""                  # FULL, resolved; "" when unknown
    edid: str = ""                  # editor id; "" when unknown
    named_by: str = ""              # plugin the name came from

    @property
    def rel_key(self) -> str:
        return self.entry.rel_key

    @property
    def wins(self) -> bool:
        return bool(self.entry.wins)

    @property
    def source(self) -> str:
        return source_label(self.entry)

    @property
    def formid_hex(self) -> str:
        return f"{self.formid:06X}"


def npc_label(npc: NpcEntry) -> str:
    """Display name: the NPC's name, else its editor id, else the FormID."""
    return npc.name or npc.edid or npc.formid_hex


def build_npc_catalog(resolver, staging, modlist_path, data_dir, *,
                      extra_mods=(), cancel=None) -> list[NpcEntry]:
    """Every FaceGen head copy, named. Sorted by display name then source."""
    entries = build_catalog(resolver, staging, modlist_path, data_dir,
                            prefix=FACEGEN_PREFIX, exts=DEFAULT_EXTS,
                            extra_mods=extra_mods, cancel=cancel)
    if cancel and cancel():
        return []

    forms = _load_forms(staging, data_dir, entries, modlist_path, cancel)
    if cancel and cancel():
        return []
    strings = _StringsCache(data_dir)

    out: list[NpcEntry] = []
    for e in entries:
        got = facegen_npc(e.rel_key)
        if got is None:
            continue
        plugin, formid = got
        name, edid, named_by = _identify(plugin, formid, forms, strings)
        out.append(NpcEntry(entry=e, plugin=plugin, formid=formid,
                            name=name, edid=edid, named_by=named_by))
    out.sort(key=lambda n: (npc_label(n).lower(), n.source.lower()))
    return out


def _identify(plugin: str, formid: int, forms, strings):
    """Name/EDID for one FormID, from the first plugin that defines it.

    A record is keyed by the plugin its master index points at. An overriding
    patch that does NOT declare the original as a master keys the same NPC
    under its own name, so an exact-owner match alone would miss every rename
    a patch makes; the FormID is the fallback identity.
    """
    exact = (plugin, formid)
    name = edid = named_by = ""
    for pf in forms:
        for key in (exact, (pf.name, formid)):
            if not name:
                raw = pf.npc_name.get(key)
                if raw is not None:
                    # A localised FULL is a string id: resolve against ITS
                    # plugin's tables, not the mesh's - often different files.
                    text = resolve(raw, strings.get(pf.name) if pf.localised
                                   else None)
                    if text:
                        name, named_by = text, pf.name
            if not edid:
                edid = pf.npc_edid.get(key, "")
        if name and edid:
            break
    return name, edid, named_by


def _load_forms(staging, data_dir, entries, modlist_path=None,
                cancel=None) -> list:
    """Parse the plugins that could name these NPCs, mods before masters.

    Only plugins that are actually implicated are read: the mods providing a
    head, plus the plugin each mesh path names. Parsing all of a large
    profile's plugins would cost seconds for records nothing asks about.

    Mods are visited in MODLIST PRIORITY order, so when two mods define the
    same NPC the name comes from the one that also wins the mesh - a lower
    patch renaming an NPC must not be overruled by whatever sorted first.
    """
    wanted: list[Path] = []
    seen: set[str] = set()

    def add(path: Path):
        key = str(path).lower()
        if key not in seen and path.is_file():
            seen.add(key)
            wanted.append(path)

    providing = {e.mod for e in entries
                 if e.kind in (MOD_LOOSE, MOD_ARCHIVE) and e.mod}
    ranked = [m for m in _enabled_mods(modlist_path) if m in providing]
    mods = ranked + sorted(providing - set(ranked))
    if staging is not None:
        for mod in mods:
            mod_dir = Path(staging) / mod
            try:
                for p in sorted(mod_dir.iterdir()):
                    if p.suffix.lower() in _PLUGIN_EXTS:
                        add(p)
            except OSError:
                continue
    if cancel and cancel():
        return []

    # The plugin each FaceGen path is named for - usually a vanilla master.
    named = {got[0] for got in (facegen_npc(e.rel_key) for e in entries)
             if got}
    for owner in sorted(named):
        found = _find_named(owner, staging, mods, data_dir)
        if found is not None:
            add(found)

    out = []
    for p in wanted:
        if cancel and cancel():
            return []
        parsed = _parse_cached(p)
        if parsed is not None:
            out.append(parsed)
    return out


def _find_named(name: str, staging, mods, data_dir) -> "Path | None":
    """Locate a plugin by name: a providing mod first, then the data folder."""
    low = name.lower()
    if staging is not None:
        for mod in dict.fromkeys(mods):
            cand = Path(staging) / mod / name
            if cand.is_file():
                return cand
            try:
                for p in (Path(staging) / mod).iterdir():
                    if p.name.lower() == low and p.is_file():
                        return p
            except OSError:
                continue
    if data_dir is not None:
        cand = Path(data_dir) / name
        if cand.is_file():
            return cand
        try:
            for p in Path(data_dir).iterdir():
                if p.name.lower() == low and p.is_file():
                    return p
        except OSError:
            pass
    return None


class _StringsCache:
    """Per-plugin localised string tables, loaded on first use."""

    def __init__(self, data_dir):
        self._data = Path(data_dir) if data_dir else None
        self._tables: dict[str, dict] = {}
        self._archives = None

    def get(self, plugin_name: str) -> dict:
        if self._data is None:
            return {}
        hit = self._tables.get(plugin_name)
        if hit is None:
            from Utils.mesh_catalog import read_archive_member
            hit = load_tables(plugin_name, [self._data], read_archive_member,
                              self._interface_archives())
            self._tables[plugin_name] = hit
        return hit

    def _interface_archives(self) -> list:
        """Vanilla STRINGS live inside an archive, not loose on disk."""
        if self._archives is None:
            found = []
            try:
                for p in os.listdir(self._data):
                    if p.lower().endswith((".bsa", ".ba2")):
                        found.append(self._data / p)
            except OSError:
                pass
            # Interface archives hold the tables; try them before the rest.
            found.sort(key=lambda p: ("interface" not in p.name.lower(),
                                      p.name.lower()))
            self._archives = found
        return self._archives
