"""
heroic_finder.py
Utilities for locating game installations managed by Heroic Games Launcher.

Heroic supports Epic Games (via Legendary) and GOG (via heroic-gogdl).
It can be installed as a Flatpak (most common on Steam Deck) or natively.

No UI, no game-specific knowledge.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

_HOME = Path.home()
_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", _HOME / ".config"))

# ---------------------------------------------------------------------------
# Heroic config root candidates
# GamesConfig/<Appname>.json lives under each root; path varies by install type.
# ---------------------------------------------------------------------------
def _heroic_config_candidates() -> list[Path]:
    """All possible Heroic config roots, ordered by likelihood.

    Covers Flatpak, native/AppImage (XDG_CONFIG_HOME), Snap, and the common
    alternate directory name used by some distro packagers
    ('heroic-games-launcher' instead of 'heroic').
    """
    candidates: list[Path] = []

    # User-configured path takes highest priority
    try:
        from Utils.ui_config import load_heroic_config_path
        custom = load_heroic_config_path()
        if custom:
            candidates.append(Path(custom))
    except Exception:
        pass

    # Built-in fallbacks, ordered by likelihood
    candidates += [
        # Flatpak (most common on Steam Deck)
        _HOME / ".var" / "app" / "com.heroicgameslauncher.hgl" / "config" / "heroic",
        # Native / AppImage — respects XDG_CONFIG_HOME
        _XDG_CONFIG / "heroic",
        _HOME / ".config" / "heroic",  # Fallback if XDG_CONFIG was overridden
        # Alternate directory name used by some distro packages
        _XDG_CONFIG / "heroic-games-launcher",
        _HOME / ".config" / "heroic-games-launcher",
        # Snap
        _HOME / "snap" / "heroic" / "current" / ".config" / "heroic",
        _HOME / "snap" / "heroic-games-launcher" / "current" / ".config" / "heroic",
    ]
    return candidates


def _heroic_binary_exists() -> bool:
    """True if a `heroic` binary is reachable from the current PATH.

    Used to distinguish "Heroic not installed" from "Heroic installed but we
    can't find its config dir" (unusual install location, Nix store, etc.),
    so we can surface a useful hint instead of silently returning nothing.
    """
    for name in ("heroic", "heroic-games-launcher"):
        if shutil.which(name):
            return True
    return False


_heroic_missing_config_logged = False


def _maybe_log_heroic_config_missing() -> None:
    """Log once when the heroic binary is on PATH but no config dir was found.

    Points the user at the per-game 'Heroic config path' override rather than
    leaving them confused about why their installed games aren't detected.
    """
    global _heroic_missing_config_logged
    if _heroic_missing_config_logged:
        return
    if not _heroic_binary_exists():
        return
    _heroic_missing_config_logged = True
    try:
        from Utils.app_log import app_log
        app_log(
            "Heroic binary found on PATH but no Heroic config directory was "
            "located — set a custom Heroic config path in the app's settings "
            "if Heroic-managed games aren't detected"
        )
    except Exception:
        pass


def _find_heroic_config_roots() -> list[Path]:
    """Return all Heroic config directories that exist on disk."""
    seen: set[Path] = set()
    out: list[Path] = []
    for p in _heroic_config_candidates():
        if p not in seen and p.is_dir():
            seen.add(p)
            out.append(p)
    if not out:
        _maybe_log_heroic_config_missing()
    return out


# ---------------------------------------------------------------------------
# Epic Games (Legendary backend)
# ---------------------------------------------------------------------------

def _load_epic_installed(heroic_root: Path) -> dict:
    """
    Parse legendaryConfig/legendary/installed.json from a Heroic config root.
    Returns a dict keyed by appName, each value containing at least:
      install_path, title
    Returns an empty dict on any error.
    """
    installed_json = heroic_root / "legendaryConfig" / "legendary" / "installed.json"
    if not installed_json.is_file():
        return {}
    try:
        data = json.loads(installed_json.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _find_epic_game(heroic_root: Path, app_names: list[str]) -> Path | None:
    """
    Search Epic installed.json for any of the given appNames.
    Returns the install_path as a Path if found and the directory exists.
    """
    installed = _load_epic_installed(heroic_root)
    for app_name in app_names:
        entry = installed.get(app_name)
        if not entry:
            continue
        install_path = entry.get("install_path", "")
        if install_path:
            p = Path(install_path)
            if p.is_dir():
                return p
    return None


# ---------------------------------------------------------------------------
# GOG (heroic-gogdl backend)
# ---------------------------------------------------------------------------

def _load_gog_installed(heroic_root: Path) -> list[dict]:
    """
    Parse gog_store/installed.json from a Heroic config root.
    The file has shape {"installed": [ {appName, install_path, executable, ...}, ... ]}.
    Returns the list of entries, or an empty list on any error.
    """
    installed_json = heroic_root / "gog_store" / "installed.json"
    if not installed_json.is_file():
        return []
    try:
        data = json.loads(installed_json.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, dict):
            entries = data.get("installed", [])
            if isinstance(entries, list):
                return entries
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _find_gog_game(heroic_root: Path, app_names: list[str]) -> Path | None:
    """
    Search gog_store/installed.json for any of the given app_names (GOG product
    IDs as strings).  Returns the install_path as a Path if found and the
    directory exists on disk.
    """
    app_names_lower = {n.lower() for n in app_names}
    for entry in _load_gog_installed(heroic_root):
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("appName") or entry.get("app_name") or "")
        if entry_id and entry_id.lower() in app_names_lower:
            install_path = entry.get("install_path", "")
            if install_path:
                p = Path(install_path)
                if p.is_dir():
                    return p
    return None


# ---------------------------------------------------------------------------
# Sideloaded apps (manually-added games)
# ---------------------------------------------------------------------------

def _load_sideload_installed(heroic_root: Path) -> list[dict]:
    """
    Parse sideload_apps/library.json from a Heroic config root.
    The file has shape {"games": [ {app_name, title, install: {executable, ...},
    folder_name, ...}, ... ]}.  Returns the list of game entries, or an empty
    list on any error.
    """
    library_json = heroic_root / "sideload_apps" / "library.json"
    if not library_json.is_file():
        return []
    try:
        data = json.loads(library_json.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, dict):
            games = data.get("games", [])
            if isinstance(games, list):
                return games
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _sideload_game_root(executable: str, exe_name: str) -> Path | None:
    """
    Derive a game's root directory from a sideloaded app's stored executable.

    Sideload entries store the full path to the launch exe but no install_path.
    The handler's ``exe_name`` may include subdirs (e.g.
    'bin/x64/Cyberpunk2077.exe'); strip that relative path off the stored
    executable so we return the true game root, not the exe's folder.  Falls
    back to the exe's own directory if the suffix doesn't line up.
    """
    exe_path = Path(executable.replace("\\", "/"))
    rel = exe_name.replace("\\", "/").strip("/")
    rel_parts = [p for p in rel.split("/") if p]
    # Strip the handler's relative exe path from the tail of the stored path.
    if len(rel_parts) > 1 and len(exe_path.parts) >= len(rel_parts):
        tail = [p.lower() for p in exe_path.parts[-len(rel_parts):]]
        if tail == [p.lower() for p in rel_parts]:
            root = Path(*exe_path.parts[:-len(rel_parts)])
            if root.is_dir():
                return root
    # Single-segment exe name (or mismatch): root is the exe's directory.
    if exe_path.parent.is_dir():
        return exe_path.parent
    return None


# ---------------------------------------------------------------------------
# Wine prefix lookup
# ---------------------------------------------------------------------------

def _find_heroic_prefix_for_app(heroic_root: Path, app_name: str) -> Path | None:
    """
    Look up the Wine prefix for a game in Heroic's GamesConfig/<appName>.json.

    If the per-game config doesn't specify a winePrefix, fall back to the
    global default from config.json (defaultWinePrefix), and if that is also
    absent, try ~/Games/Heroic/Prefixes/<appName>/.

    Returns the prefix Path if it exists on disk, otherwise None.
    """
    # 1. Per-game: heroic_root/GamesConfig/<app_name>.json
    #    Path varies: Flatpak ~/.var/app/.../config/heroic, native ~/.config/heroic (or XDG_CONFIG_HOME)
    games_config = heroic_root / "GamesConfig"
    game_cfg_file = games_config / f"{app_name}.json"
    if game_cfg_file.is_file():
        try:
            cfg = json.loads(game_cfg_file.read_text(encoding="utf-8", errors="replace"))
            # Settings nested under appName key (Heroic format)
            inner = cfg.get(app_name, cfg)
            wine_prefix = (
                inner.get("winePrefix", "")
                or inner.get("wine_prefix", "")
                or cfg.get("winePrefix", "")
                or cfg.get("wine_prefix", "")
            )
            if wine_prefix:
                p = Path(wine_prefix)
                if (p / "pfx").is_dir():
                    return p / "pfx"
                if p.is_dir():
                    return p
        except (OSError, json.JSONDecodeError):
            pass

    # 2. Global default from config.json
    global_cfg_file = heroic_root / "config.json"
    if global_cfg_file.is_file():
        try:
            cfg = json.loads(global_cfg_file.read_text(encoding="utf-8", errors="replace"))
            # Heroic nests settings inside a "defaultSettings" key
            settings = cfg.get("defaultSettings", cfg)
            default_prefix_folder = settings.get("defaultWinePrefix", "")
            if default_prefix_folder:
                p = Path(default_prefix_folder) / app_name
                if (p / "pfx").is_dir():
                    return p / "pfx"
                if p.is_dir():
                    return p
        except (OSError, json.JSONDecodeError):
            pass

    # 3. Hard-coded conventional fallback
    fallback = _HOME / "Games" / "Heroic" / "Prefixes" / app_name
    if (fallback / "pfx").is_dir():
        return fallback / "pfx"
    if fallback.is_dir():
        return fallback

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_heroic_game(app_names: list[str]) -> Path | None:
    """
    Search all Heroic config roots for a game matching any of the given
    app_names.  Checks Epic (Legendary) installs first, then GOG.

    app_names should contain the Heroic/Epic appName identifiers and/or GOG
    product IDs declared by the game handler.  Matching is case-insensitive
    for GOG titles.

    Returns the game install directory Path, or None if not found.
    """
    for heroic_root in _find_heroic_config_roots():
        result = _find_epic_game(heroic_root, app_names)
        if result:
            return result
        result = _find_gog_game(heroic_root, app_names)
        if result:
            return result
    return None


def find_heroic_game_info_by_app_names(
        app_names: list[str]) -> "tuple[Path, Path | None, str] | None":
    """Resolve (install_path, prefix|None, app_name) from declared app names.

    Same result shape as find_heroic_game_info_by_exe.  Used when a handler
    declares heroic_app_names: those are authoritative, and the caller skips
    exe-name matching entirely — generic launcher names collide across
    different games (e.g. GOG Fallout 3 GOTY and classic Fallout both ship
    FalloutLauncher.exe), so an exe scan can resolve to the wrong title.
    """
    app_names_lower = {n.lower() for n in app_names}
    for heroic_root in _find_heroic_config_roots():
        # Epic (Legendary): installed.json is keyed by appName.
        installed = _load_epic_installed(heroic_root)
        for app_name in app_names:
            entry = installed.get(app_name)
            if not isinstance(entry, dict):
                continue
            install_path_raw = entry.get("install_path", "")
            if not install_path_raw:
                continue
            install_path = Path(install_path_raw)
            if not install_path.is_dir():
                continue
            prefix_path = _find_heroic_prefix_for_app(heroic_root, app_name)
            return (install_path, prefix_path, app_name)

        # GOG: entries carry the product id in appName.
        for entry in _load_gog_installed(heroic_root):
            if not isinstance(entry, dict):
                continue
            app_id = str(entry.get("appName") or entry.get("app_name") or "")
            if not app_id or app_id.lower() not in app_names_lower:
                continue
            install_path_raw = entry.get("install_path", entry.get("path", ""))
            if not install_path_raw:
                continue
            install_path = Path(install_path_raw)
            if not install_path.is_dir():
                continue
            prefix_path = _find_heroic_prefix_for_app(heroic_root, app_id)
            return (install_path, prefix_path, app_id)

        # Sideload: only matches a previously-saved per-game app name
        # (Heroic generates these ids).  No install_path is stored, so
        # derive the game root from the stored executable.
        for entry in _load_sideload_installed(heroic_root):
            if not isinstance(entry, dict):
                continue
            app_id = str(entry.get("app_name") or entry.get("appName") or "")
            if not app_id or app_id.lower() not in app_names_lower:
                continue
            install = entry.get("install") or {}
            stored_exe = install.get("executable", "") if isinstance(install, dict) else ""
            if not stored_exe:
                continue
            bare = stored_exe.replace("\\", "/").rsplit("/", 1)[-1]
            install_path = _sideload_game_root(stored_exe, bare)
            if not install_path:
                continue
            prefix_path = _find_heroic_prefix_for_app(heroic_root, app_id)
            return (install_path, prefix_path, app_id)
    return None


def find_heroic_launch_info(app_names: list[str]) -> "tuple[str, str] | None":
    """
    Search Heroic config for a game matching any of the given app_names.
    Returns (store, matched_app_name) where store is 'legendary' (Epic),
    'gog', or 'sideload', or None if not found.

    The returned values can be used to build a heroic:// launch URL:
        heroic://launch/<store>/<app_name>
    """
    app_names_lower = {n.lower() for n in app_names}
    for heroic_root in _find_heroic_config_roots():
        installed = _load_epic_installed(heroic_root)
        for app_name in app_names:
            if app_name in installed:
                install_path = installed[app_name].get("install_path", "")
                if install_path and Path(install_path).is_dir():
                    return ("legendary", app_name)
        for entry in _load_gog_installed(heroic_root):
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("appName") or entry.get("app_name") or "")
            if entry_id and entry_id.lower() in app_names_lower:
                install_path = entry.get("install_path", "")
                if install_path and Path(install_path).is_dir():
                    return ("gog", entry_id)
        # Sideloaded apps are keyed by their generated app_name; the launch
        # caller resolves that name via find_heroic_app_name_by_exe first.
        for entry in _load_sideload_installed(heroic_root):
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("app_name") or entry.get("appName") or "")
            if entry_id and entry_id.lower() in app_names_lower:
                return ("sideload", entry_id)
    return None


# ---------------------------------------------------------------------------
# Epic auth arguments (legendary)
# ---------------------------------------------------------------------------

_HEROIC_FLATPAK_ID = "com.heroicgameslauncher.hgl"
# Path inside every Heroic install (flatpak, native and AppImage share it).
_LEGENDARY_REL = "resources/app.asar.unpacked/build/bin/x64/linux/legendary"
_NATIVE_HEROIC_DIRS = ("/opt/Heroic", "/usr/lib/heroic", "/usr/share/heroic")


def _legendary_commands(heroic_root: Path) -> list[list[str]]:
    """Ways to run legendary against *heroic_root*'s config, best first."""
    cfg = str(heroic_root / "legendaryConfig" / "legendary")
    cmds: list[list[str]] = []
    on_path = shutil.which("legendary")
    if on_path:
        cmds.append(["env", f"LEGENDARY_CONFIG_PATH={cfg}", on_path])
    for base in _NATIVE_HEROIC_DIRS:
        binary = Path(base) / _LEGENDARY_REL
        if os.access(binary, os.X_OK):
            cmds.append(["env", f"LEGENDARY_CONFIG_PATH={cfg}", str(binary)])
    if shutil.which("flatpak"):
        # The flatpak's legendary only exists inside the sandbox, so the config
        # path must be the sandbox's own ($XDG_CONFIG_HOME), not the host path.
        cmds.append([
            "flatpak", "run", "--command=sh", _HEROIC_FLATPAK_ID, "-c",
            'LEGENDARY_CONFIG_PATH=$XDG_CONFIG_HOME/heroic/legendaryConfig/legendary '
            f'exec /app/bin/heroic/{_LEGENDARY_REL} "$@"', "sh",
        ])
    return cmds


