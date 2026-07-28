"""
installed_scan.py
One-pass installed-game detection for the Add Game picker.

The picker needs an installed/not-installed verdict for every known game
(~60-100 with synced custom handlers). The individual finders in
steam_finder/heroic_finder re-enumerate the disk on every call, so calling
them per game made the scan O(picker games x installed games) — minutes on
slow media. Worst offender: the Heroic GOG fallback re-walked entire game
installs recursively for every handler exe that didn't match.

InstalledIndex touches each data source once and answers game_installed()
from memory:
  - Steam: appmanifest_*.acf parsed once per library, plus a memoized
    directory-listing cache over steamapps/common — each directory is
    scandir'd at most once across the whole scan, no matter how many games
    probe it.
  - Heroic: Epic/GOG/sideload manifests parsed once. GOG entries with no
    stored executable get their install tree walked once (not once per
    handler exe) into a filename index.

Detection semantics mirror the old per-game finder calls: steam-id+manifest
first, then exe search across Steam libraries, then Heroic app names, then
Heroic by bare exe name. All lookups are case-insensitive on names.

No UI, no game-specific knowledge.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from Utils.steam_finder import find_steam_libraries, _parse_acf_installdir
from Utils.heroic_finder import (
    _find_heroic_config_roots, _load_epic_installed, _load_gog_installed,
    _load_sideload_installed, _sideload_game_root, _stored_exe_matches,
)

_ACF_ID_RE = re.compile(r"appmanifest_(\d+)\.acf$")


def _exe_parts(exe_name: str) -> list[str]:
    """Lowercase path segments of an exe name ('bin/x64/Game.exe' or bare)."""
    return [p for p in exe_name.replace("\\", "/").lower().split("/") if p]


class InstalledIndex:
    """Build once (worker thread), then call game_installed(game) per game."""

    def __init__(self, log=None):
        self._log = log or (lambda msg: None)
        # dir path (str) -> {lowercase name: (actual name, is_file)}
        self._listings: dict[str, dict[str, tuple[str, bool]]] = {}
        # GOG install path (str) -> {lowercase filename: [relative part tuples]}
        self._gog_walks: dict[str, dict[str, list[tuple[str, ...]]]] = {}
        self._build_steam()
        self._build_heroic()
        self._build_lutris()

    # ------------------------------------------------------------------
    # Steam
    # ------------------------------------------------------------------

    def _build_steam(self) -> None:
        try:
            libraries = find_steam_libraries()
        except Exception as exc:
            self._log(f"find_steam_libraries failed, continuing with none: {exc}")
            libraries = []

        # Every <library>/steamapps/common/<GameDir>, listed once each.
        self._steam_game_dirs: list[Path] = []
        # steam_id -> [game dirs from appmanifest installdir], across libraries.
        self._acf_dirs: dict[str, list[Path]] = {}

        for common in libraries:
            for low, (name, is_file) in self._listing(common).items():
                if not is_file:
                    self._steam_game_dirs.append(common / name)
            steamapps = common.parent
            try:
                acf_files = list(steamapps.glob("appmanifest_*.acf"))
            except OSError:
                acf_files = []
            for acf in acf_files:
                m = _ACF_ID_RE.match(acf.name)
                if not m:
                    continue
                installdir = _parse_acf_installdir(acf)
                if not installdir:
                    continue
                game_dir = common / installdir
                if game_dir.is_dir():
                    self._acf_dirs.setdefault(m.group(1), []).append(game_dir)

    def _listing(self, path: Path) -> dict[str, tuple[str, bool]]:
        """Memoized scandir: lowercase name -> (actual name, is_file)."""
        key = str(path)
        cached = self._listings.get(key)
        if cached is None:
            cached = {}
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        try:
                            is_file = entry.is_file()
                        except OSError:
                            is_file = False
                        cached[entry.name.lower()] = (entry.name, is_file)
            except OSError:
                pass
            self._listings[key] = cached
        return cached

    def _exe_in_dir(self, game_dir: Path, exe_name: str) -> bool:
        """Case-insensitive check that exe_name (may contain subdirs) exists
        as a file under game_dir. Only descends directories a lookup actually
        names, each listed at most once (memoized)."""
        parts = _exe_parts(exe_name)
        if not parts:
            return False
        cur = game_dir
        for i, part in enumerate(parts):
            hit = self._listing(cur).get(part)
            if hit is None:
                return False
            name, is_file = hit
            if i == len(parts) - 1:
                return is_file
            if is_file:
                return False
            cur = cur / name
        return False

    # ------------------------------------------------------------------
    # Heroic
    # ------------------------------------------------------------------

    def _build_heroic(self) -> None:
        # Epic app names with an existing install dir (exact-case, like
        # _find_epic_game's installed.get(app_name)).
        self._epic_ok_names: set[str] = set()
        # Stored executables of installed Epic games, for exe matching.
        self._epic_exes: list[str] = []
        # GOG product ids (lowercase) with an existing install dir.
        self._gog_ok_ids: set[str] = set()
        # (stored_exe or "", install_path) of installed GOG games.
        self._gog_exes: list[tuple[str, Path]] = []
        # Stored executables of sideloaded apps.
        self._sideload_exes: list[str] = []

        try:
            roots = _find_heroic_config_roots()
        except Exception as exc:
            self._log(f"heroic config lookup failed, continuing with none: {exc}")
            roots = []

        for root in roots:
            for app_name, entry in _load_epic_installed(root).items():
                if not isinstance(entry, dict):
                    continue
                install_path = entry.get("install_path", "")
                if not install_path or not Path(install_path).is_dir():
                    continue
                self._epic_ok_names.add(str(app_name))
                stored_exe = entry.get("executable", "")
                if stored_exe:
                    self._epic_exes.append(stored_exe)

            for entry in _load_gog_installed(root):
                if not isinstance(entry, dict):
                    continue
                install_path_raw = entry.get("install_path", entry.get("path", ""))
                if not install_path_raw:
                    continue
                install_path = Path(install_path_raw)
                if not install_path.is_dir():
                    continue
                app_id = str(entry.get("appName") or entry.get("app_name") or "")
                if app_id:
                    self._gog_ok_ids.add(app_id.lower())
                stored_exe = entry.get("executable", "") or entry.get("exe", "")
                self._gog_exes.append((stored_exe, install_path))

            for entry in _load_sideload_installed(root):
                if not isinstance(entry, dict):
                    continue
                install = entry.get("install") or {}
                stored_exe = (install.get("executable", "")
                              if isinstance(install, dict) else "")
                if stored_exe:
                    self._sideload_exes.append(stored_exe)

    def _gog_walk_index(self, install_path: Path) -> dict[str, list[tuple[str, ...]]]:
        """Walk a GOG install once (memoized): lowercase filename -> relative
        lowercase part tuples. Only built for entries with no stored exe."""
        key = str(install_path)
        index = self._gog_walks.get(key)
        if index is None:
            self._log(f"indexing GOG install with no stored executable: {install_path}")
            index = {}
            try:
                for dirpath, _dirnames, filenames in os.walk(install_path):
                    rel = os.path.relpath(dirpath, str(install_path))
                    rel_parts = (() if rel == "." else
                                 tuple(rel.lower().split(os.sep)))
                    for fn in filenames:
                        low = fn.lower()
                        index.setdefault(low, []).append(rel_parts + (low,))
            except OSError:
                pass
            self._gog_walks[key] = index
        return index

    def _heroic_by_app_names(self, app_names: list[str]) -> bool:
        if any(str(n) in self._epic_ok_names for n in app_names):
            return True
        lower = {str(n).lower() for n in app_names}
        return bool(lower & self._gog_ok_ids)

    def _heroic_by_exe(self, exe_name: str) -> bool:
        rel_parts = _exe_parts(exe_name)
        if not rel_parts:
            return False
        exe_lower = rel_parts[-1]

        for stored_exe in self._epic_exes:
            if _stored_exe_matches(stored_exe, rel_parts):
                return True

        for stored_exe, install_path in self._gog_exes:
            if stored_exe:
                if _stored_exe_matches(stored_exe, rel_parts):
                    return True
            else:
                hits = self._gog_walk_index(install_path).get(exe_lower)
                if hits and (len(rel_parts) == 1 or any(
                        t[-len(rel_parts):] == tuple(rel_parts) for t in hits)):
                    return True

        for stored_exe in self._sideload_exes:
            if (_stored_exe_matches(stored_exe, rel_parts)
                    and _sideload_game_root(stored_exe, exe_name)):
                return True

        return False

    # ------------------------------------------------------------------
    # Lutris
    # ------------------------------------------------------------------

    def _build_lutris(self) -> None:
        # Per installed Lutris game: lowercase path segments of its configured
        # game.exe. Read from Lutris's database + YAML configs in a single
        # pass (build_installed_exe_index), so per-handler lookups are O(1)
        # against memory rather than re-reading Lutris's data each time.
        self._lutris_exes: list[list[str]] = []
        try:
            from Utils.lutris_finder import build_installed_exe_index
            self._lutris_exes = build_installed_exe_index()
        except Exception as exc:
            self._log(f"lutris index build failed, continuing with none: {exc}")
            self._lutris_exes = []

    def _lutris_by_exe(self, exe_name: str) -> bool:
        """True if any installed Lutris game's exe tail matches exe_name.

        Matching mirrors find_lutris_game_info_by_exe: a bare handler name
        matches on filename; a multi-segment name must match segment-for-
        segment at the tail (same rule as _stored_exe_matches)."""
        rel_parts = _exe_parts(exe_name)
        if not rel_parts:
            return False
        for stored_parts in self._lutris_exes:
            if len(rel_parts) > 1:
                if (len(stored_parts) >= len(rel_parts)
                        and stored_parts[-len(rel_parts):] == rel_parts):
                    return True
            elif stored_parts and stored_parts[-1] == rel_parts[0]:
                return True
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def game_installed(self, game) -> bool:
        """True if *game* (a handler object) appears installed via Steam or
        Heroic. Same detection order as the old per-game finder calls."""
        exe_name = getattr(game, "exe_name", "") or ""
        all_exe = [e for e in
                   ([exe_name] + list(getattr(game, "exe_name_alts", []) or []))
                   if e]
        steam_id = str(getattr(game, "steam_id", "") or "")

        if steam_id and exe_name:
            for game_dir in self._acf_dirs.get(steam_id, ()):
                if self._exe_in_dir(game_dir, exe_name):
                    return True

        for exe in all_exe:
            for game_dir in self._steam_game_dirs:
                if self._exe_in_dir(game_dir, exe):
                    return True

        # Declared app names are authoritative for Heroic — when set, the exe
        # scan is skipped: generic launcher names collide across games
        # (FalloutLauncher.exe = both Fallout 3 GOTY and classic Fallout).
        heroic_names = list(getattr(game, "heroic_app_names", []) or [])
        if heroic_names:
            if self._heroic_by_app_names(heroic_names):
                return True
        else:
            for exe in all_exe:
                bare = exe.replace("\\", "/").rsplit("/", 1)[-1]
                if bare and self._heroic_by_exe(bare):
                    return True

        for exe in all_exe:
            if self._lutris_by_exe(exe):
                return True

        return False
