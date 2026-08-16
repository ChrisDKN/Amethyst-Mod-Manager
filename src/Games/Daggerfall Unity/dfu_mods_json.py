"""
dfu_mods_json.py
Load-order sync for Daggerfall Unity's Mods.json.

DFU discovers .dfmod files by scanning StreamingAssets/Mods recursively and
assigns each one a LoadPriority from the scan order - which is filesystem
order, not anything the user chose.  It then overlays whatever is stored in
<PersistentDataPath>/Mods/GameData/Mods.json, reading back only Title,
Enabled and LoadPriority per entry and matching on Title.  DFU displays and
validates this order from the lowest LoadPriority to the highest, so
Amethyst's top-to-bottom order is written with increasing priorities.

Matching is by ModTitle, which lives inside the .dfmod asset bundle.  It is
read from the sibling <name>.dfmod.json manifest when the author shipped one
or otherwise included it in the archive; otherwise the embedded manifest
TextAsset is read from the bundle's decompressed UnityFS blocks.
Entries we cannot title are still written but DFU will simply not match them,
so a miss costs ordering, never correctness.

Set AMM_DFU_MODS_JSON=0 to skip this step entirely and manage the load order
from DFU's own Mod Loader UI.
"""

from __future__ import annotations

import heapq
import io
import json
import lzma
import os
import re
import shutil
import struct
from pathlib import Path
from typing import NamedTuple

import msgpack

from Utils.atomic_write import atomic_writer

CACHE_NAME = "dfu_modinfo.bin"
_CACHE_VERSION = 1

# Written next to Mods.json before the first sync so restore() can put the
# user's own file back.  A zero-byte backup means "there was no file here".
BACKUP_SUFFIX = ".amm-backup"

# Where a stashed set of per-mod settings folders lives inside overwrite/.
# Sibling GUID folders of Mods.json, one per mod, holding modsettings.json.
SETTINGS_STASH_DIR = "DFU_ModSettings"

# Cap raw inspection of legacy UnityRaw/UnityWeb bundles.  Modern UnityFS
# bundles are decompressed a block at a time until the manifest is found;
# limiting that scan used to miss valid manifests stored after the first
# 32 MiB and caused DFU to ignore the resulting filename-as-title entry.
_LEGACY_SCAN_LIMIT = 32 * 1024 * 1024
_SCAN_CONTEXT = 64 * 1024

_TITLE_RE = re.compile(rb'"ModTitle"\s*:\s*"((?:[^"\\]|\\.)*)"')
_MANIFEST_KEY_RE = re.compile(
    rb'"(?:ModVersion|ModAuthor|DFUnity_Version|ModDescription|GUID)"\s*:')
# Dependencies sits in the same ModInfo object as ModTitle, but ModInfo.Files
# lists every asset in the bundle and is written BETWEEN them - tens of
# thousands of paths in a large mod.  So the array cannot be found by a bounded
# regex from ModTitle (it is routinely ~85 KB further on) and must not be
# matched with `\[[^\]]*\]`, which stops at the end of Files.  Decode the whole
# ModInfo object instead; _MANIFEST_SCAN_LIMIT bounds the work.
_MANIFEST_SCAN_LIMIT = 4 * 1024 * 1024


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


class ModDependency(NamedTuple):
    """One entry of a bundle's ModInfo.Dependencies array."""
    name: str
    is_peer: bool
    is_optional: bool


class ModInfo(NamedTuple):
    """The parts of a .dfmod manifest that affect load order."""
    title: str
    dependencies: tuple[ModDependency, ...] = ()


def _deps_from_list(raw) -> tuple[ModDependency, ...]:
    """Normalise a decoded ModInfo.Dependencies array."""
    deps: list[ModDependency] = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict):
            continue
        # DFU matches dependencies against Mod.Title case-insensitively; the
        # declared Name is frequently cased differently from the target's own
        # ModTitle, so callers must fold case before comparing.
        name = item.get("Name")
        if not isinstance(name, str) or not name.strip():
            continue
        deps.append(ModDependency(name.strip(),
                                  bool(item.get("IsPeer")),
                                  bool(item.get("IsOptional"))))
    return tuple(deps)


