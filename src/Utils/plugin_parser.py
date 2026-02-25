"""
plugin_parser.py
Read master-file dependencies from Bethesda plugin headers (.esp/.esm/.esl).

Only the first record (TES4) is parsed — this contains MAST subrecords that
list the plugin's required master files.

Record layout (TES4):
    type    4 bytes   "TES4"
    datasize 4 bytes  uint32 LE  (size of subrecord block, excludes header)
    flags   4 bytes
    formID  4 bytes
    vc-info 8 bytes
    ------- 24 bytes total header, then `datasize` bytes of subrecords

Subrecord layout:
    type    4 bytes   e.g. "MAST", "DATA", "HEDR"
    size    2 bytes   uint16 LE
    data    `size` bytes
"""

from __future__ import annotations

import struct
from pathlib import Path


def read_masters(plugin_path: Path) -> list[str]:
    """
    Return the list of master filenames declared in a plugin's TES4 header.

    Returns an empty list on any error (missing file, corrupt header, etc.).
    """
    try:
        with plugin_path.open("rb") as f:
            # --- Record header (24 bytes) ---
            rec_header = f.read(24)
            if len(rec_header) < 24:
                return []

            rec_type = rec_header[0:4]
            if rec_type not in (b"TES4", b"TES3"):
                return []

            data_size = struct.unpack_from("<I", rec_header, 4)[0]

            # --- Subrecord block ---
            block = f.read(data_size)
            if len(block) < data_size:
                return []

            masters: list[str] = []
            offset = 0
            while offset + 6 <= data_size:
                sub_type = block[offset:offset + 4]
                sub_size = struct.unpack_from("<H", block, offset + 4)[0]
                offset += 6

                if offset + sub_size > data_size:
                    break

                if sub_type == b"MAST":
                    # Null-terminated string
                    raw = block[offset:offset + sub_size]
                    name = raw.rstrip(b"\x00").decode("utf-8", errors="replace")
                    if name:
                        masters.append(name)

                offset += sub_size

            return masters
    except (OSError, struct.error):
        return []


def check_missing_masters(
    plugin_names: list[str],
    plugin_paths: dict[str, Path],
) -> dict[str, list[str]]:
    """
    Check every plugin for missing master dependencies.

    Parameters
    ----------
    plugin_names : list[str]
        All plugin filenames in the current load order (enabled or not).
    plugin_paths : dict[str, Path]
        Mapping of lowercase plugin name → absolute path on disk.

    Returns
    -------
    dict[str, list[str]]
        Mapping of plugin name → list of missing master filenames.
        Only plugins that actually have missing masters are included.
    """
    known = {name.lower() for name in plugin_names}
    missing_map: dict[str, list[str]] = {}

    for plugin_name in plugin_names:
        path = plugin_paths.get(plugin_name.lower())
        if path is None or not path.is_file():
            continue

        masters = read_masters(path)
        missing = [m for m in masters if m.lower() not in known]
        if missing:
            missing_map[plugin_name] = missing

    return missing_map
