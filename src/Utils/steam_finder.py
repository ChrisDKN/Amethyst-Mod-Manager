"""
steam_finder.py
Utilities for locating Steam game installations across all configured library paths.
No UI, no game-specific knowledge.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Known Steam base directories for different install methods
# ---------------------------------------------------------------------------
_HOME = Path.home()

_STEAM_CANDIDATES: list[Path] = [
    _HOME / ".local" / "share" / "Steam",                                          # Standard
    _HOME / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",  # Flatpak
    _HOME / "snap" / "steam" / "common" / ".local" / "share" / "Steam",            # Snap
    _HOME / ".steam" / "steam",                                                     # Symlink fallback
]

# Steam normally writes "libraryfolders.vdf", but some installs (and older
# clients) use the singular "libraryfolder.vdf". Accept both spellings.
_VDF_FILENAMES = ("libraryfolders.vdf", "libraryfolder.vdf")
_VDF_FILENAME = _VDF_FILENAMES[0]
_COMMON_SUBDIR = Path("steamapps") / "common"


_STEAM_FLATPAK_ID = "com.valvesoftware.Steam"
_STEAM_FLATPAK_DATA = _HOME / ".var" / "app" / _STEAM_FLATPAK_ID


def _proton_script_in_steam_flatpak(proton_script: "Path") -> bool:
    """True if *proton_script* lives inside the Flatpak Steam's data dir.

    Flatpak Steam ships Proton (and its runtime + steamclient libraries) inside
    the ``com.valvesoftware.Steam`` sandbox. Running that Proton bare from a host
    process (e.g. the AppImage manager) breaks: the runtime can't find its
    sandbox libraries and lsteamclient aborts. Such a Proton must be launched
    *through* the Steam Flatpak so it runs inside its own sandbox.
    """
    try:
        Path(proton_script).resolve().relative_to(_STEAM_FLATPAK_DATA.resolve())
        return True
    except Exception:
        return False


def _own_process_in_steam_flatpak() -> bool:
    """True if *this* process is already inside the com.valvesoftware.Steam sandbox."""
    try:
        info = Path("/.flatpak-info")
        if not info.is_file():
            return False
        return _STEAM_FLATPAK_ID in info.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False


def _in_flatpak_sandbox() -> bool:
    """True when this process runs inside any Flatpak sandbox."""
    return os.path.exists("/.flatpak-info")


def steam_client_installed() -> bool:
    """True when any known Steam client install exists on this machine.

    Heroic/Lutris-only systems (GH#320) have none: there is no Steam root to
    use as STEAM_COMPAT_CLIENT_INSTALL_PATH and no Steam runtime for a raw
    ``proton run``, so Proton launches on such systems are routed through
    umu-run instead (see :func:`proton_run_command`).
    """
    return any((root / "steamapps").is_dir() for root in _STEAM_CANDIDATES)


_steamless_no_umu_logged = False


def _maybe_log_steamless_no_umu() -> None:
    """Log once when a Steam-less system has no umu-run launcher either.

    The raw ``proton <verb>`` fallback that follows needs Steam's client and
    runtime, so it will very likely fail — point the user at the fix instead
    of leaving only an opaque wine error.
    """
    global _steamless_no_umu_logged
    if _steamless_no_umu_logged:
        return
    _steamless_no_umu_logged = True
    try:
        from Utils.app_log import app_log
        app_log(
            "No Steam client and no umu-run launcher were found — attempting "
            "a raw Proton launch, which will likely fail. umu-run ships with "
            "Heroic and Lutris (installing either, or the umu-launcher "
            "package, provides it)."
        )
    except Exception:
        pass


def _host_python() -> str:
    """Return a *host* python3 to run the Proton script with.

    Inside our AppImage, ``python3`` on PATH resolves to the bundle's
    quick-sharun interpreter (``$APPDIR/usr/bin/python3``). Running the host's
    Proton script with THAT interpreter re-injects anylinux.so's LD_PRELOAD
    sentinel, so Proton's startup Vulkan probe fails with
    ``OSError: /anylinux_blocked_lib_that_does_not_exist.so`` when it tries to
    ``CDLL('libvulkan.so.1')``. Scrubbing the env dict we pass to Popen isn't
    enough — the bundled interpreter re-pollutes itself on startup — so the
    interpreter itself must be a real host one.

    Prefer ``/usr/bin/python3`` (present on every target host); otherwise take
    the first ``python3`` on PATH that is NOT under ``$APPDIR`` or a
    ``/tmp/.mount_*`` FUSE mount. Falls back to plain ``"python3"`` when no
    host interpreter can be located (native/flatpak installs, where ``python3``
    is already the host one).
    """
    appdir = os.environ.get("APPDIR")
    appimage = os.environ.get("APPIMAGE")
    if not appdir and not appimage:
        return "python3"           # not an AppImage — PATH python3 is the host's

    def _is_bundled(path: str) -> bool:
        rp = os.path.realpath(path)
        if rp.startswith("/tmp/.mount_"):
            return True
        if appdir and rp.startswith(os.path.realpath(appdir)):
            return True
        return False

    if os.path.exists("/usr/bin/python3") and not _is_bundled("/usr/bin/python3"):
        return "/usr/bin/python3"

    # Scan PATH for the first non-bundled python3 (skip $APPDIR//tmp/.mount_*).
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        if not d or d.startswith("/tmp/.mount_"):
            continue
        cand = os.path.join(d, "python3")
        if os.path.isfile(cand) and os.access(cand, os.X_OK) and not _is_bundled(cand):
            return cand
    return "python3"


def proton_run_command(
    proton_script: "Path", *args: str, env: "dict | None" = None,
    host_cwd: "str | Path | None" = None,
) -> list[str]:
    """Build the command to invoke ``proton <args>`` for *proton_script*.

    Normally this is ``[<host python3>, <proton_script>, *args]`` run on the
    host. Inside the AppImage the interpreter is resolved to a *host* python3
    (see ``_host_python``) so Proton doesn't inherit the bundle's blocked
    library loader.

    When the Proton script belongs to the Flatpak Steam install and we are NOT
    already inside that sandbox, wrap the invocation in
    ``flatpak run --command=python3 com.valvesoftware.Steam ...`` so Proton runs
    inside the sandbox its runtime and steamclient libraries expect. Without
    this, a host-side manager (AppImage/native) driving Flatpak-Steam's Proton
    hits an lsteamclient assertion or missing-library failures.

    When we are *ourselves* inside a Flatpak sandbox (the manager's own
    flatpak), there is no ``flatpak`` CLI in the runtime — the ``flatpak run``
    must be forwarded to the host via ``flatpak-spawn --host``. flatpak-spawn
    does not forward the environment, so pass the Popen env dict as *env*:
    every var the caller added on top of ``os.environ`` (STEAM_COMPAT_*,
    WINEDLLOVERRIDES, …) is re-exported with ``--env=`` flags.

    *proton_script* may also be a plain ``wine``/``wine64`` binary (Lutris
    classic-wine prefixes): the caller's env must then carry WINEPREFIX. Wine
    takes the payload directly — Proton's verbs (run / runinprefix /
    waitforexitandrun) have no wine equivalent and are dropped — and there is
    no python interpreter in front of the command.
    """
    # Directory flatpak-spawn's portal chdirs the host process into. Proton's
    # runinprefix (and bare wine) start the exe from this cwd, and Windows apps
    # that write files relative to their working directory (e.g.
    # WitcherScriptMerger's MergeInventory.xml) resolve them here — under Wine
    # cwd "/" maps to "Z:\\", which is not writable, so a real host directory
    # must be passed when the caller has one. Defaults to "/" because the app's
    # own sandbox cwd does not exist on the host and the portal would fail to
    # chdir into it.
    directory = str(host_cwd) if host_cwd is not None else "/"

    script = Path(proton_script)
    if script.name in ("wine", "wine64"):
        payload = [a for a in map(str, args) if a not in
                   ("run", "runinprefix", "waitforexitandrun")]
        cmd = [str(script), *payload]
        if _in_flatpak_sandbox() and shutil.which("flatpak-spawn"):
            fwd = [
                f"--env={k}={v}"
                for k, v in (env or {}).items()
                if os.environ.get(k) != v
            ]
            cmd = ["flatpak-spawn", "--host", f"--directory={directory}",
                   *fwd, *cmd]
        return cmd

    if not steam_client_installed():
        # Steam-less system (Heroic/Lutris-only, GH#320): a raw
        # ``python3 proton <verb>`` needs a Steam client for its compat
        # plumbing and runtime, which doesn't exist here. Route the launch
        # through umu-run — the launcher Heroic itself uses — which runs
        # Proton inside the Steam Linux Runtime container with no Steam
        # client at all. umu has no verbs (it always does a full Proton
        # session) and derives its plumbing from WINEPREFIX / PROTONPATH /
        # GAMEID; the caller's STEAM_COMPAT_* vars are overridden by umu
        # itself, so they can stay in *env*. Mutates *env* (callers pass the
        # same dict to Popen, and umu_run_command re-exports the diff under
        # flatpak-spawn).
        try:
            from Utils.lutris_finder import find_umu_run, umu_run_command
            umu_bin = find_umu_run()
        except Exception:
            umu_bin = None
        if umu_bin is not None and env is not None:
            payload = [a for a in map(str, args) if a not in
                       ("run", "runinprefix", "waitforexitandrun")]
            if not env.get("WINEPREFIX") and env.get("STEAM_COMPAT_DATA_PATH"):
                # Proton resolves the real prefix as $WINEPREFIX/pfx, same
                # shape as $STEAM_COMPAT_DATA_PATH/pfx (umu self-links pfx →
                # "." when absent), so the compat-data root maps straight
                # across.
                env["WINEPREFIX"] = env["STEAM_COMPAT_DATA_PATH"]
            env["PROTONPATH"] = str(script.parent)
            env.setdefault("GAMEID", "umu-default")
            return umu_run_command(umu_bin, *payload, env=env,
                                   host_cwd=directory)
        _maybe_log_steamless_no_umu()

    base = [_host_python(), str(proton_script), *map(str, args)]
    if not (_proton_script_in_steam_flatpak(proton_script)
            and not _own_process_in_steam_flatpak()):
        if _in_flatpak_sandbox() and shutil.which("flatpak-spawn"):
            fwd = [
                f"--env={k}={v}"
                for k, v in (env or {}).items()
                if os.environ.get(k) != v
            ]
            return ["flatpak-spawn", "--host", f"--directory={directory}",
                    *fwd, *base]
        return base
    # Steam-flatpak Proton runs INSIDE the sandbox, so --command=python3 uses
    # the sandbox's own interpreter (not our host resolver) — that's correct.
    # --filesystem=host so the sandbox can reach the staging/game/tool paths
    # that live outside Steam's own data dir.
    cmd = [
        "flatpak", "run",
        "--command=python3",
        "--filesystem=host",
        _STEAM_FLATPAK_ID,
        str(proton_script), *map(str, args),
    ]
    if _in_flatpak_sandbox() and shutil.which("flatpak-spawn"):
        fwd = [
            f"--env={k}={v}"
            for k, v in (env or {}).items()
            if os.environ.get(k) != v
        ]
        cmd = ["flatpak-spawn", "--host", *fwd, *cmd]
    return cmd


def _normalize_tool_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _proton_sort_key(name: str) -> tuple[int, tuple[int, ...], str]:
    """
    Sort key for Proton tool names.

    Priority:
      1) GE-Proton builds before Valve Proton builds
      2) Higher numeric versions first
      3) Name as a final tie-breaker
    """
    lower = name.lower()
    is_ge = lower.startswith("ge-proton")
    nums = tuple(int(n) for n in re.findall(r"\d+", lower))
    return (0 if is_ge else 1, tuple(-n for n in nums), lower)


def list_installed_proton() -> list[Path]:
    """Return all installed Proton launcher scripts, sorted by _proton_sort_key.

    Covers the Steam roots (compatibilitytools.d + steamapps/common) and the
    Proton builds Heroic's Wine Manager downloads (the only source on
    Steam-less systems, GH#320). Deduplicates by resolved path so symlinked
    Steam roots (e.g. ~/.steam/steam) don't produce duplicate entries.
    """
    seen: set[Path] = set()
    candidates: list[Path] = []
    for steam_root in _all_proton_search_roots():
        for search_dir in (
            steam_root / "compatibilitytools.d",
            steam_root / "steamapps" / "common",
        ):
            if not search_dir.is_dir():
                continue
            try:
                for entry in search_dir.iterdir():
                    if not entry.is_dir():
                        continue
                    proton_script = entry / "proton"
                    if not proton_script.is_file():
                        continue
                    resolved = proton_script.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    candidates.append(proton_script)
            except OSError:
                continue
    # Heroic-managed Proton builds. A Heroic copy whose directory name matches
    # a Steam-provided tool is skipped — it's the same build, and the Steam
    # copy plays nicer with the Steam runtime plumbing.
    try:
        from Utils.heroic_finder import list_heroic_proton_scripts
        heroic_scripts = list_heroic_proton_scripts()
    except Exception:
        heroic_scripts = []
    steam_names = {c.parent.name.lower() for c in candidates}
    for proton_script in heroic_scripts:
        try:
            resolved = proton_script.resolve()
        except OSError:
            resolved = proton_script
        if resolved in seen or proton_script.parent.name.lower() in steam_names:
            continue
        seen.add(resolved)
        candidates.append(proton_script)
    candidates.sort(key=lambda p: _proton_sort_key(p.parent.name))
    return candidates


def find_any_installed_proton(preferred_name: str = "") -> Path | None:
    """
    Find an installed Proton launcher script without requiring a Steam app mapping.

    This is used as a fallback for custom prefixes where Steam does not maintain
    a CompatToolMapping entry for the selected app ID.

    Args:
        preferred_name: Optional Proton tool directory name to prefer,
                        e.g. "GE-Proton10-28".
    """
    preferred_norm = _normalize_tool_name(preferred_name) if preferred_name else ""

    candidates = list_installed_proton()

    if not candidates:
        return None

    if preferred_norm:
        for candidate in candidates:
            if _normalize_tool_name(candidate.parent.name) == preferred_norm:
                return candidate

    proton_like = [
        c for c in candidates
        if c.parent.name.lower().startswith(("proton", "ge-proton"))
    ]
    if not proton_like:
        proton_like = candidates

    proton_like.sort(key=lambda p: _proton_sort_key(p.parent.name))
    return proton_like[0]


def find_steam_root_for_proton_script(proton_script: Path) -> Path | None:
    """
    Resolve the Steam client root to use as STEAM_COMPAT_CLIENT_INSTALL_PATH
    for a given Proton script.

    Supports Proton installs from both:
      - <steam_root>/steamapps/common/<Tool>/proton
      - <steam_root>/compatibilitytools.d/<Tool>/proton

    When the Proton tool lives in a *secondary* library (SD card, second
    drive), that library is not a real Steam client install — Proton needs the
    runtime that lives under the main Steam root. In that case we fall back to
    the first real Steam client candidate so the runtime is still found.
    """
    script = proton_script.resolve()

    # Prefer a genuine Steam client root that owns this script directly.
    for steam_root in _STEAM_CANDIDATES:
        try:
            rel = script.relative_to(steam_root.resolve())
        except Exception:
            continue

        if len(rel.parts) < 2:
            continue

        if rel.parts[0] == "steamapps" and len(rel.parts) >= 4 and rel.parts[1] == "common":
            return steam_root
        if rel.parts[0] == "compatibilitytools.d" and len(rel.parts) >= 3:
            return steam_root

    # The script lives outside every known client root (e.g. a secondary
    # library). Use the first real Steam client install for the runtime.
    for steam_root in _STEAM_CANDIDATES:
        if (steam_root / "steamapps").is_dir():
            return steam_root

    # Last resort: derive a root from the path itself.
    parts = script.parts
    try:
        idx = parts.index("compatibilitytools.d")
        if idx > 0:
            return Path(*parts[:idx])
    except ValueError:
        pass

    try:
        idx = parts.index("steamapps")
        if idx > 0:
            return Path(*parts[:idx])
    except ValueError:
        pass

    # No Steam client is installed at all (Heroic/Lutris-only system, GH#320)
    # and the tool lives outside any Steam-shaped path (e.g. Heroic's
    # tools/proton). Return the tool's own directory as a stand-in so callers
    # can proceed: launches on such systems are routed through umu-run
    # (proton_run_command), which builds its own compat plumbing and
    # overrides STEAM_COMPAT_CLIENT_INSTALL_PATH itself.
    if not steam_client_installed():
        return script.parent

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# find_steam_libraries() cache — the GUI calls it on every refresh, and a
# full run re-reads the ini + every candidate VDF and resolves every library.
# Validated against the (st_mtime_ns, st_size) of the source files actually
# read, so any change to them re-parses; stat-only validation is ~free.
_libraries_lock = threading.Lock()
_libraries_cache: "tuple[tuple, tuple[Path, ...]] | None" = None  # (signature, libs)
_custom_vdf_cache: "tuple[tuple | None, str] | None" = None       # (ini sig, path)


def _stat_sig(path) -> "tuple[int, int] | None":
    """(st_mtime_ns, st_size) for a regular file, None if absent/unreadable."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size) if stat.S_ISREG(st.st_mode) else None


