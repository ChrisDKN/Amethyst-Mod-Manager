"""
mesh_catalog.py
Enumerate every copy of an asset kind (default .nif) across a profile + vanilla.

Unlike Utils.asset_resolver, which answers "which copy applies", this lists ALL
copies — loose files from every enabled mod, loose files in the game data folder,
and members of BSA/BA2 archives on both sides — and flags the one the game would
load, using the resolver's own winner tables so the two never disagree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from Utils.asset_resolver import normalise

__all__ = [
    "MeshEntry", "build_catalog", "find_copies", "mod_has_assets",
    "read_entry", "source_label",
    "MOD_LOOSE", "DATA_LOOSE", "MOD_ARCHIVE", "DATA_ARCHIVE",
    "DEFAULT_PREFIX", "DEFAULT_EXTS",
]

MOD_LOOSE = "mod_loose"
DATA_LOOSE = "data_loose"
MOD_ARCHIVE = "mod_archive"
DATA_ARCHIVE = "data_archive"

# Engine precedence: loose always beats archived, mods beat the data folder.
_ORDER = {MOD_LOOSE: 0, DATA_LOOSE: 1, MOD_ARCHIVE: 2, DATA_ARCHIVE: 3}

DEFAULT_PREFIX = "meshes/"
DEFAULT_EXTS = (".nif",)


@dataclass(frozen=True)
class MeshEntry:
    """One physical copy of one asset path."""
    rel_key: str                    # 'meshes/armor/x.nif' (lower, forward slash)
    kind: str                       # MOD_LOOSE | DATA_LOOSE | MOD_* ARCHIVE
    mod: str = ""                   # staged mod name (mod_* kinds)
    archive: Path | None = None     # holding .bsa/.ba2 (*_archive kinds)
    wins: bool = False              # the copy the game loads

    @property
    def name(self) -> str:
        return self.rel_key.rsplit("/", 1)[-1]


def source_label(entry: MeshEntry) -> str:
    """Short human-readable origin, e.g. 'Lux' or 'Skyrim - Meshes0.bsa'."""
    if entry.kind == MOD_LOOSE:
        return entry.mod
    if entry.kind == DATA_LOOSE:
        return "Data"
    if entry.kind == MOD_ARCHIVE:
        return f"{entry.mod} ▸ {entry.archive.name}" if entry.archive else entry.mod
    return entry.archive.name if entry.archive else "Data"


def _ext_ok(rel_key: str, exts: tuple[str, ...]) -> bool:
    return rel_key.endswith(exts)


def _walk_ext(root: Path, prefix: str, exts: tuple[str, ...]) -> list[str]:
    """Loose asset keys under *root*/<prefix>, case-insensitively.

    A mod can ship 'Meshes/' AND 'meshes/' as two real directories on Linux, so
    every case variant of the top folder is walked, not just the first found.
    """
    top = prefix.strip("/").split("/")[0].lower()
    out: list[str] = []
    try:
        with os.scandir(root) as it:
            starts = [e.path for e in it if e.is_dir() and e.name.lower() == top]
    except OSError:
        return out
    for start in starts:
        for dirpath, _dirs, files in os.walk(start):
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/").lower()
            for fname in files:
                key = f"{rel_dir}/{fname.lower()}"
                if _ext_ok(key, exts):
                    out.append(key)
    return out


def build_catalog(resolver, staging: Path | None, modlist_path: Path | None,
                  data_dir: Path | None, *, prefix: str = DEFAULT_PREFIX,
                  exts: tuple[str, ...] = DEFAULT_EXTS, extra_mods=(),
                  cancel=None) -> list[MeshEntry]:
    """Every copy of every matching asset, winner-flagged, sorted by path.

    *resolver* is an AssetResolver whose keep_prefix covers *prefix* (its winner
    tables decide the flags). *extra_mods* are listed even when the modlist
    doesn't enable them (browsing one disabled mod's meshes), ranked last so
    they never displace a real winner. *cancel* is an optional callable polled
    between sources so a view can abandon a stale scan.
    """
    staging = Path(staging) if staging else None
    data_dir = Path(data_dir) if data_dir else None
    entries: list[MeshEntry] = []

    prio = _enabled_mods(modlist_path)
    for mod in extra_mods or ():
        if mod and mod not in prio:
            prio.append(mod)
    prio_rank = {name: i for i, name in enumerate(prio)}

    def stop() -> bool:
        return bool(cancel and cancel())

    if staging is not None:
        entries += _mod_loose(staging, prio, prefix, exts)
    if stop():
        return []

    # Loose files the game sees but no mod owns. A DEPLOYED profile puts every
    # winning mod file in the data folder too, so those keys are dropped here —
    # otherwise each one sprouts a phantom "Data" duplicate of itself.
    if data_dir is not None:
        deployed = set(_winner_map(resolver, "loose_winners"))
        for key in _walk_ext(data_dir, prefix, exts):
            if key not in deployed:
                entries.append(MeshEntry(key, DATA_LOOSE))
    if stop():
        return []

    if staging is not None:
        entries += _mod_archives(staging, prio, prefix, exts)
    if stop():
        return []

    if data_dir is not None:
        entries += _data_archives(data_dir, prefix, exts)
    if stop():
        return []

    return _flag_winners(entries, resolver, prio_rank)


def find_copies(rel_keys, resolver, staging: Path | None,
                modlist_path: Path | None, data_dir: Path | None,
                *, keep_prefix=("textures/", "materials/")) -> dict:
    """Every copy of each of *rel_keys*, winner first — build_catalog for a
    handful of known paths, cheap enough to run per preview."""
    keys = [normalise(k) for k in rel_keys if k]
    keys = [k for k in dict.fromkeys(keys) if k]          # de-dupe, keep order
    if not keys:
        return {}
    staging = Path(staging) if staging else None
    data_dir = Path(data_dir) if data_dir else None
    wanted = set(keys)
    entries: list[MeshEntry] = []

    prio = _enabled_mods(modlist_path)
    prio_rank = {name: i for i, name in enumerate(prio)}

    from Utils.asset_resolver import DirCache
    dirs = DirCache()

    if staging is not None:
        try:
            from Utils.filemap import read_mod_index
            index = read_mod_index(staging.parent / "modindex.bin") or {}
        except Exception:                                # noqa: BLE001
            index = {}
        for mod in prio:
            got = index.get(mod)
            if not got:
                continue
            for key in wanted.intersection(got[0]):
                entries.append(MeshEntry(key, MOD_LOOSE, mod=mod))

    if data_dir is not None:
        deployed = set(_winner_map(resolver, "loose_winners"))
        for key in keys:
            if key not in deployed and dirs.resolve(data_dir, key) is not None:
                entries.append(MeshEntry(key, DATA_LOOSE))

    if staging is not None:
        try:
            from Utils.bsa_filemap import read_bsa_index
            bsa = read_bsa_index(staging.parent / "bsa_index.bin") or {}
        except Exception:                                # noqa: BLE001
            bsa = {}
        for mod in prio:
            for archive_name, _mtime, paths in bsa.get(mod, ()):
                hits = wanted.intersection(paths)
                if not hits:
                    continue
                archive = dirs.resolve(staging / mod, archive_name)
                if archive is None:
                    continue
                for key in hits:
                    entries.append(MeshEntry(key, MOD_ARCHIVE, mod=mod,
                                             archive=archive))

    if data_dir is not None:
        entries += _data_archive_copies(data_dir, wanted, keep_prefix)

    grouped: dict[str, list[MeshEntry]] = {k: [] for k in keys}
    for e in _flag_winners(entries, resolver, prio_rank):
        grouped.setdefault(e.rel_key, []).append(e)
    return grouped


def mod_has_assets(staging: Path | None, mod: str, *,
                   prefix: str = DEFAULT_PREFIX,
                   exts: tuple[str, ...] = DEFAULT_EXTS) -> bool:
    """True if *mod* ships at least one matching asset, loose or in its own
    BSA/BA2 — the right-click gate for "is there anything to view here".

    Reads the cached modindex/bsa_index (no disk walk) when they exist, so it
    is cheap enough to run while a context menu is being built.
    """
    if staging is None or not mod:
        return False
    staging = Path(staging)

    index = None
    try:
        from Utils.filemap import read_mod_index
        index = read_mod_index(staging.parent / "modindex.bin")
    except Exception:                                    # noqa: BLE001
        index = None
    if index is not None:
        got = index.get(mod)
        for key in (got[0] if got else ()):
            if key.startswith(prefix) and _ext_ok(key, exts):
                return True
    elif _walk_ext(staging / mod, prefix, exts):
        return True

    try:
        from Utils.bsa_filemap import read_bsa_index
        bsa = read_bsa_index(staging.parent / "bsa_index.bin") or {}
    except Exception:                                    # noqa: BLE001
        bsa = {}
    for _archive_name, _mtime, paths in bsa.get(mod, ()):
        for key in paths:
            if key.startswith(prefix) and _ext_ok(key, exts):
                return True
    return False


def _data_archive_copies(data_dir: Path, wanted: set, keep_prefix) -> list:
    """Vanilla archives holding any of *wanted*, in the game's mount order."""
    try:
        from Utils.archive_lookup import find_archives, index_archive
    except Exception:                                    # noqa: BLE001
        return []
    out: list[MeshEntry] = []
    for archive in find_archives([data_dir]):
        # The TOC cache is keyed by keep_prefix — pass the resolver's to reuse
        # the indexes it already built.
        for key in wanted.intersection(index_archive(archive, keep_prefix)):
            out.append(MeshEntry(key, DATA_ARCHIVE, archive=archive))
    return out


