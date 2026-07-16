"""
ba2_writer.py
Pure-Python BA2 (Bethesda Archive 2) writer for Fallout 4 / FO4 VR.

Two archive types:
    GNRL — general files (anything except DDS textures)
    DX10 — DDS textures, with per-mip chunking and DXGI metadata

Public API mirrors bsa_writer:
    write_ba2(...)          → GNRL archive
    write_ba2_textures(...) → DX10 archive (DDS only)

GNRL on-disk layout (version 1):
    Header 24 B                       BTDX | version | "GNRL" | file_count | name_table_off
    File record 36 B each             name_hash | ext | dir_hash | flags |
                                      data_offset | packed_size | unpacked_size | 0xBAADF00D
    File data block (concatenated bytes; packed_size==0 means uncompressed)
    Name table                        per file: name_length(2) + name_bytes (backslashed)

DX10 layout differs in the file records — see ``write_ba2_textures``.

The FO4 BA2 hash is CRC-32 (poly 0xEDB88320) with **init=0** and **no
final XOR** — distinct from the standard zlib CRC.  See ``ba2_hash``.
Verified against vanilla FO4 archives.
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path
from typing import Callable

from Utils.atomic_write import atomic_writer


class Ba2WriteError(Exception):
    """Raised when packing a BA2 fails.  The atomic_writer leaves no
    .ba2 at the destination on failure (a stale .ba2.tmp may remain)."""


# ---------------------------------------------------------------------------
# File filter — what gets packed.  Mirrors bsa_writer's policy: plugins,
# nested archives, readmes, executables, dotfiles, mod-manager metadata
# stay loose.  We share this with bsa_writer rather than duplicating so
# the packing rules stay in lockstep.
# ---------------------------------------------------------------------------

from Utils.bsa_writer import is_packable, _INCOMPRESSIBLE_EXT  # noqa: E402

# ---------------------------------------------------------------------------
# Hash — FO4 BA2 CRC-32 variant.  zlib.crc32 won't do because zlib's
# wrapper applies init=0xFFFFFFFF and a final XOR; the BA2 hash is a
# raw CRC-32 with init=0 and no finalisation.  Verified against vanilla
# FO4 archives — five real (path, hash) samples all match.
# ---------------------------------------------------------------------------

def _build_crc_table() -> list[int]:
    table: list[int] = []
    poly = 0xEDB88320
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ poly if c & 1 else c >> 1
        table.append(c & 0xFFFFFFFF)
    return table


_CRC_TABLE = _build_crc_table()


def ba2_hash(s: str) -> int:
    """FO4 BA2 hash of *s* (treated as lowercase latin-1 bytes).

    Used for both ``name_hash`` (filename root, no extension, no path)
    and ``dir_hash`` (backslash-separated directory path, no trailing
    slash).  Empty string hashes to 0.
    """
    crc = 0
    for b in s.lower().encode("latin-1", errors="replace"):
        crc = _CRC_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Per-game version helper.  Mirror bsa_writer.bsa_version_for_game so the
# GUI can branch BSA vs BA2 on a single ``ba2_version_for_game`` call.
# ---------------------------------------------------------------------------

# Game IDs (from src/Games/) that use BA2.  FO4 vanilla and FO4 VR ship
# v1 archives; FO4 next-gen (post-2024 update) writes v8.  We always
# write v1 since v8 only differs in a 4-byte trailing string-table flag
# field that vanilla FO4 ignores — v1 archives load on every FO4 build.
_BA2_GAME_IDS: frozenset[str] = frozenset({
    "Fallout4", "Fallout4VR",
})


def ba2_version_for_game(game_id: str | None) -> int | None:
    """Return the BA2 version to write for *game_id*, or ``None`` if
    the game uses BSA / Morrowind / non-Bethesda archives.

    Currently returns 1 for all FO4-family games.  Starfield (v2/v3)
    and Fallout 76 (v7+) need their own write paths and are not yet
    supported."""
    if game_id in _BA2_GAME_IDS:
        return 1
    return None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BTDX_MAGIC      = b"BTDX"
_TYPE_GNRL       = b"GNRL"
_TYPE_DX10       = b"DX10"
_END_MARKER      = 0xBAADF00D
# archive2.exe stamps this in flags for every record; meaning is poorly
# documented but copying it keeps us byte-aligned with vanilla archives.
_RECORD_FLAGS      = 0x00100100
_GNRL_RECORD_LEN   = 36
_DX10_HEADER_LEN   = 24       # per-record header before chunks
_DX10_CHUNK_LEN    = 24
_HEADER_LEN        = 24       # archive header (magic + 20)


# ---------------------------------------------------------------------------
# DDS parsing for DX10 packing
#
# DDS_HEADER (124 bytes) starts at file offset 4 (after the "DDS " magic).
# When pixel_format.fourCC == "DX10", a second 20-byte DDS_HEADER_DXT10
# follows.  Pixel data begins at offset 4 + 124 + (20 if DX10 else 0).
#
# We need: width, height, mip_count, dxgi_format, and per-mip byte size.
# bethutil's BA2 writer (well, rsm-bsa, which it links to) parses these
# same fields and chunks the pixel data per mip.  We do the same — one
# chunk per mip is the simplest correct packing.  The engine accepts
# that variant; vanilla archives sometimes group small mips into a tail
# chunk for streaming wins, but that's an optimisation, not a hard
# requirement.
#
# Per-mip byte size formula depends on the DXGI format:
#   * Block-compressed formats (BC1..BC7) — 4×4 pixel blocks of either
#     8 bytes (BC1, BC4) or 16 bytes (BC2/3/5/6/7).  size = ceil(w/4) *
#     ceil(h/4) * block_bytes.  Min ceil(.) is 1.
#   * Uncompressed formats — width * height * bytes_per_pixel.
#
# DXGI format constants we care about — see Microsoft DXGI_FORMAT enum.
# This list covers everything FO4 ships in vanilla Textures BA2s plus
# the formats community texture mods commonly use (BC7 for albedo,
# BC5 for normals, BC4 for masks).
# ---------------------------------------------------------------------------

# DXGI formats whose 4x4 block is 8 bytes (BC1 family + BC4).
_DXGI_BLOCK_8: frozenset[int] = frozenset({
    70, 71, 72,        # BC1_TYPELESS / UNORM / UNORM_SRGB
    79, 80, 81,        # BC4_TYPELESS / UNORM / SNORM
})

# DXGI formats whose 4x4 block is 16 bytes (BC2/3/5/6/7).
_DXGI_BLOCK_16: frozenset[int] = frozenset({
    73, 74, 75,        # BC2_TYPELESS / UNORM / UNORM_SRGB
    76, 77, 78,        # BC3_TYPELESS / UNORM / UNORM_SRGB
    82, 83, 84,        # BC5_TYPELESS / UNORM / SNORM
    94, 95, 96,        # BC6H_TYPELESS / UF16 / SF16
    97, 98, 99,        # BC7_TYPELESS / UNORM / UNORM_SRGB
})

# Common uncompressed formats and their bytes-per-pixel.
_DXGI_BPP: dict[int, int] = {
    27: 4,    # R8G8B8A8_TYPELESS
    28: 4,    # R8G8B8A8_UNORM
    29: 4,    # R8G8B8A8_UNORM_SRGB
    61: 1,    # R8_UNORM
    62: 1,    # R8_UINT
    87: 4,    # B8G8R8A8_UNORM
    88: 4,    # B8G8R8X8_UNORM
    91: 4,    # B8G8R8A8_UNORM_SRGB
    24: 4,    # R10G10B10A2_UNORM (close enough; 32-bit packed)
    10: 8,    # R16G16B16A16_FLOAT
}


def _mip_byte_size(width: int, height: int, dxgi_format: int) -> int | None:
    """Return the byte size of a single mip at *width*×*height* in the
    given *dxgi_format*, or ``None`` if the format isn't in our table.

    Block-compressed formats round up to the 4×4 block grid.
    """
    if dxgi_format in _DXGI_BLOCK_8:
        return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 8
    if dxgi_format in _DXGI_BLOCK_16:
        return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 16
    bpp = _DXGI_BPP.get(dxgi_format)
    if bpp is not None:
        return width * height * bpp
    return None


class _DdsParseError(Exception):
    """Raised when a DDS file we wanted to pack as DX10 can't be parsed.
    The caller falls back to packing the file in the GNRL archive."""


# Legacy DDS fourCC → DXGI format.  Vanilla FO4 modding tools translate
# these on read; bethutil/rsm-bsa do the same.  Unmapped fourCCs
# (uncompressed legacy formats described by the bit-mask fields in
# DDS_HEADER) fall back to DDS_HEADER_DXT10 parsing — and if the file
# doesn't have that, we punt.
_LEGACY_FOURCC_TO_DXGI: dict[bytes, int] = {
    b"DXT1": 71,    # BC1_UNORM
    b"DXT2": 74,    # BC2_UNORM (premul-alpha treated as BC2)
    b"DXT3": 74,    # BC2_UNORM
    b"DXT4": 77,    # BC3_UNORM (premul-alpha treated as BC3)
    b"DXT5": 77,    # BC3_UNORM
    b"BC4U": 80,    # BC4_UNORM
    b"ATI1": 80,
    b"BC4S": 81,    # BC4_SNORM
    b"BC5U": 83,    # BC5_UNORM
    b"ATI2": 83,
    b"BC5S": 84,    # BC5_SNORM
}


def _parse_dds(data: bytes) -> dict:
    """Parse a DDS file's header and return a dict with:

        width, height, mip_count, dxgi_format, pixel_data_offset,
        per_mip_sizes — list of byte sizes for each mip, mip 0 first.

    Handles both modern DX10 (fourCC == "DX10" + DDS_HEADER_DXT10)
    and legacy DDS where the format is encoded as a fourCC like
    ``DXT1``/``DXT5`` directly in pixel_format.

    Raises :class:`_DdsParseError` if the file isn't a DDS or uses a
    format we can't compute mip sizes for; the caller falls through to
    packing as GNRL in that case.
    """
    if len(data) < 4 + 124:
        raise _DdsParseError("file too small for a DDS header")
    if data[0:4] != b"DDS ":
        raise _DdsParseError(f"bad DDS magic {data[0:4]!r}")
    size_field = struct.unpack_from("<I", data, 4)[0]
    if size_field != 124:
        raise _DdsParseError(f"unexpected DDS_HEADER size {size_field}")
    height = struct.unpack_from("<I", data, 12)[0]
    width = struct.unpack_from("<I", data, 16)[0]
    mip_count = struct.unpack_from("<I", data, 28)[0] or 1
    fourcc = data[84:88]
    if fourcc == b"DX10":
        if len(data) < 4 + 124 + 20:
            raise _DdsParseError("file too small for DDS_HEADER_DXT10")
        dxgi_format = struct.unpack_from("<I", data, 128)[0]
        pixel_data_offset = 4 + 124 + 20
    else:
        # Legacy DDS — translate fourCC to DXGI.  Bethesda's loader
        # does this mapping internally; we do it on the way in so we
        # can write a DX10 BA2 record that the engine can resolve.
        dxgi_format_opt = _LEGACY_FOURCC_TO_DXGI.get(fourcc)
        if dxgi_format_opt is None:
            raise _DdsParseError(f"unsupported legacy fourCC {fourcc!r}")
        dxgi_format = dxgi_format_opt
        pixel_data_offset = 4 + 124

    per_mip_sizes: list[int] = []
    for m in range(mip_count):
        mw = max(1, width >> m)
        mh = max(1, height >> m)
        sz = _mip_byte_size(mw, mh, dxgi_format)
        if sz is None:
            raise _DdsParseError(
                f"unsupported DXGI format {dxgi_format} for mip math"
            )
        per_mip_sizes.append(sz)

    expected_total = sum(per_mip_sizes)
    actual_total = len(data) - pixel_data_offset
    if actual_total < expected_total:
        # File is smaller than what we computed — could be a partial
        # mip chain (some mods drop the smallest mips).  Trim our
        # per-mip list to fit the actual data.
        running = 0
        kept: list[int] = []
        for sz in per_mip_sizes:
            if running + sz > actual_total:
                break
            kept.append(sz)
            running += sz
        if not kept:
            raise _DdsParseError(
                f"pixel data {actual_total} too small for any mip"
            )
        per_mip_sizes = kept
        mip_count = len(kept)

    return {
        "width": width,
        "height": height,
        "mip_count": mip_count,
        "dxgi_format": dxgi_format,
        "pixel_data_offset": pixel_data_offset,
        "per_mip_sizes": per_mip_sizes,
    }


# ---------------------------------------------------------------------------
# Collection — walk the mod folder and group by (dir_path, leaf_name).
# Same shape as bsa_writer._collect_files: returns (groups, packed_rel_keys).
# Files at the mod root would be packable (BA2 has no "must be in a folder"
# rule like BSA — a name table entry can be just "foo.dds"), so unlike
# bsa_writer we keep them.
# ---------------------------------------------------------------------------

def _collect_files(
    source_dir: Path,
    excluded_keys: frozenset[str] = frozenset(),
    game_id: str | None = None,
) -> tuple[list[tuple[str, str, str, Path]], list[str]]:
    """Walk *source_dir* and return:

      * a flat list of ``(dir_backslash, leaf_lower, ext_no_dot, abs_path)``
        for every packable file.  *dir_backslash* is the lowercase
        backslash-separated parent directory ("" for files at the mod
        root).  *leaf_lower* is the lowercase root filename (no
        extension).  *ext_no_dot* is the lowercase extension without
        the leading dot.
      * a parallel list of rel_keys (lowercase forward-slash paths) so
        the caller can persist the auto-disable / auto-delete list.

    Files passing ``is_packable(rel_key, game_id)`` are returned — the
    per-game allowlist lives in Utils.archive_rules.
    """
    files: list[tuple[str, str, str, Path]] = []
    packed_rel_keys: list[str] = []
    src = source_dir.resolve()
    src_str = str(src)

    for dirpath, dirnames, filenames in os.walk(src):
        # Skip dot-directories (e.g. .git) at any depth.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        rel_dir = os.path.relpath(dirpath, src_str)
        if rel_dir == ".":
            dir_back = ""
            dir_fwd = ""
        else:
            dir_back = rel_dir.replace("/", "\\").replace(os.sep, "\\").lower()
            dir_fwd = dir_back.replace("\\", "/")

        for fn in filenames:
            rel_key = (dir_fwd + "/" + fn.lower()) if dir_fwd else fn.lower()
            if rel_key in excluded_keys:
                continue
            if not is_packable(rel_key, game_id):
                continue
            lower = fn.lower()
            dot = lower.rfind(".")
            if dot < 0:
                # Files with no extension are very rare in BA2s; archive2.exe
                # writes ext as four NUL bytes for them.  We do the same.
                leaf = lower
                ext = ""
            else:
                leaf = lower[:dot]
                ext = lower[dot + 1:]
            files.append((dir_back, leaf, ext, Path(dirpath) / fn))
            packed_rel_keys.append(rel_key)

    return files, packed_rel_keys


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]


def _is_incompressible(ext_no_dot: str) -> bool:
    return ("." + ext_no_dot) in _INCOMPRESSIBLE_EXT if ext_no_dot else False


def _ext_bytes(ext_no_dot: str) -> bytes:
    """Pack a lowercase extension (no dot) into the 4-byte ext field of a
    GNRL record.  Truncated to 4 chars and right-padded with NUL.  Vanilla
    archives use ``b"nif\\0"`` for ".nif", ``b"bgsm"`` for ".bgsm", etc."""
    eb = ext_no_dot.encode("latin-1", errors="replace")[:4]
    return eb + b"\x00" * (4 - len(eb))


def write_ba2(
    ba2_path: Path,
    source_dir: Path,
    *,
    game_id: str | None = None,
    compress: bool = True,
    excluded_keys: frozenset[str] = frozenset(),
    exclude_textures: bool = False,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> tuple[int, int, list[str]]:
    """Pack *source_dir* into a GNRL BA2 at *ba2_path* (atomically).

    Args:
        ba2_path:        Destination .ba2 path.
        source_dir:      Mod folder root.
        game_id:         Game ID (e.g. ``"Fallout4"``) — selects the
                         per-game packable allowlist.  ``None`` falls
                         back to a permissive policy (intended for the
                         self-test only).
        compress:        Apply zlib compression on every file whose
                         extension isn't in the bsa_writer
                         ``_INCOMPRESSIBLE_EXT`` list (which already
                         covers .wav / .mp3 / .ogg / .flac / .xwm /
                         .fuz / .lip / .mp4 / .bk2 / .*strings).
                         ``False`` writes everything raw — useful when
                         the user explicitly wants archive2.exe-style
                         uncompressed output.
        excluded_keys:   rel_keys (lowercase forward-slash) to skip —
                         the user's per-mod disable list from the Mod
                         Files tab.
        exclude_textures: If True, skip .dds files entirely.  Used by
                         the FO4 GUI flow when also writing a separate
                         textures archive (``- Textures.ba2``) via
                         :func:`write_ba2_textures`, so the same .dds
                         doesn't get packed twice.
        progress:        Optional ``(done, total, current_path)`` callback.
        cancel:          Optional callback returning True to abort.

    Returns:
        ``(file_count, bytes_written, packed_rel_keys)`` — same shape
        as bsa_writer.write_bsa.

    Raises:
        Ba2WriteError: on I/O / format failure or cancel.
    """
    ba2_path = Path(ba2_path)
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise Ba2WriteError(f"source directory does not exist: {source_dir}")

    files, packed_rel_keys = _collect_files(
        source_dir, excluded_keys, game_id=game_id,
    )
    if exclude_textures:
        files, packed_rel_keys, _, _ = _split_texture_files(files, packed_rel_keys)
    if not files:
        raise Ba2WriteError("no packable files found")

    file_count = len(files)

    # Pre-load file bytes (and compress) so we can compute exact data
    # offsets in a single forward pass.  For very large mods this could
    # be a memory pinch — vanilla FO4 archives top out around 2 GB, but
    # community FO4 mods can ship multi-GB voice packs.  If memory ever
    # becomes a problem we could stream-write instead and patch the
    # offsets at the end like bsa_writer does.  For now the simple path
    # is fine (this runs on a background thread and the user's memory
    # peak matches their archive size).
    if cancel is not None and cancel():
        raise Ba2WriteError("cancelled")

    # Each item: (dir_back, leaf, ext, abs_path, on_disk_payload,
    #             packed_size_field, unpacked_size, name_full_back)
    prepared: list[tuple[str, str, str, Path, bytes, int, int, str]] = []
    done = 0
    for dir_back, leaf, ext, abs_path in files:
        if cancel is not None and cancel():
            raise Ba2WriteError("cancelled")
        try:
            raw = abs_path.read_bytes()
        except OSError as exc:
            raise Ba2WriteError(f"failed to read {abs_path}: {exc}") from exc
        unpacked_size = len(raw)
        if compress and not _is_incompressible(ext):
            payload = zlib.compress(raw, 9)
            packed_size_field = len(payload)
        else:
            payload = raw
            # FO4 spec: packed_size==0 means "data is uncompressed; read
            # unpacked_size bytes as-is".  This matches what archive2.exe
            # writes for incompressible extensions.
            packed_size_field = 0
        # Reconstruct the lowercase backslash full path used in the name
        # table and (split) used for the per-record dir_hash / name_hash.
        if dir_back:
            full_back = f"{dir_back}\\{leaf}.{ext}" if ext else f"{dir_back}\\{leaf}"
        else:
            full_back = f"{leaf}.{ext}" if ext else leaf
        prepared.append((dir_back, leaf, ext, abs_path, payload,
                         packed_size_field, unpacked_size, full_back))
        done += 1
        if progress is not None:
            try:
                progress(done, file_count * 2, abs_path.name)  # halfway phase
            except Exception:
                pass

    # Compute layout: header + record table + data block + name table.
    # Record table starts immediately after the header.
    header_end = _HEADER_LEN
    record_table_size = file_count * _GNRL_RECORD_LEN
    data_block_offset = header_end + record_table_size
    # Cumulative data offsets — each file lives back-to-back.
    data_offsets: list[int] = []
    cur = data_block_offset
    for _dir_back, _leaf, _ext, _ap, payload, _ps, _us, _fb in prepared:
        data_offsets.append(cur)
        cur += len(payload)
    name_table_offset = cur

    # Now write everything atomically.
    try:
        with atomic_writer(ba2_path, "wb", encoding=None) as fh:
            # --- Header ---
            fh.write(struct.pack(
                "<4sI4sIQ",
                _BTDX_MAGIC,
                1,                        # version
                _TYPE_GNRL,
                file_count,
                name_table_offset,
            ))

            # --- File records ---
            for i, (dir_back, leaf, ext, _ap, payload, packed_size_field,
                    unpacked_size, _fb) in enumerate(prepared):
                rec = struct.pack(
                    "<I4sIIQIII",
                    ba2_hash(leaf),       # name_hash (filename root only)
                    _ext_bytes(ext),      # ext (4 bytes, NUL-padded)
                    ba2_hash(dir_back),   # dir_hash (parent path, "" -> 0)
                    _RECORD_FLAGS,        # flags (matches archive2.exe)
                    data_offsets[i],      # data_offset (u64)
                    packed_size_field,    # packed_size (0 = uncompressed)
                    unpacked_size,
                    _END_MARKER,
                )
                fh.write(rec)

            # --- Data block ---
            done = file_count   # progress is in halves; data write is the second half
            for (_db, _l, _e, _ap, payload, _ps, _us, full_back) in prepared:
                if cancel is not None and cancel():
                    raise Ba2WriteError("cancelled")
                fh.write(payload)
                done += 1
                if progress is not None:
                    progress(done, file_count * 2, full_back)

            # --- Name table ---
            for (_db, _l, _e, _ap, _p, _ps, _us, full_back) in prepared:
                name_bytes = full_back.encode("latin-1", errors="replace")
                fh.write(struct.pack("<H", len(name_bytes)))
                fh.write(name_bytes)
    except Ba2WriteError:
        raise
    except (OSError, struct.error, zlib.error) as exc:
        raise Ba2WriteError(str(exc)) from exc

    return file_count, ba2_path.stat().st_size, packed_rel_keys


# ---------------------------------------------------------------------------
# DX10 (textures) packer
#
# Layout (per record):
#
#   24 B header:
#       4 B  name_hash
#       4 B  ext           always b"dds\0"
#       4 B  dir_hash
#       1 B  unk1          0
#       1 B  num_chunks    one chunk per mip (simpler than vanilla's
#                          tail-grouping; the engine accepts this)
#       2 B  chunk_size    24 — the size of each chunk record below
#       2 B  height
#       2 B  width
#       1 B  num_mips
#       1 B  dxgi_format
#       2 B  unk16         vanilla writes 2048 ("tile mode"); we copy
#
#   N × 24 B chunk:
#       8 B  data_offset   absolute offset to this mip's bytes
#       4 B  packed_size   compressed length; 0 = uncompressed
#       4 B  unpacked_size
#       2 B  start_mip     mip range covered by this chunk
#       2 B  end_mip
#       4 B  end_marker    0xBAADF00D
#
# Then the name table at name_table_offset (same layout as GNRL).
# ---------------------------------------------------------------------------

def _split_texture_files(
    files: list[tuple[str, str, str, Path]],
    rel_keys: list[str],
) -> tuple[
    list[tuple[str, str, str, Path]],   # non-texture files
    list[str],                          # non-texture rel_keys
    list[tuple[str, str, str, Path]],   # texture files (.dds)
    list[str],                          # texture rel_keys
]:
    """Partition a packed-files list into ``(non_textures, textures)``.

    A "texture" here is any ``.dds`` file — those are eligible for the
    DX10 archive.  Everything else goes into the GNRL archive.  The
    ``rel_keys`` list is split in lockstep so callers can persist
    auto-disable state per archive.
    """
    nt_files: list[tuple[str, str, str, Path]] = []
    nt_rks: list[str] = []
    tx_files: list[tuple[str, str, str, Path]] = []
    tx_rks: list[str] = []
    for f, rk in zip(files, rel_keys):
        if f[2] == "dds":
            tx_files.append(f)
            tx_rks.append(rk)
        else:
            nt_files.append(f)
            nt_rks.append(rk)
    return nt_files, nt_rks, tx_files, tx_rks


def write_ba2_textures(
    ba2_path: Path,
    source_dir: Path,
    *,
    game_id: str | None = None,
    compress: bool = True,
    excluded_keys: frozenset[str] = frozenset(),
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> tuple[int, int, list[str]]:
    """Pack every ``.dds`` file under *source_dir* into a DX10 BA2.

    Texture files are packed into the DX10 archive variant — same outer
    BTDX header but per-record metadata (height, width, mip_count, DXGI
    format) plus per-mip chunks.  This matches what ``archive2.exe`` and
    bethutil produce and is what FO4 expects in a ``<plugin> -
    Textures.ba2``.

    Files that aren't valid DX10 DDS (no DXT10 extension header,
    unsupported DXGI format, etc.) are **silently skipped** — callers
    are expected to send the same input to :func:`write_ba2`, which
    will pack them in the GNRL archive instead.

    Same return shape as :func:`write_ba2`.  Raises
    :class:`Ba2WriteError` on I/O / format failure or cancel.
    """
    ba2_path = Path(ba2_path)
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise Ba2WriteError(f"source directory does not exist: {source_dir}")

    files, rel_keys = _collect_files(
        source_dir, excluded_keys, game_id=game_id,
    )
    _, _, tx_files, tx_rel_keys = _split_texture_files(files, rel_keys)
    if not tx_files:
        raise Ba2WriteError("no packable texture files found")

    if cancel is not None and cancel():
        raise Ba2WriteError("cancelled")

    # Phase 1: parse each DDS header + read its bytes.  Files we can't
    # parse drop out (the GNRL pass picks them up).
    # Each prepared tuple:
    #   (full_back, leaf, dxgi_format, height, width, num_mips,
    #    pixel_offset, per_mip_sizes, raw_bytes, rel_key)
    Prepared = tuple[str, str, int, int, int, int, int, list[int], bytes, str]
    prepared: list[Prepared] = []
    skipped_rel_keys: list[str] = []  # parse failures
    total_phase = len(tx_files) * 2
    done = 0

    for (dir_back, leaf, ext, abs_path), rk in zip(tx_files, tx_rel_keys):
        if cancel is not None and cancel():
            raise Ba2WriteError("cancelled")
        try:
            raw = abs_path.read_bytes()
        except OSError as exc:
            raise Ba2WriteError(f"failed to read {abs_path}: {exc}") from exc
        try:
            meta = _parse_dds(raw)
        except _DdsParseError:
            # Caller's GNRL pass will pick this up.
            skipped_rel_keys.append(rk)
            done += 1
            if progress is not None:
                progress(done, total_phase, abs_path.name)
            continue
        if dir_back:
            full_back = f"{dir_back}\\{leaf}.dds"
        else:
            full_back = f"{leaf}.dds"
        prepared.append((
            full_back, leaf, dir_back,
            meta["dxgi_format"], meta["height"], meta["width"],
            meta["mip_count"], meta["pixel_data_offset"],
            meta["per_mip_sizes"], raw, rk,
        ))
        done += 1
        if progress is not None:
            progress(done, total_phase, abs_path.name)

    if not prepared:
        raise Ba2WriteError("no DX10-eligible texture files found")

    file_count = len(prepared)
    packed_rel_keys = [p[10] for p in prepared]

    # Phase 2: compute layout.  Header + record table (variable size:
    # each record is 24 + 24*num_chunks) + data block + name table.
    record_table_size = sum(
        _DX10_HEADER_LEN + _DX10_CHUNK_LEN * len(p[8])
        for p in prepared
    )
    data_block_offset = _HEADER_LEN + record_table_size

    # Phase 3: prepare per-mip payloads (with optional zlib compression)
    # and accumulate offsets.
    # mip_payloads[i] is a list of (compressed_bytes, packed_size_field,
    # unpacked_size, data_offset) tuples — one per mip.
    mip_payloads: list[list[tuple[bytes, int, int, int]]] = []
    cur = data_block_offset
    for (_fb, _leaf, _db, _fmt, _h, _w, _mips, pix_off, per_mip,
         raw, _rk) in prepared:
        chunks_this_file: list[tuple[bytes, int, int, int]] = []
        mip_off = pix_off
        for mip_size in per_mip:
            mip_data = raw[mip_off:mip_off + mip_size]
            mip_off += mip_size
            if compress:
                comp = zlib.compress(mip_data, 9)
                # Only use the compressed form if it's actually smaller —
                # tiny mips often expand under zlib overhead.
                if len(comp) < len(mip_data):
                    payload = comp
                    packed_field = len(comp)
                else:
                    payload = mip_data
                    packed_field = 0
            else:
                payload = mip_data
                packed_field = 0
            chunks_this_file.append((payload, packed_field, len(mip_data), cur))
            cur += len(payload)
        mip_payloads.append(chunks_this_file)

    name_table_offset = cur

    # Phase 4: write everything atomically.
    try:
        with atomic_writer(ba2_path, "wb", encoding=None) as fh:
            # --- Header ---
            fh.write(struct.pack(
                "<4sI4sIQ",
                _BTDX_MAGIC,
                1,                    # version
                _TYPE_DX10,
                file_count,
                name_table_offset,
            ))

            # --- File records (per-file 24 B header + N×24 B chunks) ---
            for (_fb, leaf, dir_back, fmt, h, w, mips, _po, per_mip,
                 _raw, _rk), chunks in zip(prepared, mip_payloads):
                num_chunks = len(per_mip)
                # Per-record header.  unk1=0, chunk_size=24 (the size of
                # the chunk record format), unk16=2048 (vanilla constant).
                fh.write(struct.pack(
                    "<I4sIBBHHHBBH",
                    ba2_hash(leaf),
                    b"dds\x00",
                    ba2_hash(dir_back),
                    0,                # unk1
                    num_chunks,
                    24,               # chunk_size field (24 = chunk-record bytes)
                    h,
                    w,
                    mips,
                    fmt,
                    2048,             # unk16 — copied from vanilla archives
                ))
                # One chunk per mip.
                for mip_index, (_payload, packed_field, unpacked,
                                data_offset) in enumerate(chunks):
                    fh.write(struct.pack(
                        "<QIIHHI",
                        data_offset,
                        packed_field,
                        unpacked,
                        mip_index,    # start_mip (1 chunk per mip = same)
                        mip_index,    # end_mip
                        _END_MARKER,
                    ))

            # --- Data block (mip payloads, concatenated in record order) ---
            phase2_done = file_count
            for (idx, chunks) in enumerate(mip_payloads):
                if cancel is not None and cancel():
                    raise Ba2WriteError("cancelled")
                for (payload, _pf, _us, _do) in chunks:
                    fh.write(payload)
                phase2_done += 1
                if progress is not None:
                    progress(phase2_done, total_phase, prepared[idx][0])

            # --- Name table ---
            for (full_back, *_rest) in prepared:
                name_bytes = full_back.encode("latin-1", errors="replace")
                fh.write(struct.pack("<H", len(name_bytes)))
                fh.write(name_bytes)
    except Ba2WriteError:
        raise
    except (OSError, struct.error, zlib.error) as exc:
        raise Ba2WriteError(str(exc)) from exc

    return file_count, ba2_path.stat().st_size, packed_rel_keys


# ---------------------------------------------------------------------------
# Stub plugin generation — re-export bsa_writer's helpers verbatim.  The
# stub plugin is a Bethesda format concept, not a BA2 one, and FO4 uses
# the same TES4 header layout (with internal_version 131 instead of 44).
# bsa_writer.write_stub_plugin already drives the version off game_id, so
# we just point callers there.
# ---------------------------------------------------------------------------

from Utils.bsa_writer import (  # noqa: E402  (re-export)
    is_our_stub_plugin,
    write_stub_plugin,
)


__all__ = [
    "Ba2WriteError",
    "ba2_hash",
    "ba2_version_for_game",
    "is_our_stub_plugin",
    "is_packable",
    "write_ba2",
    "write_ba2_textures",
    "write_stub_plugin",
]
