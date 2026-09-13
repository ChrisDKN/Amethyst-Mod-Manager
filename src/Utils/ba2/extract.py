"""
ba2/extract.py
Pure-Python BA2 (Bethesda Archive 2) extractor for Fallout 4.

Handles both GNRL (general files) and DX10 (DDS textures), using zlib
decompression where applicable.  DX10 reassembly synthesises a standard
DDS header from the per-record metadata (height, width, mip count, DXGI
format) and concatenates the per-mip chunks into the body - yielding a
loadable .dds file on disk.

Sister to ``ba2.writer`` (which emits both GNRL and DX10 archives).
Both share the ``ba2_hash`` and filtering policy from the writer.

Output paths mirror the BA2 reader: lowercase forward-slash relative
paths, written under *dest_dir* preserving the archive's stored folder
structure.

References:
    * Empirical inspection of vanilla FO4 BA2s - verified header fields
      and chunk layout against HorseArmor (GNRL), DLCworkshop03 -
      Textures (DX10), and DLCNukaWorld - Voices_en (GNRL).
    * bsa_reader._read_ba2 in this repo - the read-list path we extend
      here.
    * Microsoft DDS specification (DDS_HEADER, DDS_HEADER_DXT10).
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Callable


class Ba2ExtractError(Exception):
    """Raised when extraction fails.  Already-written files stay on disk;
    the caller may clean up *dest_dir* on failure if desired."""


def _make_dds_header(*, height: int, width: int, mip_count: int,
                     dxgi_format: int, pitch_or_linear_size: int = 0,
                     cube_map: bool = False, legacy: bool = False) -> bytes:
    from .writer import _mip_byte_size, _DXGI_BLOCK_8, _DXGI_BLOCK_16, _LEGACY_MASKS
    if not 1 <= width <= 16384 or not 1 <= height <= 16384 or not 0 <= mip_count <= 32:
        raise Ba2ExtractError("Invalid DDS dimensions or mip count")
    compressed = dxgi_format in _DXGI_BLOCK_8 or dxgi_format in _DXGI_BLOCK_16
    pitch = _mip_byte_size(width, height if compressed else 1, dxgi_format)
    if pitch is None:
        raise Ba2ExtractError(f"Unsupported DDS format: {dxgi_format}")
    flags = 0x1007 | (0x80000 if compressed else 8) | (0x20000 if mip_count > (0 if legacy else 1) else 0)
    caps = 0x1000 | (0x400008 if mip_count > 1 else 0) | (8 if cube_map else 0)
    pixel = struct.pack("<II4s5I", 32, 4, b"DX10", 0, 0, 0, 0, 0)
    extension = struct.pack("<5I", dxgi_format, 3, 4 if cube_map else 0, 1, 0)
    if legacy:
        fourcc = {71: b"DXT1", 74: b"DXT3", 77: b"DXT5", 80: b"BC4U",
                  81: b"BC4S", 83: b"BC5U", 84: b"BC5S", 68: b"RGBG",
                  69: b"GRGB", 107: b"YUY2"}.get(dxgi_format)
        masks = next((fields for fields, fmt in _LEGACY_MASKS.items() if fmt == dxgi_format), None)
        if fourcc:
            pixel = struct.pack("<II4s5I", 32, 4, fourcc, 0, 0, 0, 0, 0)
            extension = b""
        elif masks:
            pixel = struct.pack("<8I", 32, masks[0], 0, *masks[1:])
            extension = b""
    header = struct.pack("<7I11I32s5I", 124, flags, height, width, pitch, int(legacy), max(1, mip_count),
                         *([0] * 11), pixel, caps, 0xfe00 if cube_map else 0, 0, 0, 0)
    return b"DDS " + header + extension


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]


def extract_ba2(
    ba2_path: Path,
    dest_dir: Path,
    *,
    overwrite: bool = True,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> tuple[int, list[str]]:
    """Extract every file in *ba2_path* to *dest_dir*.

    Args:
        ba2_path:  Path to a BA2 archive (GNRL or DX10).
        dest_dir:  Output root.  Created if missing.  Files land at the
                   archive's stored relative path, lowercase
                   forward-slash.
        overwrite: If True (default), pre-existing files are
                   overwritten.  ``False`` skips files that already
                   exist on disk.
        progress:  Optional ``(done, total, current_path)`` callback.
        cancel:    Optional callback returning True to abort.

    Returns:
        ``(file_count_written, list_of_rel_paths)``.

    Raises:
        Ba2ExtractError: on I/O / format failure or unsupported version.
    """
    ba2_path = Path(ba2_path)
    dest_dir = Path(dest_dir)
    if not ba2_path.is_file():
        raise Ba2ExtractError(f"archive does not exist: {ba2_path}")

    try:
        with ba2_path.open("rb") as f:
            return _extract(f, dest_dir, overwrite, progress, cancel)
    except Ba2ExtractError:
        raise
    except (OSError, struct.error, zlib.error, ValueError) as exc:
        raise Ba2ExtractError(
            f"failed to extract {ba2_path.name}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Internal: parse + write
# ---------------------------------------------------------------------------

def index_ba2(ba2_path: Path | str) -> dict[str, dict]:
    """Map an archive's contents without reading any file data.

    Returns a mapping of lowercase forward-slash internal path to the record
    dict that :func:`read_ba2_entry` consumes.
    """
    with open(ba2_path, "rb") as f:
        records, names = _parse_records(f)
    return dict(zip(names, records))


def read_ba2_entry(ba2_path: Path | str, rec: dict) -> bytes:
    """Read one file, decompressing (GNRL) or rebuilding its DDS (DX10)."""
    try:
        with open(ba2_path, "rb") as f:
            if rec["type"] == "GNRL":
                return _read_gnrl(f, rec)
            return _read_dx10(f, rec)
    except (OSError, struct.error, ValueError, zlib.error) as exc:
        raise Ba2ExtractError(f"failed to read from {ba2_path}: {exc}") from exc


def _extract(
    f,
    dest_dir: Path,
    overwrite: bool,
    progress: ProgressCb | None,
    cancel: CancelCb | None,
) -> tuple[int, list[str]]:
    records, names = _parse_records(f)

    # --- Extract each file ---
    written: list[str] = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    total = len(records)

    for done, (rec, rel) in enumerate(zip(records, names), start=1):
        if cancel is not None and cancel():
            raise Ba2ExtractError("cancelled")

        out_path = dest_dir / rel
        if not overwrite and out_path.exists():
            if progress is not None:
                progress(done, total, rel)
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if rec["type"] == "GNRL":
            data = _read_gnrl(f, rec)
        else:
            data = _read_dx10(f, rec)

        with out_path.open("wb") as out:
            out.write(data)
        written.append(rel)

        if progress is not None:
            progress(done, total, rel)

    return len(written), written


def _parse_records(f, names_override=None) -> tuple[list[dict], list[str]]:
    """Walk the header, file records and name table; read no file data."""
    magic = f.read(4)
    if magic != b"BTDX":
        raise Ba2ExtractError(f"not a BA2 archive (magic={magic!r})")
    rest = f.read(20)
    if len(rest) < 20:
        raise Ba2ExtractError("truncated BA2 header")
    version, type_tag, file_count, name_table_offset = struct.unpack(
        "<I4sIQ", rest,
    )
    if type_tag not in (b"GNRL", b"DX10"):
        raise Ba2ExtractError(f"unsupported BA2 type {type_tag!r}")
    if version not in (1, 2, 3, 7, 8):
        raise Ba2ExtractError(f"unsupported BA2 version {version}")
    compression = 0
    if version in (2, 3):
        if len(f.read(8)) != 8:
            raise Ba2ExtractError("truncated extended BA2 header")
    if version == 3:
        raw_compression = f.read(4)
        if len(raw_compression) != 4:
            raise Ba2ExtractError("truncated BA2 compression field")
        compression = struct.unpack("<I", raw_compression)[0]
        if compression not in (0, 1, 3):
            raise Ba2ExtractError(f"unsupported BA2 compression {compression}")
    end = f.seek(0, 2)
    header_end = 36 if version == 3 else 32 if version == 2 else 24
    if file_count > 1_000_000 or file_count * 24 > end or name_table_offset > end:
        raise Ba2ExtractError("invalid BA2 file count or name table offset")
    f.seek(header_end)

    # --- Read the file records ---
    records: list[dict] = []
    if type_tag == b"GNRL":
        # 36 bytes each
        for _ in range(file_count):
            buf = f.read(36)
            if len(buf) < 36:
                raise Ba2ExtractError("truncated GNRL record")
            (name_hash, ext, dir_hash, _flags, data_offset,
             packed_size, unpacked_size, _end_marker) = struct.unpack(
                "<I4sIIQIII", buf
            )
            records.append({
                "type": "GNRL",
                "data_offset": data_offset,
                "packed_size": packed_size,
                "unpacked_size": unpacked_size,
            })
    else:  # DX10
        for _ in range(file_count):
            hdr = f.read(24)
            if len(hdr) < 24:
                raise Ba2ExtractError("truncated DX10 record header")
            (_name_hash, _ext, _dir_hash, _unk1, num_chunks, _chunk_size,
             height, width, num_mips, dxgi_format,
             _unk16) = struct.unpack("<I4sIBBHHHBBH", hdr)
            chunks: list[dict] = []
            for _c in range(num_chunks):
                cb = f.read(24)
                if len(cb) < 24:
                    raise Ba2ExtractError("truncated DX10 chunk header")
                (data_offset, packed_size, unpacked_size,
                 _start_mip, _end_mip, _end_marker) = struct.unpack(
                    "<QIIHHI", cb
                )
                chunks.append({
                    "data_offset": data_offset,
                    "packed_size": packed_size,
                    "unpacked_size": unpacked_size,
                })
            records.append({
                "type": "DX10",
                "height": height,
                "width": width,
                "num_mips": num_mips,
                "dxgi_format": dxgi_format,
                "cube_map": bool(_unk16 & 0xff),
                "compression": compression,
                "chunks": chunks,
            })

    # --- Read the name table ---
    if not name_table_offset:
        if names_override is None or len(names_override) != len(records):
            raise Ba2ExtractError("BA2 archive has no filename table")
        return records, names_override
    f.seek(name_table_offset)
    names: list[str] = []
    for _ in range(file_count):
        ln_raw = f.read(2)
        if len(ln_raw) < 2:
            raise Ba2ExtractError("truncated name table")
        ln = struct.unpack("<H", ln_raw)[0]
        nb = f.read(ln)
        if len(nb) < ln:
            raise Ba2ExtractError("truncated name entry")
        # Names are case-insensitive on the engine side; we always emit
        # lowercase to match what the loader actually consumes.
        try:
            name = nb.decode("utf-8")
        except UnicodeError:
            name = nb.decode("cp1252")
        names.append(name.replace("\\", "/").lower())

    return records, names


def _read_gnrl(f, rec: dict) -> bytes:
    """Read one GNRL file's bytes, decompressing if needed."""
    f.seek(rec["data_offset"])
    if rec["packed_size"] == 0:
        # Uncompressed - read unpacked_size bytes verbatim.
        return f.read(rec["unpacked_size"])
    body = f.read(rec["packed_size"])
    return zlib.decompress(body)


def _read_dx10(f, rec: dict) -> bytes:
    """Reassemble a DDS file from its per-mip chunks plus a synthesised
    DDS_HEADER + DDS_HEADER_DXT10 prefix."""
    payload_parts: list[bytes] = []
    for chunk in rec["chunks"]:
        f.seek(chunk["data_offset"])
        if chunk["packed_size"] == 0:
            data = f.read(chunk["unpacked_size"])
        else:
            body = f.read(chunk["packed_size"])
            if rec.get("compression") == 3:
                import lz4.block
                data = lz4.block.decompress(body, uncompressed_size=chunk["unpacked_size"])
            else:
                data = zlib.decompress(body)
        if len(data) != chunk["unpacked_size"]:
            raise Ba2ExtractError(
                f"DX10 chunk size mismatch: got {len(data)}, "
                f"expected {chunk['unpacked_size']}"
            )
        payload_parts.append(data)

    header = _make_dds_header(
        height=rec["height"],
        width=rec["width"],
        mip_count=max(rec["num_mips"], 1),
        dxgi_format=rec["dxgi_format"],
        cube_map=bool(rec.get("cube_map")),
    )
    return bytes(header) + b"".join(payload_parts)