def _enabled_mods(modlist_path: Path | None) -> list[str]:
    """Enabled mod names, highest priority first (modlist file order)."""
    if not modlist_path:
        return []
    try:
        from Utils.modlist import read_modlist
        return [e.name for e in read_modlist(Path(modlist_path))
                if e.enabled and not e.is_separator]
    except Exception:                                    # noqa: BLE001
        return []


def _mod_loose(staging: Path, mods: list[str], prefix: str,
               exts: tuple[str, ...]) -> list[MeshEntry]:
    """Loose copies from every enabled mod — modindex.bin (NOT filemap.txt,
    which holds only winners), or a disk walk if the index is absent."""
    out: list[MeshEntry] = []
    index = None
    try:
        from Utils.filemap import read_mod_index
        index = read_mod_index(staging.parent / "modindex.bin")
    except Exception:                                    # noqa: BLE001
        index = None

    for mod in mods:
        if index is not None:
            got = index.get(mod)
            keys = got[0].keys() if got else ()
            for key in keys:
                if key.startswith(prefix) and _ext_ok(key, exts):
                    out.append(MeshEntry(key, MOD_LOOSE, mod=mod))
        else:
            for key in _walk_ext(staging / mod, prefix, exts):
                out.append(MeshEntry(key, MOD_LOOSE, mod=mod))
    return out


