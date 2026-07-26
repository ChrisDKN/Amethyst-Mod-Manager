"""
lutris_finder.py
Utilities for locating game installations managed by Lutris.

Lutris stores its game list in a sqlite database (pga.db) and per-game
settings in small YAML files. It can be installed as a Flatpak
(net.lutris.Lutris) or natively; both keep the same layout under their
respective data dirs.

Two prefix flavors exist in the wild:
  * umu/Proton-made (modern default): Heroic-shaped — ``pfx -> .`` symlink,
    ``config_info`` (after first run), ``lutris.json`` marker, and a
    ``steamuser`` account. The existing Proton machinery handles these.
  * classic lutris-wine: a plain WINEPREFIX run with a runner from
    ``<data>/runners/wine/<version>/bin/wine`` and no steamuser.

No UI, no game-specific knowledge.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import NamedTuple

_HOME = Path.home()
_XDG_DATA = Path(os.environ.get("XDG_DATA_HOME", _HOME / ".local" / "share"))
_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", _HOME / ".config"))
_FLATPAK_APP = _HOME / ".var" / "app" / "net.lutris.Lutris"

_PROTON_VERBS = ("run", "runinprefix", "waitforexitandrun")


class LutrisRoot(NamedTuple):
    """One Lutris installation's on-disk locations.

    ``data_dir`` holds pga.db and runners/wine/; ``config_dir`` holds
    games/*.yml and runners/wine.yml. Lutris falls back to the data dir for
    config when ``~/.config/lutris`` doesn't exist (the common case for new
    installs), so the two are often the same directory.
    """
    data_dir: Path
    config_dir: Path
    is_flatpak: bool


# ---------------------------------------------------------------------------
# Lutris root candidates
# ---------------------------------------------------------------------------

def _root_from_dirs(data_dir: Path, config_dir: Path, is_flatpak: bool) -> LutrisRoot:
    return LutrisRoot(
        data_dir=data_dir,
        config_dir=config_dir if config_dir.is_dir() else data_dir,
        is_flatpak=is_flatpak,
    )


def _lutris_root_candidates() -> list[LutrisRoot]:
    """All possible Lutris roots, ordered by likelihood."""
    candidates: list[LutrisRoot] = []

    # User-configured data path takes highest priority
    try:
        from Utils.ui_config import load_lutris_data_path
        custom = load_lutris_data_path()
        if custom:
            p = Path(custom)
            candidates.append(_root_from_dirs(p, p, is_flatpak=False))
    except Exception:
        pass

    candidates += [
        # Native / AppImage — respects XDG_DATA_HOME / XDG_CONFIG_HOME
        _root_from_dirs(_XDG_DATA / "lutris", _XDG_CONFIG / "lutris",
                        is_flatpak=False),
        _root_from_dirs(_HOME / ".local" / "share" / "lutris",
                        _HOME / ".config" / "lutris", is_flatpak=False),
        # Flatpak
        _root_from_dirs(_FLATPAK_APP / "data" / "lutris",
                        _FLATPAK_APP / "config" / "lutris", is_flatpak=True),
    ]
    return candidates


def _lutris_installed() -> bool:
    """True if a Lutris install is detectable (binary on PATH or flatpak dir).

    Used to distinguish "Lutris not installed" from "Lutris installed but we
    can't find its data dir", so we can surface a useful hint.
    """
    if shutil.which("lutris"):
        return True
    return _FLATPAK_APP.is_dir()


_lutris_missing_data_logged = False


def _maybe_log_lutris_data_missing() -> None:
    """Log once when Lutris seems installed but no data dir was found."""
    global _lutris_missing_data_logged
    if _lutris_missing_data_logged:
        return
    if not _lutris_installed():
        return
    _lutris_missing_data_logged = True
    try:
        from Utils.app_log import app_log
        app_log(
            "Lutris appears to be installed but no Lutris data directory was "
            "located — set a custom Lutris data path in the app's settings "
            "if Lutris-managed games aren't detected"
        )
    except Exception:
        pass


def find_lutris_roots() -> list[LutrisRoot]:
    """Return all Lutris roots whose data dir exists on disk."""
    seen: set[Path] = set()
    out: list[LutrisRoot] = []
    for root in _lutris_root_candidates():
        if root.data_dir not in seen and root.data_dir.is_dir():
            seen.add(root.data_dir)
            out.append(root)
    if not out:
        _maybe_log_lutris_data_missing()
    return out


# ---------------------------------------------------------------------------
# Minimal YAML subset parser
#
# Lutris writes configs with yaml.safe_dump(default_flow_style=False): block
# mappings, plain/quoted scalar values, "{}" for empty maps. That subset is
# small enough to parse without a YAML dependency. Lists (only used for keys
# we never read) are skipped.
# ---------------------------------------------------------------------------

_YAML_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "0": "\0", "a": "\a", "b": "\b",
    "f": "\f", "v": "\v", "e": "\x1b", "N": "\x85", "_": "\xa0",
    # YAML \L/\P (line/paragraph separator), written as \u escapes so no
    # literal LS/PS characters sit in the source (editors flag those).
    "L": "\u2028", "P": "\u2029",
}
_YAML_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}  # escape char → hex digit count


def _yaml_unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if len(v) >= 2 and v[0] == v[-1] == '"':
        # Manual unescape of the sequences safe_dump emits; unicode_escape
        # would corrupt non-ASCII text.
        out: list[str] = []
        s = v[1:-1]
        i = 0
        while i < len(s):
            if s[i] == "\\" and i + 1 < len(s):
                esc = s[i + 1]
                ndigits = _YAML_HEX_ESCAPES.get(esc, 0)
                if ndigits and i + 2 + ndigits <= len(s):
                    try:
                        out.append(chr(int(s[i + 2:i + 2 + ndigits], 16)))
                        i += 2 + ndigits
                        continue
                    except ValueError:
                        pass
                # Named escape, or \\ and \" via the identity fallback.
                out.append(_YAML_ESCAPES.get(esc, esc))
                i += 2
            else:
                out.append(s[i])
                i += 1
        return "".join(out)
    if v in ("null", "~"):
        return ""
    return v


def parse_lutris_yaml(text: str) -> dict:
    """Parse the yaml.safe_dump block-mapping subset Lutris writes.

    Nested dicts of any depth; scalar leaves kept as strings (booleans and
    numbers are not coerced — callers only read path/name strings). List
    items and comments are skipped.
    """
    root: dict = {}
    # (indent, dict) — innermost open mapping last
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("- "):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        key, sep, val = stripped.partition(":")
        if not sep:
            continue
        key = _yaml_unquote(key)
        val = val.strip()
        parent = stack[-1][1]
        if val == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        elif val == "{}":
            parent[key] = {}
        else:
            parent[key] = _yaml_unquote(val)
    return root


# ---------------------------------------------------------------------------
# pga.db / game config access
# ---------------------------------------------------------------------------

def _read_installed_games(root: LutrisRoot) -> list[dict]:
    """Rows from pga.db for installed games, as plain dicts.

    Opened with a read-only URI so a running Lutris is never blocked. Falls
    back to scanning games/*.yml directly when the database is unreadable
    (slug = filename stem minus the trailing "-<epoch>" Lutris appends).
    """
    db = root.data_dir / "pga.db"
    if db.is_file():
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                con.row_factory = sqlite3.Row
                cur = con.execute(
                    "SELECT id, name, slug, runner, directory, configpath "
                    "FROM games WHERE installed = 1")
                return [dict(r) for r in cur.fetchall()]
            finally:
                con.close()
        except Exception:
            pass
    return _rows_from_yml_scan(root)


def _rows_from_yml_scan(root: LutrisRoot) -> list[dict]:
    games_dir = root.config_dir / "games"
    if not games_dir.is_dir():
        return []
    rows: list[dict] = []
    try:
        yml_files = sorted(games_dir.glob("*.yml"))
    except OSError:
        return []
    for f in yml_files:
        slug = re.sub(r"-\d+$", "", f.stem)
        rows.append({
            "id": None, "name": slug, "slug": slug, "runner": "",
            "directory": "", "configpath": f.stem,
        })
    return rows


def _load_game_yml(root: LutrisRoot, configpath: str) -> dict:
    if not configpath:
        return {}
    yml = root.config_dir / "games" / f"{configpath}.yml"
    if not yml.is_file():
        return {}
    try:
        return parse_lutris_yaml(yml.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def _runner_wine_version(root: LutrisRoot) -> str:
    """Runner-level default wine version from runners/wine.yml, or ''."""
    yml = root.config_dir / "runners" / "wine.yml"
    if not yml.is_file():
        return ""
    try:
        data = parse_lutris_yaml(yml.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    wine = data.get("wine")
    if isinstance(wine, dict):
        return str(wine.get("version", "") or "")
    return ""


# ---------------------------------------------------------------------------
# Per-game path resolution
# ---------------------------------------------------------------------------

def _walk_up_for_prefix(path: Path) -> Path | None:
    """Port of Lutris's find_prefix(): ascend from an exe path looking for a
    Wine prefix (drive_c/ + user.reg), also probing prefix/ and pfx/ children.
    """
    def _is_pfx(d: Path) -> bool:
        return (d / "drive_c").is_dir() and (d / "user.reg").is_file()

    try:
        cur = path.expanduser()
    except RuntimeError:
        cur = path
    for _ in range(32):
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
        if _is_pfx(cur):
            return cur
        for child in ("prefix", "pfx", "Prefix"):
            cand = cur / child
            if _is_pfx(cand):
                return cand
    return None


def _resolve_game_exe(root: LutrisRoot, row: dict, yml: dict) -> Path | None:
    """Absolute exe path from the game yml (relative paths resolve against
    the DB directory column, matching Lutris's game_exe behaviour)."""
    gsec = yml.get("game")
    if not isinstance(gsec, dict):
        return None
    exe = str(gsec.get("exe", "") or "")
    if not exe:
        return None
    p = Path(exe).expanduser()
    if not p.is_absolute():
        directory = str(row.get("directory", "") or "")
        if not directory:
            return None
        p = Path(directory).expanduser() / p
    return p


def _resolve_game_prefix(row: dict, yml: dict, exe: Path | None) -> Path | None:
    """Prefix from the yml, else walk-up from the exe (Lutris's own order)."""
    gsec = yml.get("game")
    if isinstance(gsec, dict):
        prefix = str(gsec.get("prefix", "") or "")
        if prefix:
            p = Path(prefix).expanduser()
            if p.is_dir():
                return p
    if exe is not None:
        return _walk_up_for_prefix(exe)
    return None


def _game_root_from_exe(exe: Path, exe_name: str) -> Path | None:
    """Game root from a matched exe: strip the handler's relative exe path
    (e.g. 'bin/x64/Game.exe') off the tail so multi-segment handlers return
    the true root, not the exe's folder."""
    rel = exe_name.replace("\\", "/").strip("/")
    rel_parts = [p for p in rel.split("/") if p]
    if len(rel_parts) > 1 and len(exe.parts) >= len(rel_parts):
        tail = [p.lower() for p in exe.parts[-len(rel_parts):]]
        if tail == [p.lower() for p in rel_parts]:
            game_root = Path(*exe.parts[:-len(rel_parts)])
            if game_root.is_dir():
                return game_root
    if exe.parent.is_dir():
        return exe.parent
    return None


def _stored_exe_matches(stored_exe: str, rel_parts: list[str]) -> bool:
    """Case-insensitive tail match of a stored exe path against the handler's
    exe name segments (same rules as heroic_finder._stored_exe_matches)."""
    if not stored_exe or not rel_parts:
        return False
    stored_parts = [p.lower() for p in stored_exe.replace("\\", "/").split("/") if p]
    if not stored_parts:
        return False
    if len(rel_parts) > 1:
        if len(stored_parts) < len(rel_parts):
            return False
        return stored_parts[-len(rel_parts):] == rel_parts
    return stored_parts[-1] == rel_parts[0]


def _iter_games() -> "list[tuple[LutrisRoot, dict, dict]]":
    """(root, row, yml) for every installed Lutris game across all roots."""
    out: list[tuple[LutrisRoot, dict, dict]] = []
    for root in find_lutris_roots():
        for row in _read_installed_games(root):
            out.append((root, row, _load_game_yml(root, str(row.get("configpath", "")))))
    return out


# ---------------------------------------------------------------------------
# One-pass installed index (for the Add Game picker's "Show only installed")
# ---------------------------------------------------------------------------

def build_installed_exe_index() -> list[list[str]]:
    """Read every installed Lutris game once and return, per game, the
    lowercase path segments of its configured ``game.exe``.

    The Add Game picker probes ~60-100 handlers against the installed set;
    calling :func:`find_lutris_game_info_by_exe` per handler would re-read the
    database and re-parse YAML each time. This walks Lutris's data a single
    time so the picker's index can answer membership from memory (mirrors the
    one-pass Steam/Heroic indexes in ``installed_scan``). Games with a
    ``directory`` column but no configured exe are still directory-scanned
    lazily at query time — those are rare and only pay when actually probed.
    """
    out: list[list[str]] = []
    for root, row, yml in _iter_games():
        exe = _resolve_game_exe(root, row, yml)
        if exe is not None:
            parts = [p.lower() for p in str(exe).replace("\\", "/").split("/") if p]
            if parts:
                out.append(parts)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_lutris_prefix(path: "str | Path") -> bool:
    """True when *path* looks like a Lutris-managed Wine prefix.

    Primary signal is the ``lutris.json`` marker Lutris writes into every
    prefix it creates. Fallback: a plain WINEPREFIX layout (drive_c/ +
    user.reg at root) that is neither a Steam compatdata pfx (config_info in
    the parent) nor a Heroic/umu compat dir (config_info or a pfx entry at
    the root).
    """
    p = Path(path)
    if (p / "lutris.json").is_file():
        return True
    if not ((p / "drive_c").is_dir() and (p / "user.reg").is_file()):
        return False
    if (p / "config_info").is_file() or (p.parent / "config_info").is_file():
        return False
    if (p / "pfx").exists() or (p / "pfx").is_symlink():
        return False
    return True


def _split_exe_rel_parts(exe_name: str) -> list[str]:
    """Lowercase path segments of an exe name ('bin/x64/Game.exe' style)."""
    rel = str(exe_name).replace("\\", "/").strip("/")
    return [p.lower() for p in rel.split("/") if p]


def _match_exe_for_row(root: LutrisRoot, row: dict, yml: dict,
                       prepared: "list[list[str]]") -> "Path | None":
    """First exe of an installed game matching any pre-split name in
    *prepared* (tail-segment match, case-insensitive); games whose yml lacks
    an exe but whose DB ``directory`` exists are probed by scanning that
    directory (like Heroic's GOG entries with an empty executable field)."""
    exe = _resolve_game_exe(root, row, yml)
    if exe is not None:
        for rel_parts in prepared:
            if _stored_exe_matches(str(exe), rel_parts):
                return exe
        return None
    directory = str(row.get("directory", "") or "")
    if not directory:
        return None
    install_path = Path(directory).expanduser()
    if not install_path.is_dir():
        return None
    wanted = {rel_parts[-1] for rel_parts in prepared}
    try:
        for candidate in install_path.rglob("*"):
            if not candidate.is_file() or candidate.name.lower() not in wanted:
                continue
            cand_parts = [p.lower() for p in candidate.parts]
            for rel_parts in prepared:
                if candidate.name.lower() != rel_parts[-1]:
                    continue
                if (len(rel_parts) == 1
                        or cand_parts[-len(rel_parts):] == rel_parts):
                    return candidate
    except OSError:
        pass
    return None


def find_lutris_game_info_by_exe(exe_name: str) -> "tuple[Path, Path | None, str] | None":
    """Full Lutris detection workflow keyed by the handler's executable name.

    Matches *exe_name* against each installed game's configured ``game.exe``
    (tail-segment match, case-insensitive); games whose yml lacks an exe but
    whose DB ``directory`` exists are probed by scanning that directory.

    Returns (install_path, prefix_path | None, slug), or None. The install
    path is derived from the exe location — Lutris frequently leaves the DB
    ``directory`` column empty.
    """
    rel_parts = _split_exe_rel_parts(exe_name)
    if not rel_parts:
        return None

    for root, row, yml in _iter_games():
        matched_exe = _match_exe_for_row(root, row, yml, [rel_parts])
        if matched_exe is None:
            continue
        install_path = _game_root_from_exe(matched_exe, exe_name)
        if install_path is None:
            continue
        prefix_path = _resolve_game_prefix(row, yml, matched_exe)
        return (install_path, prefix_path, str(row.get("slug", "") or ""))

    return None


def find_lutris_slug_by_exe(exe_name: str) -> str | None:
    """Slug of the installed Lutris game whose exe matches *exe_name*."""
    info = find_lutris_game_info_by_exe(exe_name)
    return info[2] if info else None


def find_lutris_slugs_by_exes(exe_names) -> list[str]:
    """Slugs of every installed Lutris game matching any of *exe_names*, in
    ONE pass over the installed-games data.

    The Play flow probes the game's exe_name plus every exe_name_alt; calling
    :func:`find_lutris_slug_by_exe` per name re-reads the database and
    re-parses every game's YAML once per alt (N full scans per launch click).
    """
    prepared = [p for p in (_split_exe_rel_parts(e) for e in exe_names if e) if p]
    if not prepared:
        return []
    slugs: list[str] = []
    for root, row, yml in _iter_games():
        slug = str(row.get("slug", "") or "")
        if not slug or slug in slugs:
            continue
        if _match_exe_for_row(root, row, yml, prepared) is not None:
            slugs.append(slug)
    return slugs


def find_lutris_prefix(slugs: list[str]) -> Path | None:
    """Wine prefix of the first installed game matching any of *slugs*."""
    slugs_lower = {s.lower() for s in slugs if s}
    if not slugs_lower:
        return None
    for root, row, yml in _iter_games():
        if str(row.get("slug", "") or "").lower() not in slugs_lower:
            continue
        exe = _resolve_game_exe(root, row, yml)
        prefix = _resolve_game_prefix(row, yml, exe)
        if prefix is not None:
            return prefix
    return None


def find_lutris_launch_info(slugs: list[str]) -> "tuple[str, bool] | None":
    """(slug, lutris_is_flatpak) for the first installed game matching any
    of *slugs* — used to build a ``lutris:rungame/<slug>`` launch."""
    slugs_lower = {s.lower() for s in slugs if s}
    if not slugs_lower:
        return None
    for root in find_lutris_roots():
        for row in _read_installed_games(root):
            slug = str(row.get("slug", "") or "")
            if slug.lower() in slugs_lower:
                return (slug, root.is_flatpak)
    return None


# ---------------------------------------------------------------------------
# Wine runner resolution (classic lutris-wine prefixes)
# ---------------------------------------------------------------------------

def _is_protonish(version: str) -> bool:
    """True for wine version strings that denote Proton/umu-managed runners
    ('GE-Proton10-34', 'Proton - Experimental', the 'ge-proton' sentinel, …).
    Classic wine-GE builds are named 'lutris-GE-Proton8-26' — the leading
    'lutris-' keeps them out of this check."""
    v = (version or "").strip().lower()
    return v.startswith(("proton", "ge-proton")) or "umu" in v


def _wine_binary_for_version(root: LutrisRoot, version: str) -> Path | None:
    """Wine binary for a lutris-wine version string, with fallback to the
    newest installed runner.

    A version present under runners/wine/ is a wine build whatever its name,
    so the disk probe comes first; only then are Proton/umu-style names ruled
    out (those live in Steam's compatibilitytools.d, handled by the Proton
    machinery). Older Lutris releases stored runner dirs without the arch
    suffix the version string carries (and vice versa), so both spellings are
    probed."""
    wine_dir = root.data_dir / "runners" / "wine"
    if version:
        for name in (version, f"{version}-x86_64", f"{version}-i386"):
            cand = wine_dir / name / "bin" / "wine"
            if cand.is_file():
                return cand
    if _is_protonish(version):
        return None
    try:
        dirs = sorted(
            (d for d in wine_dir.iterdir() if (d / "bin" / "wine").is_file()),
            key=lambda d: [int(n) for n in re.findall(r"\d+", d.name)] or [0],
            reverse=True,
        )
    except OSError:
        return None
    return dirs[0] / "bin" / "wine" if dirs else None


def _game_for_prefix(prefix_path: "str | Path") -> "tuple[LutrisRoot, dict, dict] | None":
    """Reverse lookup: the (root, row, yml) whose resolved prefix matches."""
    target = Path(prefix_path)
    try:
        target_resolved = target.resolve()
    except OSError:
        target_resolved = target
    for root, row, yml in _iter_games():
        exe = _resolve_game_exe(root, row, yml)
        prefix = _resolve_game_prefix(row, yml, exe)
        if prefix is None:
            continue
        try:
            same = prefix.resolve() == target_resolved
        except OSError:
            same = prefix == target
        if same or prefix == target:
            return (root, row, yml)
    return None


def _wine_version_for_game(root: LutrisRoot, yml: dict) -> str:
    wine = yml.get("wine")
    if isinstance(wine, dict):
        version = str(wine.get("version", "") or "")
        if version:
            return version
    return _runner_wine_version(root)


def find_lutris_wine_for_prefix(prefix_path: "str | Path") -> Path | None:
    """Wine binary of the lutris-wine runner configured for *prefix_path*.

    Returns None when the game's runner is Proton/umu-managed (the modern
    default) — callers fall back to the Proton machinery in that case.
    """
    hit = _game_for_prefix(prefix_path)
    if hit is None:
        return None
    root, _row, yml = hit
    version = _wine_version_for_game(root, yml)
    return _wine_binary_for_version(root, version)


def find_lutris_proton_name_for_prefix(prefix_path: "str | Path") -> str | None:
    """Proton runner name for a Lutris prefix, for find_any_installed_proton().

    Prefers the prefix's own ``config_info`` (written by Proton on first run);
    for fresh prefixes that lack it, falls back to the game yml's
    ``wine.version``. Returns None when the configured runner isn't
    Proton-style.
    """
    p = Path(prefix_path)
    try:
        first = (p / "config_info").read_text(encoding="utf-8").splitlines()[0].strip()
        if first:
            return first
    except (OSError, IndexError):
        pass
    hit = _game_for_prefix(p)
    if hit is None:
        return None
    root, _row, yml = hit
    version = _wine_version_for_game(root, yml)
    if version and _is_protonish(version):
        return version
    return None


def find_lutris_wine_bin_dir_for_prefix(prefix_path: "str | Path") -> Path | None:
    """bin/ directory of the lutris-wine runner for *prefix_path* (winetricks
    PATH patching). None for Proton/umu-managed games."""
    wine_bin = find_lutris_wine_for_prefix(prefix_path)
    return wine_bin.parent if wine_bin is not None else None


def lutris_wine_env(wine_bin: Path, prefix_path: "str | Path",
                    arch: str = "") -> dict:
    """Env additions for running a lutris-wine binary against *prefix_path*:
    WINEPREFIX plus the runner's own lib dirs on LD_LIBRARY_PATH (Lutris
    injects those itself when launching)."""
    env = {"WINEPREFIX": str(prefix_path)}
    runner_root = wine_bin.parent.parent
    lib_dirs = [str(d) for d in (runner_root / "lib64", runner_root / "lib")
                if d.is_dir()]
    if lib_dirs:
        existing = os.environ.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = os.pathsep.join(
            lib_dirs + ([existing] if existing else []))
    if arch in ("win32", "win64"):
        env["WINEARCH"] = arch
    return env


def find_umu_run() -> Path | None:
    """Locate a usable ``umu-run`` launcher.

    umu is how Lutris itself runs Proton games outside Steam: it starts
    Proton inside the Steam Linux Runtime container (pressure-vessel) with
    its own compat plumbing, so no Steam client attach happens — the game
    doesn't need to be owned on Steam and doesn't show as "running" there —
    and the audio/library environment matches a real Steam launch.

    Preference: a system install on PATH (umu-launcher package), then the
    copy Lutris downloads into its runtime (native install, then Flatpak
    Lutris's data dir), then Heroic's copy (modern Heroic launches Proton
    games through umu too). The launcher copies are self-contained zipapps
    with a ``python3`` shebang, so they run fine outside their launcher.
    """
    cand = shutil.which("umu-run")
    if cand:
        return Path(cand)
    # Probe XDG_DATA_HOME and the literal home path separately: inside a
    # sandbox XDG_DATA_HOME points at the sandbox's own data dir, not the
    # host's ~/.local/share (same pattern as _lutris_root_candidates).
    for data_dir in (_XDG_DATA, _HOME / ".local" / "share",
                     _FLATPAK_APP / "data"):
        p = data_dir / "lutris" / "runtime" / "umu" / "umu-run"
        if p.is_file():
            return p
    # Heroic: a downloaded copy under its tools dir, or the build bundled
    # next to legendary/gogdl (resources/app.asar.unpacked/build/bin/...;
    # globbed since the arch/platform nesting has moved between releases).
    heroic_flatpak = _HOME / ".var" / "app" / "com.heroicgameslauncher.hgl"
    for config_dir in (_XDG_CONFIG, _HOME / ".config",
                       heroic_flatpak / "config"):
        p = config_dir / "heroic" / "tools" / "umu" / "umu-run"
        if p.is_file():
            return p
    for app_root in (
        Path("/opt/Heroic"),
        Path("/var/lib/flatpak/app/com.heroicgameslauncher.hgl/current"
             "/active/files/heroic"),
        _HOME / ".local" / "share" / "flatpak" / "app"
        / "com.heroicgameslauncher.hgl" / "current" / "active" / "files"
        / "heroic",
    ):
        base = app_root / "resources" / "app.asar.unpacked" / "build" / "bin"
        if base.is_dir():
            try:
                hit = next((h for h in base.glob("**/umu-run") if h.is_file()),
                           None)
            except OSError:
                hit = None
            if hit is not None:
                return hit
    return None