def _custom_vdf_path() -> str:
    """User-configured libraryfolders.vdf path, cached on amethyst.ini's stat."""
    global _custom_vdf_cache
    try:
        from Utils.ui_config import (
            get_ui_config_path,
            load_steam_libraries_vdf_path,
        )
        ini_sig = _stat_sig(get_ui_config_path())
        cached = _custom_vdf_cache
        if cached is not None and cached[0] == ini_sig:
            return cached[1]
        custom = load_steam_libraries_vdf_path()
        _custom_vdf_cache = (ini_sig, custom)
        return custom
    except Exception:
        return ""


def find_steam_libraries() -> list[Path]:
    """
    Parse libraryfolders.vdf from all known Steam install locations.
    Returns a deduplicated list of existing steamapps/common/ directories.

    Results are cached module-wide and revalidated by stat signature of the
    config ini and every VDF hit, so repeated GUI-refresh calls are cheap.
    Each call returns a fresh list, so callers can't corrupt the cache.
    """
    global _libraries_cache

    vdf_candidates: list[Path] = []

    # User-configured VDF path takes highest priority
    custom = _custom_vdf_path()
    if custom:
        vdf_candidates.append(Path(custom))

    # Built-in fallbacks. Steam keeps copies under both steamapps/ and the
    # root config/, and may spell the file singular or plural — try them all.
    for steam_root in _STEAM_CANDIDATES:
        for name in _VDF_FILENAMES:
            vdf_candidates.append(steam_root / "steamapps" / name)
            vdf_candidates.append(steam_root / "config" / name)
            vdf_candidates.append(steam_root / name)

    # Stat every candidate once: the existing files (in order) plus their
    # signatures form the cache key, so a VDF appearing, vanishing or being
    # rewritten all invalidate the cached result.
    hits: list[tuple[Path, tuple]] = []
    for vdf_path in vdf_candidates:
        sig = _stat_sig(vdf_path)
        if sig is not None:
            hits.append((vdf_path, sig))
    signature = (custom, tuple((str(p), s) for p, s in hits))

    with _libraries_lock:
        cached = _libraries_cache
        if cached is not None and cached[0] == signature:
            return list(cached[1])

    seen: set[Path] = set()
    libraries: list[Path] = []
    for vdf_path, _sig in hits:
        for common in parse_vdf_libraries(vdf_path):
            resolved = common.resolve()
            if resolved not in seen:
                seen.add(resolved)
                libraries.append(common)

    with _libraries_lock:
        _libraries_cache = (signature, tuple(libraries))
    return libraries