def _parse_dependencies(data: bytes, brace: int) -> tuple[ModDependency, ...]:
    """Read Dependencies from the ModInfo object opening at *brace*.

    Decodes the object with raw_decode rather than pattern-matching the array,
    because ModInfo.Files sits between ModTitle and Dependencies and contains
    unbalanced-looking bracket runs of its own.
    """
    window = data[brace:brace + _MANIFEST_SCAN_LIMIT]
    try:
        text = window.decode("utf-8", errors="replace")
        manifest, _ = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return ()
    if not isinstance(manifest, dict):
        return ()
    return _deps_from_list(manifest.get("Dependencies"))


def _title_from_manifest_bytes(data: bytes) -> str | None:
    """Find and validate a ModInfo JSON object in decompressed bundle data."""
    info = _info_from_manifest_bytes(data)
    return None if info is None else info.title


def _manifest_object_start(data: bytes, title_start: int) -> int | None:
    """Offset of the '{' opening the ModInfo object containing *title_start*.

    ModTitle is usually ModInfo's first property, but authors who list quest or
    file arrays before it push it well into the object, so the brace is not
    necessarily adjacent.  Walk back over balanced braces to find the real one
    rather than requiring it within a fixed window.
    """
    depth = 0
    for i in range(title_start - 1, max(-1, title_start - _MANIFEST_SCAN_LIMIT), -1):
        byte = data[i]
        if byte == 0x7d:                       # '}' - a nested object closed
            depth += 1
        elif byte == 0x7b:                     # '{'
            if depth == 0:
                return i
            depth -= 1
    return None


def _info_from_manifest_bytes(data: bytes) -> ModInfo | None:
    """Find and validate a ModInfo JSON object in decompressed bundle data."""
    for match in _TITLE_RE.finditer(data):
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
        # DFU ModInfo object has additional quoted manifest properties nearby;
        # requiring an enclosing JSON object as well rejects the bare field
        # names of a type tree, which have no brace structure around them.
        context = data[match.end():match.end() + _SCAN_CONTEXT]
        if not _MANIFEST_KEY_RE.search(context):
            continue
        obj_start = _manifest_object_start(data, match.start())
        if obj_start is None:
            continue
        return ModInfo(title, _parse_dependencies(data, obj_start))
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


def _info_from_unityfs(dfmod: Path) -> ModInfo | None:
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

        def next_block(index: int) -> bytes:
            unpacked, packed, block_flags = blocks[index]
            return _decompress_unity_block(
                _read_exact(stream, packed), unpacked, block_flags)

        tail = b""
        for i in range(len(blocks)):
            buffer = tail + next_block(i)
            info = _info_from_manifest_bytes(buffer)
            if info is not None:
                # ModInfo.Files sits between ModTitle and Dependencies, so the
                # object routinely spans many blocks and this first parse sees
                # it truncated.  Keep decompressing until Dependencies decodes
                # or the manifest budget runs out; the title is already known,
                # so a bundle that never completes still returns usable info.
                j = i + 1
                while (not info.dependencies and j < len(blocks)
                       and len(buffer) < _MANIFEST_SCAN_LIMIT):
                    buffer += next_block(j)
                    j += 1
                    parsed = _info_from_manifest_bytes(buffer)
                    if parsed is not None:
                        info = parsed
                return info
            tail = buffer[-_SCAN_CONTEXT:]
    return None


