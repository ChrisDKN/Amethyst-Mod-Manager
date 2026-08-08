"""
pak_reader.py
Read metadata from Baldur's Gate 3 .pak files (Larian LSPK v15/v16/v18).

Extracts the meta.lsx XML from inside a .pak archive without needing
lslib or any external tools - only the ``lz4`` Python package is required.

LSPK v18 header (40 bytes):
    4B  signature ("LSPK")
    4B  version (18)
    8B  file_list_offset
    4B  file_list_size
    1B  flags
    1B  priority
   16B  md5
    2B  num_parts
v18 entry (272 bytes):
  256B name | 4B offset_lo | 2B offset_hi | 1B part | 1B flags | 4B size_disk | 4B unc_size

LSPK v15/v16 header (40 bytes):
    4B  signature ("LSPK")
    4B  version (15 or 16)
    8B  file_list_offset
    4B  file_list_size
    2B  num_parts (v16 only; reserved on v15)
    1B  flags
    1B  priority
   16B  md5  (v16 only; v15 ends at byte 24)
v15/v16 entry (296 bytes):
  256B name | 8B offset | 8B size_disk | 8B unc_size | 4B part | 4B flags | 4B crc | 4B unused
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

try:
    import lz4.block as _lz4
except ImportError:
    _lz4 = None  # type: ignore[assignment]

try:
    import zstandard as _zstd
except ImportError:
    _zstd = None  # type: ignore[assignment]

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"  # 0xFD2FB528 little-endian

_LSPK_SIGNATURE = 0x4B50534C  # "LSPK" little-endian
_HEADER_SIZE = 40
_ENTRY_SIZE = 272
_ENTRY_SIZE_V15 = 296


def _require_lz4() -> None:
    if _lz4 is None:
        raise ImportError(
            "The 'lz4' package is required to read BG3 .pak files.\n"
            "Install it with:  pip install lz4"
        )


def _lz4_decompress_resilient(data: bytes, uncompressed_size: int) -> bytes:
    """Decompress LZ4 data, retrying with larger buffers if the stored size is wrong.

    Some mod authors produce PAK files where the stored uncompressed_size is
    zero, too small, or otherwise inaccurate.  We first try relative multiples
    of the stored value, then fall back to a range of absolute sizes so that
    even a completely wrong hint still succeeds.
    """
    candidates: list[int] = []

    if uncompressed_size > 0:
        # Try the stored hint and small multiples first.
        for mult in (1, 2, 4, 8, 16, 32):
            candidates.append(uncompressed_size * mult)

    # Absolute fallback sizes: 64 KB → 128 MB in powers of two.
    for exp in range(16, 28):  # 65536 … 134217728
        candidates.append(1 << exp)

    last_exc: Exception | None = None
    seen: set[int] = set()
    for size in candidates:
        if size in seen:
            continue
        seen.add(size)
        try:
            return _lz4.decompress(data, uncompressed_size=size)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc

    raise ValueError(f"LZ4 decompression failed after retries: {last_exc}") from last_exc


def _decompress(data: bytes, flags: int, uncompressed_size: int) -> bytes:
    """Decompress a chunk according to LSPK compression flags.

    Newer versions of Larian's packing tools use zstd for entries even when
    the flags field may nominally indicate LZ4/LZ4HC (method 3 was reassigned
    to zstd in recent tooling).  We detect by magic bytes so both old and new
    archives work correctly.
    """
    method = flags & 0x0F
    if method == 0:
        return data
    if method == 1:
        return zlib.decompress(data)
    # Magic-byte detection overrides the stored method: newer Larian tools
    # write zstd-compressed data regardless of the flag nibble value.
    if len(data) >= 4 and data[:4] == _ZSTD_MAGIC:
        if _zstd is None:
            raise ImportError(
                "The 'zstandard' package is required to read this .pak file.\n"
                "Install it with:  pip install zstandard"
            )
        dctx = _zstd.ZstdDecompressor()
        max_out = max(uncompressed_size * 4, 1 << 20)  # at least 1 MiB headroom
        return dctx.decompress(data, max_output_size=max_out)
    if method in (2, 3):
        # 2 = LZ4, 3 = LZ4HC - decompression is identical for both
        _require_lz4()
        return _lz4_decompress_resilient(data, uncompressed_size)
    raise ValueError(f"Unknown LSPK compression method: {method}")


# Proper mod metas live at Mods/<Folder>/meta.lsx inside the archive.
_MODS_META_RE = re.compile(r"^mods/([^/]+)/meta\.lsx$")


def _choose_meta(candidates: list[tuple[str, tuple]], pak_path: Path) -> tuple | None:
    """Pick which meta.lsx entry to use when a pak contains several.

    Some paks ship extra meta.lsx files that override base-game modules
    (e.g. party-size mods carrying a Mods/GustavX/meta.lsx copy).  Mirrors
    BG3 Mod Manager: prefer entries anchored at Mods/<Folder>/meta.lsx, and
    among those prefer the one whose <Folder> appears in the pak filename.
    """
    if not candidates:
        return None
    anchored: list[tuple[str, tuple]] = []
    for name, entry in candidates:
        m = _MODS_META_RE.match(name.replace("\\", "/").lower())
        if m:
            anchored.append((m.group(1), entry))
    if not anchored:
        return candidates[0][1]
    if len(anchored) > 1:
        pak_lower = pak_path.stem.lower()
        for folder, entry in anchored:
            if folder in pak_lower:
                return entry
    return anchored[0][1]


def _decode_meta_content(content: bytes) -> str:
    # Some PAKs wrap meta.lsx in an extra zlib layer (zlib magic 0x78 ??).
    if len(content) >= 2 and content[0] == 0x78 and content[1] in (
        0x01, 0x5E, 0x9C, 0xDA
    ):
        try:
            content = zlib.decompress(content)
        except zlib.error:
            pass
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1")


# A file-list entry: (name, offset, size_on_disk, flags, uncompressed_size, part)
_PakEntry = tuple[str, int, int, int, int, int]


def _entry_name(file_list: bytes, base: int) -> str:
    name_bytes = file_list[base : base + 256]
    nul = name_bytes.find(b"\x00")
    return (name_bytes[:nul] if nul >= 0 else name_bytes).decode("utf-8")


def _read_entries_v18(f) -> list[_PakEntry]:
    f.seek(8)
    file_list_offset = struct.unpack("<Q", f.read(8))[0]

    f.seek(file_list_offset)
    num_files = struct.unpack("<I", f.read(4))[0]
    compressed_size = struct.unpack("<I", f.read(4))[0]
    compressed_data = f.read(compressed_size)

    file_list = _lz4_decompress_resilient(
        compressed_data, num_files * _ENTRY_SIZE
    )

    entries: list[_PakEntry] = []
    for i in range(num_files):
        base = i * _ENTRY_SIZE
        name = _entry_name(file_list, base)
        offset_low = struct.unpack_from("<I", file_list, base + 256)[0]
        offset_high = struct.unpack_from("<H", file_list, base + 260)[0]
        file_offset = offset_low | (offset_high << 32)
        part = file_list[base + 262]
        entry_flags = file_list[base + 263]
        size_on_disk = struct.unpack_from("<I", file_list, base + 264)[0]
        unc_size = struct.unpack_from("<I", file_list, base + 268)[0]
        entries.append((name, file_offset, size_on_disk, entry_flags, unc_size, part))
    return entries


def _read_entries_v15(f) -> list[_PakEntry]:
    # v15/v16 share the same entry layout (296 B); the file-list lives at
    # the offset given in the header at bytes 8..15.
    f.seek(8)
    file_list_offset = struct.unpack("<Q", f.read(8))[0]

    f.seek(file_list_offset)
    num_files = struct.unpack("<I", f.read(4))[0]
    compressed_size = struct.unpack("<I", f.read(4))[0]
    compressed_data = f.read(compressed_size)

    file_list = _lz4_decompress_resilient(
        compressed_data, num_files * _ENTRY_SIZE_V15
    )

    entries: list[_PakEntry] = []
    for i in range(num_files):
        base = i * _ENTRY_SIZE_V15
        name = _entry_name(file_list, base)
        file_offset  = struct.unpack_from("<Q", file_list, base + 256)[0]
        size_on_disk = struct.unpack_from("<Q", file_list, base + 264)[0]
        unc_size     = struct.unpack_from("<Q", file_list, base + 272)[0]
        part         = struct.unpack_from("<I", file_list, base + 280)[0]
        entry_flags  = struct.unpack_from("<I", file_list, base + 284)[0] & 0xFF
        entries.append((name, file_offset, size_on_disk, entry_flags, unc_size, part))
    return entries


def _read_entries(f, pak_path: Path) -> list[_PakEntry]:
    f.seek(0)
    sig_bytes = f.read(8)
    if len(sig_bytes) < 8:
        raise ValueError(f"File too small to be an LSPK archive: {pak_path}")
    sig, version = struct.unpack("<II", sig_bytes)
    if sig != _LSPK_SIGNATURE:
        raise ValueError(
            f"Not an LSPK file (bad signature 0x{sig:08X}): {pak_path}"
        )
    if version >= 18:
        return _read_entries_v18(f)
    if version in (15, 16):
        return _read_entries_v15(f)
    raise ValueError(f"Unsupported LSPK version {version}: {pak_path}")


def _read_entry_content(f, entry: _PakEntry) -> bytes:
    _name, offset, size_on_disk, entry_flags, unc_size, _part = entry
    f.seek(offset)
    raw = f.read(size_on_disk)
    return _decompress(raw, entry_flags, unc_size)


@dataclass
class PakInfo:
    """Everything modsettings needs from one .pak, read in a single pass."""
    meta_xml: str | None = None
    file_names: list[str] = field(default_factory=list)
    # Decoded ScriptExtender/Config.json text, if the pak ships one.
    se_config: str | None = None


def read_pak_info(pak_path: Path | str) -> PakInfo:
    """Read the file list, the chosen meta.lsx, and any SE config from a .pak.

    Supports LSPK v15, v16, and v18.  Raises on format errors or missing
    dependencies.  Entries stored in secondary archive parts (multi-part
    paks) are listed in ``file_names`` but never extracted.
    """
    _require_lz4()
    pak_path = Path(pak_path)
    info = PakInfo()

    with pak_path.open("rb") as f:
        entries = _read_entries(f, pak_path)

        meta_candidates: list[tuple[str, _PakEntry]] = []
        se_entry: _PakEntry | None = None
        for entry in entries:
            name = entry[0]
            info.file_names.append(name)
            if entry[5] != 0:
                continue  # content lives in another archive part - can't read
            name_lower = name.lower()
            if name_lower.endswith("meta.lsx"):
                meta_candidates.append((name, entry))
            elif se_entry is None and "scriptextender/config.json" in name_lower:
                se_entry = entry

        chosen = _choose_meta(meta_candidates, pak_path)
        if chosen is not None:
            info.meta_xml = _decode_meta_content(_read_entry_content(f, chosen))
        if se_entry is not None:
            try:
                info.se_config = _decode_meta_content(_read_entry_content(f, se_entry))
            except Exception:
                info.se_config = None

    return info


def is_lspk_file(pak_path: Path | str) -> bool:
    """True when *pak_path* starts with the LSPK signature (a BG3 pak)."""
    try:
        with Path(pak_path).open("rb") as f:
            return f.read(4) == b"LSPK"
    except OSError:
        return False


def read_lspk_file_list(pak_path: Path | str) -> list[str]:
    """Return every file path inside a BG3 .pak as forward-slash strings.

    TOC only - no entry data is decompressed (the file list itself is LZ4
    compressed, so the ``lz4`` package is still needed). Names keep their
    stored casing. Entries living in secondary archive parts are listed too.
    Returns an empty list on unrecognised formats, missing dependencies or
    I/O errors, mirroring bsa_reader.read_bsa_file_list.
    """
    try:
        _require_lz4()
        pak_path = Path(pak_path)
        with pak_path.open("rb") as f:
            entries = _read_entries(f, pak_path)
    except (OSError, struct.error, ValueError, OverflowError, MemoryError,
            ImportError):
        return []
    names: set[str] = set()
    for entry in entries:
        name = entry[0].replace("\\", "/").lstrip("/")
        if name:
            names.add(name)
    return sorted(names)


def extract_meta_lsx(pak_path: Path | str) -> str | None:
    """Open a BG3 .pak and return the contents of meta.lsx as a string.

    Supports LSPK v15, v16, and v18.  Returns None if the archive does not
    contain a meta.lsx file.  Raises on format errors or missing dependencies.
    """
    return read_pak_info(pak_path).meta_xml