# Warn-once tracking so the same library path doesn't spam the log every time
# find_steam_libraries() runs (it's called frequently by GUI refreshes).
_vdf_warned_missing: set[str] = set()
_vdf_warned_readonly: set[str] = set()


def _warn_vdf_library(raw: str, common: Path) -> None:
    """Log a one-time warning for a library that is listed in VDF but
    unusable (drive unmounted, read-only, etc.). Best-effort — swallows
    import errors so this module stays UI-free.
    """
    try:
        from Utils.app_log import app_log
    except Exception:
        return

    if not common.is_dir():
        if raw not in _vdf_warned_missing:
            _vdf_warned_missing.add(raw)
            app_log(
                f"Steam library listed in libraryfolders.vdf but not accessible: "
                f"{raw} — drive may be unmounted or disconnected"
            )
        return

    # Directory exists; check writability. Deploy + install need write access
    # to the steamapps/common path (and the parent for compatdata etc.).
    if not os.access(str(common), os.W_OK):
        if raw not in _vdf_warned_readonly:
            _vdf_warned_readonly.add(raw)
            app_log(
                f"Steam library is read-only: {common} — mod deployment to games "
                f"in this library will fail until the mount is remounted writable"
            )


def parse_vdf_libraries(vdf_path: Path) -> list[Path]:
    """
    Parse a libraryfolders.vdf file and return all steamapps/common paths
    that currently exist on disk.

    The VDF format contains lines like:
        "path"    "/home/user/.local/share/Steam"
    We extract every "path" value and append steamapps/common to each.
    The Steam root containing the VDF is always included as the first entry.

    Libraries listed in the VDF but currently inaccessible (drive unmounted,
    disconnected USB, NAS offline) or mounted read-only are logged via
    app_log the first time we see them, so users get a clue when a game
    that used to work suddenly "vanishes" from the library list.
    """
    libraries: list[Path] = []
    pattern = re.compile(r'"path"\s+"([^"]+)"')

    try:
        text = vdf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return libraries

    for match in pattern.finditer(text):
        raw = match.group(1)
        common = Path(raw) / "steamapps" / "common"
        _warn_vdf_library(raw, common)
        if common.is_dir():
            libraries.append(common)

    return libraries


