"""
ba2_extract.py
Pure-Python BA2 (Bethesda Archive 2) extractor for Fallout 4.

Handles both GNRL (general files) and DX10 (DDS textures), using zlib
decompression where applicable.  DX10 reassembly synthesises a standard
DDS header from the per-record metadata (height, width, mip count, DXGI
format) and concatenates the per-mip chunks into the body — yielding a
loadable .dds file on disk.

Sister to ba2_writer (which currently only emits GNRL — DX10 packing
needs DDS-header introspection that we don't do yet).  Both share the
``ba2_hash`` and filtering policy from bsa_writer / ba2_writer.

Output paths mirror the BA2 reader: lowercase forward-slash relative
paths, written under *dest_dir* preserving the archive's stored folder
structure.

References:
    * Empirical inspection of vanilla FO4 BA2s — verified header fields
      and chunk layout against HorseArmor (GNRL), DLCworkshop03 -
      Textures (DX10), and DLCNukaWorld - Voices_en (GNRL).
    * bsa_reader._read_ba2 in this repo — the read-list path we extend
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


# ---------------------------------------------------------------------------
# DDS header reconstruction (DX10 records)
#
# A BA2 DX10 record carries the DDS metadata (height/width/mips/format) in
# its 24-byte header and the raw pixel data split across N mip chunks.  To
# turn that back into a standalone .dds file we synthesise:
#
#     "DDS " magic                           4 bytes
#     DDS_HEADER                            124 bytes
#     DDS_HEADER_DXT10  (if DX10 needed)     20 bytes
#     pixel data (concatenated chunk bytes)
#
# We always emit the DXT10 extension header — it's 20 bytes of overhead but
# it lets every DXGI format round-trip, including the post-DXT5 ones (BC7,
# R8G8B8A8_UNORM, etc.) that the legacy DDS_HEADER alone can't express.
# Tools that read DDS (DirectXTex, NVTT, GIMP+plugin, every Bethesda tool)
# accept the DX10 extension header transparently.
#
# Field layout taken from the Microsoft DDS spec.  We stamp:
#   - flags = required(0x1007) | linear_size(0x80000) | mip_count(0x20000)
#   - caps  = texture(0x1000) | complex(0x8) | mipmap(0x400000) when mips>1
#   - pixel_format.flags = DDPF_FOURCC (0x4)
#   - pixel_format.fourCC = "DX10"
#   - dxt10.dxgi_format = format from the BA2 record
#   - dxt10.dimension = 3 (TEXTURE2D)
#   - dxt10.misc = 0 (or 4 for cubemap; we set 4 if num_mips==0xff and
#     unk16 hints at cubemap, but BA2 doesn't cleanly disambiguate, so
#     we default to non-cube — vanilla BA2s don't ship cubemaps in DX10
#     records anyway in the samples we surveyed).
# ---------------------------------------------------------------------------

_DDS_MAGIC = b"DDS "

# DDS_HEADER flags
_DDSD_CAPS        = 0x1
_DDSD_HEIGHT      = 0x2
_DDSD_WIDTH       = 0x4
_DDSD_PIXELFORMAT = 0x1000
_DDSD_MIPMAPCOUNT = 0x20000
_DDSD_LINEARSIZE  = 0x80000

_DDSCAPS_COMPLEX  = 0x8
_DDSCAPS_TEXTURE  = 0x1000
_DDSCAPS_MIPMAP   = 0x400000

_DDPF_FOURCC      = 0x4

_DXGI_DIMENSION_TEXTURE2D = 3


def _make_dds_header(
    *,
    height: int,
    width: int,
    mip_count: int,
    dxgi_format: int,
    pitch_or_linear_size: int,
) -> bytes:
    """Build a DDS magic + DDS_HEADER + DDS_HEADER_DXT10 prefix (148 bytes)."""
    flags = _DDSD_CAPS | _DDSD_HEIGHT | _DDSD_WIDTH | _DDSD_PIXELFORMAT \
            | _DDSD_LINEARSIZE | (_DDSD_MIPMAPCOUNT if mip_count > 1 else 0)
    caps = _DDSCAPS_TEXTURE | (_DDSCAPS_MIPMAP | _DDSCAPS_COMPLEX if mip_count > 1 else 0)

    # pixel_format struct (32 bytes): size, flags, fourCC, rgb_bit_count,
    # r_mask, g_mask, b_mask, a_mask
    pixel_format = struct.pack(
        "<II4sIIIII",
        32,                # struct size
        _DDPF_FOURCC,
        b"DX10",
        0, 0, 0, 0, 0,
    )

    # DDS_HEADER (124 bytes):
    #   size (4) flags (4) height (4) width (4) pitch_or_linear (4) depth (4)
    #   mip_count (4) reserved1 (4 * 11) pixel_format (32) caps (4) caps2 (4)
    #   caps3 (4) caps4 (4) reserved2 (4)
    header = struct.pack(
        "<II I I I I I 11I 32s I I I I I",
        124,                            # size
        flags,
        height,
        width,
        pitch_or_linear_size,
        0,                              # depth (volume textures only)
        max(mip_count, 1),
        *([0] * 11),                    # reserved1[11]
        pixel_format,
        caps,
        0, 0, 0,                        # caps2, caps3, caps4
        0,                              # reserved2
    )

    # DDS_HEADER_DXT10 (20 bytes):
    #   dxgi_format (4) resource_dimension (4) misc_flag (4) array_size (4)
    #   misc_flags2 (4)
    dxt10 = struct.pack(
        "<IIIII",
        dxgi_format,
        _DXGI_DIMENSION_TEXTURE2D,
        0,                              # misc_flag (cubemap = 4)
        1,                              # array_size
        0,                              # misc_flags2 (alpha mode)
    )

    return _DDS_MAGIC + header + dxt10


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


def _parse_records(f) -> tuple[list[dict], list[str]]:
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
                "chunks": chunks,
            })

    # --- Read the name table ---
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
        names.append(nb.decode("latin-1").replace("\\", "/").lower())

    return records, names


def _read_gnrl(f, rec: dict) -> bytes:
    """Read one GNRL file's bytes, decompressing if needed."""
    f.seek(rec["data_offset"])
    if rec["packed_size"] == 0:
        # Uncompressed — read unpacked_size bytes verbatim.
        return f.read(rec["unpacked_size"])
    body = f.read(rec["packed_size"])
    return zlib.decompress(body)


