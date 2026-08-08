"""
Toolkit-neutral icon extraction from Windows PE executables.

The play-bar dropdown shows the game's logo for the game entry and each custom
exe's OWN icon for the exe entries. Custom exes are Windows .exe files, so their
icon lives in the PE resource section (RT_GROUP_ICON → RT_ICON). This module
parses that out and returns a standalone .ico byte blob the GUI layer can hand
to QIcon/QPixmap - no Qt dependency here so Utils stays toolkit-neutral.

Returns None (never raises) for non-PE files (.bat, ELF), icon-less exes, or any
parse failure; the caller falls back to a generic glyph.
"""

from __future__ import annotations

import struct
from pathlib import Path

# Resource type IDs (see winuser.h).
_RT_ICON = 3
_RT_GROUP_ICON = 14

# Cap the work we do on hostile/corrupt files.
_MAX_FILE = 128 * 1024 * 1024
_MAX_ICONS = 32


def extract_exe_icon(path: Path) -> "bytes | None":
    """Return the first icon in *path* as .ico file bytes, or None.

    Reads the whole file into memory (exe icons live near the resource section
    which can be anywhere), so it's capped at _MAX_FILE. Only handles PE32/PE32+.
    """
    try:
        p = Path(path)
        if p.suffix.lower() != ".exe" or not p.is_file():
            return None
        if p.stat().st_size > _MAX_FILE:
            return None
        data = p.read_bytes()
        return _extract(data)
    except Exception:
        return None


def extract_exe_version(path: Path) -> str:
    """Return the exe's file version ("1.6.1170.0") from VS_VERSIONINFO, or ""."""
    try:
        p = Path(path)
        if p.suffix.lower() != ".exe" or not p.is_file():
            return ""
        if p.stat().st_size > _MAX_FILE:
            return ""
        data = p.read_bytes()
        parser = _make_parser(data)
        if parser is None:
            return ""
        ver_dir = parser._find_type_dir(16)      # RT_VERSION
        if ver_dir is None:
            return ""
        leaf = parser._first_data_entry(ver_dir)
        if leaf is None:
            return ""
        rva, size = leaf
        off = parser.rva_to_off(rva)
        if off is None or off + size > len(data):
            return ""
        blob = data[off:off + size]
        # VS_FIXEDFILEINFO: locate by signature, dwFileVersionMS/LS at +8/+12.
        i = blob.find(struct.pack("<I", 0xFEEF04BD))
        if i < 0 or i + 16 > len(blob):
            return ""
        ms, ls = struct.unpack_from("<II", blob, i + 8)
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return ""


def _extract(data: bytes) -> "bytes | None":
    parser = _make_parser(data)
    return parser.build_first_icon() if parser is not None else None


def _make_parser(data: bytes) -> "_ResourceParser | None":
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 24 > len(data) or data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        return None

    coff = e_lfanew + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    size_opt = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    if opt + 2 > len(data):
        return None
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic == 0x10B:        # PE32
        num_dirs_off = opt + 92
        dir_off = opt + 96
    elif magic == 0x20B:      # PE32+
        num_dirs_off = opt + 108
        dir_off = opt + 112
    else:
        return None

    num_dirs = struct.unpack_from("<I", data, num_dirs_off)[0]
    # Data directory entry 2 = resource table.
    if num_dirs <= 2:
        return None
    rsrc_rva, rsrc_size = struct.unpack_from("<II", data, dir_off + 2 * 8)
    if rsrc_rva == 0:
        return None

    sections = []
    sec_base = opt + size_opt
    for i in range(num_sections):
        s = sec_base + i * 40
        if s + 40 > len(data):
            break
        virt_addr = struct.unpack_from("<I", data, s + 12)[0]
        raw_size = struct.unpack_from("<I", data, s + 16)[0]
        raw_ptr = struct.unpack_from("<I", data, s + 20)[0]
        sections.append((virt_addr, raw_size, raw_ptr))

    def rva_to_off(rva: int) -> "int | None":
        for virt_addr, raw_size, raw_ptr in sections:
            if virt_addr <= rva < virt_addr + raw_size:
                return raw_ptr + (rva - virt_addr)
        return None

    rsrc_off = rva_to_off(rsrc_rva)
    if rsrc_off is None:
        return None

    return _ResourceParser(data, rsrc_off, rsrc_rva, rva_to_off)