def _all_proton_search_roots() -> list[Path]:
    """Every directory that may hold a Proton install: the known Steam roots
    plus the steamapps/common of each extra library listed in libraryfolders.vdf.

    Proton tools live alongside games, so a tool selected via CompatToolMapping
    can sit in a secondary library (SD card, second drive) even though the
    config.vdf that names it only exists in the main Steam root.
    """
    roots: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        try:
            resolved = p.resolve()
        except Exception:
            resolved = p
        if resolved not in seen:
            seen.add(resolved)
            roots.append(p)

    for steam_root in _STEAM_CANDIDATES:
        _add(steam_root)

    for steam_root in _STEAM_CANDIDATES:
        vdf_path = steam_root / "steamapps" / _VDF_FILENAME
        if not vdf_path.is_file():
            continue
        for common in parse_vdf_libraries(vdf_path):
            # common is .../steamapps/common; the Steam-root-equivalent is its grandparent
            _add(common.parent.parent)

    return roots


def find_prefix(steam_id: str) -> Path | None:
    """
    Locate the Steam compatibility prefix directory for a given App ID.

    Steam stores per-game Proton prefixes under:
        <steam_root>/steamapps/compatdata/<steam_id>/pfx/

    Searches every known Steam root candidate first, then falls back to
    extra library folders parsed from libraryfolders.vdf (e.g. SD card or
    secondary drive libraries), since compatdata lives alongside the game.

    Args:
        steam_id: The Steam App ID as a string, e.g. '377160' for Fallout 4.
    """
    if not steam_id:
        return None

    def _check_compatdata(compatdata: Path) -> Path | None:
        """Return the prefix dir for a compatdata/<id> folder, or None."""
        pfx = compatdata / "pfx"
        if pfx.is_dir():
            return pfx
        if (compatdata / "drive_c").is_dir():
            return compatdata
        return None

    # Primary: check known Steam root candidates
    for steam_root in _STEAM_CANDIDATES:
        result = _check_compatdata(steam_root / "steamapps" / "compatdata" / steam_id)
        if result:
            return result

    # Secondary: check extra library folders (SD card, secondary drives, etc.)
    # parse_vdf_libraries returns steamapps/common paths; parent is steamapps/
    seen: set[Path] = set()
    for steam_root in _STEAM_CANDIDATES:
        vdf_path = steam_root / "steamapps" / _VDF_FILENAME
        if not vdf_path.is_file():
            continue
        for common in parse_vdf_libraries(vdf_path):
            steamapps = common.parent
            resolved = steamapps.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            result = _check_compatdata(steamapps / "compatdata" / steam_id)
            if result:
                return result

    return None