def _mod_archives(staging: Path, mods: list[str], prefix: str,
                  exts: tuple[str, ...]) -> list[MeshEntry]:
    """Archived copies inside every enabled mod's own BSA/BA2 files."""
    try:
        from Utils.bsa_filemap import read_bsa_index
        index = read_bsa_index(staging.parent / "bsa_index.bin") or {}
    except Exception:                                    # noqa: BLE001
        return []
    from Utils.asset_resolver import DirCache
    dirs = DirCache()
    out: list[MeshEntry] = []
    wanted = set(mods)
    for mod, archives in index.items():
        if mod not in wanted:
            continue
        for archive_name, _mtime, paths in archives:
            hits = [p for p in paths if p.startswith(prefix) and _ext_ok(p, exts)]
            if not hits:
                continue
            archive = dirs.resolve(staging / mod, archive_name)
            if archive is None:
                continue
            for key in hits:
                out.append(MeshEntry(key, MOD_ARCHIVE, mod=mod, archive=archive))
    return out


def _data_archives(data_dir: Path, prefix: str,
                   exts: tuple[str, ...]) -> list[MeshEntry]:
    """Archived copies in the game data folder, in the order the game mounts."""
    try:
        from Utils.archive_lookup import find_archives
        from Utils.bsa_reader import read_bsa_file_list
    except Exception:                                    # noqa: BLE001
        return []
    out: list[MeshEntry] = []
    for archive in find_archives([data_dir]):
        # Name-only TOC read: never decompresses, and a broken archive is [].
        for key in read_bsa_file_list(archive):
            if key.startswith(prefix) and _ext_ok(key, exts):
                out.append(MeshEntry(key, DATA_ARCHIVE, archive=archive))
    return out