def epic_launch_args(app_name: str, log_fn=None, timeout: int = 90) -> list[str]:
    """Epic auth/portal arguments legendary passes when launching *app_name*.

    Epic builds that use the EOS SDK need these to reach their main menu — the
    game otherwise logs "Missing platform services" and dies (Subnautica).
    Heroic gets them from legendary on every launch; the exchange code inside
    is single-use, so this must run per launch and can't be cached. Returns []
    when legendary can't be reached or the game isn't an installed Epic title.
    """
    import subprocess

    def _log(msg: str) -> None:
        if log_fn is not None:
            log_fn(msg)

    if os.environ.get("AMM_EPIC_AUTH") == "0":
        return []
    for heroic_root in _find_heroic_config_roots():
        if app_name not in _load_epic_installed(heroic_root):
            continue
        for cmd in _legendary_commands(heroic_root):
            argv = [*cmd, "launch", app_name, "--dry-run", "--json"]
            if _in_flatpak_sandbox() and shutil.which("flatpak-spawn"):
                argv = ["flatpak-spawn", "--host", "--directory=/", *argv]
            try:
                proc = subprocess.run(argv, capture_output=True, text=True,
                                      timeout=timeout)
            except Exception as e:
                _log(f"Epic auth: legendary failed to run — {e}")
                continue
            try:
                info = json.loads(proc.stdout.strip().splitlines()[-1])
            except Exception:
                err = (proc.stderr or proc.stdout or "").strip().splitlines()
                _log("Epic auth: legendary returned no launch data"
                     + (f" — {err[-1]}" if err else ""))
                continue
            args = [str(a) for a in (list(info.get("game_parameters") or [])
                                     + list(info.get("egl_parameters") or [])
                                     + list(info.get("user_parameters") or []))]
            if args:
                _log(f"Epic auth: got {len(args)} Epic launch argument(s) from "
                     "legendary (Epic Online Services login).")
            return args
        _log("Epic auth: this is an Epic game but legendary could not be run — "
             "the game may fail without its Epic login arguments. Launch via "
             "Heroic instead, or set AMM_EPIC_AUTH=0 to stop trying.")
        return []
    return []