def game_steam_id(game) -> str:
    """Return the Steam App ID actually installed for *game*.

    Resolves localized/alternate editions (e.g. FNV's 22490) through the game
    handler's ``effective_steam_id()`` when available, falling back to the plain
    ``steam_id`` attribute. Safe to call on any object.
    """
    if game is None:
        return ""
    resolver = getattr(game, "effective_steam_id", None)
    if callable(resolver):
        try:
            return resolver() or ""
        except Exception:
            pass
    return str(getattr(game, "steam_id", "") or "")


def find_proton_for_game(steam_id: str) -> Path | None:
    """
    Find the Proton launcher script assigned to a Steam game.

    Reads CompatToolMapping from Steam's config files to determine which Proton
    version the game uses, then locates the 'proton' script in steamapps/common/.

    Steam rewrites config.vdf atomically, so the live file may temporarily lack
    CompatToolMapping — we also check .bak and .tmp variants of the file.

    Returns the path to the 'proton' script, or None if the game's assigned
    Proton cannot be found (never falls back to an arbitrary version).

    The returned path can be used to run a Windows exe via:
        STEAM_COMPAT_DATA_PATH=<pfx_parent>
        STEAM_COMPAT_CLIENT_INSTALL_PATH=<steam_root>
        python3 <proton_script> run <exe_path>
    """
    import re as _re
    import glob as _glob

    _COMPAT_TOOL_NAMES: dict[str, str] = {
        "proton_experimental": "Proton - Experimental",
        "proton_hotfix":       "Proton Hotfix",
        "proton_10":           "Proton 10.0",
        "proton_9":            "Proton 9.0 (Beta)",
        "proton_8":            "Proton 8.0",
        "proton_7":            "Proton 7.0",
    }

    _ID_PATTERN = _re.compile(
        r'"' + _re.escape(steam_id) + r'"\s*\{[^}]*?"name"\s*"([^"]+)"',
        _re.DOTALL,
    )

    if not steam_id:
        return None

    for steam_root in _STEAM_CANDIDATES:
        config_dir = steam_root / "config"
        if not config_dir.is_dir():
            continue

        # Collect all config.vdf variants: live, .bak, and any .tmp files.
        # Steam writes atomically so the live file may be mid-swap.
        candidates_vdf: list[Path] = []
        for pattern in ("config.vdf", "config.vdf.bak", "config.vdf.*.tmp"):
            candidates_vdf.extend(
                Path(p) for p in _glob.glob(str(config_dir / pattern))
            )

        tool_name: str | None = None
        for vdf_path in candidates_vdf:
            try:
                text = vdf_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Only search inside the CompatToolMapping block
            compat_idx = text.find("CompatToolMapping")
            if compat_idx < 0:
                continue
            m = _ID_PATTERN.search(text, compat_idx)
            if m:
                tool_name = m.group(1)
                break

        if tool_name is None:
            continue

        # Map internal short names to steamapps/common directory names
        dir_name = _COMPAT_TOOL_NAMES.get(tool_name, tool_name)

        # Search steamapps/common/ and compatibilitytools.d/ (GE-Proton, etc.)
        # across every Steam root AND secondary library — Proton tools live
        # alongside games, so the mapped tool may sit on an SD card / second
        # drive even though config.vdf only exists in the main Steam root.
        search_dirs: list[Path] = []
        for root in _all_proton_search_roots():
            search_dirs.append(root / "steamapps" / "common")
            search_dirs.append(root / "compatibilitytools.d")

        for search_dir in search_dirs:
            # Exact match first
            candidate = search_dir / dir_name / "proton"
            if candidate.is_file():
                return candidate

            # Case-insensitive match (handles minor name variations)
            if search_dir.is_dir():
                dir_lower = dir_name.lower()
                for entry in search_dir.iterdir():
                    if entry.name.lower() == dir_lower:
                        p = entry / "proton"
                        if p.is_file():
                            return p

    # --- Fallback: read compatdata/<steam_id>/config_info ----------------
    # When Steam uses the default Proton for a game it may not write an
    # entry to CompatToolMapping.  The compatdata directory, however,
    # stores a config_info file whose lines include the Proton path used
    # to create the prefix (e.g. ".../common/Proton 10.0/files/...").
    # We extract the Proton directory name from that path.
    for steam_root in _all_proton_search_roots():
        config_info = (steam_root / "steamapps" / "compatdata"
                       / steam_id / "config_info")
        if not config_info.is_file():
            continue
        try:
            lines = config_info.read_text(encoding="utf-8",
                                          errors="replace").splitlines()
        except OSError:
            continue
        # Check for Proton paths in both steamapps/common/ and
        # compatibilitytools.d/ (GE-Proton, custom builds, etc.)
        _PROTON_PATH_MARKERS = [
            "/steamapps/common/",
            "/compatibilitytools.d/",
        ]
        for line in lines:
            for marker in _PROTON_PATH_MARKERS:
                if marker not in line:
                    continue
                idx = line.find(marker)
                after = line[idx + len(marker):]
                proton_dir_name = after.split("/")[0]
                if not proton_dir_name.lower().startswith(("proton", "ge-proton")):
                    continue
                # Reconstruct the parent directory from the marker
                parent_dir = Path(line[:idx + len(marker)].rstrip("/"))
                candidate = parent_dir / proton_dir_name / "proton"
                if candidate.is_file():
                    return candidate

    return None