def _info_from_manifest(dfmod: Path) -> ModInfo | None:
    manifest = dfmod.with_name(dfmod.name + ".json")   # foo.dfmod -> foo.dfmod.json
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    title = _clean_title(data.get("ModTitle"))
    if title is None:
        return None
    raw = data.get("Dependencies")
    deps: list[ModDependency] = []
    for item in raw if isinstance(raw, list) else ():
        if isinstance(item, dict) and isinstance(item.get("Name"), str) \
                and item["Name"].strip():
            deps.append(ModDependency(item["Name"].strip(),
                                      bool(item.get("IsPeer")),
                                      bool(item.get("IsOptional"))))
    return ModInfo(title, tuple(deps))


def _info_from_bundle(dfmod: Path) -> ModInfo | None:
    """Extract ModInfo without interpreting compressed bytes as strings."""
    try:
        with dfmod.open("rb") as stream:
            signature = stream.read(8)
        if signature == b"UnityFS\0":
            try:
                info = _info_from_unityfs(dfmod)
            except (ImportError, lzma.LZMAError, struct.error, ValueError):
                # A truncated or malformed archive still often carries a
                # readable manifest, so fall through to the raw scan below
                # rather than losing the title over a header we cannot walk.
                info = None
            if info is not None:
                return info

        # Very old UnityRaw/UnityWeb bundles are uncommon in current DFU.  A
        # validated raw scan is safe for their uncompressed variants and, on
        # failure, returning None is preferable to writing corrupt JSON.
        with dfmod.open("rb") as stream:
            return _info_from_manifest_bytes(stream.read(_LEGACY_SCAN_LIMIT))
    except (ImportError, lzma.LZMAError, OSError, struct.error, ValueError):
        return None


class ModInfoCache:
    """(size, mtime_ns) -> ModInfo cache for .dfmod bundles, as msgpack.

    Finding Dependencies means decompressing past ModInfo.Files, which is the
    bulk of a bundle, so a cold read of a large mod list costs seconds.  The
    bundles themselves never change in place, making mtime a sound key.
    """

    def __init__(self, cache_path: Path | None = None):
        self._path = cache_path
        # abs path -> [size, mtime_ns, title, [[name, is_peer, is_optional]...]]
        # title "" = a bundle whose ModInfo could not be read.
        self._data: dict[str, list] = {}
        self._seen: set[str] = set()
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            with self._path.open("rb") as handle:
                payload = msgpack.unpack(handle, raw=False, strict_map_key=False)
        except Exception:
            return
        if not isinstance(payload, dict) or payload.get("v") != _CACHE_VERSION:
            return
        mods = payload.get("mods")
        if isinstance(mods, dict):
            self._data = {k: list(v) for k, v in mods.items()
                          if isinstance(v, (list, tuple)) and len(v) == 4}

    def lookup(self, dfmod: Path) -> ModInfo | None:
        key = str(dfmod)
        try:
            st = dfmod.stat()
        except OSError:
            # Not marked seen: a bundle that has gone away should be pruned on
            # save rather than kept alive by the lookup that failed to read it.
            return None
        self._seen.add(key)

        hit = self._data.get(key)
        if hit is not None and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            if not hit[2]:
                return None
            deps = tuple(ModDependency(d[0], bool(d[1]), bool(d[2]))
                         for d in hit[3] if isinstance(d, (list, tuple))
                         and len(d) == 3)
            return ModInfo(hit[2], deps)

        info = _read_mod_info_uncached(dfmod)
        # Cache the miss too: an unreadable bundle is otherwise fully
        # decompressed again on every deploy.
        self._data[key] = [
            st.st_size, st.st_mtime_ns,
            info.title if info is not None else "",
            [[d.name, d.is_peer, d.is_optional] for d in info.dependencies]
            if info is not None else [],
        ]
        self._dirty = True
        return info

    def save(self) -> None:
        """Persist the cache, dropping entries for bundles that are gone."""
        if self._path is None:
            return
        # Prune before the dirty check: a run that was entirely cache hits
        # still needs to forget mods the user has since uninstalled, or the
        # file grows for the lifetime of the profile.
        for key in [k for k in self._data if k not in self._seen]:
            if not os.path.exists(key):
                del self._data[key]
                self._dirty = True
        if not self._dirty:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with atomic_writer(self._path, "wb", encoding=None,
                               suffix=f".{os.getpid()}.tmp") as handle:
                msgpack.pack({"v": _CACHE_VERSION, "mods": self._data}, handle,
                             use_bin_type=True)
            self._dirty = False
        except Exception:
            pass


