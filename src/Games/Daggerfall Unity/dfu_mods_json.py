"""
dfu_mods_json.py
Load-order sync for Daggerfall Unity's Mods.json.

DFU discovers .dfmod files by scanning StreamingAssets/Mods recursively and
assigns each one a LoadPriority from the scan order - which is filesystem
order, not anything the user chose.  It then overlays whatever is stored in
<PersistentDataPath>/Mods/GameData/Mods.json, reading back only Title,
Enabled and LoadPriority per entry and matching on Title.  Higher LoadPriority
wins an asset conflict, so Amethyst's top-of-list mod must get the *highest*
priority.

Matching is by ModTitle, which lives inside the .dfmod asset bundle.  It is
read from the sibling <name>.dfmod.json manifest when the author shipped one
(the Mod Builder emits it alongside the bundle); otherwise the embedded
manifest TextAsset is read from the bundle's decompressed UnityFS blocks.
Entries we cannot title are still written but DFU will simply not match them,
so a miss costs ordering, never correctness.

Set AMM_DFU_MODS_JSON=0 to skip this step entirely and manage the load order
from DFU's own Mod Loader UI.
"""

from __future__ import annotations

import io
import json
import lzma
import os
import re
import shutil
import struct
from pathlib import Path

# Written next to Mods.json before the first sync so restore() can put the
# user's own file back.  A zero-byte backup means "there was no file here".
BACKUP_SUFFIX = ".amm-backup"

# Cap decompressed data inspected - DREAM and friends ship multi-hundred-MB
# .dfmod files and the manifest sits near the start of the serialized bundle.
_SCAN_LIMIT = 32 * 1024 * 1024
_SCAN_CONTEXT = 64 * 1024

_TITLE_RE = re.compile(rb'"ModTitle"\s*:\s*"((?:[^"\\]|\\.)*)"')
_MANIFEST_KEY_RE = re.compile(
    rb'"(?:ModVersion|ModAuthor|DFUnity_Version|ModDescription|GUID)"\s*:')


def _noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def persistent_data_dir(game_path: Path) -> Path:
    """Return DFU's PersistentDataPath for this install."""
    # DaggerfallUnityApplication: a Portable.txt next to the player redirects
    # the whole persistent path into the install folder.
    if (game_path / "Portable.txt").is_file():
        return game_path / "PortableAppdata"
    cfg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(cfg) / "unity3d" / "Daggerfall Workshop" / "Daggerfall Unity"


def mods_json_path(game_path: Path) -> Path:
    """Return the Mods.json that carries the load order."""
    return persistent_data_dir(game_path) / "Mods" / "GameData" / "Mods.json"


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

def _clean_title(value) -> str | None:
    """Return a usable DFU title, rejecting binary false positives."""
    if not isinstance(value, str):
        return None
    title = value.strip()
    if not title or len(title) > 1024:
        return None
    # A title containing NUL or another JSON control character came from
    # matching Unity's type metadata, not from the manifest TextAsset.
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in title):
        return None
    return title