class _ResourceParser:
    """Walks the .rsrc tree to pull RT_GROUP_ICON + its RT_ICON members."""

    def __init__(self, data, rsrc_off, rsrc_rva, rva_to_off):
        self.data = data
        self.rsrc_off = rsrc_off      # file offset of resource section start
        self.rsrc_rva = rsrc_rva
        self.rva_to_off = rva_to_off

    # -- directory walking --------------------------------------------------
    def _entries(self, dir_rva_off: int):
        """Yield (id_or_name, is_dir, offset_to_child) for a resource dir.

        *dir_rva_off* is a file offset (relative resource offsets are added to
        the resource section base, per the PE spec).
        """
        base = self.rsrc_off + dir_rva_off
        data = self.data
        if base + 16 > len(data):
            return
        num_named = struct.unpack_from("<H", data, base + 12)[0]
        num_id = struct.unpack_from("<H", data, base + 14)[0]
        first = base + 16
        for i in range(num_named + num_id):
            e = first + i * 8
            if e + 8 > len(data):
                return
            name_or_id = struct.unpack_from("<I", data, e)[0]
            offset = struct.unpack_from("<I", data, e + 4)[0]
            is_named = bool(name_or_id & 0x80000000)
            is_dir = bool(offset & 0x80000000)
            child = offset & 0x7FFFFFFF
            yield (None if is_named else name_or_id), is_dir, child

    def _first_data_entry(self, dir_off: int):
        """Descend a resource dir to its first data leaf; return (rva, size)."""
        for _id, is_dir, child in self._entries(dir_off):
            if is_dir:
                return self._first_data_entry(child)
            base = self.rsrc_off + child
            if base + 8 > len(self.data):
                return None
            data_rva, size = struct.unpack_from("<II", self.data, base)
            return data_rva, size
        return None

    def _find_type_dir(self, type_id: int):
        """Return the child dir offset for a top-level resource type id."""
        for tid, is_dir, child in self._entries(0):
            if tid == type_id and is_dir:
                return child
        return None

    def _icon_data_by_id(self, icon_dir_off: int):
        """Map RT_ICON id → (bytes) for every icon image resource."""
        out = {}
        for icon_id, is_dir, child in self._entries(icon_dir_off):
            if not is_dir or icon_id is None:
                continue
            leaf = self._first_data_entry(child)
            if leaf is None:
                continue
            rva, size = leaf
            off = self.rva_to_off(rva)
            if off is None or off + size > len(self.data):
                continue
            out[icon_id] = self.data[off:off + size]
        return out

    # -- assembling the .ico ------------------------------------------------
    def build_first_icon(self) -> "bytes | None":
        group_dir = self._find_type_dir(_RT_GROUP_ICON)
        icon_dir = self._find_type_dir(_RT_ICON)
        if group_dir is None or icon_dir is None:
            return None

        # Grab the first group's directory blob (GRPICONDIR).
        leaf = self._first_data_entry(group_dir)
        if leaf is None:
            return None
        rva, size = leaf
        off = self.rva_to_off(rva)
        if off is None or off + size > len(self.data):
            return None
        grp = self.data[off:off + size]
        if len(grp) < 6:
            return None

        count = struct.unpack_from("<H", grp, 4)[0]
        if count == 0 or count > _MAX_ICONS:
            return None

        images = self._icon_data_by_id(icon_dir)

        # GRPICONDIRENTRY is 14 bytes; ICONDIRENTRY (on disk) is 16 bytes and
        # ends in a 4-byte file offset instead of the 2-byte resource id.
        entries = []
        blobs = []
        data_offset = 6 + count * 16
        for i in range(count):
            e = 6 + i * 14
            if e + 14 > len(grp):
                break
            width, height, colors, _res = struct.unpack_from("<BBBB", grp, e)
            planes, bitcount, bytes_in_res = struct.unpack_from("<HHI", grp, e + 4)
            icon_id = struct.unpack_from("<H", grp, e + 12)[0]
            blob = images.get(icon_id)
            if not blob:
                continue
            entries.append(struct.pack(
                "<BBBBHHII", width, height, colors, 0,
                planes, bitcount, len(blob), data_offset))
            blobs.append(blob)
            data_offset += len(blob)

        if not blobs:
            return None

        header = struct.pack("<HHH", 0, 1, len(blobs))
        return header + b"".join(entries) + b"".join(blobs)
