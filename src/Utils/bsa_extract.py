"""
bsa_extract.py
Pure-Python BSA v103 / v104 / v105 extractor.

Sister to bsa_reader.py (TOC-only) and bsa_writer.py (encoder). Walks the
TOC the same way as bsa_reader, then for each file seeks to its data
offset, optionally decompresses, and writes the bytes to the output
folder under the original folder/file path.

    extract_bsa(bsa_path, dest_dir, ...)

Compression algorithm matches what we use on write:
    v103 - zlib (deflate)   (Oblivion)
    v104 - zlib (deflate)
    v105 - LZ4 frame format
A compressed entry stores a 4-byte uncompressed-size prefix followed by
the compressed payload.

archive_flags bit 0x100 (embed_filename) is honoured: when set, each
file's data block is prefixed by a 1-byte length and the full backslash
path before the actual file payload. We strip that prefix before
decompressing / writing. Oblivion (v103) predates that feature and reuses
the bit for something else - most vanilla archives set it - so it is only
read from v104 up. Honouring it on v103 shifts every read by one bogus
prefix and corrupts all 8032 meshes in Oblivion - Meshes.bsa.

Output paths: the archive's stored folder + filename (already lowercase
forward-slash) is written directly under *dest_dir*. We do not preserve
the on-disk casing of any pre-existing loose files; the extractor's
output is always lowercase, matching what the engine actually loads.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Callable

import lz4.frame


class BsaExtractError(Exception):
    """Raised when extraction fails. Already-written files are left in
    place; the caller may choose to clean up *dest_dir* on failure."""


# ---------------------------------------------------------------------------
# Archive flag bits we care about on the read path
# ---------------------------------------------------------------------------

_AF_HAS_DIR_NAMES   = 0x0001
_AF_HAS_FILE_NAMES  = 0x0002
_AF_COMPRESSED_DEF  = 0x0004
_AF_EMBED_FILE_NAMES = 0x0100

_FILE_COMPRESS_INVERT = 0x40000000
_FILE_SIZE_MASK       = 0x3FFFFFFF


ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]


def extract_bsa(
    bsa_path: Path,
    dest_dir: Path,
    *,
    overwrite: bool = True,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> tuple[int, list[str]]:
    """Extract every file in *bsa_path* to *dest_dir*.

    Args:
        bsa_path:  Path to a BSA v104 or v105 archive.
        dest_dir:  Output root. Created if missing. Files are written
                   under the archive's stored folder/file path
                   (lowercase, forward-slash).
        overwrite: If True (default), existing loose files at the same
                   path are overwritten. If False, existing files are
                   skipped - useful when a user wants the BSA to seed a
                   mod folder without clobbering edits.
        progress:  Optional callback ``(done, total, current_path)``.
        cancel:    Optional callback returning True to abort. Already-
                   written files remain on disk.

    Returns:
        (file_count_written, list_of_rel_paths) - list_of_rel_paths is
        every file actually placed on disk (skipped files excluded).

    Raises:
        BsaExtractError: on I/O / format failure or unsupported version.
    """
    bsa_path = Path(bsa_path)
    dest_dir = Path(dest_dir)
    if not bsa_path.is_file():
        raise BsaExtractError(f"archive does not exist: {bsa_path}")

    try:
        with bsa_path.open("rb") as f:
            return _extract(f, dest_dir, overwrite, progress, cancel)
    except BsaExtractError:
        raise
    except (OSError, struct.error, ValueError, zlib.error) as exc:
        raise BsaExtractError(f"failed to extract {bsa_path.name}: {exc}") from exc


# ---------------------------------------------------------------------------
# Internal: parse + write
# ---------------------------------------------------------------------------

def index_bsa(bsa_path: Path | str) -> tuple[dict, dict[str, tuple[int, int]]]:
    """Map an archive's contents without reading any file data.

    Returns ``(info, entries)`` where *entries* maps the lowercase
    forward-slash internal path to ``(size_field, data_offset)``. Pass both
    back to :func:`read_bsa_entry` to pull a single file out.
    """
    with open(bsa_path, "rb") as f:
        info, flat = _parse_toc(f)
    return info, {rel: (sz, off) for rel, sz, off in flat}


def read_bsa_entry(bsa_path: Path | str, info: dict,
                   entry: tuple[int, int]) -> bytes:
    """Read and decompress one file, seeking straight to it."""
    size_field, data_offset = entry
    try:
        with open(bsa_path, "rb") as f:
            return _decode_entry(f, info, size_field, data_offset)
    except BsaExtractError:
        raise
    except (OSError, struct.error, ValueError, zlib.error) as exc:
        raise BsaExtractError(f"failed to read from {bsa_path}: {exc}") from exc


def _decode_entry(f, info: dict, size_field: int, data_offset: int,
                  rel: str = "") -> bytes:
    """Pull one file's bytes: seek, strip any embedded name, decompress."""
    on_disk_size = size_field & _FILE_SIZE_MASK
    invert = bool(size_field & _FILE_COMPRESS_INVERT)
    file_compressed = info["archive_compressed"] ^ invert

    f.seek(data_offset)
    block = f.read(on_disk_size)
    if len(block) < on_disk_size:
        raise BsaExtractError(f"short read for {rel}")

    # Strip embedded filename prefix if the archive uses it.
    if info["embed_filenames"]:
        if not block:
            raise BsaExtractError(f"empty embedded-filename block for {rel}")
        n = block[0]
        if 1 + n > len(block):
            raise BsaExtractError(f"bad embedded-filename length for {rel}")
        block = block[1 + n:]

    if not file_compressed:
        return block
    if len(block) < 4:
        raise BsaExtractError(f"compressed file too small for {rel}")
    # 4-byte original-size prefix precedes the compressed stream.
    body = block[4:]
    if info["version"] == 105:
        try:
            return lz4.frame.decompress(body)
        except RuntimeError as exc:
            # lz4.frame has no error class of its own; corrupt/truncated
            # frames surface as bare RuntimeError. Wrap it here, at the
            # decompress call, so a RuntimeError from elsewhere (e.g. a
            # caller's progress/cancel callback) is not mislabelled as an
            # archive failure.
            raise BsaExtractError(f"lz4 decompression failed for {rel}: {exc}") from exc
    return zlib.decompress(body)