def _read_mod_info_uncached(dfmod: Path) -> ModInfo | None:
    return _info_from_manifest(dfmod) or _info_from_bundle(dfmod)


def read_mod_info(dfmod: Path, cache: ModInfoCache | None = None) -> ModInfo | None:
    """Return the mod's ModInfo, or None when it cannot be determined."""
    if cache is not None:
        return cache.lookup(dfmod)
    return _read_mod_info_uncached(dfmod)


def read_mod_title(dfmod: Path) -> str | None:
    """Return the mod's ModTitle, or None when it cannot be determined."""
    info = read_mod_info(dfmod)
    return None if info is None else info.title


# ---------------------------------------------------------------------------
# Sync / restore
# ---------------------------------------------------------------------------

def sort_by_dependencies(
    titles: list[tuple[str, str]],
    deps_by_file: dict[str, tuple[ModDependency, ...]],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Order *titles* so every dependency precedes its dependent.

    DFU's CheckModDependencies errors when a mod's LoadPriority is lower than
    that of a non-peer dependency, i.e. a dependency must load first.  Amethyst
    otherwise knows nothing about these declarations, so the user's list order
    alone routinely trips the Mod Loader's "must be positioned above" prompt.

    Both *titles* and *deps_by_file* are keyed on the .dfmod file name without
    its extension, because that - not ModTitle - is what a declared dependency
    names.  GetModFromName matches Mod.FileName with StringComparison.Ordinal,
    so the match is exact: "beautiful cities" is the bundle beautiful
    cities.dfmod, whose ModTitle is the quite different "Beautiful Cities of
    Daggerfall".  A dependency whose spelling does not match any installed file
    is one DFU reports as missing rather than misordered, so leaving it out of
    the graph is correct - no placement would satisfy it.

    This is a stable topological sort: a mod moves only when a constraint
    demands it, so the user's ordering survives everywhere it is already legal.
    DFU's own AutoSortMods discards list order entirely, which is why we do not
    simply mirror it.  Returns the new order plus any titles dropped from a
    dependency cycle's constraints.
    """
    order = {filename: i for i, (filename, _) in enumerate(titles)}
    # Peer dependencies impose no ordering; optional ones still do when the
    # target is actually installed, which is exactly how DFU validates them.
    edges: dict[int, set[int]] = {i: set() for i in range(len(titles))}
    for i, (filename, _) in enumerate(titles):
        for dep in deps_by_file.get(filename, ()):
            if dep.is_peer:
                continue
            j = order.get(dep.name)
            if j is not None and j != i:
                edges[i].add(j)          # i must come after j

    # Kahn's algorithm over the original indices.  Always taking the smallest
    # ready index is what makes the result stable and deterministic.
    remaining = {i: set(js) for i, js in edges.items()}
    dependents: dict[int, list[int]] = {i: [] for i in range(len(titles))}
    for i, js in edges.items():
        for j in js:
            dependents[j].append(i)

    ready = [i for i, js in remaining.items() if not js]
    heapq.heapify(ready)
    result: list[int] = []
    while ready:
        i = heapq.heappop(ready)
        result.append(i)
        for k in dependents[i]:
            remaining[k].discard(i)
            if not remaining[k]:
                heapq.heappush(ready, k)

    # A cycle (mods declaring each other) leaves entries unemitted.  Append
    # them in list order rather than dropping them: an unsatisfiable constraint
    # is the author's problem, and losing the mod entirely would be worse.
    cycled = [i for i in range(len(titles)) if i not in set(result)]
    result.extend(cycled)
    return [titles[i] for i in result], [titles[i][1] for i in cycled]


def count_sort_issues(
    titles: list[tuple[str, str]],
    deps_by_file: dict[str, tuple[ModDependency, ...]],
) -> int:
    """How many constraints DFU's CheckModDependencies would flag in *titles*.

    *titles* is in load order (index 0 loads first), matching what
    sort_by_dependencies consumes and produces.
    """
    at = {filename: i for i, (filename, _) in enumerate(titles)}
    issues = 0
    for i, (filename, _) in enumerate(titles):
        for dep in deps_by_file.get(filename, ()):
            if dep.is_peer:
                continue
            j = at.get(dep.name)
            if j is not None and i < j:
                issues += 1
    return issues


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


def sync_mods_json(game_path: Path, ordered: list[Path], log_fn=None,
                   cache_path: Path | None = None) -> int:
    """Write Amethyst's load order into Mods.json; returns entries written.

    *ordered* is the deployed .dfmod paths in Amethyst list order (top first).
    *cache_path* stores parsed ModInfo between deploys; without it every
    bundle is decompressed again on each sync.

    Both tools resolve a conflict in favour of one end of their order, but they
    spell that end differently, so the list is REVERSED on the way out:

      Amethyst - the top of the mod list wins; its Priority column counts down,
                 so the top row holds the highest number.
      DFU      - the highest LoadPriority wins.  TryGetAsset walks
                 EnumerateEnabledModsReverse() and takes the first hit, and
                 both FormulaHelper and PlayerActivate keep the provider with
                 the greater LoadPriority.

    So the top of the Amethyst list must come out with the HIGHEST
    LoadPriority, i.e. loaded last.  Beware the word "above": DFU's
    CheckModDependencies flags `mod.LoadPriority < target.LoadPriority`, so a
    dependency "positioned above in the load order" needs the LOWER number -
    it loads earlier and is overridden.  That is load order, not precedence,
    and it is the opposite end from the top of the Amethyst list.
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
    deps_by_file: dict[str, tuple[ModDependency, ...]] = {}
    info_cache = ModInfoCache(cache_path)
    for dfmod in ordered:
        stem = dfmod.name[: -len(".dfmod")]
        # The bundle is authoritative.  DFU's own previous entry is a safe
        # fallback for an unusual/unsupported bundle format.
        info = read_mod_info(dfmod, info_cache)
        title = (info.title if info is not None else None) \
            or existing_titles.get(stem)
        if title is None:
            untitled.append(stem)
            title = stem
        # Keyed on the file name: that is what a dependency declaration names.
        if info is not None and info.dependencies:
            deps_by_file[stem] = info.dependencies
        titles.append((stem, title))
    info_cache.save()

    # Amethyst's list order is winner-first; DFU's LoadPriority is load order,
    # where the LAST mod loaded wins.  Reverse into load order so the top of
    # the Amethyst list ends up with the highest LoadPriority.
    load_order = list(reversed(titles))
    # DFU refuses to load a mod placed before a declared dependency, so honour
    # those declarations before assigning priorities.
    issues = count_sort_issues(load_order, deps_by_file)
    load_order, cycled = sort_by_dependencies(load_order, deps_by_file)
    titles = load_order

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
    # Keep the managed block together after anything hand-added.  Within that
    # block, priorities increase down the load order, i.e. UP the Amethyst mod
    # list, so the mod the user put on top is the last one DFU loads.
    base = max((e.get("LoadPriority", 0) for e in preserved
                if isinstance(e.get("LoadPriority"), int)), default=-1) + 1

    total = len(titles)
    entries = list(preserved)
    for i, (stem, title) in enumerate(titles):
        entries.append({
            "FileName": stem,
            "Title": title,
            "Enabled": True,
            "LoadPriority": base + i,
        })

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=4), encoding="utf-8")

    _log(f"  Wrote {total} mod(s) to {path}.")
    if issues:
        _log(f"  Reordered to satisfy {issues} declared dependency(s) - DFU "
             "would otherwise ask to sort the load order on startup.")
    if cycled:
        _log("  Circular dependencies between: " + ", ".join(cycled) +
             " - left in mod-list order; DFU may still report these.")
    if preserved:
        _log(f"  Left {len(preserved)} entry(s) not managed by Amethyst untouched.")
    if untitled:
        _log("  No readable ModTitle found for: " + ", ".join(untitled) +
             " - DFU will keep its own load position for them. Order them in "
             "DFU's Mod Loader.")
    return total


