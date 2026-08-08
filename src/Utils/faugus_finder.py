"""
faugus_finder.py
Utilities for locating game installations managed by Faugus Launcher.

Faugus stores its game list as a JSON array in games.json under its data
dir and app settings in config.json under its config dir. It ships as a
Flatpak (io.github.Faugus.faugus-launcher), an AppImage or a native
package; the non-Flatpak flavors share the XDG locations.

Prefixes are umu-managed: WINEPREFIX is the per-game ``prefix`` path
itself (drive_c at the root, umu adds a ``pfx -> .`` self-symlink on
first run and creates the ``steamuser`` account), so the existing Proton
machinery handles them. There is no marker file inside the prefix -
identity comes from games.json membership or the configured default
prefix directory. Proton runners referenced by name live in Steam's
compatibilitytools.d (Faugus downloads them there itself).

No UI, no game-specific knowledge.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import NamedTuple

from Utils.lutris_finder import (
    _game_root_from_exe,
    _split_exe_rel_parts,
    _stored_exe_matches,
)

_HOME = Path.home()
_XDG_DATA = Path(os.environ.get("XDG_DATA_HOME", _HOME / ".local" / "share"))
_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", _HOME / ".config"))

FAUGUS_FLATPAK_ID = "io.github.Faugus.faugus-launcher"
_FLATPAK_APP = _HOME / ".var" / "app" / FAUGUS_FLATPAK_ID

# Runner values that don't name a Proton build in compatibilitytools.d:
# "Steam" delegates to the Steam client, "Linux-Native" runs the ELF
# directly, "umu-sniper" is umu's Linux runtime sentinel, and the (System)
# CachyOS build lives outside the roots list_installed_proton() scans.
_NON_PROTON_RUNNERS = ("steam", "linux-native", "umu-sniper",
                      "proton-cachyos (system)")


class FaugusRoot(NamedTuple):
    """One Faugus installation's on-disk locations.

    ``data_dir`` holds games.json (and the umu-run copy Faugus downloads);
    ``config_dir`` holds config.json with default-prefix / default-runner.
    """
    data_dir: Path
    config_dir: Path
    is_flatpak: bool


# ---------------------------------------------------------------------------
# Faugus root candidates
# ---------------------------------------------------------------------------

def _faugus_root_candidates() -> list[FaugusRoot]:
    """All possible Faugus roots, ordered by likelihood."""
    candidates: list[FaugusRoot] = []

    # User-configured data path takes highest priority
    try:
        from Utils.ui_config import load_faugus_data_path
        custom = load_faugus_data_path()
        if custom:
            p = Path(custom)
            candidates.append(FaugusRoot(p, p, is_flatpak=False))
    except Exception:
        pass

    candidates += [
        # Native / AppImage - respects XDG_DATA_HOME / XDG_CONFIG_HOME
        FaugusRoot(_XDG_DATA / "faugus-launcher",
                   _XDG_CONFIG / "faugus-launcher", is_flatpak=False),
        FaugusRoot(_HOME / ".local" / "share" / "faugus-launcher",
                   _HOME / ".config" / "faugus-launcher", is_flatpak=False),
        # Flatpak
        FaugusRoot(_FLATPAK_APP / "data" / "faugus-launcher",
                   _FLATPAK_APP / "config" / "faugus-launcher",
                   is_flatpak=True),
    ]
    return candidates


def _faugus_installed() -> bool:
    """True if a Faugus install is detectable (binary on PATH or flatpak dir)."""
    if shutil.which("faugus-launcher"):
        return True
    return _FLATPAK_APP.is_dir()


_faugus_missing_data_logged = False


def _maybe_log_faugus_data_missing() -> None:
    """Log once when Faugus seems installed but no data dir was found."""
    global _faugus_missing_data_logged
    if _faugus_missing_data_logged:
        return
    if not _faugus_installed():
        return
    _faugus_missing_data_logged = True
    try:
        from Utils.app_log import app_log
        app_log(
            "Faugus Launcher appears to be installed but no Faugus data "
            "directory was located - set a custom Faugus data path in the "
            "app's settings if Faugus-managed games aren't detected"
        )
    except Exception:
        pass


def find_faugus_roots() -> list[FaugusRoot]:
    """Return all Faugus roots whose data dir exists on disk.

    Presence of the data dir (not games.json) is the signal: Faugus only
    writes games.json when the first game is added, but the data dir exists
    from first launch.
    """
    seen: set[Path] = set()
    out: list[FaugusRoot] = []
    for root in _faugus_root_candidates():
        if root.data_dir not in seen and root.data_dir.is_dir():
            seen.add(root.data_dir)
            out.append(root)
    if not out:
        _maybe_log_faugus_data_missing()
    return out


# ---------------------------------------------------------------------------
# games.json / config.json access
# ---------------------------------------------------------------------------

def _load_games(root: FaugusRoot) -> list[dict]:
    """Game entries from a root's games.json ([] on absence or bad JSON)."""
    try:
        data = json.loads((root.data_dir / "games.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("games", [])
    if not isinstance(data, list):
        return []
    return [g for g in data if isinstance(g, dict)]


def _load_config(root: FaugusRoot) -> dict:
    """Parsed config.json for a root ({} on absence or bad JSON)."""
    try:
        data = json.loads((root.config_dir / "config.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _game_exe(entry: dict) -> Path | None:
    """Absolute exe path from a game entry's ``path`` field."""
    raw = str(entry.get("path", "") or "")
    if not raw:
        return None
    return Path(os.path.expanduser(raw))


def _game_prefix(entry: dict) -> Path | None:
    """Prefix path from a game entry's ``prefix`` field (may not exist yet)."""
    raw = str(entry.get("prefix", "") or "")
    if not raw:
        return None
    return Path(os.path.expanduser(raw))


def _iter_games() -> "list[tuple[FaugusRoot, dict]]":
    """(root, entry) for every game across all Faugus roots."""
    out: list[tuple[FaugusRoot, dict]] = []
    for root in find_faugus_roots():
        for entry in _load_games(root):
            out.append((root, entry))
    return out


# ---------------------------------------------------------------------------
# One-pass installed index (for the Add Game picker's "Show only installed")
# ---------------------------------------------------------------------------

def build_installed_exe_index() -> list[list[str]]:
    """Read every Faugus game once and return, per game, the lowercase path
    segments of its configured exe (mirrors the Lutris/Heroic one-pass
    indexes in ``installed_scan``)."""
    out: list[list[str]] = []
    for _root, entry in _iter_games():
        exe = _game_exe(entry)
        if exe is not None:
            parts = [p.lower() for p in str(exe).replace("\\", "/").split("/") if p]
            if parts:
                out.append(parts)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_faugus_game_info_by_exe(exe_name: str) -> "tuple[Path, Path | None, str] | None":
    """Faugus detection keyed by the handler's executable name.

    Matches *exe_name* against each game's configured ``path`` (tail-segment
    match, case-insensitive). Returns (install_path, prefix_path | None,
    gameid), or None. The prefix is only returned once it exists on disk -
    Faugus records it at add time but umu creates it on first run.
    """
    rel_parts = _split_exe_rel_parts(exe_name)
    if not rel_parts:
        return None

    for _root, entry in _iter_games():
        exe = _game_exe(entry)
        if exe is None or not _stored_exe_matches(str(exe), rel_parts):
            continue
        install_path = _game_root_from_exe(exe, exe_name)
        if install_path is None:
            continue
        prefix = _game_prefix(entry)
        if prefix is not None and not prefix.is_dir():
            prefix = None
        return (install_path, prefix, str(entry.get("gameid", "") or ""))

    return None


def find_faugus_gameids_by_exes(exe_names) -> list[str]:
    """Gameids of every Faugus game matching any of *exe_names*, in ONE pass
    over games.json (the Play flow probes exe_name plus every alt)."""
    prepared = [p for p in (_split_exe_rel_parts(e) for e in exe_names if e) if p]
    if not prepared:
        return []
    gameids: list[str] = []
    for _root, entry in _iter_games():
        gameid = str(entry.get("gameid", "") or "")
        if not gameid or gameid in gameids:
            continue
        exe = _game_exe(entry)
        if exe is None:
            continue
        if any(_stored_exe_matches(str(exe), rel) for rel in prepared):
            gameids.append(gameid)
    return gameids


def find_faugus_launch_info(gameids: list) -> "tuple[str, bool] | None":
    """(gameid, faugus_is_flatpak) for the first game matching any of
    *gameids* - used to build a ``faugus-launcher --game <id>`` launch."""
    wanted = {str(g).lower() for g in gameids if g}
    if not wanted:
        return None
    for root in find_faugus_roots():
        for entry in _load_games(root):
            gameid = str(entry.get("gameid", "") or "")
            if gameid.lower() in wanted:
                return (gameid, root.is_flatpak)
    return None


def _game_for_prefix(prefix_path: "str | Path") -> "tuple[FaugusRoot, dict] | None":
    """Reverse lookup: the (root, entry) whose prefix matches *prefix_path*."""
    target = Path(prefix_path)
    try:
        target_resolved = target.resolve()
    except OSError:
        target_resolved = target
    for root, entry in _iter_games():
        prefix = _game_prefix(entry)
        if prefix is None:
            continue
        try:
            same = prefix.resolve() == target_resolved
        except OSError:
            same = prefix == target
        if same or prefix == target:
            return (root, entry)
    return None


def is_faugus_prefix(path: "str | Path") -> bool:
    """True when *path* is a Faugus-managed Wine prefix.

    Faugus writes no marker file into its prefixes, so identity comes from
    games.json (a game's recorded prefix path) or from living at/under a
    root's configured default-prefix directory. Cheap False when no Faugus
    install exists.
    """
    roots = find_faugus_roots()
    if not roots:
        return False
    p = Path(path)
    try:
        p_resolved = p.resolve()
    except OSError:
        p_resolved = p
    if _game_for_prefix(p) is not None:
        return True
    for root in roots:
        raw = str(_load_config(root).get("default-prefix", "") or "")
        if not raw:
            continue
        base = Path(os.path.expanduser(raw))
        try:
            base = base.resolve()
        except OSError:
            pass
        if p_resolved == base or base in p_resolved.parents:
            return True
    return False


# ---------------------------------------------------------------------------
# Proton runner resolution
# ---------------------------------------------------------------------------

def _runner_for_prefix(prefix_path: "str | Path") -> str:
    """The configured runner name for a prefix ('' when unknown), with the
    per-game empty value resolved through the root's default-runner."""
    hit = _game_for_prefix(prefix_path)
    if hit is None:
        return ""
    root, entry = hit
    runner = str(entry.get("runner", "") or "")
    if not runner:
        runner = str(_load_config(root).get("default-runner", "") or "")
    return runner


def find_faugus_proton_name_for_prefix(prefix_path: "str | Path") -> str | None:
    """Proton runner name Faugus has configured for *prefix_path*.

    Returns None when the runner isn't a Proton build we can resolve
    ("Steam", "Linux-Native", the sniper runtime, the system CachyOS build,
    or no runner at all) - callers fall back to config_info / newest
    installed Proton in that case.
    """
    runner = _runner_for_prefix(prefix_path)
    if not runner or runner.strip().lower() in _NON_PROTON_RUNNERS:
        return None
    return runner


def _match_latest_runner(name: str, installed_names: list) -> str | None:
    """Resolve a '<family> Latest' runner name against installed Proton dirs.

    Pure string matching: strips the ' Latest' suffix, alnum-normalizes, and
    also tries the reversed two-word spelling ('Proton-GE' vs 'GE-Proton' -
    Faugus renamed its families at some point). *installed_names* is expected
    newest-first (list_installed_proton order); first prefix-match wins.
    """
    base = name[:-len(" Latest")] if name.lower().endswith(" latest") else name

    def norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    families = {norm(base)}
    parts = [p for p in base.replace("_", "-").split("-") if p]
    if len(parts) == 2:
        families.add(norm(parts[1] + parts[0]))
    families.discard("")
    if not families:
        return None
    for cand in installed_names:
        cn = norm(str(cand))
        if any(cn.startswith(f) for f in families):
            return str(cand)
    return None


def find_faugus_proton_for_prefix(prefix_path: "str | Path") -> Path | None:
    """Proton script for the runner Faugus has configured for *prefix_path*.

    Exact (normalized) directory-name match first - the '<family> Latest'
    names are literal directory names Faugus creates in compatibilitytools.d
    - then a family fallback so an older Faugus spelling still finds the
    newest installed build of that family. None when nothing matches (the
    callers' own fallback chain continues).
    """
    name = find_faugus_proton_name_for_prefix(prefix_path)
    if not name:
        return None
    try:
        from Utils.steam_finder import list_installed_proton, _normalize_tool_name
        candidates = list_installed_proton()
    except Exception:
        return None
    norm = _normalize_tool_name(name)
    for cand in candidates:
        if _normalize_tool_name(cand.parent.name) == norm:
            return cand
    hit = _match_latest_runner(name, [c.parent.name for c in candidates])
    if hit:
        for cand in candidates:
            if cand.parent.name == hit:
                return cand
    return None