def redact_epic_args(args: "list[str]") -> list[str]:
    """Mask the single-use Epic exchange code so logs stay shareable."""
    return [a.split("=", 1)[0] + "=***" if a.startswith("-AUTH_PASSWORD=") else a
            for a in args]


def _in_flatpak_sandbox() -> bool:
    """True when this process runs inside any Flatpak sandbox."""
    return os.path.exists("/.flatpak-info")


def find_heroic_app_name_by_exe(exe_name: str) -> str | None:
    """
    Search Heroic's installed.json for a game whose executable matches exe_name.
    Returns the app_name string if found, otherwise None.

    exe_name should be the bare filename, e.g. 'SubnauticaZero.exe'.
    Matching is case-insensitive.
    """
    info = find_heroic_game_info_by_exe(exe_name)
    return info[2] if info else None


def _stored_exe_matches(stored_exe: str, rel_parts: list[str]) -> bool:
    """Case-insensitive match of a stored executable path against the
    handler's exe name (pre-split into lowercase path segments).

    When the handler name includes directories (e.g. 'launcher/Launcher.exe')
    every segment must match the tail of the stored path — a bare-filename
    match is not enough, since generic names like Launcher.exe collide across
    games.  Single-segment names compare by filename only (Heroic may store a
    bare name or a relative/absolute path).
    """
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


