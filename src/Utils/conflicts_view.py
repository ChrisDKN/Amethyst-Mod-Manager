"""Toolkit-neutral file-level conflict computation for the "Show Conflicts" view.

Given a mod, produces three lists of the files it provides plus a tint set:
  - files_win        : (path, "modA, modB")  - this mod overrides those mods here
  - files_lose       : (path, winning_mod)   - this mod is overridden here
  - files_no_conflict: [path]                - no other enabled mod provides it
  - bsa_win_paths    : {path}                - win rows beating archive contents
                        only; the UI tints these cyan like the archive rows

Ported verbatim from the Tk `gui/modlist_panel.py:_show_overwrites_dialog` worker
(the logic is pure os/index/filemap I/O - no GUI). Both loose files and BSA/BA2
archive contents are covered (BSA rows are prefixed ``archive.bsa : inner/path``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Optional

from Utils.filemap import OVERWRITE_NAME

# Rows whose path looks like ``archive.bsa : inner/path`` come from an archive.
BSA_ROW_RE = re.compile(r"^[^/\\:]+\.(?:bsa|ba2)\s+:\s", re.IGNORECASE)


def compute_mod_conflicts(
    mod_name: str,
    *,
    staging_root: Path,
    profile_dir: Path,
    filemap_path: Path,
    modindex_path: Optional[Path],
    bsa_index_path: Optional[Path],
    strip_prefixes: set,
    beaten_mods: set,
    archive_exts: frozenset = frozenset(),
    plugin_order: Optional[list] = None,
    plugin_exts: Optional[frozenset] = None,
    archive_name_ordering: bool = False,
    modlist_path: Optional[Path] = None,
    ckfn: Optional[Callable[[str], str]] = None,
    root_ctx: Optional[tuple] = None,
) -> "tuple[list, list, list, set]":
    """Return (files_win, files_lose, files_no_conflict, bsa_win_paths) for
    *mod_name*.

    *beaten_mods* - the set of mod names this mod overrides (mod-level conflict
    data). *strip_prefixes* - the game-level folder strip set. *ckfn* - optional
    UE5 path remap (rel -> canonical key). *plugin_order* is the enabled plugin
    load order (high→low or as snapshotted); *modlist_path* defaults to
    profile_dir/modlist.txt.
    """
    from Utils.deploy_shared import load_per_mod_strip_prefixes

    if modlist_path is None:
        modlist_path = profile_dir / "modlist.txt"
    plugin_order = plugin_order or []
    plugin_exts = plugin_exts or frozenset()

    per_mod = load_per_mod_strip_prefixes(profile_dir)
    strip_lower = {s.lower() for s in strip_prefixes}
    if root_ctx and len(root_ctx) >= 3:
        root_mods, root_tags, root_data_prefix = root_ctx[:3]
    else:
        root_mods, root_tags = root_ctx or (frozenset(), {})
        root_data_prefix = ""
    root_data_prefix = (
        (root_data_prefix or "").replace("\\", "/").strip("/").lower()
    )
    _root_marker = "\0root/"

    def _is_root(name: str, rel_key: str) -> bool:
        return (name in root_mods
                or rel_key in (root_tags.get(name) or frozenset()))

    def _root_key(rel_key: str) -> str:
        """Conflict key for a game-root entry.

        Entries below the game's Data prefix alias the normal Data namespace;
        other root files get a private marker so ``dinput8.dll`` at game root
        never collides with a same-named file under Data/.
        """
        key = rel_key.replace("\\", "/").lower()
        if root_data_prefix:
            pfx = root_data_prefix + "/"
            if key.startswith(pfx) and len(key) > len(pfx):
                return key[len(pfx):]
        return _root_marker + key

    def _key_for(name: str, rel_key: str) -> str:
        if _is_root(name, rel_key):
            return _root_key(rel_key)
        return ckfn(rel_key) if ckfn else rel_key.lower()

    def _display_key(key: str) -> str:
        return key[len(_root_marker):] if key.startswith(_root_marker) else key

    # Per-mod strip data memoized once per mod (the sort + set merge used to
    # be rebuilt on every _strip_for call, i.e. once per file walked).
    _strip_cache: dict[str, tuple[list[tuple[str, str]], set[str]]] = {}

    def _strip_data(name: str) -> "tuple[list[tuple[str, str]], set[str]]":
        """(path prefixes longest-first with lowercase, merged segment set)."""
        data = _strip_cache.get(name)
        if data is None:
            entries = per_mod.get(name, [])
            paths = sorted((p for p in entries if "/" in p),
                           key=len, reverse=True)
            data = ([(p, p.lower()) for p in paths],
                    strip_lower | {s.lower() for s in entries if "/" not in s})
            _strip_cache[name] = data
        return data

    def _strip_for(name: str, rel: str) -> str:
        """Strip prefixes the same way filemap.py does for a given mod."""
        mod_paths, mod_segs = _strip_data(name)
        if mod_paths:
            rl = rel.lower()
            for p, pl in mod_paths:
                if rl.startswith(pl + "/"):
                    rel = rel[len(p) + 1:]
                    break
                elif rl == pl:
                    rel = ""
                    break
        while "/" in rel and rel.split("/", 1)[0].lower() in mod_segs:
            rel = rel.split("/", 1)[1]
        return rel

    # Build winner map from filemap.txt, keyed by deploy path (or staged path).
    winning_map: dict[str, tuple[str, str]] = {}
    if filemap_path.is_file():
        # surrogateescape: filemap.txt rel paths derive from on-disk filenames
        # whose non-UTF-8 bytes decode to surrogate code points - a plain utf-8
        # read raises on them.
        with filemap_path.open(encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                line = line.rstrip("\n")
                if "\t" not in line:
                    continue
                rel_path, winner = line.split("\t", 1)
                key = ckfn(rel_path) if ckfn else rel_path.lower()
                winning_map[key] = (rel_path, winner)
    root_filemap_path = filemap_path.parent / "filemap_root.txt"
    if root_filemap_path.is_file():
        with root_filemap_path.open(
                encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                line = line.rstrip("\n")
                if "\t" not in line:
                    continue
                rel_path, winner = line.split("\t", 1)
                # Root deploy runs after the normal Data deploy, so assigning
                # last intentionally replaces a normal winner at an aliased
                # Data-relative key.
                winning_map[_root_key(rel_path)] = (rel_path, winner)

    # Collect this mod's files. Prefer modindex.bin (already normalized with the
    # same strip logic filemap.py uses); fall back to a staging walk.
    my_files: dict[str, str] = {}
    _my_index_entry = None
    if modindex_path is not None and modindex_path.is_file():
        try:
            from Utils.filemap import read_mod_index as _read_mi
            _mi = _read_mi(modindex_path)
            if _mi is not None:
                _my_index_entry = _mi.get(mod_name)
        except Exception:
            _my_index_entry = None
    if _my_index_entry is not None:
        _normal, _root = _my_index_entry
        for _k, _rel_str in _normal.items():
            my_files[_key_for(mod_name, _k)] = _rel_str
        for _k, _rel_str in _root.items():
            my_files[_key_for(mod_name, _k)] = _rel_str
    else:
        my_staging = (staging_root.parent / "overwrite"
                      if mod_name == OVERWRITE_NAME else staging_root / mod_name)
        if my_staging.is_dir():
            for dirpath, _, fnames in os.walk(my_staging):
                for fname in fnames:
                    if fname.lower() == "meta.ini":
                        continue
                    full = os.path.join(dirpath, fname)
                    rel = os.path.relpath(full, my_staging).replace("\\", "/")
                    if mod_name not in root_mods:
                        rel = _strip_for(mod_name, rel)
                    if rel:
                        key = _key_for(mod_name, rel)
                        my_files[key] = rel

    # Classify each file.
    files_i_win: list[tuple[str, str]] = []
    files_i_lose: list[tuple[str, str]] = []
    for deploy_key, _orig_rel in sorted(my_files.items()):
        if deploy_key in winning_map:
            _orig, winner = winning_map[deploy_key]
            if winner == mod_name:
                files_i_win.append((deploy_key, ""))
            else:
                files_i_lose.append((deploy_key, winner))
        else:
            files_i_lose.append((deploy_key, "(no winner - disabled?)"))

    # Annotate wins: look up each beaten mod's files in modindex.bin.
    rel_to_losers: dict[str, list[str]] = {}
    mod_index = None
    if modindex_path is not None and modindex_path.is_file():
        try:
            from Utils.filemap import read_mod_index as _read_mi
            mod_index = _read_mi(modindex_path)
        except Exception:
            mod_index = None
    if mod_index is not None:
        for loser_mod in beaten_mods:
            entry = mod_index.get(loser_mod)
            if not entry:
                continue
            normal_files, root_files = entry
            for _key in normal_files:
                effective = _key_for(loser_mod, _key)
                if effective in my_files:
                    rel_to_losers.setdefault(effective, []).append(loser_mod)
            for _key in root_files:
                effective = _key_for(loser_mod, _key)
                if effective in my_files:
                    rel_to_losers.setdefault(effective, []).append(loser_mod)
    # Per-path losers found only inside an archive (feeds bsa_win_paths).
    arch_loser_at: dict[str, set[str]] = {}
    # Wins against BSA-only losers (engine rule: loose > BSA). Scans EVERY
    # enabled mod's archives rather than *beaten_mods*, which the caller
    # derives from cached conflict data - a stale entry there would drop these
    # rows and misfile the paths under "no conflict". UE paks keep the
    # beaten_mods scope: they resolve by mount order and loose assets don't
    # blanket-override them, so only engine-reported wins are trustworthy.
    if archive_exts and bsa_index_path is not None and bsa_index_path.is_file():
        try:
            from Utils.bsa_filemap import read_bsa_index as _read_bi
            _bi = _read_bi(bsa_index_path) or {}
            if archive_name_ordering:
                _arch_victims = [m for m in beaten_mods if m in _bi]
            else:
                from Utils.modlist import read_modlist as _read_ml
                _enabled = {e.name for e in _read_ml(modlist_path)
                            if not e.is_separator and e.enabled}
                _arch_victims = [m for m in _bi
                                 if m != mod_name and m in _enabled]
            for loser_mod in _arch_victims:
                archives = _bi.get(loser_mod)
                if not archives:
                    continue
                for _bsa, _mt, _paths in archives:
                    for _fp in _paths:
                        if _fp in my_files and loser_mod not in rel_to_losers.get(_fp, ()):
                            rel_to_losers.setdefault(_fp, []).append(loser_mod)
                            # Loser known only via its archive (loose losers
                            # were recorded above) - drives the cyan tint.
                            arch_loser_at.setdefault(_fp, set()).add(loser_mod)
        except Exception:
            pass
    if mod_index is None:
        # Fallback: walk beaten mods' staging directly (older profiles).
        for loser_mod in beaten_mods:
            loser_staging = staging_root / loser_mod
            if not loser_staging.is_dir():
                continue
            for dirpath, _, fnames in os.walk(loser_staging):
                for fname in fnames:
                    if fname.lower() == "meta.ini":
                        continue
                    full = os.path.join(dirpath, fname)
                    rel = os.path.relpath(full, loser_staging).replace("\\", "/")
                    if loser_mod not in root_mods:
                        rel = _strip_for(loser_mod, rel)
                    if rel:
                        key = _key_for(loser_mod, rel)
                        if key in my_files:
                            rel_to_losers.setdefault(key, []).append(loser_mod)

    # Rows beating archive contents only - tinted cyan. Rows that also beat a
    # loose mod keep the normal win colour: they're loose conflicts too.
    bsa_win_paths: set[str] = {
        k for k, losers in rel_to_losers.items()
        if (a := arch_loser_at.get(k)) and set(losers) <= a
    }

    files_i_win_final: list[tuple[str, str]] = [
        (_display_key(deploy_key), beaten_str)
        for deploy_key, _ in files_i_win
        if (beaten_str := ", ".join(rel_to_losers.get(deploy_key, [])))
    ]
    # Files where this mod beats a lower-priority mod but ultimately loses to a
    # higher-priority winner (conflict engine reports these as wins).
    _win_keys = {k for k, _ in files_i_win}
    for _lose_key, _ in files_i_lose:
        _losers_under = rel_to_losers.get(_lose_key)
        if _losers_under and _lose_key not in _win_keys:
            files_i_win_final.append(
                (_display_key(_lose_key), ", ".join(_losers_under)))
    files_no_conflict: list[str] = [
        _display_key(deploy_key)
        for deploy_key, _ in files_i_win
        if not rel_to_losers.get(deploy_key)
    ]

    # BSA-vs-BSA conflicts - append rows from this mod's archives.
    if archive_exts and bsa_index_path is not None and bsa_index_path.is_file():
        try:
            from Utils.bsa_filemap import read_bsa_index, compute_bsa_winner_map
            from Utils.modlist import read_modlist as _read_ml
            bsa_index = read_bsa_index(bsa_index_path) or {}
            entries_ml = _read_ml(modlist_path)
            enabled_ml = [e for e in entries_ml if not e.is_separator and e.enabled]
            priority_low_to_high = [e.name for e in reversed(enabled_ml)]

            bsa_winner, bsa_losers = compute_bsa_winner_map(
                bsa_index, priority_low_to_high,
                plugin_order or None, plugin_exts or None,
                modindex_path, archive_name_ordering,
            )

            my_archives = bsa_index.get(mod_name, [])
            for _bsa_name, _mt, _paths in my_archives:
                for _fp in sorted(_paths):
                    _display = f"{_bsa_name} : {_fp}"
                    winner = bsa_winner.get(_fp)
                    if winner is None:
                        continue
                    _loose = winning_map.get(_fp)
                    _loose_winner = _loose[1] if _loose else None
                    if _loose_winner is not None and _loose_winner != mod_name:
                        files_i_lose.append((_display, _loose_winner))
                        continue
                    if winner == mod_name:
                        _losers = [
                            l for l in bsa_losers.get(_fp, []) if l != mod_name
                        ]
                        if _losers:
                            files_i_win_final.append(
                                (_display, ", ".join(_losers)))
                        else:
                            files_no_conflict.append(_display)
                    else:
                        files_i_lose.append((_display, winner))
        except Exception:
            pass

    files_i_lose = [(_display_key(path), winner)
                    for path, winner in files_i_lose]
    return files_i_win_final, files_i_lose, files_no_conflict, bsa_win_paths