def _read_dx10(f, rec: dict) -> bytes:
    """Reassemble a DDS file from its per-mip chunks plus a synthesised
    DDS_HEADER + DDS_HEADER_DXT10 prefix."""
    payload_parts: list[bytes] = []
    first_chunk_unpacked = 0
    for i, chunk in enumerate(rec["chunks"]):
        f.seek(chunk["data_offset"])
        if chunk["packed_size"] == 0:
            data = f.read(chunk["unpacked_size"])
        else:
            data = zlib.decompress(f.read(chunk["packed_size"]))
        if len(data) != chunk["unpacked_size"]:
            raise Ba2ExtractError(
                f"DX10 chunk size mismatch: got {len(data)}, "
                f"expected {chunk['unpacked_size']}"
            )
        if i == 0:
            first_chunk_unpacked = len(data)
        payload_parts.append(data)

    header = _make_dds_header(
        height=rec["height"],
        width=rec["width"],
        mip_count=max(rec["num_mips"], 1),
        dxgi_format=rec["dxgi_format"],
        # The DDS spec expects `pitch_or_linear_size` to be the
        # top-mip linear size for compressed formats.  Using the first
        # chunk's unpacked size is correct since chunk 0 is mip 0.
        pitch_or_linear_size=first_chunk_unpacked,
    )
    return header + b"".join(payload_parts)