def _title_from_manifest_bytes(data: bytes) -> str | None:
    """Find and validate a ModInfo JSON object in decompressed bundle data."""
    for match in _TITLE_RE.finditer(data):
        # FullSerializer writes ModTitle as the first property of ModInfo.  A
        # nearby opening brace distinguishes that JSON object from C# source
        # strings and the bare field names in a Unity type tree.
        prefix = data[max(0, match.start() - 64):match.start()]
        brace = prefix.rfind(b"{")
        if brace < 0 or prefix[brace + 1:].strip():
            continue

        # Let the JSON decoder handle escaped quotes, backslashes and Unicode;
        # unicode_escape would corrupt already-decoded non-ASCII UTF-8.
        try:
            value = json.loads((b'"' + match.group(1) + b'"').decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        title = _clean_title(value)
        if title is None:
            continue

        # ModTitle can occur in source code and Unity type trees too.  A real
        # DFU ModInfo object has additional quoted manifest properties nearby.
        context = data[match.end():match.end() + _SCAN_CONTEXT]
        if _MANIFEST_KEY_RE.search(context):
            return title
    return None


def _read_cstring(stream, limit: int = 1024) -> bytes:
    value = bytearray()
    while len(value) < limit:
        char = stream.read(1)
        if not char:
            raise ValueError("truncated C string")
        if char == b"\0":
            return bytes(value)
        value.extend(char)
    raise ValueError("overlong C string")


def _read_exact(stream, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("truncated UnityFS bundle")
    return data


def _decompress_unity_block(data: bytes, size: int, flags: int) -> bytes:
    """Decompress a UnityFS metadata or payload block."""
    compression = flags & 0x3f
    if compression == 0:
        if len(data) != size:
            raise ValueError("invalid uncompressed UnityFS block")
        return data
    if compression == 1:
        if len(data) < 5:
            raise ValueError("truncated Unity LZMA block")
        props, dictionary_size = struct.unpack("<BI", data[:5])
        lc = props % 9
        remainder = props // 9
        pb, lp = divmod(remainder, 5)
        result = lzma.decompress(
            data[5:],
            format=lzma.FORMAT_RAW,
            filters=[{"id": lzma.FILTER_LZMA1, "dict_size": dictionary_size,
                      "lc": lc, "lp": lp, "pb": pb}],
        )
    elif compression in (2, 3):
        # lz4 is already a packaged dependency (used by BG3 and BSA support).
        import lz4.block
        try:
            result = lz4.block.decompress(data, uncompressed_size=size)
        except Exception as exc:
            raise ValueError("invalid Unity LZ4 block") from exc
    else:
        raise ValueError(f"unsupported UnityFS compression type {compression}")
    if len(result) != size:
        raise ValueError("UnityFS block decompressed to the wrong size")
    return result


def _unity_version_needs_alignment(version: int, engine: bytes) -> bool:
    if version >= 7:
        return True
    # Unity 2019.4.15+ also aligned some format-6 bundles.  DFU currently uses
    # 2019.4.41, but accepting this variant costs very little.
    match = re.match(rb"2019\.4\.(\d+)", engine)
    return bool(match and int(match.group(1)) >= 15)


def _title_from_unityfs(dfmod: Path) -> str | None:
    """Read ModInfo from the decompressed blocks of a modern UnityFS bundle."""
    with dfmod.open("rb") as stream:
        if _read_cstring(stream) != b"UnityFS":
            return None
        version = struct.unpack(">I", _read_exact(stream, 4))[0]
        _read_cstring(stream)                 # minimum Unity version
        engine = _read_cstring(stream)
        _bundle_size, compressed_size, uncompressed_size, flags = struct.unpack(
            ">QIII", _read_exact(stream, 20))
        file_size = dfmod.stat().st_size
        if compressed_size > file_size or uncompressed_size > 64 * 1024 * 1024:
            raise ValueError("invalid UnityFS metadata size")

        if _unity_version_needs_alignment(version, engine):
            stream.seek((stream.tell() + 15) & ~15)
        blocks_start = stream.tell()

        if flags & 0x80:                      # BlocksInfoAtTheEnd
            stream.seek(-compressed_size, os.SEEK_END)
            compressed_info = _read_exact(stream, compressed_size)
            stream.seek(blocks_start)
        else:
            compressed_info = _read_exact(stream, compressed_size)

        info = _decompress_unity_block(compressed_info, uncompressed_size, flags)
        reader = io.BytesIO(info)
        _read_exact(reader, 16)                # uncompressed data hash
        block_count = struct.unpack(">I", _read_exact(reader, 4))[0]
        if block_count > 1_000_000:
            raise ValueError("invalid UnityFS block count")

        blocks: list[tuple[int, int, int]] = []
        for _ in range(block_count):
            unpacked, packed, block_flags = struct.unpack(
                ">IIH", _read_exact(reader, 10))
            if packed > file_size or unpacked > 128 * 1024 * 1024:
                raise ValueError("invalid UnityFS block size")
            blocks.append((unpacked, packed, block_flags))

        # Newer Unity versions can request padding between block metadata and
        # payload.  DFU's 2019 bundles do not set this flag, but support it for
        # forwards compatibility.
        if flags & 0x200:
            stream.seek((stream.tell() + 15) & ~15)

        scanned = 0
        tail = b""
        for unpacked, packed, block_flags in blocks:
            if scanned >= _SCAN_LIMIT:
                break
            compressed = _read_exact(stream, packed)
            block = _decompress_unity_block(compressed, unpacked, block_flags)
            remaining = _SCAN_LIMIT - scanned
            block = block[:remaining]
            title = _title_from_manifest_bytes(tail + block)
            if title is not None:
                return title
            scanned += len(block)
            tail = (tail + block)[-_SCAN_CONTEXT:]
    return None


def _title_from_manifest(dfmod: Path) -> str | None:
    manifest = dfmod.with_name(dfmod.name + ".json")   # foo.dfmod -> foo.dfmod.json
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    title = data.get("ModTitle") if isinstance(data, dict) else None
    return _clean_title(title)


def _title_from_bundle(dfmod: Path) -> str | None:
    """Extract ModTitle without interpreting compressed bytes as strings."""
    try:
        with dfmod.open("rb") as stream:
            signature = stream.read(8)
        if signature == b"UnityFS\0":
            return _title_from_unityfs(dfmod)

        # Very old UnityRaw/UnityWeb bundles are uncommon in current DFU.  A
        # validated raw scan is safe for their uncompressed variants and, on
        # failure, returning None is preferable to writing corrupt JSON.
        with dfmod.open("rb") as stream:
            return _title_from_manifest_bytes(stream.read(_SCAN_LIMIT))
    except (ImportError, lzma.LZMAError, OSError, struct.error, ValueError):
        return None


def read_mod_title(dfmod: Path) -> str | None:
    """Return the mod's ModTitle, or None when it cannot be determined."""
    return _title_from_manifest(dfmod) or _title_from_bundle(dfmod)


# ---------------------------------------------------------------------------
# Sync / restore
# ---------------------------------------------------------------------------

def _load_existing(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _ensure_backup(path: Path, log_fn) -> None:
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.exists():
        return
    backup.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, backup)
        log_fn(f"  Backed up the existing Mods.json → {backup.name}.")
    else:
        backup.write_bytes(b"")


def sync_mods_json(game_path: Path, ordered: list[Path], log_fn=None) -> int:
    """Write Amethyst's load order into Mods.json; returns entries written.

    *ordered* is the deployed .dfmod paths in Amethyst priority order -
    index 0 is the top of the mod list and must end up with the highest
    DFU LoadPriority.
    """
    _log = log_fn or _noop
    if os.environ.get("AMM_DFU_MODS_JSON") == "0":
        _log("  Mods.json sync disabled (AMM_DFU_MODS_JSON=0) - "
             "set the load order in DFU's Mod Loader.")
        return 0

    path = mods_json_path(game_path)
    _ensure_backup(path, _log)

    existing = _load_existing(path)
    existing_titles: dict[str, str] = {}
    for entry in existing:
        filename = entry.get("FileName")
        title = _clean_title(entry.get("Title"))
        if isinstance(filename, str) and title is not None:
            existing_titles[filename] = title

    titles: list[tuple[str, str]] = []          # (FileName, Title)
    untitled: list[str] = []
    for dfmod in ordered:
        stem = dfmod.name[: -len(".dfmod")]
        # The bundle is authoritative.  DFU's own previous entry is a safe
        # fallback for an unusual/unsupported bundle format.
        title = read_mod_title(dfmod) or existing_titles.get(stem)
        if title is None:
            untitled.append(stem)
            title = stem
        titles.append((stem, title))

    managed = {filename for filename, _ in titles}
    deployed_dir = (game_path / "DaggerfallUnity_Data" / "StreamingAssets" /
                    "Mods")
    deployed = ({path.name[:-len(".dfmod")] for path in
                 deployed_dir.rglob("*.dfmod") if path.is_file()}
                if deployed_dir.is_dir() else None)
    # FileName identifies the on-disk bundle.  Reconcile on it rather than
    # Title so a bad title from an older Amethyst deploy is replaced instead
    # of being preserved as a duplicate entry.  Also discard stale entries
    # for bundles no longer installed while retaining manually dropped mods.
    preserved = [entry for entry in existing
                 if _clean_title(entry.get("Title")) is not None
                 and isinstance(entry.get("FileName"), str)
                 and entry["FileName"] not in managed
                 and (deployed is None or entry["FileName"] in deployed)]
    # Sit the managed block above anything hand-added so a manually dropped
    # mod can never outrank a mod the user ordered in Amethyst.
    base = max((e.get("LoadPriority", 0) for e in preserved
                if isinstance(e.get("LoadPriority"), int)), default=-1) + 1

    total = len(titles)
    entries = list(preserved)
    for i, (stem, title) in enumerate(titles):
        entries.append({
            "FileName": stem,
            "Title": title,
            "Enabled": True,
            "LoadPriority": base + (total - 1 - i),
        })

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=4), encoding="utf-8")

    _log(f"  Wrote {total} mod(s) to {path}.")
    if preserved:
        _log(f"  Left {len(preserved)} entry(s) not managed by Amethyst untouched.")
    if untitled:
        _log("  No readable ModTitle found for: " + ", ".join(untitled) +
             " - DFU will keep its own load position for them. Order them in "
             "DFU's Mod Loader.")
    return total


def restore_mods_json(game_path: Path, log_fn=None) -> bool:
    """Put the pre-deploy Mods.json back; returns True if anything changed."""
    _log = log_fn or _noop
    path = mods_json_path(game_path)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        return False
    try:
        if backup.stat().st_size == 0:
            # There was no Mods.json before we deployed.
            path.unlink(missing_ok=True)
            _log("  Removed the Mods.json written at deploy time.")
        else:
            shutil.copy2(backup, path)
            _log("  Restored the pre-deploy Mods.json.")
        backup.unlink(missing_ok=True)
        return True
    except OSError as exc:
        _log(f"  Could not restore Mods.json: {exc}")
        return False