def find_heroic_game_info_by_exe(exe_name: str) -> "tuple[Path, Path | None, str] | None":
    """
    Full Heroic detection workflow keyed by executable name from the handler:

    1. Look in Heroic's installed.json (legendaryConfig/legendary/installed.json)
       for a game whose executable matches exe_name.
    2. Get app_name and install_path from that entry.
    3. Look in GamesConfig/<appname>.json for the winePrefix.
    4. Return (install_path, prefix_path, app_name) if all found.

    Handler names that include directories ('launcher/Launcher.exe') must
    match the stored executable path segment-for-segment at the tail; bare
    names match on filename alone.

    Used for games like Subnautica Below Zero where the handler provides
    SubnauticaZero.exe; we resolve appname (Foxglove), install path, and prefix.
    """
    rel = exe_name.replace("\\", "/").strip("/")
    rel_parts = [p.lower() for p in rel.split("/") if p]
    if not rel_parts:
        return None
    exe_lower = rel_parts[-1]

    for heroic_root in _find_heroic_config_roots():
        # 1. Epic (Legendary) installed.json
        installed = _load_epic_installed(heroic_root)
        for app_name, entry in installed.items():
            if not isinstance(entry, dict):
                continue
            stored_exe = entry.get("executable", "")
            if not _stored_exe_matches(stored_exe, rel_parts):
                continue
            install_path_raw = entry.get("install_path", "")
            if not install_path_raw:
                continue
            install_path = Path(install_path_raw)
            if not install_path.is_dir():
                continue
            # 2. GamesConfig/<appname>.json for prefix
            prefix_path = _find_heroic_prefix_for_app(heroic_root, app_name)
            if prefix_path:
                return (install_path, prefix_path, app_name)
            # Still return install_path + app_name if prefix lookup fails;
            # caller can retry prefix later
            return (install_path, None, app_name)

        # 3. GOG: check gog_store/installed.json for executable match.
        #    Heroic's GOG entries often have an empty "executable" field, so
        #    fall back to probing install_path for the exe filename.
        for entry in _load_gog_installed(heroic_root):
            if not isinstance(entry, dict):
                continue
            app_id = str(entry.get("appName") or entry.get("app_name") or "")
            install_path_raw = entry.get("install_path", entry.get("path", ""))
            if not install_path_raw:
                continue
            install_path = Path(install_path_raw)
            if not install_path.is_dir():
                continue

            stored_exe = entry.get("executable", "") or entry.get("exe", "")

            matched = False
            if stored_exe:
                matched = _stored_exe_matches(stored_exe, rel_parts)
            else:
                # Scan install_path for the exe (case-insensitive, recursive).
                try:
                    for candidate in install_path.rglob("*"):
                        if not candidate.is_file() or candidate.name.lower() != exe_lower:
                            continue
                        cand_parts = [p.lower() for p in candidate.parts]
                        if len(rel_parts) == 1 or cand_parts[-len(rel_parts):] == rel_parts:
                            matched = True
                            break
                except OSError:
                    pass

            if not matched:
                continue

            prefix_path = _find_heroic_prefix_for_app(heroic_root, app_id)
            if prefix_path:
                return (install_path, prefix_path, app_id)
            return (install_path, None, app_id)

        # 4. Sideloaded apps: match the stored executable filename.
        #    No install_path is stored, so derive the game root from the exe.
        for entry in _load_sideload_installed(heroic_root):
            if not isinstance(entry, dict):
                continue
            install = entry.get("install") or {}
            stored_exe = install.get("executable", "") if isinstance(install, dict) else ""
            if not _stored_exe_matches(stored_exe, rel_parts):
                continue
            install_path = _sideload_game_root(stored_exe, exe_name)
            if not install_path:
                continue
            app_id = str(entry.get("app_name") or entry.get("appName") or "")
            prefix_path = _find_heroic_prefix_for_app(heroic_root, app_id) if app_id else None
            if prefix_path:
                return (install_path, prefix_path, app_id)
            return (install_path, None, app_id)

    return None


