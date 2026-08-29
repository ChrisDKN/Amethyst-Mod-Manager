"""
Utils/protontricks.py
Helpers for running protontricks commands (native or flatpak),
and winetricks via the bundled copy in the manager's tools folder.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable

from Utils.app_log import safe_log as _safe_log

from Utils.appimage_env import in_appimage, strip_appimage_vars


def strip_appimage_env(env: dict) -> dict:
    """Return *env* with AppImage-injected loader/bundle vars removed.

    No-op outside the AppImage (from-source / flatpak callers keep their env
    untouched). When bundled, these vars point into the AppImage's FUSE mount;
    passing them to a Proton subprocess makes it load the bundle's libraries
    and its blocked LD_LIBRARY_PATH sentinel instead of the host's, breaking
    Proton's Vulkan GPU probe on startup.

    The var list and strip logic live in :mod:`Utils.appimage_env` - the
    single source of truth shared with ``xdg.host_env``.
    """
    if not in_appimage():
        return env
    return strip_appimage_vars(env)


_WINETRICKS_URL = "https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks"
_CABEXTRACT_URL = "https://archlinux.org/packages/extra/x86_64/cabextract/download/"

# d3dcompiler_47: prebuilt DLLs from Mozilla's fxc2 repo (Windows 8.1 SDK redist).
# The winetricks/protontricks d3dcompiler_47 verb installs an older Win7-era DLL
# that lacks SM5.x extended typed UAV loads, which makes Community Shaders / ENB
# fail to compile with "error X3676: typed UAV loads are only allowed for
# single-component 32-bit element types". The fxc2 8.1 build supports it.
_D3DCOMPILER_47_64_URL = "https://github.com/mozilla/fxc2/raw/master/dll/d3dcompiler_47.dll"
_D3DCOMPILER_47_64_SHA256 = "4432bbd1a390874f3f0a503d45cc48d346abc3a8c0213c289f4b615bf0ee84f3"
_D3DCOMPILER_47_32_URL = "https://github.com/mozilla/fxc2/raw/master/dll/d3dcompiler_47_32.dll"
_D3DCOMPILER_47_32_SHA256 = "2ad0d4987fc4624566b190e747c9d95038443956ed816abfd1e2d389b5ec0851"

_DEPS_FILE = "amethyst_deps.json"

D3D_DEP_KEY = "d3dcompiler_47"
VCREDIST_DEP_KEY = "vcredist_x64"


def dotnet_dep_key(version: str) -> str:
    """Marker key for a .NET WindowsDesktop runtime version (e.g. '8' → 'dotnet8_windowsdesktop')."""
    return f"dotnet{version}_windowsdesktop"


def winetricks_verb_dep_key(verb: str) -> str:
    """Marker key for a generic winetricks verb (e.g. 'fontsmooth=rgb' → 'wt_fontsmooth_rgb')."""
    return "wt_" + re.sub(r"\W+", "_", verb).strip("_")


# Marker key → the Utils.prefix_health component token that can confirm it by
# reading the prefix. Deliberately mapped HERE rather than in prefix_health, so
# the import stays one-way (protontricks → prefix_health, never the reverse)
# and is_dep_installed can never recurse into itself.
_DETECTABLE_DEPS: dict[str, str] = {
    VCREDIST_DEP_KEY: "vcredist",
    D3D_DEP_KEY: "d3dcompiler_47",
    winetricks_verb_dep_key("lavfilters"): "lavfilters",
    **{winetricks_verb_dep_key(_v): _v for _v in (
        "d3dx9_43", "d3dx11_43", "d3dcompiler_43",
        "d3dcompiler_42", "d3dcompiler_46", "d3dx10_43", "d3dx11_42",
        "d3dx9", "d3dx10", "quartz", "dx8vb",
    )},
}

# Tokens that are plain winetricks verbs - no bespoke installer, no ordering
# constraints. Used both by auto_install_deps and by the health overlay's Fix
# buttons, so the two can never disagree about how a component is installed.
#
# Deliberately excludes dxvk: Proton ships its own DXVK, and the winetricks
# verb overwrites it with an older bundled build. vcrun2022 is excluded too -
# it is the same runtime our vcredist installer already provides (vc_redist.x64).
WINETRICKS_VERB_DEPS: frozenset[str] = frozenset((
    "d3dx9_43", "d3dx11_43", "d3dcompiler_43",
    "d3dcompiler_42", "d3dcompiler_46", "d3dx10_43", "d3dx11_42",
    "d3dx9", "d3dx10", "quartz", "dx8vb",
))


def _deps_file(prefix_path: Path) -> Path:
    return prefix_path.parent / _DEPS_FILE


def read_installed_deps(prefix_path: Path) -> list[str]:
    """Return the list of components recorded as installed in *prefix_path*."""
    try:
        return json.loads(_deps_file(prefix_path).read_text(encoding="utf-8")).get("installed", [])
    except (OSError, ValueError):
        return []


def is_dep_installed(prefix_path: Path, key: str) -> bool:
    """True when *key* is recorded in amethyst_deps.json OR really in the prefix.

    The marker only exists for prefixes Amethyst itself provisioned, and only
    since the marker was introduced. Prefixes built by hand with winetricks /
    protontricks, or by another manager, would otherwise be re-installed on
    every add-game and save. For the components we can positively identify on
    disk, fall back to reading the prefix (see Utils.prefix_health).

    Read-only by design: a predicate must not write into the user's prefix.
    Self-healing the marker here would create amethyst_deps.json outside
    Heroic/Lutris/Faugus prefixes (``_deps_file`` is the prefix's *sibling*),
    destroy the file's "did Amethyst install this?" meaning, and race the
    background dependency worker through an unlocked read-modify-write.
    """
    if key in read_installed_deps(prefix_path):
        return True
    token = _DETECTABLE_DEPS.get(key)
    if token is None:
        match = re.fullmatch(r"dotnet(\d+)_windowsdesktop", key)
        if match:
            token = f"dotnet{match.group(1)}"
    if token is None:
        return False
    try:
        from Utils.prefix_health import detect_component
        # `is True` keeps "cannot tell" (None) behaving as before: install it.
        return detect_component(token, Path(prefix_path)) is True
    except Exception:
        return False        # detection must never break an installer path


def mark_dep_installed(prefix_path: Path, key: str) -> None:
    f = _deps_file(prefix_path)
    try:
        data: dict = {}
        if f.is_file():
            data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    installed: list = data.get("installed", [])
    if key not in installed:
        installed.append(key)
    data["installed"] = installed
    try:
        f.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def _get_tools_dir() -> Path:
    from Utils.config_paths import get_tools_dir
    return get_tools_dir()


def _bundled_winetricks() -> Path:
    return _get_tools_dir() / "winetricks"


def _bundled_cabextract() -> Path:
    return _get_tools_dir() / "cabextract"


def winetricks_installed() -> bool:
    """Return True if winetricks is present in the manager's tools folder."""
    return _bundled_winetricks().is_file()


def cabextract_installed() -> bool:
    """Return True if cabextract is available (system PATH or bundled)."""
    return shutil.which("cabextract") is not None or _bundled_cabextract().is_file()


def install_cabextract(log_fn: Callable[[str], None] | None = None) -> bool:
    """Download a portable cabextract binary into the manager's tools folder."""
    _log = _safe_log(log_fn)
    dest = _bundled_cabextract()
    _log("Downloading cabextract …")
    try:
        import zstandard
    except ImportError as exc:
        _log(f"cabextract install needs the 'zstandard' Python module: {exc}")
        return False
    try:
        req = urllib.request.Request(
            _CABEXTRACT_URL,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        from Utils.ca_bundle import get_ssl_context
        with urllib.request.urlopen(req, timeout=60, context=get_ssl_context()) as resp:
            pkg_bytes = resp.read()
        dctx = zstandard.ZstdDecompressor()
        raw = dctx.stream_reader(io.BytesIO(pkg_bytes))
        with tarfile.open(fileobj=raw, mode="r|") as tf:
            for member in tf:
                if member.name == "usr/bin/cabextract" and member.isfile():
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    dest.write_bytes(extracted.read())
                    break
            else:
                _log("cabextract binary not found inside the downloaded package.")
                return False
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _log(f"cabextract installed to {dest}.")
        return True
    except Exception as exc:
        _log(f"cabextract download failed: {exc}")
        return False


def install_winetricks(log_fn: Callable[[str], None] | None = None) -> bool:
    """Download winetricks into the manager's tools folder.

    Returns True on success, False on failure.
    """
    _log = _safe_log(log_fn)
    dest = _bundled_winetricks()
    _log("Downloading winetricks …")
    try:
        req = urllib.request.Request(
            _WINETRICKS_URL,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        from Utils.ca_bundle import get_ssl_context
        with urllib.request.urlopen(req, timeout=30, context=get_ssl_context()) as resp:
            data = resp.read()
        dest.write_bytes(data)
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _log(f"winetricks installed to {dest}.")
        return True
    except Exception as exc:
        _log(f"winetricks download failed: {exc}")
        return False


def _get_proton_bin() -> str | None:
    """Return the bin/ path of the newest available Proton installation, or None.

    Uses steam_finder's discovery so non-standard layouts (~/.steam/steam,
    Snap, extra libraries, compatibilitytools.d) all work. Flatpak-Steam
    Protons are skipped: their wine binaries need the Steam sandbox's runtime
    libraries and break when run bare (winetricks runs wine directly).
    """
    from Utils.steam_finder import (
        list_installed_proton,
        _proton_script_in_steam_flatpak,
    )
    for script in list_installed_proton():
        if _proton_script_in_steam_flatpak(script):
            continue
        for sub in ("files/bin", "dist/bin"):
            cand = script.parent / sub
            if (cand / "wine").is_file():
                return str(cand)
    return None


def wine_bin_dir_for_prefix(prefix_path, env: dict | None = None) -> str | None:
    """bin/ dir of the lutris-wine runner when *prefix_path* is a Lutris
    classic-wine prefix, else None (callers fall back to ``_get_proton_bin``).

    winetricks runs ``wine`` off PATH, so the runner whose libraries built the
    prefix must come first. When *env* is given, the runner's lib dirs are
    also prepended to its LD_LIBRARY_PATH (Lutris injects those itself when
    it launches the runner).
    """
    try:
        from Utils.lutris_finder import (
            is_lutris_prefix, find_lutris_wine_for_prefix, lutris_wine_env)
        if not is_lutris_prefix(prefix_path):
            return None
        wine_bin = find_lutris_wine_for_prefix(prefix_path)
        if wine_bin is None:
            return None
        if env is not None:
            extra = lutris_wine_env(wine_bin, prefix_path)
            if "LD_LIBRARY_PATH" in extra:
                env["LD_LIBRARY_PATH"] = extra["LD_LIBRARY_PATH"]
        return str(wine_bin.parent)
    except Exception:
        return None


def _host_spawn_prefix() -> list[str] | None:
    """Command prefix to run a program on the host system.

    Returns ``[]`` outside a Flatpak sandbox (run directly), the
    ``flatpak-spawn --host`` prefix inside one, or None when the host is
    unreachable (sandboxed without flatpak-spawn / the Flatpak portal).
    ``--directory=/`` avoids inheriting a sandbox-only cwd on the host.
    """
    if not os.path.exists("/.flatpak-info"):
        return []
    if shutil.which("flatpak-spawn"):
        return ["flatpak-spawn", "--host", "--directory=/"]
    return None


def _resolve_protontricks() -> list[str] | None:
    """Command prefix to invoke protontricks (native or flatpak), or None.

    Inside our own Flatpak sandbox neither ``shutil.which("protontricks")``
    nor the ``flatpak`` CLI can see the host, so both probes go through
    ``flatpak-spawn --host`` (flatpak-spawn exits 127 when the host binary
    is missing, which the ``!= 0`` checks treat as unavailable).
    """
    host = _host_spawn_prefix()
    if host is None:
        return None
    if not host:
        if shutil.which("protontricks") is not None:
            return ["protontricks"]
        if shutil.which("flatpak") is not None and subprocess.run(
            ["flatpak", "info", "com.github.Matoking.protontricks"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0:
            return ["flatpak", "run", "com.github.Matoking.protontricks"]
        return None
    if subprocess.run(
        [*host, "sh", "-c", "command -v protontricks"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0:
        return [*host, "protontricks"]
    if subprocess.run(
        [*host, "flatpak", "info", "com.github.Matoking.protontricks"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0:
        return [*host, "flatpak", "run", "com.github.Matoking.protontricks"]
    return None


def _get_protontricks_cmd(steam_id: str) -> list[str] | None:
    """Return the protontricks command prefix for *steam_id*, or None if not found."""
    base = _resolve_protontricks()
    return [*base, steam_id] if base else None


def _install_via_winetricks(
    prefix_path: Path,
    component: str,
    log_fn: Callable[[str], None],
    timeout: int = 300,
) -> bool:
    """Install *component* directly via the bundled winetricks using WINEPREFIX."""
    if not _bundled_winetricks().is_file():
        log_fn("winetricks not found - downloading it now …")
        if not install_winetricks(log_fn=log_fn):
            return False

    if not cabextract_installed():
        log_fn("cabextract not found - downloading a portable copy now …")
        if not install_cabextract(log_fn=log_fn):
            return False

    winetricks = str(_bundled_winetricks())

    env = strip_appimage_env(os.environ.copy())
    env["WINEPREFIX"] = str(prefix_path)

    path_prefix = str(_get_tools_dir())
    proton_bin = wine_bin_dir_for_prefix(prefix_path, env) or _get_proton_bin()
    if proton_bin:
        path_prefix = proton_bin + os.pathsep + path_prefix
    env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")

    log_fn(f"Installing {component} via winetricks (this may take a minute) …")
    try:
        # -q = unattended: suppresses the per-DLL regsvr32 success dialogs
        # (xact alone pops ~a dozen) and makes verb installers run silent.
        result = subprocess.run(
            [winetricks, "-q", component],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if result.returncode == 0:
            log_fn(f"{component} installed successfully.")
            return True
        else:
            log_fn(f"{component} install failed: {result.stderr or result.stdout or 'unknown error'}")
            return False
    except subprocess.TimeoutExpired:
        log_fn(f"{component} install timed out after {timeout // 60} minutes.")
        return False
    except Exception as exc:
        log_fn(f"{component} error: {exc}")
        return False


def _install_via_protontricks(
    steam_id: str,
    component: str,
    log_fn: Callable[[str], None],
    timeout: int = 300,
) -> bool:
    """Install *component* via system protontricks against *steam_id*."""
    cmd = _get_protontricks_cmd(steam_id)
    if cmd is None:
        return False
    # -q = unattended (forwarded to winetricks inside the prefix) - no
    # regsvr32 popups, silent verb installers.
    cmd = cmd + ["-q", component]
    log_fn(f"Installing {component} via protontricks (this may take a minute) …")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            log_fn(f"{component} installed successfully.")
            return True
        log_fn(f"{component} install failed: {result.stderr or result.stdout or 'unknown error'}")
        return False
    except subprocess.TimeoutExpired:
        log_fn(f"{component} install timed out after {timeout // 60} minutes.")
        return False
    except Exception as exc:
        log_fn(f"{component} error: {exc}")
        return False


def install_winetricks_verb(
    game,
    verb: str,
    log_fn: Callable[[str], None] | None = None,
    *,
    timeout: int = 300,
) -> bool:
    """Install a generic winetricks *verb* into *game*'s Proton prefix.

    Uses the bundled winetricks with WINEPREFIX first (self-contained - no
    protontricks needed on the system), falling back to system protontricks
    against the game's Steam ID when winetricks fails or no prefix path is
    configured. Success is recorded in the prefix's amethyst_deps.json so
    repeat calls skip instantly. *timeout* is per attempt - pass a large
    value for slow verbs like dotnet48.
    """
    _log = _safe_log(log_fn)
    get_prefix = getattr(game, "get_prefix_path", None)
    prefix = get_prefix() if callable(get_prefix) else None
    if prefix is not None and not Path(prefix).is_dir():
        prefix = None

    key = winetricks_verb_dep_key(verb)
    if prefix is not None and is_dep_installed(Path(prefix), key):
        _log(f"{verb} already installed in this prefix - skipping.")
        return True

    def _mark():
        if prefix is not None:
            mark_dep_installed(Path(prefix), key)

    if prefix is not None:
        if _install_via_winetricks(Path(prefix), verb, _log, timeout):
            _mark()
            return True
        _log("Falling back to protontricks …")

    from Utils.steam_finder import game_steam_id
    steam_id = game_steam_id(game)
    if steam_id and _get_protontricks_cmd(steam_id) is not None:
        if _install_via_protontricks(steam_id, verb, _log, timeout):
            _mark()
            return True
        return False

    if prefix is None:
        _log(f"{verb}: no prefix path or working protontricks available - cannot install.")
    return False


def _download_verified(url: str, sha256: str, log_fn: Callable[[str], None]) -> bytes | None:
    """Download *url* and return its bytes if the SHA-256 matches, else None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        from Utils.ca_bundle import get_ssl_context
        with urllib.request.urlopen(req, timeout=60, context=get_ssl_context()) as resp:
            data = resp.read()
    except Exception as exc:
        log_fn(f"Download failed for {url}: {exc}")
        return None
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        log_fn(f"Checksum mismatch for {url} (got {digest}).")
        return None
    return data


def _install_d3dcompiler_47_fxc2(prefix_path: Path, log_fn: Callable[[str], None]) -> bool:
    """Drop the Mozilla fxc2 (Win 8.1 SDK) d3dcompiler_47 DLLs into the prefix.

    64-bit → system32, 32-bit → syswow64 - the standard Wine wow64 layout.
    This is the build that supports SM5.x extended typed UAV loads, which the
    winetricks verb's older DLL does not (causes shader error X3676).
    """
    win = prefix_path / "drive_c" / "windows"
    sys32 = win / "system32"
    syswow64 = win / "syswow64"
    if not sys32.is_dir():
        log_fn(f"Prefix has no system32 dir at {sys32}.")
        return False

    log_fn("Installing d3dcompiler_47 (Mozilla fxc2, Win 8.1 SDK) …")
    data64 = _download_verified(_D3DCOMPILER_47_64_URL, _D3DCOMPILER_47_64_SHA256, log_fn)
    if data64 is None:
        return False
    try:
        (sys32 / "d3dcompiler_47.dll").write_bytes(data64)
    except OSError as exc:
        log_fn(f"Failed to write d3dcompiler_47.dll to system32: {exc}")
        return False

    # 32-bit DLL is best-effort: only matters if the prefix has a syswow64.
    if syswow64.is_dir():
        data32 = _download_verified(_D3DCOMPILER_47_32_URL, _D3DCOMPILER_47_32_SHA256, log_fn)
        if data32 is not None:
            try:
                (syswow64 / "d3dcompiler_47.dll").write_bytes(data32)
            except OSError as exc:
                log_fn(f"Failed to write 32-bit d3dcompiler_47.dll to syswow64: {exc}")

    from Utils.deploy_wine_dll import apply_wine_dll_overrides
    apply_wine_dll_overrides(prefix_path, {"d3dcompiler_47": "native"}, log_fn=log_fn)
    log_fn("d3dcompiler_47 installed (fxc2).")
    return True


def install_d3dcompiler_47(
    steam_id: str,
    log_fn: Callable[[str], None] | None = None,
    prefix_path: "Path | None" = None,
) -> bool:
    """Install d3dcompiler_47 into the game's Proton prefix.

    Prefers dropping the Mozilla fxc2 (Win 8.1 SDK) DLL directly, which supports
    SM5.x extended typed UAV loads (the winetricks verb installs an older DLL
    that fails to compile Community Shaders / ENB with error X3676). Falls back
    to protontricks/winetricks only if the direct install can't run. Records
    success in the prefix's amethyst_deps.json so other wizards can skip it.
    """
    _log = _safe_log(log_fn)
    prefix = Path(prefix_path) if prefix_path else None

    def _mark():
        if prefix and prefix.is_dir():
            mark_dep_installed(prefix, D3D_DEP_KEY)

    if prefix and prefix.is_dir():
        if _install_d3dcompiler_47_fxc2(prefix, _log):
            _mark()
            return True
        _log("fxc2 install failed - falling back to protontricks/winetricks "
             "(note: that DLL may not support Community Shaders / ENB).")

    if steam_id and _get_protontricks_cmd(steam_id) is not None:
        if _install_via_protontricks(steam_id, "d3dcompiler_47", _log):
            _mark()
            return True
        _log("Falling back to bundled winetricks …")

    if prefix and prefix.is_dir():
        if _install_via_winetricks(prefix, "d3dcompiler_47", _log):
            _mark()
            return True
        return False

    _log("d3dcompiler_47: no prefix path or working protontricks available - cannot install.")
    return False


# --- shared silent-installer runner ----------------------------------------
# Silent in-prefix installers run under ``proton runinprefix``, which never
# returns if wine wedges - the app then sits on a dead progress bar forever
# (GH#333). Cap them: on a healthy prefix these finish in seconds, so two
# minutes is already generous.
PREFIX_INSTALLER_TIMEOUT_S = 120
_HEARTBEAT_S = 20


def _kill_installer(proc: "subprocess.Popen") -> None:
    """Kill a wedged installer's whole process group - Proton spawns children."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run_prefix_installer(
    cmd: list[str],
    env: dict,
    cwd: "str | Path",
    *,
    label: str,
    log_fn: Callable[[str], None] | None = None,
    timeout: int = PREFIX_INSTALLER_TIMEOUT_S,
    proton_script: "Path | str | None" = None,
    compat_data: "Path | str | None" = None,
) -> "tuple[int | None, str]":
    """Run a silent in-prefix installer under a hard timeout.

    Returns ``(exit_code, captured_output)``, or ``(None, output)`` when
    *timeout* elapsed - in that case the process group is killed and the
    prefix's wineserver shut down, so a wedged run can't block the next
    attempt. Wine's chatter is captured rather than leaking into the app's
    stdout; callers log the tail when the exit code says something went wrong.
    A heartbeat line every :data:`_HEARTBEAT_S` keeps a slow-but-working
    install from looking dead in the log.
    """
    _log = _safe_log(log_fn)
    # NamedTemporaryFile, NOT TemporaryFile: the latter is an anonymous
    # O_TMPFILE fd on Linux, and 32-bit wine startup segfaults (exit 245, NULL
    # read in host libc) when its stdout is a nameless fd - verified against
    # GE-Proton10-33. Any *named* file works.
    out = tempfile.NamedTemporaryFile(
        mode="w+", encoding="utf-8", errors="replace", prefix="amm-installer-")
    try:
        proc = subprocess.Popen(
            cmd, env=env, cwd=str(cwd), stdin=subprocess.DEVNULL,
            stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
        )
        started = time.monotonic()
        next_beat = started + _HEARTBEAT_S
        rc: "int | None" = None
        while True:
            try:
                rc = proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if now - started >= timeout:
                break
            if now >= next_beat:
                next_beat = now + _HEARTBEAT_S
                _log(f"{label}: still working ({int(now - started)}s elapsed) …")
        if rc is None:
            _log(f"{label}: no response after {timeout}s - aborting and shutting "
                 "down the prefix's wine processes.")
            _kill_installer(proc)
            if proton_script is not None and compat_data is not None:
                try:
                    from Utils.exe_launch import shutdown_prefix_wineserver
                    shutdown_prefix_wineserver(Path(proton_script), Path(compat_data))
                except Exception:
                    pass
        out.seek(0)
        return rc, out.read()[-4000:].strip()
    finally:
        out.close()


# --- prefix / Proton version mismatch preflight ----------------------------
_PREFIX_VER_RE = re.compile(r'^CURRENT_PREFIX_VERSION\s*=\s*"([^"]+)"', re.M)


def _proton_prefix_version(proton_script: "Path | str") -> "str | None":
    """The ``CURRENT_PREFIX_VERSION`` *proton_script* declares, e.g. '9.0-203'."""
    script = Path(proton_script)
    if script.name in ("wine", "wine64"):
        return None                     # classic lutris-wine: no proton script
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _PREFIX_VER_RE.search(text)
    return m.group(1) if m else None


def _numeric_prefix_version(ver: str) -> "tuple[int, int] | None":
    """``(major, minor)`` of a stock Proton prefix version ('9.0-203' → (9, 0)).

    None for GE-Proton-style versions ('GE-Proton10-33'), which Proton's own
    ``upgrade_pfx`` cannot order either - those skip the mismatch check.
    """
    head, sep, _build = ver.partition("-")
    if not sep or "." not in head:
        return None
    maj, _, minor = head.partition(".")
    try:
        return int(maj), int(minor)
    except ValueError:
        return None


def prefix_downgrade_warning(
    proton_script: "Path | str", compat_data: "Path | str | None",
) -> "str | None":
    """Explain why a silent install would hang here, or None when it's safe.

    ``proton runinprefix`` is dispatched with ``init_session(False)``, and that
    flag is the only thing gating ``setup_prefix()`` - the sole caller of
    ``upgrade_pfx()``. So when a prefix built by a NEWER Proton is driven by an
    OLDER one, the "Removing newer prefix" downgrade Proton normally performs
    on a version change never runs for our silent installers, and the old wine
    executes against the newer build's drive_c and registry. That combination
    hangs forever instead of failing (GH#333: Proton 9.0-4 on a Proton 10
    prefix). Launching the game once under the older Proton does the downgrade.
    """
    if not compat_data:
        return None
    try:
        old_ver = (Path(compat_data) / "version").read_text(
            encoding="utf-8", errors="replace").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    new_ver = _proton_prefix_version(proton_script)
    if not new_ver or old_ver == new_ver:
        return None
    old = _numeric_prefix_version(old_ver)
    new = _numeric_prefix_version(new_ver)
    if old is None or new is None or new >= old:
        return None
    # Report the major only - Proton's minor is an internal prefix-schema
    # number ("10.1000"), meaningless to users; the raw strings disambiguate.
    return (
        f"this prefix was built by Proton {old[0]} ({old_ver}) but the game is "
        f"set to Proton {new[0]} ({new_ver}). Proton only downgrades a prefix "
        "when the game itself launches, so a silent install here would hang. "
        f"Launch the game once under Proton {new[0]} (or reset the prefix), "
        "then run this again."
    )


_VCREDIST_URL = "https://aka.ms/vc14/vc_redist.x64.exe"


def build_proton_env_for_game(game) -> "tuple[Path, dict] | tuple[None, None]":
    """Resolve the Proton script + environment for running an installer in a
    game's prefix, mirroring the Proton Tools menu (gui.dialogs._get_proton_env).

    Returns (proton_script, env) on success or (None, None) when no usable
    Proton install / prefix can be found. The env is suitable for
    ``python3 <proton_script> run <installer.exe> …``.
    """
    from Utils.steam_finder import (
        find_any_installed_proton,
        find_proton_for_game,
        find_steam_root_for_proton_script,
        game_steam_id,
    )

    get_prefix = getattr(game, "get_prefix_path", None)
    prefix_path = get_prefix() if callable(get_prefix) else None
    if prefix_path is None or not prefix_path.is_dir():
        return None, None

    # winecfg's "Show dot files", matching the other prefix-env resolvers so
    # installers run from here get file dialogs that can see the manager's
    # dot-dirs. Applied before anything is launched into the prefix.
    from Utils.deploy_wine_dll import set_show_dot_files
    set_show_dot_files(prefix_path)

    # Classic lutris-wine prefixes run the installer with the Lutris runner's
    # own wine binary (proton_run_command handles the wine-binary form).
    from Utils.proton_tools import _resolve_lutris_wine_env
    wine_bin, wenv = _resolve_lutris_wine_env(prefix_path)
    if wine_bin is not None:
        return wine_bin, wenv

    steam_id = game_steam_id(game)
    proton_script = find_proton_for_game(steam_id) if steam_id else None

    from Utils.proton_prefix import read_prefix_runner as _read_prefix_runner, \
        resolve_compat_data as _resolve_compat_data
    compat_data = _resolve_compat_data(prefix_path)

    if proton_script is None:
        try:
            from Utils.heroic_finder import find_heroic_proton_for_prefix
            proton_script = find_heroic_proton_for_prefix(prefix_path)
        except Exception:
            proton_script = None

    if proton_script is None:
        try:
            from Utils.lutris_finder import find_lutris_proton_name_for_prefix
            lutris_runner = find_lutris_proton_name_for_prefix(prefix_path)
        except Exception:
            lutris_runner = None
        if lutris_runner:
            proton_script = find_any_installed_proton(lutris_runner)

    if proton_script is None:
        try:
            from Utils.faugus_finder import find_faugus_proton_for_prefix
            proton_script = find_faugus_proton_for_prefix(prefix_path)
        except Exception:
            proton_script = None

    if proton_script is None:
        proton_script = find_any_installed_proton(_read_prefix_runner(compat_data))
        if proton_script is None:
            return None, None

    steam_root = find_steam_root_for_proton_script(proton_script)
    if steam_root is None:
        return None, None

    env = strip_appimage_env(os.environ.copy())
    env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
    game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
    if game_path:
        env["STEAM_COMPAT_INSTALL_PATH"] = str(game_path)
    if steam_id:
        env.setdefault("SteamAppId", steam_id)
        env.setdefault("SteamGameId", steam_id)
    return proton_script, env


def install_vcredist(
    proton_script: "Path",
    env: dict,
    log_fn: Callable[[str], None] | None = None,
    prefix_path: "Path | None" = None,
) -> bool:
    """Install the VC++ Redistributable silently into the prefix via Proton.

    Downloads (and caches) Microsoft's official ``vc_redist.x64.exe`` and runs
    it with ``/install /quiet /norestart`` through ``proton runinprefix`` - the
    exact mechanism the Proton Tools menu uses (runinprefix skips the steam.exe
    shim, so the silent install doesn't show the game as "Running" in Steam).
    Records success in the prefix's amethyst_deps.json so other callers can
    skip a re-install.
    """
    _log = _safe_log(log_fn)
    from Utils.config_paths import get_vcredist_cache_path

    # Flatpak without the Compat.i386 extension: wine would die with the
    # cryptic "/lib/ld-linux.so.2: could not open" - fail with the fix instead.
    from Utils.flatpak_i386 import preflight_i386_error
    i386_err = preflight_i386_error(proton_script)
    if i386_err:
        _log(f"VC++ Redistributable: {i386_err}")
        return False

    # Older Proton driving a newer prefix hangs under runinprefix - refuse with
    # the fix instead of sitting on a dead progress bar (GH#333).
    compat_data = env.get("STEAM_COMPAT_DATA_PATH")
    stale_prefix = prefix_downgrade_warning(proton_script, compat_data)
    if stale_prefix:
        _log(f"VC++ Redistributable: {stale_prefix}")
        return False

    cache_path = get_vcredist_cache_path()
    try:
        if not cache_path.is_file():
            _log("Downloading VC++ Redistributable …")
            from Utils.ca_bundle import download_file
            download_file(_VCREDIST_URL, cache_path)
            _log("Download complete.")
        else:
            _log("Using cached VC++ Redistributable installer.")
        _log("Installing VC++ Redistributable in game prefix (silent) - please wait …")
        from Utils.steam_finder import proton_run_command
        rc, output = run_prefix_installer(
            proton_run_command(proton_script, "runinprefix",
             str(cache_path), "/install", "/quiet", "/norestart",
             env=env),
            env, cache_path.parent,
            label="VC++ Redistributable", log_fn=_log,
            proton_script=proton_script, compat_data=compat_data,
        )
        if rc is None:
            return False                # run_prefix_installer logged the abort
        # 0 = success, 1638 = already installed, 3010 = reboot required, 1641 = reboot initiated
        if rc in {0, 1638, 3010, 1641}:
            _log(f"VC++ Redistributable installed (exit {rc}).")
            if prefix_path and Path(prefix_path).is_dir():
                mark_dep_installed(Path(prefix_path), VCREDIST_DEP_KEY)
            return True
        _log(f"VC++ Redistributable installer exited with code {rc}.")
        if output:
            _log(f"VC++ Redistributable output:\n{output}")
        return False
    except Exception as exc:
        _log(f"VC++ Redistributable install error: {exc}")
        return False