def _parse_acf_installdir(acf_path: Path) -> str | None:
    """Parse installdir from a Steam appmanifest_*.acf file. Returns None if not found."""
    installdir_pattern = re.compile(r'"installdir"\s+"([^"]+)"')
    try:
        text = acf_path.read_text(encoding="utf-8", errors="replace")
        m = installdir_pattern.search(text)
        return m.group(1) if m else None
    except OSError:
        return None


def find_game_by_steam_id(
    libraries: list[Path], steam_id: str, exe_name: str
) -> Path | None:
    """
    Locate a game's install folder using its Steam App ID and appmanifest.

    When multiple games share the same exe name (e.g. Enderal and Enderal SE both
    use "Enderal Launcher.exe"), the exe-based search returns the first match.
    This function uses Steam's app manifest to find the exact folder for the
    given steam_id, then verifies the exe is present.

    Returns the game root directory or None if not found.
    """
    if not steam_id:
        return None

    exe_lower = exe_name.lower()
    has_subdir = "/" in exe_name or "\\" in exe_name

    for common in libraries:
        steamapps = common.parent  # steamapps/common -> steamapps
        acf = steamapps / f"appmanifest_{steam_id}.acf"
        if not acf.is_file():
            continue
        installdir = _parse_acf_installdir(acf)
        if not installdir:
            continue
        game_dir = common / installdir
        if not game_dir.is_dir():
            continue
        if has_subdir:
            candidate = game_dir / exe_name
            if candidate.is_file():
                return game_dir
            parts = exe_lower.replace("\\", "/").split("/")
            cur = game_dir
            for part in parts:
                match = None
                try:
                    for entry in cur.iterdir():
                        if entry.name.lower() == part:
                            match = entry
                            break
                except PermissionError:
                    break
                if match is None:
                    break
                cur = match
            else:
                if cur.is_file():
                    return game_dir
        else:
            try:
                for entry in game_dir.iterdir():
                    if entry.name.lower() == exe_lower and entry.is_file():
                        return game_dir
            except PermissionError:
                pass

    return None