def _proton_script_from_wine_version(wine_version: dict) -> Path | None:
    """Resolve the Proton launcher script from a Heroic ``wineVersion`` block.

    Heroic stores ``bin`` as the full path to the Proton executable, e.g.
    ``.../compatibilitytools.d/GE-Proton10-34/proton``.  Returns that path when
    it exists and the runner is Proton (not plain wine/crossover).
    """
    if not isinstance(wine_version, dict):
        return None
    if str(wine_version.get("type", "")).lower() != "proton":
        return None
    bin_path = wine_version.get("bin", "")
    if not bin_path:
        return None
    p = Path(bin_path)
    # ``bin`` may point at the proton script directly or at its containing dir.
    if p.is_file() and p.name == "proton":
        return p
    if p.is_dir() and (p / "proton").is_file():
        return p / "proton"
    return None


def list_heroic_proton_scripts() -> list[Path]:
    """Proton launcher scripts installed or referenced by Heroic.

    Heroic's Wine Manager downloads GE-Proton builds into
    ``<config root>/tools/proton/<Tool>/`` (``tools/wine`` holds plain Wine
    builds, which have no ``proton`` script and are skipped naturally). Proton
    bins referenced from ``GamesConfig/*.json`` / global ``config.json``
    ``wineVersion`` blocks are included too, so builds kept in custom
    locations are still found. Lets Steam-less systems (Heroic-only, GH#320)
    offer Proton versions in the wizards and tool launchers.
    """
    seen: set[Path] = set()
    out: list[Path] = []

    def _add(script: Path | None) -> None:
        if script is None or not script.is_file():
            return
        try:
            resolved = script.resolve()
        except OSError:
            resolved = script
        if resolved not in seen:
            seen.add(resolved)
            out.append(script)

    for heroic_root in _find_heroic_config_roots():
        tools_dir = heroic_root / "tools" / "proton"
        if tools_dir.is_dir():
            try:
                for entry in tools_dir.iterdir():
                    if entry.is_dir():
                        _add(entry / "proton")
            except OSError:
                pass

        global_cfg_file = heroic_root / "config.json"
        if global_cfg_file.is_file():
            try:
                cfg = json.loads(global_cfg_file.read_text(
                    encoding="utf-8", errors="replace"))
                settings = cfg.get("defaultSettings", cfg)
                if isinstance(settings, dict):
                    _add(_proton_script_from_wine_version(
                        settings.get("wineVersion", {})))
            except (OSError, json.JSONDecodeError):
                pass

        games_config = heroic_root / "GamesConfig"
        if games_config.is_dir():
            try:
                cfg_files = list(games_config.glob("*.json"))
            except OSError:
                cfg_files = []
            for cfg_file in cfg_files:
                try:
                    cfg = json.loads(cfg_file.read_text(
                        encoding="utf-8", errors="replace"))
                except (OSError, json.JSONDecodeError):
                    continue
                inner = cfg.get(cfg_file.stem, cfg) if isinstance(cfg, dict) else {}
                if isinstance(inner, dict):
                    _add(_proton_script_from_wine_version(
                        inner.get("wineVersion", {})))
    return out


