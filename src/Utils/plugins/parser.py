"""
Utils.plugins.parser
Read master-file dependencies from Bethesda plugin headers (.esp/.esm/.esl).

Only the first record (TES4/TES3) is parsed - this contains MAST subrecords
that list the plugin's required master files.

TES4 record layout (Oblivion and newer):
    type     4 bytes   "TES4"
    datasize 4 bytes   uint32 LE  (size of subrecord block, excludes header)
    flags    4 bytes
    formID   4 bytes
    vc-info  8 bytes
    -------- 24 bytes total header, then `datasize` bytes of subrecords

TES4 subrecord layout:
    type    4 bytes   e.g. "MAST", "DATA", "HEDR"
    size    2 bytes   uint16 LE
    data    `size` bytes

TES3 record layout (Morrowind):
    type     4 bytes   "TES3"
    datasize 4 bytes   uint32 LE  (size of subrecord block, excludes header)
    unknown  4 bytes
    flags    4 bytes
    -------- 16 bytes total header, then `datasize` bytes of subrecords

TES3 subrecord layout:
    type    4 bytes   e.g. "MAST", "DATA", "HEDR"
    size    4 bytes   uint32 LE   (NOT 2 bytes like TES4)
    data    `size` bytes
"""

from __future__ import annotations

import os
import struct
from pathlib import Path


_MASTERS_CACHE: dict[str, tuple[int, int, list[str]]] = {}
_MASTERS_CACHE_MAX = 8192


def _cache_get(path_str: str, mtime_ns: int, size: int) -> list[str] | None:
    entry = _MASTERS_CACHE.get(path_str)
    if entry is None:
        return None
    c_mtime, c_size, c_masters = entry
    if c_mtime == mtime_ns and c_size == size:
        return c_masters
    return None