def find_game_in_libraries(libraries: list[Path], exe_name: str) -> Path | None:
    """
    Search each library's steamapps/common/* subfolder for exe_name.
    Returns the game root directory (the <GameFolder>) or None if not found.

    exe_name may be a bare filename (e.g. "SkyrimSE.exe") — searched one
    level deep — or a relative path with subdirectories (e.g. "bin/bg3.exe")
    which is checked as an exact relative path under each game folder.

    The search is case-insensitive on the exe name to handle Linux/Proton layouts.
    """
    exe_lower = exe_name.lower()
    has_subdir = "/" in exe_name or "\\" in exe_name

    for common in libraries:
        try:
            for game_dir in common.iterdir():
                if not game_dir.is_dir():
                    continue
                if has_subdir:
                    # Check as a relative path: <GameFolder>/bin/bg3.exe
                    candidate = game_dir / exe_name
                    if candidate.is_file():
                        return game_dir
                    # Case-insensitive fallback: walk the subpath segments
                    parts = exe_lower.replace("\\", "/").split("/")
                    cur = game_dir
                    for part in parts:
                        match = None
                        try:
                            for entry in cur.iterdir():
                                if entry.name.lower() == part:
                                    match = entry
                                    break
                        except PermissionError:
                            break
                        if match is None:
                            break
                        cur = match
                    else:
                        if cur.is_file():
                            return game_dir
                else:
                    for entry in game_dir.iterdir():
                        if entry.name.lower() == exe_lower and entry.is_file():
                            return game_dir
        except PermissionError:
            continue

    return None