def _winner_map(resolver, attr: str) -> dict[str, str]:
    if resolver is None:
        return {}
    try:
        return getattr(resolver, attr)() or {}
    except Exception:                                    # noqa: BLE001
        return {}


def _flag_winners(entries: list[MeshEntry], resolver,
                  prio_rank: dict[str, int]) -> list[MeshEntry]:
    """Mark one copy per path as the winner, mirroring AssetResolver.read()."""
    loose_w = _winner_map(resolver, "loose_winners")
    arch_w = _winner_map(resolver, "archive_winners")

    groups: dict[str, list[MeshEntry]] = {}
    for e in entries:
        groups.setdefault(e.rel_key, []).append(e)

    out: list[MeshEntry] = []
    for key in sorted(groups):
        group = groups[key]
        group.sort(key=lambda e: (_ORDER[e.kind],
                                  prio_rank.get(e.mod, 1 << 30),
                                  str(e.archive or "")))
        winner = _pick_winner(key, group, loose_w, arch_w)
        for e in group:
            out.append(e if e is not winner else
                       MeshEntry(e.rel_key, e.kind, e.mod, e.archive, True))
    # Winner first inside each path group; paths already sorted.
    out.sort(key=lambda e: (e.rel_key, not e.wins, _ORDER[e.kind],
                            prio_rank.get(e.mod, 1 << 30), str(e.archive or "")))
    return out


def _pick_winner(key: str, group: list[MeshEntry], loose_w: dict[str, str],
                 arch_w: dict[str, str]) -> MeshEntry | None:
    """Same four steps as AssetResolver.read(), against listed copies only."""
    lw = loose_w.get(key)
    for e in group:
        if e.kind == MOD_LOOSE and e.mod == lw:
            return e
    if not loose_w:
        # No filemap AT ALL: fall back to modlist priority (group is sorted).
        # Gated on the whole map being empty, not this key missing from it —
        # a filemap that omits a path means the engine skips the loose copy
        # too, and guessing there would disagree with AssetResolver.read().
        for e in group:
            if e.kind == MOD_LOOSE:
                return e
    for e in group:
        if e.kind == DATA_LOOSE:
            return e
    aw = arch_w.get(key)
    for e in group:
        if e.kind == MOD_ARCHIVE and e.mod == aw:
            return e
    if not arch_w:
        # Same rule: only guess when no winner map exists at all.
        for e in group:
            if e.kind == MOD_ARCHIVE:
                return e
    for e in group:
        if e.kind == DATA_ARCHIVE:
            return e
    return group[0] if group else None


def read_entry(entry: MeshEntry, staging: Path | None, data_dir: Path | None,
               dirs=None) -> bytes | None:
    """Bytes of THIS copy — not the winner, which is all resolver.read() gives."""
    if dirs is None:
        from Utils.asset_resolver import DirCache
        dirs = DirCache()
    try:
        if entry.kind == MOD_LOOSE:
            if staging is None:
                return None
            path = dirs.resolve(Path(staging) / entry.mod, entry.rel_key)
            return path.read_bytes() if path else None
        if entry.kind == DATA_LOOSE:
            if data_dir is None:
                return None
            path = dirs.resolve(Path(data_dir), entry.rel_key)
            return path.read_bytes() if path else None
        if entry.archive is None:
            return None
        return read_archive_member(entry.archive, entry.rel_key)
    except OSError:
        return None


def read_archive_member(archive: Path, inner_path: str) -> bytes | None:
    """One member's bytes from a BSA/BA2, without unpacking the archive."""
    key = inner_path.replace("\\", "/").lower()
    archive = Path(archive)
    try:
        if archive.suffix.lower() == ".ba2":
            from Utils.ba2_extract import index_ba2, read_ba2_entry
            rec = index_ba2(archive).get(key)
            return read_ba2_entry(archive, rec) if rec else None
        from Utils.bsa_extract import index_bsa, read_bsa_entry
        info, entries = index_bsa(archive)
        found = entries.get(key)
        return read_bsa_entry(archive, info, found) if found else None
    except Exception:                                    # noqa: BLE001
        return None