def _parse_toc(f) -> tuple[dict, list[tuple[str, int, int]]]:
    """Walk the header + TOC, returning archive info and every file's
    ``(rel_path_lower, size_field, data_offset)`` in stored order."""
    magic = f.read(4)
    if magic != b"BSA\x00":
        raise BsaExtractError(f"not a BSA archive (magic={magic!r})")
    header = f.read(32)
    if len(header) < 32:
        raise BsaExtractError("truncated header")

    (
        version,
        folder_offset,
        archive_flags,
        folder_count,
        file_count,
        _total_folder_name_length,
        total_file_name_length,
        _file_flags,
    ) = struct.unpack("<IIIIIIII", header)

    if version not in (103, 104, 105):
        raise BsaExtractError(f"unsupported BSA version {version}")

    has_dir_names = bool(archive_flags & _AF_HAS_DIR_NAMES)
    has_file_names = bool(archive_flags & _AF_HAS_FILE_NAMES)
    archive_compressed = bool(archive_flags & _AF_COMPRESSED_DEF)
    # v103 has no embedded filenames; it reuses bit 0x100 for something else.
    embed_filenames = version >= 104 and bool(archive_flags & _AF_EMBED_FILE_NAMES)

    if not has_dir_names or not has_file_names:
        raise BsaExtractError("archive lacks folder or file names - cannot extract")

    # --- Folder records ---
    f.seek(folder_offset)
    folder_rec_size = 24 if version == 105 else 16
    raw = f.read(folder_rec_size * folder_count)
    if len(raw) < folder_rec_size * folder_count:
        raise BsaExtractError("truncated folder record block")
    folder_records: list[int] = []  # just file-count per folder
    for i in range(folder_count):
        base = i * folder_rec_size
        if version == 105:
            _hash, count, _pad = struct.unpack_from("<QII", raw, base)
        else:
            _hash, count, _offset = struct.unpack_from("<QII", raw, base)
        folder_records.append(count)

    # --- Folder name + file record blocks ---
    folder_names: list[str] = []
    # Flat list of (size_field, data_offset) for every file, in TOC order.
    file_specs: list[tuple[int, int]] = []
    for count in folder_records:
        nl_raw = f.read(1)
        if not nl_raw:
            raise BsaExtractError("truncated folder block")
        nl = nl_raw[0]
        name_bytes = f.read(nl)
        if len(name_bytes) < nl:
            raise BsaExtractError("truncated folder name")
        folder_name = name_bytes.rstrip(b"\x00").decode("latin-1").replace("\\", "/").lower()
        folder_names.append(folder_name)
        for _ in range(count):
            rec = f.read(16)
            if len(rec) < 16:
                raise BsaExtractError("truncated file record")
            _fhash, sz, do = struct.unpack("<QII", rec)
            file_specs.append((sz, do))

    # --- File name block ---
    name_block = f.read(total_file_name_length)
    if len(name_block) < total_file_name_length:
        raise BsaExtractError("truncated file-name block")
    names_text = name_block.decode("latin-1").lower()
    file_names = names_text.split("\x00")
    # Some shipped archives pad the name block with extra nulls (vanilla
    # MarketplaceTextures.bsa carries ten), so drop every trailing empty, not
    # just the one terminator.
    while file_names and file_names[-1] == "":
        file_names.pop()

    # Pair names with file records.
    if len(file_names) != len(file_specs):
        raise BsaExtractError(
            f"name count {len(file_names)} != file record count {len(file_specs)}"
        )

    # Pair files with their folder. file_specs is in the same order as
    # file_names - folder N's *count* file records, in turn.
    flat: list[tuple[str, int, int]] = []   # (rel_path_lower, size_field, data_offset)
    idx = 0
    for folder_idx, count in enumerate(folder_records):
        folder = folder_names[folder_idx]
        for _ in range(count):
            fn = file_names[idx]
            sz, do = file_specs[idx]
            rel = f"{folder}/{fn}" if folder else fn
            flat.append((rel, sz, do))
            idx += 1

    # Sanity: flat length == file_count.
    if len(flat) != file_count:
        raise BsaExtractError(
            f"file count drift: header={file_count} computed={len(flat)}"
        )

    info = {
        "version": version,
        "archive_compressed": archive_compressed,
        "embed_filenames": embed_filenames,
    }
    return info, flat


def _extract(
    f,
    dest_dir: Path,
    overwrite: bool,
    progress: ProgressCb | None,
    cancel: CancelCb | None,
) -> tuple[int, list[str]]:
    info, flat = _parse_toc(f)

    # --- Extract each file ---
    written: list[str] = []
    dest_dir.mkdir(parents=True, exist_ok=True)

    total = len(flat)
    for done, (rel, size_field, data_offset) in enumerate(flat, start=1):
        if cancel is not None and cancel():
            raise BsaExtractError("cancelled")

        data = _decode_entry(f, info, size_field, data_offset, rel)

        # Write to disk under dest_dir / rel.
        out_path = dest_dir / rel
        if not overwrite and out_path.exists():
            # Skip silently - caller chose not to clobber.
            if progress is not None:
                progress(done, total, rel)
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-ish write: temp file + rename.  We don't cross-link with
        # atomic_writer because dest may not be writable for the .tmp
        # sibling (e.g. read-only mounts) - a plain write is fine here
        # since extraction is recoverable: rerun the unpack.
        with out_path.open("wb") as out:
            out.write(data)
        written.append(rel)

        if progress is not None:
            progress(done, total, rel)

    return len(written), written