def _cache_put(path_str: str, mtime_ns: int, size: int, masters: list[str]) -> None:
    if len(_MASTERS_CACHE) >= _MASTERS_CACHE_MAX:
        # Simple eviction: drop ~1/4 of entries
        for k in list(_MASTERS_CACHE.keys())[: _MASTERS_CACHE_MAX // 4]:
            _MASTERS_CACHE.pop(k, None)
    _MASTERS_CACHE[path_str] = (mtime_ns, size, masters)


def read_masters(plugin_path: Path) -> list[str]:
    """
    Return the list of master filenames declared in a plugin's TES4 header.

    Returns an empty list on any error (missing file, corrupt header, etc.).
    """
    path_str = str(plugin_path)
    try:
        st = os.stat(path_str)
        mtime_ns = st.st_mtime_ns
        size = st.st_size
    except OSError:
        return []

    cached = _cache_get(path_str, mtime_ns, size)
    if cached is not None:
        return cached

    try:
        with open(path_str, "rb") as f:
            # --- Record header ---
            # Read the first 8 bytes to determine type and subrecord block size,
            # then skip the rest of the record header before reading the block.
            # TES4 (Oblivion+): 24-byte header; TES3 (Morrowind): 16-byte header.
            rec_header = f.read(8)
            if len(rec_header) < 8:
                return []

            rec_type = rec_header[0:4]
            if rec_type == b"TES3":
                is_tes3 = True
                hdr_remaining = 8   # 16 total - 8 already read
            elif rec_type == b"TES4":
                is_tes3 = False
                hdr_remaining = 16  # 24 total - 8 already read
            else:
                return []

            data_size = struct.unpack_from("<I", rec_header, 4)[0]

            # Skip the rest of the record header to land at the subrecord block.
            f.read(hdr_remaining)

            # --- Subrecord block ---
            block = f.read(data_size)
            if len(block) < data_size:
                return []

            # TES3 subrecord header is 8 bytes (4-byte size field).
            # TES4 subrecord header is 6 bytes (2-byte size field).
            sub_hdr_size = 8 if is_tes3 else 6

            masters: list[str] = []
            offset = 0
            while offset + sub_hdr_size <= data_size:
                sub_type = block[offset:offset + 4]
                if is_tes3:
                    sub_size = struct.unpack_from("<I", block, offset + 4)[0]
                else:
                    sub_size = struct.unpack_from("<H", block, offset + 4)[0]
                offset += sub_hdr_size

                if offset + sub_size > data_size:
                    break

                if sub_type == b"MAST":
                    # Null-terminated string
                    raw = block[offset:offset + sub_size]
                    name = raw.rstrip(b"\x00").decode("utf-8", errors="replace")
                    if name:
                        masters.append(name)

                offset += sub_size

            _cache_put(path_str, mtime_ns, size, masters)
            return masters
    except (OSError, struct.error):
        return []


def read_masters_with_sizes(plugin_path: Path) -> dict[str, int]:
    """Return {master_filename: expected_size} from the plugin header.

    The DATA subrecord immediately following each MAST subrecord contains
    the file size (uint64 LE) of that master as recorded when the plugin
    was built. Only present in TES3 (Morrowind) format.

    Returns an empty dict on any error or for TES4+ plugins (which don't
    record master sizes in the same way).
    """
    try:
        with plugin_path.open("rb") as f:
            rec_header = f.read(8)
            if len(rec_header) < 8:
                return {}

            rec_type = rec_header[0:4]
            if rec_type == b"TES3":
                hdr_remaining = 8
            else:
                return {}  # Only meaningful for TES3

            data_size = struct.unpack_from("<I", rec_header, 4)[0]
            f.read(hdr_remaining)
            block = f.read(data_size)
            if len(block) < data_size:
                return {}

            result: dict[str, int] = {}
            last_mast: str | None = None
            offset = 0
            while offset + 8 <= data_size:
                sub_type = block[offset:offset + 4]
                sub_size = struct.unpack_from("<I", block, offset + 4)[0]
                offset += 8

                if offset + sub_size > data_size:
                    break

                if sub_type == b"MAST":
                    raw = block[offset:offset + sub_size]
                    last_mast = raw.rstrip(b"\x00").decode("utf-8", errors="replace")
                elif sub_type == b"DATA" and last_mast is not None:
                    if sub_size >= 8:
                        expected = struct.unpack_from("<Q", block, offset)[0]
                        result[last_mast] = expected
                    last_mast = None
                else:
                    last_mast = None

                offset += sub_size

            return result
    except (OSError, struct.error):
        return {}


# ---------------------------------------------------------------------------
# ESL (Light Master) flag helpers
# ---------------------------------------------------------------------------

# Bit in the TES4 record header flags field that marks a plugin as a master.
# The engine loads all master-flagged plugins before all non-masters,
# regardless of extension or plugins.txt position.
TES4_FLAG_MASTER = 0x0001

# Bit in the TES4 record header flags field that marks a plugin as "light".
# Introduced in Fallout 4; also supported by Skyrim SE/VR, Starfield, Enderal SE.
TES4_FLAG_ESL = 0x0200

# Starfield-only: marks a plugin as a "blueprint" (or BlueprintShips) plugin.
# Per esplugin (src/plugin.rs is_blueprint_plugin) and libloadorder
# (asterisk_based.rs save()), ALL blueprint plugins must be stripped from
# Plugins.txt - Starfield silently drops any blueprint plugin it sees there.
TES4_FLAG_BLUEPRINT = 0x0800

# Games that fully support the ESL flag (set by the game panel via supports_esl_flag).
# This constant is informational - the authoritative gate is the game property.
_ESL_SUPPORTED_GAME_IDS: frozenset[str] = frozenset({
    "Fallout4", "Fallout4VR",
    "SkyrimSE", "SkyrimAE", "skyrimvr",
    "Starfield",
    "enderalse",
})


# Header-flags cache, mtime+size keyed like _MASTERS_CACHE. Every plugin
# reload reads two flag bits (ESL + master) per plugin; without this, a
# 250-plugin load order costs 500 file opens per reload - and the plugin
# list reloads several times per profile switch.
_HDR_FLAGS_CACHE: dict[str, tuple[int, int, "int | None"]] = {}


def read_plugin_header_flags(plugin_path: Path) -> int | None:
    """Return the 32-bit flags from the TES4 record header (bytes 8–11).

    Returns ``None`` on any error or if the file is not a TES4-format plugin.
    Results are cached by (path, mtime, size)."""
    path_str = str(plugin_path)
    try:
        st = os.stat(path_str)
    except OSError:
        return None
    entry = _HDR_FLAGS_CACHE.get(path_str)
    if (entry is not None and entry[0] == st.st_mtime_ns
            and entry[1] == st.st_size):
        return entry[2]
    flags = None
    try:
        with open(path_str, "rb") as f:
            hdr = f.read(12)
            if len(hdr) >= 12 and hdr[0:4] == b"TES4":
                flags = struct.unpack_from("<I", hdr, 8)[0]
    except OSError:
        return None
    if len(_HDR_FLAGS_CACHE) >= _MASTERS_CACHE_MAX:
        for k in list(_HDR_FLAGS_CACHE.keys())[: _MASTERS_CACHE_MAX // 4]:
            _HDR_FLAGS_CACHE.pop(k, None)
    _HDR_FLAGS_CACHE[path_str] = (st.st_mtime_ns, st.st_size, flags)
    return flags


def is_esl_flagged(plugin_path: Path) -> bool:
    """Return ``True`` if the plugin has the ESL (light) bit set in its TES4 header."""
    flags = read_plugin_header_flags(plugin_path)
    return bool(flags is not None and (flags & TES4_FLAG_ESL))


def is_master_flagged(plugin_path: Path) -> bool:
    """Return ``True`` if the plugin has the master (ESM) bit set in its TES4 header."""
    flags = read_plugin_header_flags(plugin_path)
    return bool(flags is not None and (flags & TES4_FLAG_MASTER))


def is_blueprint_flagged(plugin_path: Path) -> bool:
    """Return ``True`` if the plugin has the Starfield Blueprint bit (0x800) set.

    Blueprint plugins must be omitted from Plugins.txt - Starfield strips any
    blueprint line at load time, and listing one causes the game to skip every
    plugin after it.
    """
    flags = read_plugin_header_flags(plugin_path)
    return bool(flags is not None and (flags & TES4_FLAG_BLUEPRINT))


def set_esl_flag(plugin_path: Path, enable: bool) -> bool:
    """Set or clear the ESL flag bit (``0x200``) in a TES4 plugin's header.

    Writes in-place - the plugin file must be writable.  Returns ``True`` on
    success, ``False`` if the file could not be opened/written or is not a
    TES4 plugin.
    """
    try:
        with plugin_path.open("r+b") as f:
            hdr = f.read(12)
            if len(hdr) < 12 or hdr[0:4] != b"TES4":
                return False
            flags = struct.unpack_from("<I", hdr, 8)[0]
            new_flags = (flags | TES4_FLAG_ESL) if enable else (flags & ~TES4_FLAG_ESL)
            if new_flags == flags:
                return True  # Nothing to do
            f.seek(8)
            f.write(struct.pack("<I", new_flags))
        return True
    except OSError:
        return False


def check_esl_eligible(plugin_path: Path, game_type_attr: str) -> bool:
    """Return ``True`` if libloot considers the plugin safe to ESL-flag.

    Delegates to ``LOOT.eligibility.check_esl_eligible`` - libloot's scan is
    stricter than a simple FormID-range walk: it also checks that every
    referenced FormID resolves correctly once the plugin sits in the 0xFE
    slot.

    ``game_type_attr`` is the libloot ``GameType`` attribute name (e.g.
    ``"SkyrimSE"``), typically ``self._game.loot_game_type`` from the GUI.

    Returns ``False`` if libloot is unavailable, the game type is unknown,
    or the plugin cannot be parsed.
    """
    try:
        from LOOT.eligibility import check_esl_eligible as _loot_check
    except ImportError:
        return False
    return _loot_check(plugin_path, game_type_attr)


def check_version_mismatched_masters(
    plugin_names: list[str],
    plugin_paths: dict[str, Path],
    data_dir: Path,
) -> dict[str, list[str]]:
    """Check for masters that are present but whose file size doesn't match
    the size recorded in the plugin header (version mismatch).

    Only meaningful for TES3 (Morrowind) plugins. Returns {} for TES4+.

    Parameters
    ----------
    plugin_names : list[str]
        Enabled plugin filenames in load order.
    plugin_paths : dict[str, Path]
        Mapping of lowercase plugin name → absolute path on disk.
    data_dir : Path
        The game's Data Files directory where masters are deployed.

    Returns
    -------
    dict[str, list[str]]
        Mapping of plugin name → list of master filenames with size mismatches.
    """
    mismatch_map: dict[str, list[str]] = {}

    for plugin_name in plugin_names:
        path = plugin_paths.get(plugin_name.lower())
        if path is None or not path.is_file():
            continue

        masters_with_sizes = read_masters_with_sizes(path)
        if not masters_with_sizes:
            continue

        mismatched: list[str] = []
        for master_name, expected_size in masters_with_sizes.items():
            # Find the master file on disk (case-insensitive)
            master_lower = master_name.lower()
            master_path: Path | None = None
            if data_dir.is_dir():
                for f in data_dir.iterdir():
                    if f.name.lower() == master_lower:
                        master_path = f
                        break
            if master_path is None or not master_path.is_file():
                continue  # Missing masters handled separately
            actual_size = master_path.stat().st_size
            if actual_size != expected_size:
                mismatched.append(master_name)

        if mismatched:
            mismatch_map[plugin_name] = mismatched

    return mismatch_map


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


def check_late_masters(
    plugin_names: list[str],
    plugin_paths: dict[str, Path],
) -> dict[str, list[str]]:
    """
    Check for masters that are present in the load order but loaded *after*
    the plugin that requires them (master loaded after dependent).

    Parameters
    ----------
    plugin_names : list[str]
        Enabled plugin filenames in load order (index = position).
    plugin_paths : dict[str, Path]
        Mapping of lowercase plugin name → absolute path on disk.

    Returns
    -------
    dict[str, list[str]]
        Mapping of plugin name → list of master filenames that appear later
        in the load order than the plugin itself.
        Only plugins with at least one late master are included.
    """
    index_map = {name.lower(): i for i, name in enumerate(plugin_names)}
    late_map: dict[str, list[str]] = {}

    for i, plugin_name in enumerate(plugin_names):
        path = plugin_paths.get(plugin_name.lower())
        if path is None or not path.is_file():
            continue

        masters = read_masters(path)
        late = [m for m in masters if index_map.get(m.lower(), -1) > i]
        if late:
            late_map[plugin_name] = late

    return late_map