def umu_run_command(umu_bin: Path, *args: str,
                    env: "dict | None" = None,
                    host_cwd: "str | Path | None" = None) -> list[str]:
    """Build the command to invoke ``umu-run <args>``.

    The caller's env must carry WINEPREFIX/PROTONPATH (and optionally
    GAMEID); umu derives everything else itself. Inside our own Flatpak
    sandbox the launch is forwarded to the host via ``flatpak-spawn --host``
    (pressure-vessel can't nest inside a sandbox); flatpak-spawn doesn't
    forward the environment, so the env diff vs os.environ is re-exported
    with ``--env=`` flags — same pattern as steam_finder.proton_run_command.

    *host_cwd*, when given, becomes the host process's working directory
    under flatpak-spawn (``--directory=``). Callers whose Popen cwd is a
    host-valid path can omit it; callers running from a sandbox-only cwd
    (e.g. the Proton-tools plumbing) must pass one or the portal fails to
    chdir.
    """
    cmd = [str(umu_bin), *map(str, args)]
    if Path("/.flatpak-info").exists() and shutil.which("flatpak-spawn"):
        fwd = [
            f"--env={k}={v}"
            for k, v in (env or {}).items()
            if os.environ.get(k) != v
        ]
        dir_flag = [f"--directory={host_cwd}"] if host_cwd is not None else []
        cmd = ["flatpak-spawn", "--host", *dir_flag, *fwd, *cmd]
    return cmd


def ensure_steamuser_compat(prefix_path: "str | Path") -> None:
    """Symlink drive_c/users/steamuser to the real user dir in a classic
    lutris-wine prefix, so handler paths hardcoded to steamuser (Bethesda
    AppData/My Games, to_prefix deploy rules, …) resolve to the same files
    the game writes. No-op when steamuser already exists (umu/Proton-made
    prefixes create it themselves)."""
    users = Path(prefix_path) / "drive_c" / "users"
    su = users / "steamuser"
    try:
        if su.exists() or su.is_symlink() or not users.is_dir():
            return
        real = next(
            (d for d in sorted(users.iterdir())
             if d.is_dir() and d.name.lower() not in ("public", "root")),
            None,
        )
        if real is not None:
            # Relative link so the prefix survives being moved wholesale.
            su.symlink_to(real.name)
    except OSError:
        pass