def find_heroic_proton_for_prefix(prefix_path: "str | Path") -> Path | None:
    """Return the Proton launcher script Heroic uses for *prefix_path*.

    Scans every Heroic root's GamesConfig/<app>.json for an entry whose
    ``winePrefix`` matches *prefix_path* and reads the proton bin from its
    ``wineVersion`` block.  Returns None if no match (or the runner isn't
    Proton).  Heroic appends ``/pfx`` to the prefix at launch but stores the
    parent in winePrefix, so both forms are accepted.
    """
    target = Path(prefix_path)
    candidates = {target}
    if target.name == "pfx":
        candidates.add(target.parent)
    cand_resolved = set()
    for c in candidates:
        try:
            cand_resolved.add(c.resolve())
        except OSError:
            cand_resolved.add(c)

    for heroic_root in _find_heroic_config_roots():
        games_config = heroic_root / "GamesConfig"
        if not games_config.is_dir():
            continue
        try:
            cfg_files = list(games_config.glob("*.json"))
        except OSError:
            continue
        for cfg_file in cfg_files:
            try:
                cfg = json.loads(cfg_file.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            app_name = cfg_file.stem
            inner = cfg.get(app_name, cfg) if isinstance(cfg, dict) else {}
            if not isinstance(inner, dict):
                continue
            wine_prefix = inner.get("winePrefix", "") or inner.get("wine_prefix", "")
            if not wine_prefix:
                continue
            wp = Path(wine_prefix)
            try:
                wp_resolved = wp.resolve()
            except OSError:
                wp_resolved = wp
            if wp_resolved not in cand_resolved and wp not in candidates:
                continue
            proton = _proton_script_from_wine_version(inner.get("wineVersion", {}))
            if proton:
                return proton
    return None


def find_heroic_prefix(app_names: list[str]) -> Path | None:
    """
    Search all Heroic config roots for the Wine prefix of a game matching any
    of the given app_names.

    Returns the prefix Path (the pfx-equivalent root that Heroic manages),
    or None if not found.
    """
    for heroic_root in _find_heroic_config_roots():
        for app_name in app_names:
            result = _find_heroic_prefix_for_app(heroic_root, app_name)
            if result:
                return result
    return None
