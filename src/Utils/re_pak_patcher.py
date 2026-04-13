"""
re_pak_patcher.py
Utilities for patching and restoring RE Engine PAK files.

Zeroes out the 8-byte hash field of every PAK entry that matches a deployed
mod file.  The engine uses (hash_lower, hash_upper) as a lookup key — a zero
pair never matches any real filename, so the engine falls back to loading the
loose file from disk (via REFramework's LooseFileLoader hook).

Two PAK versions are supported:

PAK v2.0 entry layout (24 bytes each — RE2 Remake, RE3 Remake):
  0  8   file_offset       int64
  8  8   decompressed_size int64
 16  4   hash_lower        uint32  Murmur3-32 of lowercase UTF-16LE path
 20  4   hash_upper        uint32  Murmur3-32 of uppercase UTF-16LE path

PAK v4.x entry layout (48 bytes each — RE Village, RE4 Remake, RE7, …):
  0  4   hash_lower        uint32  Murmur3-32 of lowercase UTF-16LE path
  4  4   hash_upper        uint32  Murmur3-32 of uppercase UTF-16LE path
  8  8   file_offset       int64
 16  8   compressed_size   int64
 24  8   decompressed_size int64
 32  8   attributes        int64
 40  8   checksum          uint64

Backup format (JSON):
  { "pak": "<abs path>",
    "entries": [ {"index": <int>, "original": "<16-hex-chars>"}, ... ] }
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

from Utils.app_log import safe_log as _safe_log

# ---------------------------------------------------------------------------
# Murmur3-32 (matches Ekey/REE.PAK.Tool Murmur3.HashCore32, seed 0xFFFFFFFF)
# ---------------------------------------------------------------------------

def _rotl32(x: int, r: int) -> int:
    return ((x << r) | (x >> (32 - r))) & 0xFFFFFFFF


def murmur3_32(data: bytes, seed: int = 0xFFFFFFFF) -> int:
    """MurMur3-32 hash over *data* with *seed*.

    Processes data in 4-byte little-endian chunks exactly as the C# reference
    implementation in REE.PAK.Tool / PakHash.iGetStringHash.
    """
    c1: int = 0xCC9E2D51
    c2: int = 0x1B873593

    h1: int = seed & 0xFFFFFFFF
    length: int = len(data)

    # Process 4-byte blocks
    nblocks: int = length // 4
    for i in range(nblocks):
        k1 = struct.unpack_from("<I", data, i * 4)[0]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = _rotl32(k1, 15)
        k1 = (k1 * c2) & 0xFFFFFFFF
        h1 ^= k1
        h1 = _rotl32(h1, 13)
        h1 = (h1 * 5 + 0xE6546B64) & 0xFFFFFFFF

    # Tail
    tail_start = nblocks * 4
    tail = data[tail_start:]
    k1 = 0
    tail_len = len(tail)
    if tail_len >= 3:
        k1 ^= tail[2] << 16
    if tail_len >= 2:
        k1 ^= tail[1] << 8
    if tail_len >= 1:
        k1 ^= tail[0]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = _rotl32(k1, 15)
        k1 = (k1 * c2) & 0xFFFFFFFF
        h1 ^= k1

    # Finalise
    h1 ^= length
    h1 ^= h1 >> 16
    h1 = (h1 * 0x85EBCA6B) & 0xFFFFFFFF
    h1 ^= h1 >> 13
    h1 = (h1 * 0xC2B2AE35) & 0xFFFFFFFF
    h1 ^= h1 >> 16
    return h1


def hash_filepath(rel_path: str) -> tuple[int, int]:
    """Return *(hash_lower, hash_upper)* for *rel_path*.

    hash_lower = Murmur3-32 of the lowercase path encoded as UTF-16LE
    hash_upper = Murmur3-32 of the uppercase path encoded as UTF-16LE
    """
    lower_bytes = rel_path.lower().encode("utf-16-le")
    upper_bytes = rel_path.upper().encode("utf-16-le")
    return murmur3_32(lower_bytes), murmur3_32(upper_bytes)


# ---------------------------------------------------------------------------
# PAK constants
# ---------------------------------------------------------------------------

_PAK_MAGIC    = 0x414B504B   # "KPKA"
_HEADER_SIZE  = 16
_ENTRY_SIZE_V2 = 24          # v2.0: RE2 Remake, RE3 Remake
_ENTRY_SIZE_V4 = 48          # v4.x: RE Village, RE4 Remake, RE7, …

# Hash field byte-offset within an entry (version-dependent)
_HASH_OFFSET_V2 = 16         # hash_lower at byte 16 of the entry
_HASH_OFFSET_V4 = 0          # hash_lower at byte 0 of the entry


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_header(data: bytes) -> tuple[int, int, int, int, int]:
    """Parse the 16-byte PAK header.

    Returns *(major_version, minor_version, feature, total_files, entry_size)*.
    Raises ValueError on bad magic or unsupported version.
    """
    if len(data) < _HEADER_SIZE:
        raise ValueError("PAK file too small to contain a valid header.")
    magic, major, minor, feature, total_files, _fp = struct.unpack_from("<IBBHII", data, 0)
    if magic != _PAK_MAGIC:
        raise ValueError(f"Not a valid RE Engine PAK file (bad magic 0x{magic:08X}).")
    if major == 2:
        entry_size = _ENTRY_SIZE_V2
    elif major == 4:
        entry_size = _ENTRY_SIZE_V4
    else:
        raise ValueError(f"Unsupported PAK major version {major} (only v2 and v4 supported).")
    return major, minor, feature, total_files, entry_size


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def patch_pak_file(
    pak_path: Path,
    hashes: set[tuple[int, int]],
    backup_path: Path,
    log_fn=None,
) -> int:
    """Invalidate PAK entries whose hash pair is in *hashes*.

    Only reads the 16-byte header and the entry table (N × 48 bytes) into
    memory — the file data is never loaded.  Matching entries are patched
    in-place via seek+write so even multi-GB PAK files work fine.

    Writes the original 8-byte hash fields to *backup_path* (JSON) so they
    can be restored later.  If *backup_path* already exists it is extended
    (idempotent for already-patched entries).

    Returns the number of entries newly invalidated.
    """
    _log = _safe_log(log_fn)

    # Load existing backup so we are idempotent
    existing: dict[int, str] = {}
    if backup_path.exists():
        try:
            saved = json.loads(backup_path.read_text(encoding="utf-8"))
            for e in saved.get("entries", []):
                existing[e["index"]] = e["original"]
        except (json.JSONDecodeError, KeyError):
            pass

    newly_patched = 0
    with pak_path.open("r+b") as fh:
        # Read only the header (16 bytes)
        header_bytes = fh.read(_HEADER_SIZE)
        try:
            _, _, _, total_files, entry_size = _read_header(header_bytes)
        except ValueError as exc:
            _log(f"  [WARN] Skipping {pak_path.name}: {exc}")
            return 0

        hash_off = _HASH_OFFSET_V2 if entry_size == _ENTRY_SIZE_V2 else _HASH_OFFSET_V4

        # Read only the entry table
        table_size = total_files * entry_size
        table = fh.read(table_size)

        for i in range(total_files):
            entry_start = i * entry_size
            hash_start = entry_start + hash_off
            if hash_start + 8 > len(table):
                break
            hl, hu = struct.unpack_from("<II", table, hash_start)
            if (hl, hu) not in hashes:
                continue
            if hl == 0 and hu == 0:
                continue  # already zeroed — skip
            # Save original bytes
            if i not in existing:
                existing[i] = table[hash_start:hash_start + 8].hex()
            # Zero out the hash pair in-place
            file_off = _HEADER_SIZE + hash_start
            fh.seek(file_off)
            fh.write(b"\x00" * 8)
            newly_patched += 1

    if newly_patched == 0:
        return 0

    # Write/update backup
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    entries_out = [{"index": idx, "original": orig} for idx, orig in sorted(existing.items())]
    backup_path.write_text(
        json.dumps({"pak": str(pak_path), "entries": entries_out}, indent=2),
        encoding="utf-8",
    )

    _log(f"  Patched {newly_patched} entr{'y' if newly_patched == 1 else 'ies'} in {pak_path.name}.")
    return newly_patched


def restore_pak_file(pak_path: Path, backup_path: Path, log_fn=None) -> int:
    """Restore the original hash bytes saved by *patch_pak_file*.

    Returns the number of entries restored.
    """
    _log = _safe_log(log_fn)

    if not backup_path.exists():
        return 0

    try:
        saved = json.loads(backup_path.read_text(encoding="utf-8"))
        entries = saved.get("entries", [])
    except (json.JSONDecodeError, KeyError) as exc:
        _log(f"  [WARN] Could not read PAK backup {backup_path.name}: {exc}")
        return 0

    if not pak_path.exists():
        _log(f"  [WARN] PAK file not found for restore: {pak_path}")
        return 0

    restored = 0
    with pak_path.open("r+b") as fh:
        header_bytes = fh.read(_HEADER_SIZE)
        try:
            _, _, _, _, entry_size = _read_header(header_bytes)
        except ValueError as exc:
            _log(f"  [WARN] Could not read PAK header during restore {pak_path.name}: {exc}")
            return 0
        hash_off = _HASH_OFFSET_V2 if entry_size == _ENTRY_SIZE_V2 else _HASH_OFFSET_V4

        for e in entries:
            idx = e["index"]
            original_bytes = bytes.fromhex(e["original"])
            file_off = _HEADER_SIZE + idx * entry_size + hash_off
            fh.seek(file_off)
            fh.write(original_bytes)
            restored += 1

    if restored:
        _log(f"  Restored {restored} entr{'y' if restored == 1 else 'ies'} in {pak_path.name}.")

    backup_path.unlink(missing_ok=True)
    return restored


def find_pak_files(game_root: Path) -> list[Path]:
    """Return RE Engine PAK files in *game_root*, patch PAKs first.

    RE Engine games (RE2, RE3, RE4, RE Village, …) use:
      re_chunk_000.pak
      re_chunk_000.pak.patch_001.pak, patch_002.pak, …
      dlc/re_dlc_*.pak

    Patch PAKs are processed first (higher priority), then the main PAK,
    then DLC PAKs.
    """
    main_pak = game_root / "re_chunk_000.pak"
    patches = sorted(game_root.glob("re_chunk_000.pak.patch_*.pak"), reverse=True)
    dlc_paks = sorted((game_root / "dlc").glob("*.pak"))
    result = list(patches)
    if main_pak.exists():
        result.append(main_pak)
    result.extend(dlc_paks)
    return result