# ---------------------------------------------------------------------------
# Per-mod runtime folders
# ---------------------------------------------------------------------------

# The sibling folders under <PersistentDataPath>/Mods that DFU fills with
# per-mod, runtime-generated content, each keyed by the containing folder name:
#   GameData/<GUID>/modsettings.json  - the user's settings for that mod
#   ExtractedFiles/<mod title>/...    - assets a mod unpacked out of its bundle
# Both hold one folder per mod and both are discarded for mods DFU is not
# currently loading, so both need stashing across a profile switch.
RUNTIME_DIRS = ("GameData", "ExtractedFiles")


def mod_runtime_dir(game_path: Path, name: str) -> Path:
    """Return one of DFU's per-mod runtime folders (see RUNTIME_DIRS)."""
    return persistent_data_dir(game_path) / "Mods" / name


def mod_settings_dir(game_path: Path) -> Path:
    """Return the folder holding DFU's per-mod settings folders."""
    return mod_runtime_dir(game_path, "GameData")


def _settings_folders(parent: Path) -> list[Path]:
    """Return the per-mod folders under a runtime folder.

    Every mod gets one folder of its own - named for its GUID under GameData,
    for its lowercased title under ExtractedFiles.  The only non-folder entries
    are Mods.json and our own backup of it, both of which are left alone.
    """
    if not parent.is_dir():
        return []
    try:
        return sorted(p for p in parent.iterdir() if p.is_dir())
    except OSError:
        return []