def scan_drives_for_exe(exe_names: list[str],
                        stop_event: "threading.Event | None" = None) -> Path | None:
    """Scan all mounted drives for any of *exe_names*, stopping at first match.

    Walks every real (non-pseudo) mount point from /proc/mounts, fanning the
    top-level subtree walks out across a thread pool so a big multi-drive scan
    doesn't run serially. Matching is on the bare filename (case-sensitive, to
    match the Tk behaviour); *exe_names* entries with sub-paths are matched on
    their final component. Returns the directory holding the exe, or None.

    Pass *stop_event* to allow an external caller (e.g. a closing dialog) to
    abort the walk early.
    """
    import concurrent.futures

    names = {Path(e.replace("\\", "/")).name for e in exe_names if e}
    if not names:
        return None

    skip_types = {"sysfs", "proc", "devtmpfs", "devpts", "tmpfs", "cgroup",
                  "cgroup2", "pstore", "bpf", "tracefs", "debugfs",
                  "securityfs", "fusectl", "hugetlbfs", "mqueue", "configfs",
                  "efivarfs", "overlay", "squashfs"}
    skip_dirs = {"proc", "sys", "dev", "run", "snap"}

    roots: list[Path] = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                fstype = parts[2]
                mountpoint = parts[1]
                if fstype in skip_types:
                    continue
                p = Path(mountpoint)
                if p == Path("/"):
                    roots.insert(0, p)  # scan root first
                else:
                    roots.append(p)
    except OSError:
        roots = [Path("/")]

    if stop_event is None:
        stop_event = threading.Event()

    def _scan_subtree(start: Path) -> Path | None:
        for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
            if stop_event.is_set():
                return None
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            if names & set(filenames):
                return Path(dirpath)
        return None

    scan_roots: list[Path] = []
    for root in roots:
        try:
            scan_roots.extend(
                p for p in root.iterdir()
                if p.is_dir() and p.name not in skip_dirs)
        except (PermissionError, OSError):
            pass

    found: Path | None = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_scan_subtree, sr): sr for sr in scan_roots}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result is not None:
                found = result
                stop_event.set()
                break

    return found


def find_wine() -> tuple[str, Path]:
    """
    Finds a wine binary and the Proton root.
    This will first search for wine64 (Proton 9, 10), then fallback to wine (Proton 11+).
    Returns (wine_path_str, proton_files_dir).
    """
    proton_script = find_any_installed_proton()
    if proton_script is None:
        raise RuntimeError("No Proton/Wine installation found. Install proton via Steam.")
    files_dir = proton_script.parent / "files"
    wine = files_dir / "bin" / "wine64"
    if not wine.is_file():
        wine = files_dir / "bin" / "wine"
        if not wine.is_file():
            raise RuntimeError(f"wine/wine64 not found at expected path in {files_dir / 'bin'}")
    return str(wine), files_dir
