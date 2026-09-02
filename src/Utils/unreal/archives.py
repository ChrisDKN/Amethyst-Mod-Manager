"""List asset paths inside Unreal Engine .pak and IoStore .utoc archives
(table-of-contents only - no file data is ever decompressed).

.pak - the index at the tail of the archive stores every entry's path as a
plaintext string (all pak versions, v1 through v11). The footer is located
(magic 0x5A6F12E1 within the last few KB) and the index parsed:

  * v1–v9 store one full path FString per entry, relative to the index's
    mount point - read each string, skip the version-dependent FPakEntry
    struct behind it, prepend the mount point.
  * v10/v11 replaced that with a path-hash index plus a "full directory
    index" (directory-name → file-name string maps); the primary index
    holds its offset. Both are parsed and mount + directory + name joined.

If structured parsing fails (unknown future version, corrupt index), a
regex plaintext scan of the index region recovers whatever asset paths it
can - the approach the Nexus Mods App uses for its pak conflict diagnostic,
restricted here to the index region so multi-GB paks cost only one small
read instead of a whole-file scan. An AES-encrypted index (bEncryptedIndex
in the footer) yields an empty result - paths are not recoverable without
the game's key.

.utoc - IoStore containers store their directory index as a structured
tree (FIoDirectoryIndexResource): directory/file entries referencing a
string table of individual path *segments*. A plaintext scan would only
recover basenames, so the header and directory index are parsed properly
and full paths reconstructed. When the directory index is missing or
AES-encrypted (common - e.g. Marvel Rivals), the parser falls back to the
unencrypted FIoChunkId table: chunk ids are deterministic hashes of the
package path plus a chunk-type byte, so identical ``chunkid:...`` keys
across two mods still mean "same package" and conflicts are still caught -
they just display as opaque ids instead of readable paths.

Companion .ucas files hold only bulk data (no names) and are not scanned.

Paths are returned lowercase, forward-slash separated, with any leading
"../" mount-point hops stripped, matching the normalisation used by
bsa_reader so the shared conflict engine can compare them directly.

Caveat: pak entry paths are relative to the pak's mount point and the
mount point itself is not folded in (it isn't recoverable from a regex
hit). Mod paks virtually always mount at "../../../", so relative paths
are directly comparable; a mod pak with an exotic deeper mount point may
produce keys that miss (never falsely flag) a conflict.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

UE_ARCHIVE_EXTENSIONS = frozenset({".pak", ".utoc"})

_PAK_MAGIC = struct.pack("<I", 0x5A6F12E1)  # FPakInfo::PakFile_Magic
# Footer size varies by pak version (44 bytes at v1 up to ~230 at v11 with
# encryption guid + compression method names). One 4 KB tail read covers all.
_PAK_FOOTER_WINDOW = 4096
# Sanity cap on the index region read - real mod pak indexes are KB-scale.
_MAX_INDEX_BYTES = 256 * 1024 * 1024

_UTOC_MAGIC = b"-==--==--==--==-"
_UTOC_HEADER_SIZE = 144

# EIoContainerFlags
_IO_FLAG_ENCRYPTED = 1 << 1
_IO_FLAG_SIGNED = 1 << 2
_IO_FLAG_INDEXED = 1 << 3

# EIoChunkType chunk-id type bytes that carry package content. Restricting
# the chunk-id fallback to these avoids flagging bookkeeping chunks
# (ContainerHeader, ShaderCodeLibrary, ...) that differ per container anyway.
_IO_PACKAGE_CHUNK_TYPES = frozenset({1, 2, 3, 4})
# (ExportBundleData, BulkData, OptionalBulkData, MemoryMappedBulkData)

# Plaintext asset-path extraction. Extension list extends the Nexus Mods App
# regex (uasset|uexp|ubulk|cfg) with the other conflict-relevant UE content
# types. Dots are allowed mid-path for names like "Foo.Bar.uasset"; leading
# dots/slashes from mount-point hops are stripped afterwards.
_UE_PATH_RX = re.compile(
    rb"[-\w/.]*[-\w]\.(?:"
    rb"uasset|uexp|ubulk|uptnl|umap|locres|locmeta|uplugin|"
    rb"ushaderbytecode|upipelinecache|cfg|ini|json|bnk|wem"
    rb")",
    re.IGNORECASE,
)


def read_ue_archive_file_list(path: Path | str) -> list[str]:
    """Return asset paths inside a UE .pak / .utoc as lowercase strings.

    Dispatches on file extension. Returns an empty list on unrecognised
    formats, encrypted-and-unindexable archives, or I/O errors.
    """
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext == ".utoc":
            return _read_utoc(p)
        if ext == ".pak":
            return _read_pak(p)
    except (OSError, struct.error, ValueError, OverflowError, MemoryError):
        pass
    return []


def _normalise(raw: str) -> str:
    """Lowercase, forward slashes, leading '../' hops and slashes removed."""
    s = raw.replace("\\", "/").lower()
    while s.startswith("../"):
        s = s[3:]
    return s.lstrip("/.")


def _extract_paths(blob: bytes) -> list[str]:
    """Regex plaintext asset paths out of raw index bytes."""
    seen: set[str] = set()
    for m in _UE_PATH_RX.finditer(blob):
        s = _normalise(m.group(0).decode("latin-1"))
        if s:
            seen.add(s)
    return sorted(seen)


# ---------------------------------------------------------------------------
# .pak
# ---------------------------------------------------------------------------

def _read_pak(pak_path: Path) -> list[str]:
    """Locate the pak footer, parse the index; regex the region on failure."""
    with pak_path.open("rb") as f:
        f.seek(0, 2)
        fsize = f.tell()
        if fsize < 44:  # smaller than the smallest possible footer
            return []
        window = min(fsize, _PAK_FOOTER_WINDOW)
        f.seek(fsize - window)
        tail = f.read(window)
        pos = tail.rfind(_PAK_MAGIC)
        if pos < 0 or pos + 24 > len(tail):
            return []
        # Fields after the magic are identical across pak versions:
        # version(i32), index_offset(i64), index_size(i64). Version-dependent
        # fields (encryption guid, compression names) sit before/after and
        # don't affect these offsets.
        (version,) = struct.unpack_from("<i", tail, pos + 4)
        index_offset, index_size = struct.unpack_from("<qq", tail, pos + 8)
        if index_offset <= 0 or index_size <= 0 or index_offset >= fsize:
            return []
        # bEncryptedIndex sits immediately before the magic from v4 on.
        if version >= 4 and pos >= 1 and tail[pos - 1] == 1:
            return []  # AES-encrypted index - nothing recoverable
        # Read from the primary index through EOF: v10/v11 paks store the
        # full directory index (the part holding path strings) *after* the
        # primary index, before the footer, so index_size alone would miss it.
        read_len = min(fsize - index_offset, _MAX_INDEX_BYTES)
        f.seek(index_offset)
        blob = f.read(read_len)
    try:
        if version >= 10:
            paths = _parse_pak_index_v10(blob, index_offset)
        else:
            # UE 4.22 ("v8a") serialises FPakEntry's compression-method
            # index as u8; 4.23+ ("v8b") widened it to u32. Both stamp
            # version 8 - they differ only in footer length (4 vs 5
            # compression-name slots of 32 bytes after the 44 fixed bytes).
            comp_u8 = version == 8 and (window - pos) <= 188
            paths = _parse_pak_index_legacy(blob[:index_size], version, comp_u8)
        if paths:
            return paths
    except (struct.error, ValueError, OverflowError, MemoryError, IndexError):
        pass
    # Unknown/corrupt index layout - plaintext scan recovers what it can.
    return _extract_paths(blob)


def _parse_pak_index_legacy(
    blob: bytes, version: int, comp_u8: bool = False,
) -> list[str]:
    """Parse a v1–v9 pak index: mount point, then per entry a full-path
    FString followed by an FPakEntry struct (skipped by computed size)."""
    mount, o = _read_fstring_checked(blob, 0)
    (count,) = struct.unpack_from("<i", blob, o)
    o += 4
    if count < 0 or count > 10_000_000:
        raise ValueError("implausible pak entry count")
    prefix = _mount_prefix(mount)
    result: list[str] = []
    for _ in range(count):
        name, o = _read_fstring_checked(blob, o)
        o = _skip_pak_entry(blob, o, version, comp_u8)
        norm = _normalise(name)
        if norm:
            result.append(prefix + norm)
    return sorted(set(result))


def _skip_pak_entry(blob: bytes, o: int, version: int, comp_u8: bool) -> int:
    """Advance past one serialised FPakEntry (v1–v9 layouts)."""
    # Offset(i64) Size(i64) UncompressedSize(i64)
    o += 24
    # CompressionMethod: i32 enum up to v7; from v8 an index into the
    # footer's method-name list - u8 in UE 4.22 (v8a), u32 from 4.23 (v8b+).
    if comp_u8:
        compression = blob[o]
        o += 1
    else:
        (compression,) = struct.unpack_from("<I", blob, o)
        o += 4
    if version <= 1:
        o += 8  # Timestamp
    o += 20  # Hash (SHA1)
    if version >= 3:
        if compression != 0:
            (block_count,) = struct.unpack_from("<I", blob, o)
            if block_count > 1_000_000:
                raise ValueError("implausible compression block count")
            o += 4 + block_count * 16  # FPakCompressedBlock{Start,End}
        o += 1  # Flags (encrypted / deleted)
        o += 4  # CompressionBlockSize
    return o


def _parse_pak_index_v10(blob: bytes, index_offset: int) -> list[str]:
    """Parse a v10/v11 primary index + full directory index.

    ``blob`` starts at the primary index (absolute file offset
    ``index_offset``) and extends to EOF. The full directory index - the
    section holding the path strings - is written after the primary index
    and path-hash index, so it is inside the blob; its absolute offset from
    the primary index header is rebased into blob coordinates.
    """
    mount, o = _read_fstring_checked(blob, 0)
    _num_entries, _path_hash_seed = struct.unpack_from("<iQ", blob, o)
    o += 12
    (has_path_hash_index,) = struct.unpack_from("<i", blob, o)
    o += 4
    if has_path_hash_index:
        o += 16 + 20  # offset(i64), size(i64), hash(20)
    (has_full_dir_index,) = struct.unpack_from("<i", blob, o)
    o += 4
    if not has_full_dir_index:
        raise ValueError("pak has no full directory index")
    fdi_offset, fdi_size = struct.unpack_from("<qq", blob, o)
    rel = fdi_offset - index_offset
    if rel < 0 or fdi_size <= 0 or rel + fdi_size > len(blob):
        raise ValueError("full directory index out of bounds")
    fdi = blob[rel:rel + fdi_size]
    prefix = _mount_prefix(mount)
    (dir_count,) = struct.unpack_from("<i", fdi, 0)
    if dir_count < 0 or dir_count > 10_000_000:
        raise ValueError("implausible directory count")
    o = 4
    result: list[str] = []
    for _ in range(dir_count):
        dir_name, o = _read_fstring_checked(fdi, o)
        (file_count,) = struct.unpack_from("<i", fdi, o)
        o += 4
        if file_count < 0 or file_count > 10_000_000:
            raise ValueError("implausible file count")
        dir_norm = _normalise(dir_name)
        if dir_norm and not dir_norm.endswith("/"):
            dir_norm += "/"
        for _ in range(file_count):
            fname, o = _read_fstring_checked(fdi, o)
            o += 4  # PakEntryLocation (i32)
            norm = fname.replace("\\", "/").lower()
            if norm:
                result.append(prefix + dir_norm + norm)
    return sorted(set(result))


def _mount_prefix(mount: str) -> str:
    """Normalised mount point ending in '/' (or '' for a root mount)."""
    prefix = _normalise(mount)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return prefix


def _read_fstring_checked(blob: bytes, o: int) -> tuple[str, int]:
    """_read_fstring with a plausibility bound so AES garbage or corrupt
    data raises instead of allocating a gigabyte string."""
    (n,) = struct.unpack_from("<i", blob, o)
    if n < -65536 or n > 65536:
        raise ValueError("implausible FString length")
    return _read_fstring(blob, o)


# ---------------------------------------------------------------------------
# .utoc
# ---------------------------------------------------------------------------

def _read_utoc(utoc_path: Path) -> list[str]:
    """Parse the IoStore TOC directory index into full asset paths.

    Falls back to package chunk-id keys when the directory index is absent
    or encrypted, so conflicts are still detected (as opaque ids).
    """
    with utoc_path.open("rb") as f:
        hdr = f.read(_UTOC_HEADER_SIZE)
        if len(hdr) < _UTOC_HEADER_SIZE or hdr[:16] != _UTOC_MAGIC:
            return []
        version = hdr[16]
        (toc_header_size, entry_count, block_count, block_entry_size,
         comp_name_count, comp_name_len, _comp_block_size,
         dir_index_size, _partition_count) = struct.unpack_from("<9I", hdr, 20)
        container_flags = hdr[80]
        perfect_hash_seeds = struct.unpack_from("<I", hdr, 84)[0] if version >= 4 else 0
        chunks_no_hash = struct.unpack_from("<I", hdr, 96)[0] if version >= 5 else 0
        if toc_header_size < _UTOC_HEADER_SIZE:
            toc_header_size = _UTOC_HEADER_SIZE

        # Section offsets after the header, in serialisation order.
        offset = toc_header_size
        chunk_ids_offset = offset
        offset += entry_count * 12                      # FIoChunkId[]
        offset += entry_count * 10                      # FIoOffsetAndLength[]
        offset += perfect_hash_seeds * 4
        offset += chunks_no_hash * 4
        offset += block_count * block_entry_size        # compression blocks
        offset += comp_name_count * comp_name_len       # method names
        if container_flags & _IO_FLAG_SIGNED:
            f.seek(offset)
            raw = f.read(4)
            if len(raw) < 4:
                return []
            hash_size = struct.unpack("<I", raw)[0]
            offset += 4 + hash_size * 2 + block_count * 20  # sigs + SHA1s

        indexed = bool(container_flags & _IO_FLAG_INDEXED) and dir_index_size > 0
        encrypted = bool(container_flags & _IO_FLAG_ENCRYPTED)
        if indexed and not encrypted:
            f.seek(offset)
            blob = f.read(min(dir_index_size, _MAX_INDEX_BYTES))
            paths = _parse_directory_index(blob)
            if paths:
                return paths

        # No readable directory index - fall back to package chunk ids.
        f.seek(chunk_ids_offset)
        raw = f.read(entry_count * 12)
    result: set[str] = set()
    for i in range(len(raw) // 12):
        cid = raw[i * 12:(i + 1) * 12]
        if cid[11] in _IO_PACKAGE_CHUNK_TYPES:
            result.add("chunkid:" + cid.hex())
    return sorted(result)


def _read_fstring(blob: bytes, o: int) -> tuple[str, int]:
    """Deserialise a UE FString: i32 length (negative = UTF-16), then bytes."""
    (n,) = struct.unpack_from("<i", blob, o)
    o += 4
    if n == 0:
        return "", o
    if n < 0:
        n = -n
        s = blob[o:o + n * 2].decode("utf-16-le", errors="replace")
        o += n * 2
    else:
        s = blob[o:o + n].decode("utf-8", errors="replace")
        o += n
    return s.rstrip("\x00"), o


def _parse_directory_index(blob: bytes) -> list[str]:
    """Rebuild full paths from an FIoDirectoryIndexResource buffer.

    Layout: FString mount point, then arrays of directory entries
    (name, first_child, next_sibling, first_file - u32 string-table /
    entry indices, ~0 = none), file entries (name, next_file, user_data)
    and finally the string table of path segments.
    """
    none = 0xFFFFFFFF
    mount, o = _read_fstring(blob, 0)
    (dir_count,) = struct.unpack_from("<I", blob, o)
    o += 4
    dirs = [struct.unpack_from("<4I", blob, o + i * 16) for i in range(dir_count)]
    o += dir_count * 16
    (file_count,) = struct.unpack_from("<I", blob, o)
    o += 4
    files = [struct.unpack_from("<3I", blob, o + i * 12) for i in range(file_count)]
    o += file_count * 12
    (str_count,) = struct.unpack_from("<I", blob, o)
    o += 4
    strings: list[str] = []
    for _ in range(str_count):
        s, o = _read_fstring(blob, o)
        strings.append(s)

    if not dirs:
        return []
    mount_prefix = _normalise(mount)
    if mount_prefix and not mount_prefix.endswith("/"):
        mount_prefix += "/"

    result: list[str] = []
    # Iterative walk from the root (entry 0, unnamed). Each stack element
    # carries the *parent* prefix: siblings share it, children extend it.
    # The visited set guards against cycles in a corrupt index.
    stack: list[tuple[int, str]] = [(0, mount_prefix)]
    visited: set[int] = set()
    while stack:
        di, parent = stack.pop()
        if di == none or di >= dir_count or di in visited:
            continue
        visited.add(di)
        name_idx, first_child, next_sibling, first_file = dirs[di]
        if di == 0 or name_idx == none or name_idx >= len(strings):
            full = parent
        else:
            full = parent + strings[name_idx].replace("\\", "/").lower() + "/"
        fi = first_file
        seen_files: set[int] = set()
        while fi != none and fi < file_count and fi not in seen_files:
            seen_files.add(fi)
            fname_idx, next_file, _user = files[fi]
            if fname_idx != none and fname_idx < len(strings):
                result.append(full + strings[fname_idx].replace("\\", "/").lower())
            fi = next_file
        stack.append((next_sibling, parent))
        stack.append((first_child, full))
    return sorted(set(result))