def _link_tree(src: Path, dest: Path, mode) -> None:
    """Mirror *src* into *dest*, linking each file with the deploy's mode."""
    from Utils.deploy import _transfer

    dest.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dest / child.name
        if child.is_dir() and not child.is_symlink():
            _link_tree(child, target, mode)
            continue
        # A stale link (or a file DFU replaced) has to go before we relink, or
        # os.link/os.symlink would raise EEXIST.
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
        _transfer(child, target, mode)


def _same_file(a: Path, b: Path) -> bool:
    """True if *a* and *b* are the same inode, or *a* is a link to *b*."""
    try:
        if a.is_symlink() and Path(os.readlink(a)) == b:
            return True
        return a.samefile(b)
    except OSError:
        return False


def _merge_dir(src: Path, dest: Path) -> None:
    """Move *src* onto *dest*, replacing colliding files but keeping the rest."""
    dest.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dest / child.name
        if child.is_dir() and not child.is_symlink():
            _merge_dir(child, target)
            continue
        # Already the stash's own file, linked in by a previous deploy: there
        # is nothing to move, and unlinking the target first would throw the
        # only copy away.
        if _same_file(child, target):
            child.unlink(missing_ok=True)
            continue
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
        shutil.move(str(child), str(target))
    shutil.rmtree(src, ignore_errors=True)


