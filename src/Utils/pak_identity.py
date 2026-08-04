"""
pak_identity.py
Identify a Baldur's Gate 3 .pak by the ModuleInfo UUID inside its meta.lsx.

BG3 keys mods by UUID, never by file name, and the in-game manager / mod.io hand
out the same module under different pak names. Path-keyed conflict detection sees
unrelated files and deploys both, so the game gets two paks claiming one module.
BG3 Mod Manager drops all but one copy per UUID for the same reason (it keeps the
highest Version64; Amethyst keeps the highest-priority mod and warns when that is
the older one).

Supplies a conflict_key_fn for build_filemap that keys paks by UUID. Reads are
cached in pak_uuid.bin next to modindex.bin, keyed by (size, mtime_ns).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import msgpack

from Utils.atomic_write import atomic_writer

CACHE_NAME = "pak_uuid.bin"
_CACHE_VERSION = 2

# Conflict-key namespace for paks. The NUL prefix can't collide with a real
# staged path, and build_filemap uses it to tell identity conflicts from path
# conflicts (an identity clash has its own icon, not the loose-file one).
_PAK_NS = "\x00pak\x00"
IDENTITY_KEY_PREFIX = _PAK_NS

OVERWRITE_NAME = "[Overwrite]"

_ENV_FLAG = "AMM_BG3_PAK_UUID_CONFLICTS"


_MULTIPART_RE = re.compile(r"^(.*)_\d+\.pak$")


def _is_multipart(rel_key: str, mod_files: dict) -> bool:
    """True for "Foo_1.pak" when the mod also ships "Foo.pak" (a split archive)."""
    m = _MULTIPART_RE.match(rel_key)
    if m is None:
        return False
    base = m.group(1) + ".pak"
    return any(k == base or k.endswith("/" + base) for k in mod_files)


def uuid_conflicts_enabled() -> bool:
    """False when the user disabled UUID-keyed pak conflicts via the env var."""
    return os.environ.get(_ENV_FLAG, "1").strip().lower() not in ("0", "false", "no")


class PakUuidCache:
    """(size, mtime_ns) → (uuid, version) cache for .pak files, as msgpack."""

    def __init__(self, cache_path: Path | None = None, readonly: bool = False):
        self._path = cache_path
        self._readonly = readonly
        # abs path → [size, mtime_ns, uuid, version]  (uuid "" = no meta.lsx)
        self._data: dict[str, list] = {}
        self._seen: set[str] = set()
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            with self._path.open("rb") as f:
                payload = msgpack.unpack(f, raw=False, strict_map_key=False)
        except Exception:
            return
        if not isinstance(payload, dict) or payload.get("v") != _CACHE_VERSION:
            return
        paks = payload.get("paks")
        if isinstance(paks, dict):
            self._data = {k: list(v) for k, v in paks.items()
                          if isinstance(v, (list, tuple)) and len(v) == 4}

    def lookup(self, pak_path: Path) -> tuple[str, int]:
        """(module UUID, Version64) for a pak; ("", 0) when it has no meta.lsx."""
        key = str(pak_path)
        self._seen.add(key)
        try:
            st = pak_path.stat()
        except OSError:
            return "", 0
        hit = self._data.get(key)
        if hit is not None and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            return hit[2], hit[3]

        uuid, version = "", 0
        try:
            from Utils.modsettings import parse_meta_lsx
            from Utils.pak_reader import extract_meta_lsx

            xml = extract_meta_lsx(pak_path)
            info = parse_meta_lsx(xml) if xml else None
            if info is not None:
                # Lowercase: build_filemap lowercases conflict keys, so two mods
                # spelling one UUID differently must land on the same key.
                uuid = (info.uuid or "").strip().lower()
                try:
                    version = int(info.version64 or info.version or 0)
                except ValueError:
                    version = 0
        except Exception:
            # Unreadable pak — cache the miss so a multi-GB archive isn't
            # reopened on every filemap build.
            pass

        self._data[key] = [st.st_size, st.st_mtime_ns, uuid, version]
        self._dirty = True
        return uuid, version

    def uuid_for(self, pak_path: Path) -> str:
        return self.lookup(pak_path)[0]

    def save(self) -> None:
        """Persist the cache, dropping entries for paks that no longer exist."""
        if self._path is None or self._readonly or not self._dirty:
            return
        for key in [k for k in self._data if k not in self._seen]:
            if not os.path.exists(key):
                del self._data[key]
        try:
            # Unique temp name: the filemap build and the Mod Files conflict
            # scan can reach this from different threads.
            with atomic_writer(self._path, "wb", encoding=None,
                               suffix=f".{os.getpid()}.tmp") as f:
                msgpack.pack({"v": _CACHE_VERSION, "paks": self._data}, f,
                             use_bin_type=True)
            self._dirty = False
        except Exception:
            pass


def make_pak_uuid_conflict_key_fn(
    staging_root: Path,
    overwrite_dir: Path,
    index_path: Path,
    log_fn=None,
    fallback=None,
    skip_top_level: "set[str] | None" = None,
):
    """Build a (mod_name, rel_key) → conflict-key callback keying paks by UUID.

    Non-pak files go to *fallback* (the path-based key fn) unchanged. A pak with
    no readable meta.lsx keys by file NAME — BG3 deploys every pak flattened into
    one Mods/ folder, so same-named paks collide whatever their staged subfolder.
    *skip_top_level* names folders whose paks are custom-routed elsewhere.
    Returns None when the index can't be read.
    """
    from Utils.filemap import read_mod_index

    try:
        index = read_mod_index(index_path) or {}
    except Exception:
        index = {}
    if not index:
        return None

    cache = PakUuidCache(index_path.parent / CACHE_NAME)
    # uuid → (mod, rel_key, version) currently claiming it. build_filemap walks
    # mods lowest-priority-first, so the last mod seen for a UUID wins.
    seen: dict[str, tuple[str, str, int]] = {}
    wins: dict[str, set[str]] = {}
    losses: dict[str, set[str]] = {}
    _skip = skip_top_level or set()

    def _mod_root(mod_name: str) -> Path:
        return overwrite_dir if mod_name == OVERWRITE_NAME else staging_root / mod_name

    def _basename(rel_key: str) -> str:
        return rel_key.rsplit("/", 1)[-1]

    def _path_key(mod_name: str, rel_key: str) -> str:
        return fallback(mod_name, rel_key) if fallback is not None else rel_key

    def _ck(mod_name: str, rel_key: str) -> str:
        if not rel_key.endswith(".pak"):
            return _path_key(mod_name, rel_key)
        if _skip and "/" in rel_key and rel_key.split("/", 1)[0] in _skip:
            return _path_key(mod_name, rel_key)
        name_key = _PAK_NS + _basename(rel_key)
        entry = index.get(mod_name)
        rel_str = entry[0].get(rel_key) if entry else None
        if not rel_str:
            return name_key
        if _is_multipart(rel_key, entry[0]):
            # "Foo_1.pak" alongside "Foo.pak" is a continuation archive, not a
            # mod (BG3MM's PakIsNotPartial). Never dedupe those by identity —
            # dropping one would break the mod it belongs to.
            return name_key
        uuid, version = cache.lookup(_mod_root(mod_name) / rel_str)
        if not uuid:
            return name_key

        prev = seen.get(uuid)
        if prev is not None and (prev[0], prev[1]) != (mod_name, rel_key):
            # Same module again — the earlier pak loses (build_filemap drops
            # it). Only a cross-mod clash of DIFFERENTLY-named paks gets the
            # UUID icon: same-named ones already collide by path, and two copies
            # inside one mod folder have no second mod to point an icon at.
            cross_mod = prev[0] != mod_name
            same_name = _basename(rel_key) == _basename(prev[1])
            if cross_mod and not same_name:
                losses.setdefault(prev[0], set()).add(uuid)
                wins.setdefault(prev[0], set()).discard(uuid)
                wins.setdefault(mod_name, set()).add(uuid)
            if log_fn is not None:
                if not (cross_mod and same_name):   # that one is an obvious clash
                    log_fn(f'BG3: "{prev[0]}/{prev[1]}" duplicates '
                           f'"{mod_name}/{rel_key}" (module {uuid}) — only the '
                           f"latter deploys.")
                if prev[2] > version > 0:
                    # BG3 Mod Manager keeps the newer pak; we keep the
                    # higher-priority one, so say so rather than downgrade
                    # silently.
                    log_fn(f'BG3: WARNING: the copy being kept '
                           f'("{mod_name}/{rel_key}", version {version}) is '
                           f'OLDER than "{prev[0]}/{prev[1]}" ({prev[2]}) — '
                           f"reorder them to deploy the newer one.")
        seen[uuid] = (mod_name, rel_key, version)
        return _PAK_NS + uuid

    def _uuid_conflict_codes() -> dict:
        """mod → 1 wins / -1 loses / 2 both, for the modlist's UUID icon."""
        out: dict[str, int] = {}
        for mod in set(wins) | set(losses):
            w, lo = bool(wins.get(mod)), bool(losses.get(mod))
            out[mod] = 2 if (w and lo) else (1 if w else -1 if lo else 0)
        return {m: c for m, c in out.items() if c}

    _ck.save_cache = cache.save                      # type: ignore[attr-defined]
    _ck.uuid_conflict_codes = _uuid_conflict_codes   # type: ignore[attr-defined]
    _ck.identity_key_prefix = IDENTITY_KEY_PREFIX    # type: ignore[attr-defined]
    return _ck
