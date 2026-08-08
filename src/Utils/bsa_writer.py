"""
bsa_writer.py
Pure-Python BSA v104 / v105 encoder.

Sister to bsa_reader.py (which decodes the same TOC formats). Produces a
single archive containing all packable loose files under a source directory:

    write_bsa(bsa_path, source_dir, version=105, compress=True, ...)

The on-disk format is documented in bsa_reader.py's module docstring; the
write order is:

    1. Header (36 B)
    2. Folder records (16 B each for v104, 24 B each for v105) - folders
       sorted by TES4 hash.
    3. For each folder: 1 B name length + null-terminated folder name (using
       backslash separators), then that folder's file records (16 B each:
       file name hash, size+flags, data offset). Files within a folder
       sorted by TES4 hash.
    4. File name block: concatenated null-terminated file names in
       folder-then-file iteration order.
    5. File data blob, in the same order as the file records. Compressed
       entries are prefixed by a 4-byte little-endian original-size header
       followed by the compressed payload.

The compression algorithm is **version-dependent**:

    v104 (Oblivion / FO3 / FNV / Skyrim LE):  zlib (deflate)
    v105 (Skyrim Special Edition / VR):       LZ4 frame format

Skyrim SE switched to LZ4 for faster decompression at load time. Storing
zlib bytes inside a v105 archive makes the engine silently fail every
asset lookup (visible as missing-texture purple grids) - the file list
parses fine, the data extracts fine with zlib, but the engine refuses to
decode it because it expects LZ4-frame magic at the start of each
compressed payload.

Compression is global (archive_flags bit 2). The per-file size field has
bit 30 set when that file's compression state is **inverted** relative to
the archive default, so we use this bit to mark known-incompressible
extensions (.wav, .mp3, .ogg, .flac, .xwm, .mp4, .bk2, .fuz, .lip,
.*strings) as "do not compress" even when the archive default is
"compress".

The TES4 hash matches BSArch / libbsarch / bethutil. Folder names are
hashed with backslash separators, file names without their parent path,
both lowercase, both encoded as cp1252.

BA2 (Fallout 4 / Starfield) and Morrowind v103 are not supported here.
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path
from typing import Callable

import lz4.frame

from Utils.atomic_write import atomic_writer


# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------

class BsaWriteError(Exception):
    """Raised when packing fails. Atomic writer guarantees no .bsa is left
    behind on the destination path; a stale .bsa.tmp may remain on disk."""


# Re-export for callers importing Utils.bsa_writer.is_packable.
from Utils.archive_rules import is_packable  # noqa: F401

# Per-extension "do not compress" list - kept here because it drives
# ``write_bsa``'s compression-bit decision and is independent of the
# packable-or-not question (an .ogg under sound/ IS packable but should
# not be re-compressed).  Superset of bethutil's incompressible-set
# extension list.
_INCOMPRESSIBLE_EXT: frozenset[str] = frozenset({
    ".wav", ".mp3", ".ogg", ".flac", ".xwm",
    ".mp4", ".bk2",
    ".fuz", ".lip",
    ".dlstrings", ".ilstrings", ".strings",
})


def _is_incompressible(name_lower: str) -> bool:
    dot = name_lower.rfind(".")
    return dot >= 0 and name_lower[dot:] in _INCOMPRESSIBLE_EXT


# ---------------------------------------------------------------------------
# Per-game version helper
# ---------------------------------------------------------------------------

# Game IDs that use BSA v105 (Skyrim SE engine onward). Everything else
# Bethesda-flavoured uses v104 (Oblivion / FO3 / FNV / Skyrim LE).
# Game IDs come from each handler's `game_id` property - see src/Games/.
_V105_GAME_IDS: frozenset[str] = frozenset({
    "skyrim_se",   # Skyrim Special Edition (Games/Bethesda/skyrim_se.py)
    "skyrimvr",    # Skyrim VR
    "enderalse",   # Enderal Special Edition
})

_V104_GAME_IDS: frozenset[str] = frozenset({
    "Oblivion",
    "Fallout3", "Fallout3GOTY",
    "FalloutNV",
    "skyrim",      # Skyrim Legendary Edition (Games/Bethesda/skyrim.py)
    "enderal",     # Enderal LE
})


def bsa_version_for_game(game_id: str | None) -> int | None:
    """Return 104 or 105 for a Bethesda game id, or None if BSA packing is
    not supported for that game (BA2 games, Morrowind, non-Bethesda)."""
    if not game_id:
        return None
    if game_id in _V105_GAME_IDS:
        return 105
    if game_id in _V104_GAME_IDS:
        return 104
    return None


# ---------------------------------------------------------------------------
# Minimal stub plugin (TES4 record)
#
# A BSA only auto-loads if a same-named plugin is present in the load order.
# For a mod that ships only loose assets (no real .esp), we synthesise a
# minimal empty plugin: one TES4 record with one HEDR subrecord, nothing
# else. The engine accepts this and uses the file's presence as the trigger
# to mount <ModName>.bsa from the Data folder.
#
# The TES4 record header size is GAME-SPECIFIC:
#
# Oblivion - 20-byte header (no timestamp/version fields; they postdate it):
#     type        4 B   b"TES4"
#     datasize    4 B   uint32 LE - size of subrecord block (excludes header)
#     flags       4 B   record flags
#     formID      4 B   0 for the TES4 header record
#     vcs         4 B   version-control info, 0 is fine
#
# FO3/FNV/Skyrim/FO4 (+VR/Enderal SE) - 24-byte header (8-byte trailer):
#     type/datasize/flags/formID  as above (16 B)
#     timestamp   2 B   editor timestamp, 0 is fine
#     last_mod    2 B   last user-mod-index, 0
#     internal_v  2 B   per-game; these engines refuse plugins with version 0!
#                       LE=43, SSE=44, FO3/NV=15, FO4=131
#     unknown     2 B   0
#
# HEDR subrecord (18 B total, 12 B data):
#     type     4 B   b"HEDR"
#     size     2 B   uint16 LE - 12
#     version  4 B   float32 LE - game-specific (0.94 / 1.0 / 1.7)
#     numRecs  4 B   uint32 LE - 0
#     nextID   4 B   uint32 LE - 0x800 (lowest valid free FormID)
#
# Getting the header size wrong shifts HEDR off its expected offset and the
# engine silently rejects the plugin: assets ship in the BSA but never load
# (missing-texture purple grid, or just vanilla content). Writing the FO3+
# 24-byte header for Oblivion put HEDR at offset 24 instead of 20 and broke
# plugin-association BSA loading - the bug this split fixes.
# ---------------------------------------------------------------------------

# Per-game HEDR version - what the official editors stamp.
_HEDR_VERSION: dict[str, float] = {
    "Oblivion":     1.0,
    "Fallout3":     0.94,
    "Fallout3GOTY": 0.94,
    "FalloutNV":    0.94,
    "skyrim":       0.94,    # Skyrim LE
    "skyrim_se":    1.7,     # Skyrim SE/AE 1.6+
    "skyrimvr":     1.7,
    "enderal":      0.94,
    "enderalse":    1.7,
    "Fallout4":     0.95,    # FO4 / FO4 VR / FO4 Next-Gen all use 0.95
    "Fallout4VR":   0.95,
}

# Per-game TES4 record-header internal version (uint16 at offset 20). xEdit
# calls this "Form Version"; the engine checks it on load.
_INTERNAL_VERSION: dict[str, int] = {
    "Oblivion":     0,       # Oblivion's TES4 record predates this field.
    "Fallout3":     15,
    "Fallout3GOTY": 15,
    "FalloutNV":    15,
    "skyrim":       43,      # Skyrim LE
    "skyrim_se":    44,      # Skyrim SE/AE
    "skyrimvr":     44,
    "enderal":      43,
    "enderalse":    44,
    "Fallout4":     131,     # FO4 / FO4 VR
    "Fallout4VR":   131,
}

# ESL flag - only meaningful for engines that support light masters.
# The existing TES4_FLAG_ESL constant in plugin_parser.py is 0x0200.
_TES4_FLAG_ESL = 0x0200
_ESL_CAPABLE_GAMES: frozenset[str] = frozenset({
    "skyrim_se", "skyrimvr", "enderalse",
    "Fallout4", "Fallout4VR",     # FO4 introduced the ESL flag
})


def is_our_stub_plugin(plugin_path: Path) -> bool:
    """Return True if *plugin_path* looks like a stub previously generated
    by ``write_stub_plugin``.

    Recognised variants (all start with a TES4 record carrying only HEDR
    [+ optional empty CNAM], which real authored plugins never do - they
    carry MAST/records and run well over 60 bytes):
      - FO3+ 24-byte header: 49 B (HEDR+CNAM) or older 42 B (HEDR only),
        HEDR at offset 24. NB: Oblivion stubs written before the header-size
        fix also land here (49 B, HEDR at 24) so they re-pack cleanly.
      - Oblivion 20-byte header: 45 B (HEDR+CNAM) or 38 B (HEDR only),
        HEDR at offset 20.
    """
    try:
        size = plugin_path.stat().st_size
        if size not in (38, 42, 45, 49):
            return False
        data = plugin_path.read_bytes()
    except OSError:
        return False
    if data[0:4] != b"TES4":
        return False
    # datasize: 18 (HEDR only) or 25 (HEDR + 1-byte CNAM).
    datasize = struct.unpack_from("<I", data, 4)[0]
    if datasize not in (18, 25):
        return False
    # HEDR follows the record header: offset 20 (Oblivion) or 24 (FO3+).
    return data[20:24] == b"HEDR" or data[24:28] == b"HEDR"


def write_stub_plugin(
    plugin_path: Path,
    *,
    game_id: str,
    esl: bool | None = None,
) -> None:
    """Write a minimal TES4 plugin to *plugin_path* (atomically).

    The plugin contains a single TES4 record with one HEDR subrecord and
    an empty CNAM (author) subrecord - no masters, no content. Used to
    make a same-named ``<ModName>.bsa`` auto-load.

    Args:
        plugin_path: Output path (typically ``<ModName>.esp``).
        game_id:     One of the recognised Bethesda ``game_id`` values.
                     Determines the HEDR version and TES4 internal version.
        esl:         Set the ESL (light master) flag. ``None`` (default)
                     auto-enables it on ESL-capable engines (Skyrim
                     SE/VR, Enderal SE) - recommended, since an empty
                     stub trivially satisfies ESL constraints and keeps
                     the load order from filling up. Pass ``False`` to
                     force a regular plugin slot, or ``True`` to demand
                     it (no-op on engines that don't support ESL).

    Raises:
        BsaWriteError: on I/O failure or unrecognised game.
    """
    plugin_path = Path(plugin_path)
    hedr_version = _HEDR_VERSION.get(game_id)
    internal_version = _INTERNAL_VERSION.get(game_id)
    if hedr_version is None or internal_version is None:
        raise BsaWriteError(f"no plugin-format mapping for game {game_id!r}")

    if esl is None:
        esl = game_id in _ESL_CAPABLE_GAMES
    record_flags = 0
    if esl and game_id in _ESL_CAPABLE_GAMES:
        record_flags |= _TES4_FLAG_ESL

    # HEDR subrecord - 12 B of data prefixed by 6 B subrecord header.
    hedr = struct.pack("<4sHfII", b"HEDR", 12, hedr_version, 0, 0x800)
    # CNAM (author) subrecord - 1 B null-terminated empty string.  Every
    # "real" plugin carries one; LOOT/xEdit complain about plugins without.
    cnam = struct.pack("<4sH", b"CNAM", 1) + b"\x00"
    subrecord_block = hedr + cnam

    # TES4 record header. Oblivion uses a 20-byte header (the trailing 8-byte
    # timestamp/version trailer postdates it); FO3+ use 24 bytes. Writing the
    # 24-byte form for Oblivion shifts HEDR to offset 24 and the engine refuses
    # the plugin, so its BSA never auto-loads.
    if game_id == "Oblivion":
        record_header = struct.pack(
            "<4sIIII",
            b"TES4",
            len(subrecord_block),  # datasize
            record_flags,
            0,                     # formID
            0,                     # version-control info
        )
    else:
        # The trailing 8 B encode (in order):
        # timestamp(u16) | last_mod(u16) | internal_version(u16) | unknown(u16).
        record_header = struct.pack(
            "<4sIIIHHHH",
            b"TES4",
            len(subrecord_block),  # datasize
            record_flags,
            0,                     # formID
            0,                     # timestamp
            0,                     # last_mod
            internal_version,      # internal version (== "Form Version" in xEdit)
            0,                     # unknown
        )

    payload = record_header + subrecord_block
    try:
        with atomic_writer(plugin_path, "wb", encoding=None) as fh:
            fh.write(payload)
    except (OSError, struct.error) as exc:
        raise BsaWriteError(f"failed to write plugin: {exc}") from exc


# ---------------------------------------------------------------------------
# TES4 hash - folder + file. Matches BSArch / libbsarch.
#
# The algorithm splits the name at the final '.' (file extension) and
# computes:
#
#     hash1 (low 32 bits) =
#         last_char | (second_to_last_char << 8) | (length << 16) | (first_char << 24)
#
#     plus a small per-extension "magic" OR for .kf, .nif, .dds, .wav.
#
#     hash2 = polynomial accumulation over the middle chars (cp1252 bytes,
#             skipping first and last two chars of the "root" portion),
#             multiplier 0x1003F.
#
#     hash3 = same polynomial accumulation over the extension bytes
#             (including the leading '.'), multiplier 0x1003F.
#
#     final hash (uint64) = ((hash2 + hash3) << 32) | hash1
#
# Folder hashes use the same routine with the *full folder path* as the
# "name" (no extension splitting - there's no '.' in normal folder names).
# ---------------------------------------------------------------------------


# Per-extension "magic OR" applied to hash1 - verified against vanilla
# BSAs.  Same table is consumed by the self-test as a known-good
# reference; if you change a value, update both places.
TES4_EXT_MAGIC: dict[bytes, int] = {
    b".kf":  0x80,
    b".nif": 0xA000,
    b".dds": 0x8080,
    b".wav": 0x80000000,
}


def _tes4_hash(name: bytes) -> int:
    """Hash a single name (cp1252 bytes, lowercase, no leading slash)."""
    if not name:
        return 0

    # Split at last '.' to separate root and extension.
    dot = name.rfind(b".")
    if dot >= 0:
        root = name[:dot]
        ext = name[dot:]
    else:
        root = name
        ext = b""

    if not root:
        # All-extension input (e.g. ".dds") - treat the whole thing as root.
        root = name
        ext = b""

    n = len(root)
    h1 = (
        root[n - 1]
        | ((root[n - 2] << 8) if n >= 3 else 0)
        | (n << 16)
        | (root[0] << 24)
    ) & 0xFFFFFFFF
    h1 |= TES4_EXT_MAGIC.get(ext, 0)

    h2 = 0
    if n > 3:
        for c in root[1:n - 2]:
            h2 = (h2 * 0x1003F + c) & 0xFFFFFFFF

    h3 = 0
    for c in ext:
        h3 = (h3 * 0x1003F + c) & 0xFFFFFFFF

    h_high = (h2 + h3) & 0xFFFFFFFF
    return (h_high << 32) | h1


def tes4_hash_file(filename: str) -> int:
    """Hash a leaf file name (no parent path), lowercased, cp1252-encoded."""
    return _tes4_hash(filename.lower().encode("cp1252", errors="replace"))


def tes4_hash_folder(folder_path: str) -> int:
    """Hash a folder path. Backslash-separated, lowercased, cp1252-encoded."""
    return _tes4_hash(
        folder_path.replace("/", "\\").lower().encode("cp1252", errors="replace")
    )


# ---------------------------------------------------------------------------
# Archive flag / file-flag constants
# ---------------------------------------------------------------------------

_AF_HAS_DIR_NAMES   = 0x0001
_AF_HAS_FILE_NAMES  = 0x0002
_AF_COMPRESSED_DEF  = 0x0004
_AF_RETAIN_DIR_NAMES = 0x0008  # set by Bethesda tools; harmless to include
_AF_RETAIN_FILE_NAMES = 0x0010
_AF_XBOX360 = 0x0040  # not set
_AF_EMBED_FILE_NAMES = 0x0100  # not set - names live in name block

# file_flags: meshes(0x1) + textures(0x2) - generic safe value matching what
# BSArch writes for arbitrary archives. The game largely ignores this for
# v104/v105.
_FILE_FLAGS = 0x0003

# Per-file size field bit 30: when set, this file's compression state is
# inverted relative to archive_flags bit 2.
_FILE_COMPRESS_INVERT = 0x40000000
_FILE_SIZE_MASK       = 0x3FFFFFFF


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def _collect_files(
    source_dir: Path,
    excluded_keys: frozenset[str] = frozenset(),
    game_id: str | None = None,
    texture_mode: str = "all",
) -> tuple[dict[str, list[tuple[str, Path]]], list[str]]:
    """Walk *source_dir* and return:

        ({backslash_folder_lower: [(file_lower, abs_path), ...]},
         [rel_key_lower_fwd_slash, ...])

    Files passing ``is_packable(rel_key, game_id)`` are kept; entries
    in *excluded_keys* and root-level files (BSA requires every file
    under a folder) are dropped silently.  ``texture_mode`` further
    filters the allowlist hits: ``"all"`` keeps everything, ``"exclude"``
    drops textures, ``"only"`` keeps only textures (see
    ``archive_rules.texture_extensions_for_game``).
    """
    from Utils.archive_rules import texture_extensions_for_game
    texture_exts = (
        texture_extensions_for_game(game_id) if texture_mode != "all"
        else frozenset()
    )

    folders: dict[str, list[tuple[str, Path]]] = {}
    packed_rel_keys: list[str] = []
    src = source_dir.resolve()
    src_str = str(src)

    for dirpath, dirnames, filenames in os.walk(src):
        # Skip dot-directories (e.g. .git) at any depth.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        rel_dir = os.path.relpath(dirpath, src_str)
        if rel_dir == ".":
            # Root-level files cannot be packed (BSA stores everything
            # under a folder).  Skip silently - they remain loose.
            continue

        rel_dir_norm = rel_dir.replace("/", "\\").replace(os.sep, "\\").lower()
        rel_dir_fwd = rel_dir_norm.replace("\\", "/")

        for fn in filenames:
            rel_key = rel_dir_fwd + "/" + fn.lower()
            if rel_key in excluded_keys:
                continue
            if not is_packable(rel_key, game_id):
                continue
            if texture_mode != "all":
                fn_lower = fn.lower()
                dot = fn_lower.rfind(".")
                ext = fn_lower[dot:] if dot >= 0 else ""
                is_texture = ext in texture_exts
                if texture_mode == "exclude" and is_texture:
                    continue
                if texture_mode == "only" and not is_texture:
                    continue
            entry = (fn.lower(), Path(dirpath) / fn)
            folders.setdefault(rel_dir_norm, []).append(entry)
            packed_rel_keys.append(rel_key)

    return folders, packed_rel_keys


# ---------------------------------------------------------------------------
# Main writer
# ---------------------------------------------------------------------------

ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]


def write_bsa(
    bsa_path: Path,
    source_dir: Path,
    *,
    version: int = 105,
    game_id: str | None = None,
    compress: bool = True,
    excluded_keys: frozenset[str] = frozenset(),
    texture_mode: str = "all",
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> tuple[int, int, list[str]]:
    """Pack *source_dir* into a BSA at *bsa_path*.

    Args:
        bsa_path:      Destination .bsa path (overwritten atomically).
        source_dir:    Mod folder root. All packable files under this
                       folder are written into the archive.
        version:       104 (Oblivion / FO3 / FNV / Skyrim LE) or
                       105 (Skyrim SE / VR).
        game_id:       Game ID (e.g. ``"skyrim_se"``) - selects the
                       per-game packable allowlist.  ``None`` falls
                       back to a permissive policy (intended for the
                       self-test only).
        compress:      Archive-default compression. Per-file overrides
                       automatically disable compression for known
                       incompressible formats.
        excluded_keys: rel_keys (lowercase forward-slash relative paths)
                       that should be skipped - e.g. files the user
                       disabled in the Mod Files tab.
        texture_mode:  ``"all"`` (default) packs everything; ``"exclude"``
                       drops files whose extension is in the game's
                       texture allowlist (use for the non-textures
                       sibling of a split pair); ``"only"`` keeps only
                       texture files (use for the ``- Textures.bsa``
                       sibling).
        progress:      Optional callback ``(done, total, current_path)``.
        cancel:        Optional callback returning True to abort. The .tmp
                       file is removed on cancel; *bsa_path* is left
                       untouched.

    Returns:
        (file_count, bytes_written, packed_rel_keys) - the size of the
        finished .bsa and the list of rel_keys that were packed (lower-
        case forward-slash). Callers use the rel_key list to update the
        Mod Files tab so packed loose files are auto-disabled.

    Raises:
        BsaWriteError: on any I/O or format error, or on cancel.
    """
    if version not in (104, 105):
        raise BsaWriteError(f"unsupported BSA version {version}")
    if texture_mode not in ("all", "exclude", "only"):
        raise BsaWriteError(f"unsupported texture_mode {texture_mode!r}")

    bsa_path = Path(bsa_path)
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise BsaWriteError(f"source directory does not exist: {source_dir}")

    folders, packed_rel_keys = _collect_files(
        source_dir, excluded_keys, game_id=game_id, texture_mode=texture_mode,
    )
    if not folders:
        raise BsaWriteError("no packable files found")

    # Sort folders by hash, files within each folder by hash. BSA TOC requires
    # this.
    sorted_folders: list[tuple[str, int, list[tuple[str, int, Path]]]] = []
    for folder_name, files in folders.items():
        folder_hash = tes4_hash_folder(folder_name)
        scored: list[tuple[str, int, Path]] = [
            (fn, tes4_hash_file(fn), p) for fn, p in files
        ]
        scored.sort(key=lambda t: t[1])
        sorted_folders.append((folder_name, folder_hash, scored))
    sorted_folders.sort(key=lambda t: t[1])

    folder_count = len(sorted_folders)
    file_count = sum(len(files) for _, _, files in sorted_folders)
    if file_count == 0:
        raise BsaWriteError("no packable files found")

    # total_folder_name_length: each folder name has a 1-byte length prefix
    # (the prefix is itself NOT counted), and the name itself includes a
    # trailing null byte. The header field is the sum of those (length+1
    # for the null), per BSA spec.
    total_folder_name_length = sum(
        len(folder_name) + 1 for folder_name, _, _ in sorted_folders
    )
    # total_file_name_length: every filename + trailing null, summed.
    total_file_name_length = sum(
        len(fn) + 1 for _, _, files in sorted_folders for fn, _, _ in files
    )

    folder_record_size = 24 if version == 105 else 16
    file_record_size = 16

    # Compute the header values now; offsets get patched after writing data.
    archive_flags = _AF_HAS_DIR_NAMES | _AF_HAS_FILE_NAMES
    if compress:
        archive_flags |= _AF_COMPRESSED_DEF
    archive_flags |= _AF_RETAIN_DIR_NAMES | _AF_RETAIN_FILE_NAMES

    if cancel and cancel():
        raise BsaWriteError("cancelled")

    try:
        with atomic_writer(bsa_path, "wb", encoding=None) as fh:
            # --- Header --------------------------------------------------
            fh.write(struct.pack(
                "<4sIIIIIIII",
                b"BSA\x00",
                version,
                36,                        # folder_offset
                archive_flags,
                folder_count,
                file_count,
                total_folder_name_length,
                total_file_name_length,
                _FILE_FLAGS,
            ))

            # --- Folder records (placeholder - patched after we know the
            #     folder block offsets) ----------------------------------
            folder_record_block_offset = fh.tell()
            fh.write(b"\x00" * (folder_record_size * folder_count))

            # --- Folder name + file-record blocks (interleaved) ---------
            # Track byte offset *of each folder's name+file-record group*
            # from the start of the file. That's what folder_record.offset
            # holds - but per BSA spec it includes total_file_name_length
            # (so writing tools and game agree on the constant offset of
            # the file data start). We follow the convention bethutil uses:
            # folder.offset = absolute byte offset of folder_name +
            # total_file_name_length. The reader subtracts at parse time
            # by ignoring that constant; what matters is consistency.
            #
            # In practice both Bethesda's tools and BSArch store the
            # **absolute** offset to the folder-name-plus-file-records
            # block PLUS total_file_name_length. Our reader (which only
            # walks sequentially from folder_offset) doesn't depend on
            # this value being meaningful for *its* parse - but the game
            # and other tools do. So we record absolute offsets here and
            # add total_file_name_length at the patch step.
            folder_block_offsets: list[int] = []

            # We also need to remember each file's record position so we can
            # patch in size + data offset after writing the data blob.
            # Layout per folder block:
            #   1 B name_length
            #   N B folder_name + 0x00
            #   16 B per file record
            file_record_positions: list[int] = []   # absolute file-position
            file_descriptors: list[tuple[Path, str, bool]] = []
            # (abs_path, name_lower, do_compress) - do_compress is the
            # *effective* compression for that file.

            for folder_name, folder_hash, files in sorted_folders:
                folder_block_offsets.append(fh.tell())

                fname_bytes = folder_name.encode("cp1252", errors="replace") + b"\x00"
                # name_length includes the trailing null.
                fh.write(bytes([len(fname_bytes)]))
                fh.write(fname_bytes)

                for fn, fhash, abs_path in files:
                    file_record_positions.append(fh.tell())
                    fh.write(b"\x00" * file_record_size)
                    incompressible = _is_incompressible(fn)
                    do_compress = compress and not incompressible
                    file_descriptors.append((abs_path, fn, do_compress))

            # --- File name block ----------------------------------------
            for _, _, files in sorted_folders:
                for fn, _, _ in files:
                    fh.write(fn.encode("cp1252", errors="replace") + b"\x00")

            # --- File data ----------------------------------------------
            # Track each file's data offset and on-disk record size.
            file_data_specs: list[tuple[int, int, bool]] = []
            # (data_offset, record_size_field, compress_invert_flag)

            done = 0
            total = file_count
            for abs_path, fn, do_compress in file_descriptors:
                if cancel and cancel():
                    raise BsaWriteError("cancelled")

                data_offset = fh.tell()

                try:
                    raw = abs_path.read_bytes()
                except OSError as exc:
                    raise BsaWriteError(
                        f"failed to read {abs_path}: {exc}"
                    ) from exc

                if do_compress:
                    if version == 105:
                        # Skyrim SE/VR - LZ4 frame format. Engine rejects
                        # zlib payloads in v105 archives.
                        compressed = lz4.frame.compress(raw, compression_level=9)
                    else:
                        # v104 - zlib deflate.
                        compressed = zlib.compress(raw, 9)
                    payload = struct.pack("<I", len(raw)) + compressed
                else:
                    payload = raw
                on_disk_size = len(payload)

                # Per-file size field is 30 bits - files larger than
                # ~1 GiB don't fit and must stay loose.  Check before
                # writing so we don't leave a partial archive on disk.
                if on_disk_size > _FILE_SIZE_MASK:
                    raise BsaWriteError(
                        f"file too large for BSA size field "
                        f"({on_disk_size} bytes > {_FILE_SIZE_MASK}): {fn}"
                    )
                fh.write(payload)

                # Bit 30 of the size field is set when this file's
                # compression state is *inverted* relative to
                # archive_flags (so size_field stays the on-disk byte
                # count).
                invert = compress != do_compress
                size_field = on_disk_size
                if invert:
                    size_field |= _FILE_COMPRESS_INVERT

                file_data_specs.append((data_offset, size_field, invert))

                done += 1
                if progress is not None:
                    progress(done, total, fn)

            end_offset = fh.tell()

            # v104 BSAs use 32-bit offsets; the engine cannot read past
            # 4 GiB.  v105 widened folder offsets to 64-bit but file
            # records still pack data_offset as uint32 (line below) - so
            # archives >4 GiB are unsafe in either version.
            if end_offset > 0xFFFFFFFF:
                raise BsaWriteError(
                    f"BSA exceeds 4 GiB ({end_offset} bytes); split textures "
                    "or shrink the mod and try again"
                )

            # --- Patch folder records ----------------------------------
            fh.seek(folder_record_block_offset)
            for (folder_name, folder_hash, files), block_offset in zip(
                sorted_folders, folder_block_offsets
            ):
                if version == 105:
                    fh.write(struct.pack(
                        "<QIIQ",
                        folder_hash,
                        len(files),
                        0,  # padding
                        block_offset + total_file_name_length,
                    ))
                else:
                    fh.write(struct.pack(
                        "<QII",
                        folder_hash,
                        len(files),
                        block_offset + total_file_name_length,
                    ))

            # --- Patch file records ------------------------------------
            i = 0
            for folder_name, folder_hash, files in sorted_folders:
                for fn, fhash, _ in files:
                    rec_pos = file_record_positions[i]
                    data_offset, size_field, _invert = file_data_specs[i]
                    fh.seek(rec_pos)
                    fh.write(struct.pack(
                        "<QII",
                        fhash,
                        size_field,
                        data_offset,
                    ))
                    i += 1

            fh.seek(end_offset)

    except BsaWriteError:
        raise
    except (OSError, struct.error) as exc:
        raise BsaWriteError(str(exc)) from exc

    return file_count, bsa_path.stat().st_size, packed_rel_keys