def _migrate_flat_stash(overwrite_dir: Path, log_fn=None) -> None:
    """Move a pre-RUNTIME_DIRS stash into its GameData/ subfolder.

    The first version of this stash held the GUID folders directly, before
    ExtractedFiles was handled too.  Anything sitting at the top level that is
    not one of the runtime folder names is one of those GUIDs.
    """
    _log = log_fn or _noop
    stash = overwrite_dir / SETTINGS_STASH_DIR
    stale = [p for p in _settings_folders(stash) if p.name not in RUNTIME_DIRS]
    if not stale:
        return
    target_root = stash / "GameData"
    for folder in stale:
        try:
            target = target_root / folder.name
            if target.exists():
                _merge_dir(folder, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(folder), str(target))
        except OSError as exc:
            _log(f"  Could not migrate stashed settings "
                 f"'{folder.name}': {exc}")
    _log(f"  Migrated {len(stale)} stashed settings folder(s) into GameData/.")


def stash_mod_settings(game_path: Path, overwrite_dir: Path, log_fn=None) -> int:
    """Move DFU's per-mod runtime folders into overwrite/; returns how many.

    DFU keeps per-mod state outside the game folder entirely, under
    <PersistentDataPath>/Mods - so nothing in the StreamingAssets deploy
    protects it:

      GameData/<GUID>/modsettings.json  the user's settings for that mod
      ExtractedFiles/<mod title>/...    assets the mod unpacked from its bundle

    DFU discards both for any mod it is not currently loading, which means
    deploying a second profile silently resets the first profile's settings.
    Restore therefore parks them in the profile's overwrite/ folder and deploy
    links them back.

    The folders are keyed by GUID and by mod title, neither of which Amethyst
    can map back to a staged mod, so each set moves as a unit rather than per
    mod.  Files that are already links into this stash (put there by the
    matching restore_mod_settings) are simply dropped - the stash copy IS the
    file.
    """
    _log = log_fn or _noop
    moved = 0
    for name in RUNTIME_DIRS:
        source = mod_runtime_dir(game_path, name)
        folders = _settings_folders(source)
        if not folders:
            continue
        stash = overwrite_dir / SETTINGS_STASH_DIR / name
        for folder in folders:
            try:
                target = stash / folder.name
                if target.exists():
                    # A previous stash for the same mod: the folder now in the
                    # game is the newer one, so let it win file by file.
                    _merge_dir(folder, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(folder), str(target))
                moved += 1
            except OSError as exc:
                _log(f"  Could not stash {name} folder '{folder.name}': {exc}")
        _log(f"  Stashed {len(folders)} {name} folder(s) → "
             f"{SETTINGS_STASH_DIR}/{name}/.")
    return moved


def restore_mod_settings(game_path: Path, overwrite_dir: Path,
                         mode: "LinkMode | None" = None, log_fn=None) -> int:
    """Link DFU's stashed per-mod runtime folders back; returns how many.

    The stash stays the canonical copy - each file is hardlinked (or symlinked)
    back into place rather than moved out, exactly like a deployed mod file.  So
    an interrupted session, a crash, or DFU pruning a folder outright can never
    lose the data: overwrite/ still holds it.

    With a hardlink the game's own writes are seen by both paths, so settings
    changed in-game persist to the stash for free.  A symlink behaves the same
    as long as DFU rewrites the file in place; if it replaces the file instead,
    the next stash_mod_settings() picks the new copy up.
    """
    from Utils.deploy import LinkMode

    _log = log_fn or _noop
    if mode is None:
        mode = LinkMode.HARDLINK

    _migrate_flat_stash(overwrite_dir, log_fn=_log)

    linked = 0
    for name in RUNTIME_DIRS:
        stash = overwrite_dir / SETTINGS_STASH_DIR / name
        folders = _settings_folders(stash)
        if not folders:
            continue
        dest_root = mod_runtime_dir(game_path, name)
        for folder in folders:
            try:
                _link_tree(folder, dest_root / folder.name, mode)
                linked += 1
            except OSError as exc:
                _log(f"  Could not restore {name} folder "
                     f"'{folder.name}': {exc}")
        _log(f"  Linked {len(folders)} {name} folder(s) into {dest_root}.")
    return linked


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
