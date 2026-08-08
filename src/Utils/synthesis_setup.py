"""Neutral (GUI-free) Mutagen Synthesis install/setup/launch pipeline.

Extracted verbatim from the Tk ``bethesda_synthesis`` plugin so the heavy
Wine/.NET prefix bootstrap, plugins/My-Games symlinking, deploy orchestration
and Proton launch can be driven by the Qt view (and unit-tested) without any
GUI toolkit.  No tkinter or Qt imports here.

Pipeline:
  1. ``fetch_latest_synthesis_asset`` + ``download_and_extract_synthesis`` - get
     the latest Synthesis release into ``Applications/Synthesis/``.
  2. ``list_proton`` / ``load_saved_proton`` / ``save_proton`` - per-game Proton
     choice persisted in ``synthesis.ini``.
  3. ``setup_synthesis_prefix`` - idempotent (per-step ``.done`` markers) Wine
     prefix bootstrap: mscoree cleanup, win11 version, vcredist, the .NET
     SDK/Desktop runtimes, cert bundles, regedit/xEdit DLL overrides, fonts,
     nuget config, game-path registration.
  4. ``launch_synthesis`` - symlink the profile's plugins.txt + My Games into
     the prefix, deploy the active profile, run ``Synthesis.exe`` via
     ``proton run`` and clean up the symlinks afterwards.
"""

from __future__ import annotations

import base64
import configparser
import hashlib
import os
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.request
import json as _json
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from Utils.config_paths import (
    get_dotnet_cache_dir,
    get_game_config_dir,
    get_vcredist_cache_path,
)
from Utils.protontricks import strip_appimage_env
from Utils.steam_finder import list_installed_proton

if TYPE_CHECKING:
    from Games.base_game import BaseGame

LogFn = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


APP_DIR_NAME = "Synthesis"
EXE_NAME = "Synthesis.exe"
_GITHUB_API = "https://api.github.com/repos/Mutagen-Modding/Synthesis/releases/latest"
_INI_SECTION = "synthesis"
_INI_PROTON_KEY = "proton"


# ---------------------------------------------------------------------------
# Download URLs (kept in sync with .NET LTS / current releases)
# ---------------------------------------------------------------------------

_DOTNET10_SDK_URL = (
    "https://builds.dotnet.microsoft.com/dotnet/Sdk/10.0.302/"
    "dotnet-sdk-10.0.302-win-x64.exe"
)
_DOTNET10_SDK_FILENAME = "dotnet-sdk-10.0.302-win-x64.exe"

_DOTNET10_DESKTOP_URL = (
    "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/10.0.2/"
    "windowsdesktop-runtime-10.0.2-win-x64.exe"
)
_DOTNET10_DESKTOP_FILENAME = "windowsdesktop-runtime-10.0.2-win-x64.exe"

_DOTNET8_DESKTOP_URL = (
    "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/8.0.11/"
    "windowsdesktop-runtime-8.0.11-win-x64.exe"
)
_DOTNET8_DESKTOP_FILENAME = "windowsdesktop-runtime-8.0.11-win-x64.exe"

_DOTNET7_DESKTOP_URL = (
    "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/7.0.20/"
    "windowsdesktop-runtime-7.0.20-win-x64.exe"
)
_DOTNET7_DESKTOP_FILENAME = "windowsdesktop-runtime-7.0.20-win-x64.exe"

_DOTNET6_DESKTOP_URL = (
    "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/6.0.36/"
    "windowsdesktop-runtime-6.0.36-win-x64.exe"
)
_DOTNET6_DESKTOP_FILENAME = "windowsdesktop-runtime-6.0.36-win-x64.exe"

_VCREDIST_URL = "https://aka.ms/vc14/vc_redist.x64.exe"


_XEDIT_EXECUTABLES = [
    "SSEEdit.exe", "SSEEdit64.exe",
    "FO4Edit.exe", "FO4Edit64.exe",
    "TES4Edit.exe", "TES4Edit64.exe",
    "xEdit64.exe",
    "SF1Edit64.exe",
    "FNVEdit.exe", "FNVEdit64.exe",
    "xFOEdit.exe", "xFOEdit64.exe",
    "xSFEEdit.exe", "xSFEEdit64.exe",
    "xTESEdit.exe", "xTESEdit64.exe",
    "FO3Edit.exe", "FO3Edit64.exe",
]

_DLL_OVERRIDES = [
    "dwrite", "winmm", "version", "dxgi", "dbghelp",
    "d3d12", "wininet", "winhttp", "dinput", "dinput8",
]


# ===========================================================================
# Download + extraction
# ===========================================================================

def fetch_latest_synthesis_asset() -> tuple[str, str]:
    """Return (version_tag, download_url) for the latest Synthesis zip."""
    req = urllib.request.Request(
        _GITHUB_API,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "ModManager/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = _json.loads(resp.read().decode())
    tag = data.get("tag_name", "unknown")
    for asset in data.get("assets", []):
        name: str = asset.get("name", "").lower()
        if name.endswith(".zip") and "synthesis" in name:
            return tag, asset["browser_download_url"]
    raise RuntimeError(f"No Synthesis zip in latest GitHub release ({tag}).")


def _strip_single_top_dir(tmp: Path) -> Path:
    entries = [e for e in tmp.iterdir() if e.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return tmp


def extract_zip_flat(archive: Path, dest: Path) -> None:
    """Extract *archive* into *dest*, stripping a single top-level wrapper."""
    tmp = Path(tempfile.mkdtemp())
    try:
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(tmp)
        src = _strip_single_top_dir(tmp)
        for root, _dirs, files in os.walk(src):
            for f in files:
                src_file = Path(root) / f
                rel = src_file.relative_to(src)
                dst_file = dest / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_file), str(dst_file))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def download_and_extract_synthesis(game: "BaseGame", reporthook=None,
                                   log_fn: LogFn = _noop) -> str:
    """Download the latest Synthesis zip and extract into Applications/Synthesis.

    Returns the release tag.  Raises if Synthesis.exe is missing afterward.
    """
    tag, url = fetch_latest_synthesis_asset()
    log_fn(f"Synthesis: downloading {url}")
    tmpdir = Path(tempfile.mkdtemp(prefix="synthesis_dl_"))
    archive = tmpdir / url.split("/")[-1]
    urllib.request.urlretrieve(url, archive, reporthook=reporthook)

    dest = synthesis_dir(game)
    dest.mkdir(parents=True, exist_ok=True)
    extract_zip_flat(archive, dest)
    try:
        archive.unlink()
        archive.parent.rmdir()
    except OSError:
        pass

    if not synthesis_exe(game).is_file():
        raise RuntimeError(
            f"{EXE_NAME} not found after extraction - the release asset layout "
            "may have changed.")
    return tag


# ===========================================================================
# Wine prefix bootstrap
# ===========================================================================

def _wine_bin(proton_script: Path) -> Path:
    return proton_script.parent / "files" / "bin" / "wine"


def _proton_files_dir(wine: Path) -> Path:
    return wine.parent.parent


def build_proton_env(
    pfx: Path,
    wine: Path,
    dll_overrides: str = "mshtml=d;winemenubuilder.exe=d",
) -> dict[str, str]:
    """Build the env needed to run a Wine binary against a Proton install.

    Mirrors what ``proton run`` does so Wine can find its bundled DLLs
    (icu, vkd3d, gstreamer, …) and Linux libs without Proton's sniper
    container wrapper.
    """
    files = _proton_files_dir(wine)
    env = strip_appimage_env(os.environ.copy())
    env["WINEPREFIX"] = str(pfx)
    env["WINEDEBUG"] = "-all"
    env["WINEDLLOVERRIDES"] = dll_overrides
    env.setdefault("DISPLAY", os.environ.get("DISPLAY", ":0"))

    dll_paths = [str(files / "lib" / "vkd3d"), str(files / "lib" / "wine")]
    if "WINEDLLPATH" in os.environ:
        dll_paths.append(os.environ["WINEDLLPATH"])
    env["WINEDLLPATH"] = os.pathsep.join(dll_paths)

    ld_paths = [
        str(files / "lib" / "x86_64-linux-gnu"),
        str(files / "lib" / "i386-linux-gnu"),
    ]
    if os.environ.get("LD_LIBRARY_PATH"):
        ld_paths.append(os.environ["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = ":".join(ld_paths)
    return env


def _base_env(pfx: Path, wine: Path | None = None) -> dict[str, str]:
    if wine is not None:
        return build_proton_env(pfx, wine)
    env = strip_appimage_env(os.environ.copy())
    env["WINEPREFIX"] = str(pfx)
    env["WINEDEBUG"] = "-all"
    env["WINEDLLOVERRIDES"] = "mshtml=d;winemenubuilder.exe=d"
    env.setdefault("DISPLAY", os.environ.get("DISPLAY", ":0"))
    return env


def _markers_dir(pfx: Path) -> Path:
    d = pfx / ".synthesis_setup"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_done(pfx: Path, step: str) -> bool:
    return (_markers_dir(pfx) / f"{step}.done").is_file()


def _mark_done(pfx: Path, step: str) -> None:
    (_markers_dir(pfx) / f"{step}.done").write_text("ok\n")


def _posix_to_wine_path(p: Path) -> str:
    s = str(p).replace("/", "\\")
    if not s.endswith("\\"):
        s += "\\"
    return "Z:" + s


def _download_if_missing(url: str, dest: Path, log: LogFn) -> bool:
    if dest.is_file() and dest.stat().st_size > 0:
        log(f"  cached: {dest.name}")
        return True
    log(f"  downloading {dest.name} …")
    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as exc:
        log(f"  download failed: {exc}")
        return False
    return True


def _ensure_prefix(pfx: Path, wine: Path, log: LogFn) -> bool:
    if (pfx / "system.reg").is_file():
        return True
    pfx.mkdir(parents=True, exist_ok=True)
    log("Creating Wine prefix (this can take a minute on first run) …")
    result = subprocess.run(
        [str(wine), "wineboot", "-i"],
        env=_base_env(pfx, wine),
        capture_output=True, text=True, errors="replace", timeout=300,
    )
    if result.returncode != 0:
        log(f"  wineboot exited with {result.returncode}: {result.stderr[:200]}")
        return False
    log("  prefix created.")
    return True


def _step_dotnet10_sdk(pfx: Path, wine: Path, log: LogFn) -> bool:
    if _is_done(pfx, "dotnet10_sdk"):
        log("  .NET 10 SDK already installed, skipping.")
        return True
    installer = get_dotnet_cache_dir() / _DOTNET10_SDK_FILENAME
    if not _download_if_missing(_DOTNET10_SDK_URL, installer, log):
        return False
    log("Installing .NET 10 SDK (this can take several minutes) …")
    result = subprocess.run(
        [str(wine), str(installer), "/install", "/quiet", "/norestart"],
        env=_base_env(pfx, wine),
        capture_output=True, text=True, errors="replace", timeout=900,
    )
    # exit 1 under Wine ~ already present, bundle declined to reinstall - accept it
    if result.returncode not in (0, 3010, 1):
        log(f"  .NET 10 SDK installer exited with {result.returncode}")
        return False
    _mark_done(pfx, "dotnet10_sdk")
    log("  .NET 10 SDK installed.")
    return True


def _step_dotnet10_desktop(pfx: Path, wine: Path, log: LogFn) -> bool:
    if _is_done(pfx, "dotnet10_desktop"):
        log("  .NET 10 Desktop Runtime already installed, skipping.")
        return True
    installer = get_dotnet_cache_dir() / _DOTNET10_DESKTOP_FILENAME
    if not _download_if_missing(_DOTNET10_DESKTOP_URL, installer, log):
        return False
    log("Installing .NET 10 Desktop Runtime …")
    result = subprocess.run(
        [str(wine), str(installer), "/install", "/quiet", "/norestart"],
        env=_base_env(pfx, wine),
        capture_output=True, text=True, errors="replace", timeout=600,
    )
    # exit 1 under Wine ~ already present, bundle declined to reinstall - accept it
    if result.returncode not in (0, 3010, 1):
        log(f"  .NET 10 Desktop Runtime installer exited with {result.returncode}")
        return False
    _mark_done(pfx, "dotnet10_desktop")
    log("  .NET 10 Desktop Runtime installed.")
    return True


def _install_desktop_runtime(
    pfx: Path, wine: Path, log: LogFn, *,
    marker: str, url: str, filename: str, label: str,
) -> bool:
    if _is_done(pfx, marker):
        log(f"  {label} already installed, skipping.")
        return True
    installer = get_dotnet_cache_dir() / filename
    if not _download_if_missing(url, installer, log):
        return False
    log(f"Installing {label} …")
    result = subprocess.run(
        [str(wine), str(installer), "/install", "/quiet", "/norestart"],
        env=_base_env(pfx, wine),
        capture_output=True, text=True, errors="replace", timeout=600,
    )
    # exit 1 under Wine ~ already present, bundle declined to reinstall - accept it
    if result.returncode not in (0, 3010, 1):
        log(f"  {label} installer exited with {result.returncode}")
        return False
    _mark_done(pfx, marker)
    log(f"  {label} installed.")
    return True


def _step_dotnet8_desktop(pfx: Path, wine: Path, log: LogFn) -> bool:
    return _install_desktop_runtime(
        pfx, wine, log, marker="dotnet8_desktop", url=_DOTNET8_DESKTOP_URL,
        filename=_DOTNET8_DESKTOP_FILENAME, label=".NET 8 Desktop Runtime")


def _step_dotnet7_desktop(pfx: Path, wine: Path, log: LogFn) -> bool:
    return _install_desktop_runtime(
        pfx, wine, log, marker="dotnet7_desktop", url=_DOTNET7_DESKTOP_URL,
        filename=_DOTNET7_DESKTOP_FILENAME, label=".NET 7 Desktop Runtime")


def _step_dotnet6_desktop(pfx: Path, wine: Path, log: LogFn) -> bool:
    return _install_desktop_runtime(
        pfx, wine, log, marker="dotnet6_desktop", url=_DOTNET6_DESKTOP_URL,
        filename=_DOTNET6_DESKTOP_FILENAME, label=".NET 6 Desktop Runtime")


# ---------------------------------------------------------------------------
# Root-certificate import - crypt32 registry blobs via regedit.
#
# certutil.exe and rundll32 cryptext.dll are unimplemented stubs in Wine, and
# crypt32 store writes (X509Store.Add) under GE-Proton report success without
# ever reaching wineserver's registry - writing serialized cert blobs straight
# into the Root store key via regedit is the only import that persists.  NuGet
# treats an untrusted root on the *timestamp* chain as a hard error (NU3028)
# regardless of signatureValidationMode / allowUntrustedRoot / any env var, so
# the roots must genuinely be trusted in the prefix store; NuGet.Config alone
# cannot cover it. 
# ---------------------------------------------------------------------------

_ROOT_STORE_KEY = ("HKEY_LOCAL_MACHINE\\Software\\Microsoft\\SystemCertificates"
                   "\\Root\\Certificates")
_CERT_CERT_PROP_ID = 32
_PEM_CERT_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", re.DOTALL)


def _read_pem_certs(bundle_path: Path) -> list[bytes]:
    """Return DER-encoded certificates from a concatenated PEM bundle."""
    try:
        text = bundle_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [base64.b64decode("".join(m.group(1).split()))
            for m in _PEM_CERT_RE.finditer(text)]


def _cert_registry_blob(der: bytes) -> bytes:
    """Serialize a certificate the way crypt32 stores it in the registry: a
    property record of DWORD propid, DWORD reserved (1), DWORD length, then
    the raw DER.  A blob containing only the CERT_CERT_PROP_ID record is
    valid."""
    return struct.pack("<III", _CERT_CERT_PROP_ID, 1, len(der)) + der


def _hex_reg_value(name: str, data: bytes) -> str:
    """Format a REG_BINARY value as regedit .reg hex syntax, line-wrapped."""
    lines: list[str] = []
    line = f'"{name}"=hex:'
    for b in data:
        hb = f"{b:02x}"
        if len(line) + len(hb) + 2 > 76:
            lines.append(line + "\\")
            line = "  "
        line += hb + ","
    lines.append(line.rstrip(","))
    return "\n".join(lines)


def _build_certs_reg_content(pem_bundles: list[Path]) -> tuple[str, frozenset]:
    """Build .reg content adding every cert in *pem_bundles* to the prefix
    Root store.  Returns ``(reg_content, sha1_thumbprints)``.  Re-importing is
    idempotent - regedit overwrites identical keys in place."""
    seen: set[str] = set()
    parts = ["Windows Registry Editor Version 5.00"]
    for bundle in pem_bundles:
        for der in _read_pem_certs(bundle):
            thumbprint = hashlib.sha1(der).hexdigest().upper()
            if thumbprint in seen:
                continue
            seen.add(thumbprint)
            parts.append("")
            parts.append(f"[{_ROOT_STORE_KEY}\\{thumbprint}]")
            parts.append(_hex_reg_value("Blob", _cert_registry_blob(der)))
    parts.append("")
    return "\n".join(parts), frozenset(seen)


def _missing_thumbprints(pfx: Path, thumbprints: frozenset) -> frozenset:
    """Check which thumbprints are actually present in the on-disk system.reg.

    regedit's exit code alone is not reliable here - on large batches (~400
    keys) it has been observed to exit 0 while silently dropping individual
    entries.  system.reg stores hive-relative key paths (no HKLM prefix) with
    each backslash doubled, unlike the .reg syntax used to write them."""
    system_reg = pfx / "system.reg"
    if not system_reg.is_file():
        return thumbprints
    try:
        text = system_reg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return thumbprints
    key_path = _ROOT_STORE_KEY.split("\\", 1)[1]
    file_literal = key_path.replace("\\", "\\\\")
    pattern = re.compile(
        r"\[" + re.escape(file_literal) + r"\\\\([0-9A-Fa-f]+)\]")
    present = {t.upper() for t in pattern.findall(text)}
    return thumbprints - present


def _import_certs_via_regedit(pfx: Path, wine: Path, pem_bundles: list[Path],
                              label: str, log: LogFn,
                              max_attempts: int = 3) -> bool:
    """Import all certs from *pem_bundles* into the prefix Root store and
    verify against system.reg, retrying on partial imports."""
    reg_content, thumbprints = _build_certs_reg_content(pem_bundles)
    if not thumbprints:
        log(f"  {label}: no certificates found in bundle(s) - nothing to import.")
        return True

    env = _base_env(pfx, wine)
    wineserver = wine.parent / "wineserver"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".reg", delete=False, encoding="utf-8",
    ) as tf:
        tf.write(reg_content)
        reg_file = tf.name

    try:
        missing = thumbprints
        for attempt in range(1, max_attempts + 1):
            log(f"  importing {len(thumbprints)} root cert(s) "
                f"(attempt {attempt}/{max_attempts}) …")
            try:
                result = subprocess.run(
                    [str(wine), "regedit", reg_file],
                    env=env, capture_output=True, text=True, errors="replace",
                    timeout=180,
                )
            except subprocess.TimeoutExpired:
                log("  regedit timed out - retrying.")
                continue
            if result.returncode != 0:
                log(f"  regedit exited with {result.returncode}: "
                    f"{result.stderr[:200].strip()}")
                continue

            # wineserver batches registry writes in memory and flushes to the
            # on-disk .reg files lazily; "wineserver -w" blocks until it
            # exits, forcing the flush so the verification below sees it.
            if wineserver.is_file():
                try:
                    subprocess.run(
                        [str(wineserver), "-w"], env=env,
                        capture_output=True, timeout=60,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass

            missing = _missing_thumbprints(pfx, thumbprints)
            if not missing:
                log(f"  {label}: {len(thumbprints)} certs imported and verified.")
                return True
            log(f"  {label}: {len(missing)} of {len(thumbprints)} certs did "
                "not persist - retrying.")

        log(f"  {label}: import incomplete after {max_attempts} attempts "
            f"({len(missing)} of {len(thumbprints)} missing).")
        return False
    finally:
        try:
            os.unlink(reg_file)
        except OSError:
            pass


def _step_ca_roots(pfx: Path, wine: Path, log: LogFn) -> bool:
    """Import the full public CA bundle into the Wine Root store.

    dotnet runs under Wine as a *Windows* process, where NuGet signature
    verification uses the Windows root store - Wine's is near-empty, so
    author/timestamp certs on Mutagen's older deps fail with NU3028.
    Importing the Mozilla CA bundle gives the prefix the trust anchors those
    signatures chain to (DigiCert Trusted Root G4 included)."""
    if _is_done(pfx, "ca_roots_regedit_v1"):
        log("  CA roots already imported, skipping.")
        return True

    bundle: "str | None" = None
    try:
        from Utils.ca_bundle import resolve_ca_bundle
        bundle = resolve_ca_bundle()
    except Exception as exc:
        log(f"  resolve_ca_bundle unavailable ({exc}); trying certifi directly.")
    if not bundle:
        try:
            import certifi
            bundle = certifi.where()
        except Exception as exc:
            log(f"  No CA bundle available ({exc}) - skipping root import.")
            return True

    bundle_path = Path(bundle)
    if not bundle_path.is_file():
        log(f"  CA bundle not found at {bundle_path} - skipping root import.")
        return True

    log("Importing public CA roots into the Wine Root store …")
    if not _import_certs_via_regedit(pfx, wine, [bundle_path], "CA roots", log):
        return False
    _mark_done(pfx, "ca_roots_regedit_v1")
    return True


def _find_sdk_trusted_roots(pfx: Path) -> list[Path]:
    """Locate the newest installed .NET SDK's bundled trustedroots PEMs -
    the exact trust anchors NuGet itself uses for package and timestamp
    signature validation."""
    sdk_root = pfx / "drive_c" / "Program Files" / "dotnet" / "sdk"
    if not sdk_root.is_dir():
        return []
    for version_dir in sorted(sdk_root.iterdir(), reverse=True):
        bundles = [version_dir / "trustedroots" / name
                   for name in ("codesignctl.pem", "timestampctl.pem")]
        found = [b for b in bundles if b.is_file()]
        if found:
            return found
    return []


def _step_nuget_trusted_roots(pfx: Path, wine: Path, log: LogFn) -> bool:
    """Import the .NET SDK's code-signing + timestamp trusted roots.

    Must run after the SDK install step - the PEM bundles ship inside the
    SDK.  This is what actually fixes NU3028 on the timestamp chain (a
    dropped VeriSign Universal Root is the confirmed cause of long-standing
    intermittent Synthesis patcher-restore failures), hence the verified,
    retried import."""
    if _is_done(pfx, "nuget_trusted_roots_v1"):
        log("  NuGet trusted roots already imported, skipping.")
        return True

    bundles = _find_sdk_trusted_roots(pfx)
    if not bundles:
        log("  .NET SDK trustedroots bundles not found - was the SDK "
            "install step skipped or failed?")
        return False

    log("Importing .NET SDK code-signing/timestamp roots into the Wine "
        "Root store …")
    if not _import_certs_via_regedit(
            pfx, wine, bundles, "NuGet trusted roots", log):
        return False
    _mark_done(pfx, "nuget_trusted_roots_v1")
    return True


def _step_win11_version(pfx: Path, wine: Path, log: LogFn) -> bool:
    """Set Windows version to Win11 directly via registry."""
    if _is_done(pfx, "win11_version"):
        log("  Windows version already set, skipping.")
        return True
    log("Setting Windows version to Windows 11 …")

    updates = [
        (r"HKLM\Software\Microsoft\Windows NT\CurrentVersion",
         "CurrentBuild", "REG_SZ", "22000"),
        (r"HKLM\Software\Microsoft\Windows NT\CurrentVersion",
         "CurrentBuildNumber", "REG_SZ", "22000"),
        (r"HKLM\Software\Microsoft\Windows NT\CurrentVersion",
         "CurrentVersion", "REG_SZ", "10.0"),
        (r"HKLM\Software\Microsoft\Windows NT\CurrentVersion",
         "ProductName", "REG_SZ", "Windows 10 Pro"),
        (r"HKLM\Software\Microsoft\Windows NT\CurrentVersion",
         "CSDVersion", "REG_SZ", ""),
        (r"HKCU\Software\Wine", "Version", "REG_SZ", "win11"),
    ]

    env = _base_env(pfx, wine)
    all_ok = True
    for key, name, rtype, value in updates:
        args = [str(wine), "reg", "add", key, "/v", name, "/t", rtype, "/f"]
        if value:
            args += ["/d", value]
        result = subprocess.run(
            args, env=env, capture_output=True, text=True, errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            log(f"  reg add {name} failed: {result.stderr[:200].strip()}")
            all_ok = False

    subprocess.run(
        [str(wine), "reg", "delete",
         r"HKLM\System\CurrentControlSet\Control\Windows",
         "/v", "CSDVersion", "/f"],
        env=env, capture_output=True, text=True, errors="replace", timeout=30,
    )

    if all_ok:
        _mark_done(pfx, "win11_version")
        log("  Windows version set to Win11.")
    else:
        log("  Win11 version set with some errors (non-fatal).")
    return all_ok


def _build_reg_blob() -> str:
    lines = ["Windows Registry Editor Version 5.00", ""]
    for exe in _XEDIT_EXECUTABLES:
        lines.append(f"[HKEY_CURRENT_USER\\Software\\Wine\\AppDefaults\\{exe}]")
        lines.append('"Version"="winxp"')
        lines.append("")
    lines.append(
        "[HKEY_CURRENT_USER\\Software\\Wine\\AppDefaults\\"
        "Pandora Behaviour Engine+.exe\\X11 Driver]")
    lines.append('"Decorated"="N"')
    lines.append("")
    lines.append("[HKEY_CURRENT_USER\\Software\\Wine\\X11 Driver]")
    lines.append('"UseTakeFocus"="N"')
    lines.append("")
    lines.append("[HKEY_CURRENT_USER\\Software\\Wine\\DllOverrides]")
    for dll in _DLL_OVERRIDES:
        lines.append(f'"{dll}"="native,builtin"')
    lines.append("")
    return "\r\n".join(lines)


def _step_regedit(pfx: Path, wine: Path, log: LogFn) -> bool:
    if _is_done(pfx, "regedit_v2"):
        log("  Registry patches already applied, skipping.")
        return True
    log("Applying registry patches (xEdit compat, DLL overrides, X11 focus) …")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".reg", delete=False, encoding="utf-8",
    ) as tf:
        tf.write(_build_reg_blob())
        reg_path = tf.name
    try:
        result = subprocess.run(
            [str(wine), "regedit", reg_path],
            env=_base_env(pfx, wine),
            capture_output=True, text=True, errors="replace", timeout=60,
        )
        if result.returncode != 0:
            log(f"  wine regedit exited with {result.returncode}: "
                f"{result.stderr[:200].strip()}")
            return False
    finally:
        try:
            os.unlink(reg_path)
        except OSError:
            pass
    _mark_done(pfx, "regedit_v2")
    log("  Registry patches applied.")
    return True


def _step_game_path(pfx: Path, wine: Path, game_path: Path,
                    registry_game_name: str, log: LogFn) -> bool:
    """Register the game's install path under HKLM so Synthesis discovers it."""
    marker = f"game_path_{registry_game_name}".replace(" ", "_")
    if _is_done(pfx, marker):
        log("  Game install path already registered, skipping.")
        return True

    wine_value = _posix_to_wine_path(game_path)
    key = (r"HKLM\Software\Wow6432Node\Bethesda Softworks"
           + "\\" + registry_game_name)
    log(f"Registering {registry_game_name} install path: {wine_value}")
    result = subprocess.run(
        [str(wine), "reg", "add", key, "/v", "Installed Path",
         "/t", "REG_SZ", "/d", wine_value, "/f"],
        env=_base_env(pfx, wine),
        capture_output=True, text=True, errors="replace", timeout=60,
    )
    if result.returncode != 0:
        log(f"  reg add exited with {result.returncode}: {result.stderr[:200]}")
        return False
    _mark_done(pfx, marker)
    log("  Game path registered.")
    return True


def _step_fonts(pfx: Path, wine: Path, log: LogFn) -> bool:
    """Symlink Proton's bundled fonts into the prefix's Fonts dir."""
    if _is_done(pfx, "fonts"):
        log("  Fonts already linked, skipping.")
        return True

    files = _proton_files_dir(wine)
    share_fonts = files / "share" / "fonts"
    wine_fonts = files / "share" / "wine" / "fonts"
    if not wine_fonts.is_dir():
        log(f"  Proton wine fonts dir missing at {wine_fonts}.")
        return False

    dst = pfx / "drive_c" / "windows" / "Fonts"
    dst.mkdir(parents=True, exist_ok=True)

    overrides = {
        "arial.ttf", "arialbd.ttf", "courbd.ttf", "cour.ttf",
        "georgia.ttf", "malgun.ttf", "micross.ttf", "msgothic.ttc",
        "msyh.ttf", "nirmala.ttf", "simsun.ttc", "times.ttf",
    }

    linked = 0
    if wine_fonts.is_dir():
        for f in wine_fonts.iterdir():
            if f.is_file():
                target = dst / f.name
                if target.is_symlink() or target.exists():
                    target.unlink()
                target.symlink_to(f)
                linked += 1

    if share_fonts.is_dir():
        for name in overrides:
            src = share_fonts / name
            if src.is_file():
                target = dst / name
                if target.is_symlink() or target.exists():
                    target.unlink()
                target.symlink_to(src)

    _mark_done(pfx, "fonts")
    log(f"  Fonts linked ({linked} bundled + MS replacements).")
    return True


def _step_vcredist(pfx: Path, wine: Path, log: LogFn) -> bool:
    if _is_done(pfx, "vcredist"):
        log("  VC++ Redistributable already installed, skipping.")
        return True
    installer = get_vcredist_cache_path()
    if not _download_if_missing(_VCREDIST_URL, installer, log):
        return False
    log("Installing Visual C++ Redistributable (x64) …")
    result = subprocess.run(
        [str(wine), str(installer), "/install", "/quiet", "/norestart"],
        env=_base_env(pfx, wine),
        capture_output=True, text=True, errors="replace", timeout=600,
    )
    if result.returncode not in (0, 1638, 3010):
        log(f"  vc_redist exited with {result.returncode}")
        return False
    _mark_done(pfx, "vcredist")
    log("  VC++ Redistributable installed.")
    return True


# The NuGet.Config below is the offline fallback. The live copy is pulled from
# the Amethyst-Mod-Manager ``Resources`` branch first (see
# ``_resolve_nuget_config``) so the trustedSigners fingerprints / package
# sources can be updated without shipping a whole new app build.
_NUGET_CONFIG_URL = (
    "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/"
    "Resources/Synthesis/NuGet.Config"
)

_NUGET_CONFIG_FALLBACK = (
    '﻿<?xml version="1.0" encoding="utf-8"?>\n'
    '<configuration>\n'
    '  <packageSources>\n'
    '    <add key="nuget.org" value="https://api.nuget.org/v3/index.json" protocolVersion="3" />\n'
    '  </packageSources>\n'
    '  <config>\n'
    '    <add key="signatureValidationMode" value="accept" />\n'
    '  </config>\n'
    '  <trustedSigners>\n'
    '    <author name="microsoft">\n'
    '      <certificate fingerprint="3F9001EA83C560D712C24CF213C3D312CB3BFF51EE89435D3430BD06B5D0EECE" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
    '      <certificate fingerprint="AA12DA22A49BCE7D5C1AE64CC1F3D892F150DA76140F210ABD2CBFFCA2C18A27" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
    '      <certificate fingerprint="566A31882BE208BE4422F7CFD66ED09F5D4524A5994F50CCC8B05EC0528C1353" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
    '      <certificate fingerprint="8A17C2B974AD64F4A47982E292D9F89DCC10F0E2AE9C09CBC38C180AA94C9CBA" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
    '      <certificate fingerprint="51044706BD237B91B89B781337E6D62656C69F0FCFFBE8E43741367948127862" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
    '      <certificate fingerprint="9DC17888B5CFAD98B3CB35C1994E96227F061675955B6C5B0C842BE5B89E5885" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
    '      <certificate fingerprint="AFCEA55DD42024B8B1D07F6E5D5DD0E4A0DAF12A78AEF80C4D7C11880BE21E45" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
    '    </author>\n'
    '    <author name="dotnet-foundation">\n'
    '      <certificate fingerprint="024F162B1D09F6A0868C38B4C8B4257C1EEA6C5A31589416D520CF1624917EB3" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
    '      <certificate fingerprint="0ED671B806A0FAFC2571A7C4901EBF424CE38698872CF6B8047AD0343DC2D697" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
    '      <certificate fingerprint="8983371A2FF43E674DD3179E22A70934BBB3243FB38DC2EF12C6030E85DBAA81" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
    '    </author>\n'
    '    <repository name="nuget.org" serviceIndex="https://api.nuget.org/v3/index.json">\n'
    '      <certificate fingerprint="0E5F38F57DC1BCC806D8494F4F90FBCEDD988B46760709CBEEC6F4219AA6157D" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
    '      <certificate fingerprint="5A2901D6ADA3D18260B9C6DFE2133C95D74B9EEF6AE0E5DC334C8454D1477DF4" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
    '      <certificate fingerprint="1F4B311D9ACC115C8DC8018B5A49E00FCE6DA8E2855F9F014CA6F34570BC482D" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
    '    </repository>\n'
    '  </trustedSigners>\n'
    '</configuration>\n'
)


def _resolve_nuget_config() -> tuple[str, str]:
    """Return ``(content, source)`` for the NuGet.Config to write.

    Tries the ``Resources`` branch copy first (ETag-cached, throttled) so the
    trustedSigners / package sources can be patched without an app update. Any
    network failure, missing file, or content that does not look like a NuGet
    config falls back to the hardcoded ``_NUGET_CONFIG_FALLBACK``.
    """
    try:
        from Utils.gh_cache import fetch_text
        raw = fetch_text(
            _NUGET_CONFIG_URL, accept="*/*", timeout=10, min_interval=3600,
        )
        if raw and "<configuration>" in raw and "<trustedSigners>" in raw:
            return raw, "Resources branch"
    except Exception:
        pass
    return _NUGET_CONFIG_FALLBACK, "built-in fallback"


def _step_nuget_config(pfx: Path, wine: Path, log: LogFn) -> bool:
    """Write NuGet.Config that accepts expired-timestamp signed packages."""
    if _is_done(pfx, "nuget_config_v9"):
        return True
    cfg = (pfx / "drive_c" / "users" / "steamuser"
           / "AppData" / "Roaming" / "NuGet" / "NuGet.Config")
    cfg.parent.mkdir(parents=True, exist_ok=True)
    content, source = _resolve_nuget_config()
    cfg.write_text(content, encoding="utf-8")
    _mark_done(pfx, "nuget_config_v9")
    log(f"  NuGet.Config written with trustedSigners (allowUntrustedRoot) [{source}].")
    return True


def _step_mscoree_cleanup(pfx: Path, wine: Path, log: LogFn) -> bool:
    if _is_done(pfx, "mscoree_cleanup"):
        return True
    subprocess.run(
        [str(wine), "reg", "delete", r"HKCU\Software\Wine\DllOverrides",
         "/v", "*mscoree", "/f"],
        env=_base_env(pfx, wine),
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    _mark_done(pfx, "mscoree_cleanup")
    return True


def setup_synthesis_prefix(
    synthesis_dir_path: Path,
    proton_script: Path,
    game_path: Path,
    log_fn: LogFn,
    prefix_parent: Path | None = None,
    registry_game_name: str = "Skyrim Special Edition",
) -> bool:
    """Prepare the Synthesis prefix. Returns True on full success."""
    if prefix_parent is None:
        prefix_parent = synthesis_dir_path / "prefix"
    prefix_parent.mkdir(parents=True, exist_ok=True)
    pfx = prefix_parent / "pfx"

    wine = _wine_bin(proton_script)
    if not wine.is_file():
        log_fn(f"Wine binary not found at {wine}")
        return False

    if not _ensure_prefix(pfx, wine, log_fn):
        return False

    ok = True
    ok &= _step_mscoree_cleanup(pfx, wine, log_fn)
    ok &= _step_win11_version(pfx, wine, log_fn)
    ok &= _step_vcredist(pfx, wine, log_fn)
    ok &= _step_dotnet10_sdk(pfx, wine, log_fn)
    ok &= _step_dotnet10_desktop(pfx, wine, log_fn)
    ok &= _step_dotnet8_desktop(pfx, wine, log_fn)
    ok &= _step_dotnet7_desktop(pfx, wine, log_fn)
    ok &= _step_dotnet6_desktop(pfx, wine, log_fn)
    ok &= _step_ca_roots(pfx, wine, log_fn)
    ok &= _step_nuget_trusted_roots(pfx, wine, log_fn)
    ok &= _step_regedit(pfx, wine, log_fn)
    ok &= _step_fonts(pfx, wine, log_fn)
    ok &= _step_nuget_config(pfx, wine, log_fn)
    ok &= _step_game_path(pfx, wine, game_path, registry_game_name, log_fn)

    if ok:
        log_fn("Synthesis prefix ready.")
    else:
        log_fn("Synthesis prefix setup finished with errors - see log above.")
    return ok


# ===========================================================================
# Per-game path helpers
# ===========================================================================

def synthesis_dir(game: "BaseGame") -> Path:
    return game.get_mod_staging_path().parent / "Applications" / APP_DIR_NAME


def synthesis_prefix_parent(game: "BaseGame") -> Path:
    return synthesis_dir(game) / "prefix"


def synthesis_pfx(game: "BaseGame") -> Path:
    return synthesis_prefix_parent(game) / "pfx"


def synthesis_exe(game: "BaseGame") -> Path:
    return synthesis_dir(game) / EXE_NAME


def _settings_path(game: "BaseGame") -> Path:
    return get_game_config_dir(game.name) / "synthesis.ini"


def list_proton() -> list[Path]:
    return list_installed_proton()


def load_saved_proton(game: "BaseGame") -> str:
    ini = _settings_path(game)
    if not ini.is_file():
        return ""
    parser = configparser.ConfigParser()
    try:
        parser.read(ini)
    except configparser.Error:
        return ""
    return parser.get(_INI_SECTION, _INI_PROTON_KEY, fallback="")


def save_proton(game: "BaseGame", proton_name: str) -> None:
    ini = _settings_path(game)
    parser = configparser.ConfigParser()
    if ini.is_file():
        try:
            parser.read(ini)
        except configparser.Error:
            parser = configparser.ConfigParser()
    if _INI_SECTION not in parser:
        parser[_INI_SECTION] = {}
    parser[_INI_SECTION][_INI_PROTON_KEY] = proton_name
    with ini.open("w") as f:
        parser.write(f)


def _plugins_appdata_targets(game: "BaseGame", pfx: Path) -> list[Path]:
    targets: list[Path] = []
    for attr in ("_APPDATA_SUBPATH", "_APPDATA_SUBPATH_GOG"):
        subpath = getattr(game, attr, None)
        if subpath is not None:
            targets.append(pfx / subpath / "plugins.txt")
    return targets


def _active_profile_plugins_source(game: "BaseGame", profile: str) -> Path:
    return game.get_profile_root() / "profiles" / profile / "plugins.txt"


def _mygames_root(pfx: Path, game: "BaseGame") -> "Path | None":
    """The ``Documents/.../My Games`` folder inside a prefix for this game."""
    docs = getattr(game, "_MYGAMES_DOCS", None)
    if docs is None:
        return None
    return pfx / docs


# ===========================================================================
# Symlink / deploy / launch (neutral ports of the wizard's methods)
# ===========================================================================

def symlink_plugins(game: "BaseGame", profile: str, log: LogFn) -> list[Path]:
    """Symlink the active profile's plugins.txt into the prefix AppData dir(s).
    Returns the list of created symlinks (to remove afterwards)."""
    pfx = synthesis_pfx(game)
    targets = _plugins_appdata_targets(game, pfx)
    if not targets:
        log("Skipping plugins.txt link (game has no AppData subpath).")
        return []
    source = _active_profile_plugins_source(game, profile)
    if not source.is_file():
        log(f"plugins.txt source not found: {source}")
        return []
    log(f"Using profile: {profile}")
    created: list[Path] = []
    for target in targets:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(source)
            created.append(target)
            log(f"Linked plugins.txt → {target}")
        except OSError as exc:
            log(f"Failed to link plugins.txt at {target}: {exc}")
    return created


def remove_symlinks(links: list[Path], log: LogFn) -> None:
    for link in links:
        try:
            if link.is_symlink():
                link.unlink()
                log(f"Synthesis: removed symlink {link}")
        except OSError:
            pass


def symlink_mygames(game: "BaseGame", log: LogFn) -> Path | None:
    """Symlink the game prefix's whole My Games folder into the Synthesis
    prefix so patchers see the same Skyrim/Fallout INIs the game uses."""
    game_pfx = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
    if game_pfx is None:
        log("Skipping My Games link (game prefix not configured).")
        return None
    src = _mygames_root(Path(game_pfx), game)
    dst = _mygames_root(synthesis_pfx(game), game)
    if src is None or dst is None:
        log("Skipping My Games link (game has no My Games path).")
        return None
    if not src.is_dir():
        log(f"Game-prefix My Games folder not found ({src}) - skipping link.")
        return None
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink() or dst.exists():
            if dst.is_symlink():
                dst.unlink()
            else:
                log(f"My Games already exists as a real folder at {dst} - "
                    "skipping link.")
                return None
        dst.symlink_to(src, target_is_directory=True)
        log(f"Linked My Games → {dst}")
        return dst
    except OSError as exc:
        log(f"My Games link failed: {exc}")
        return None


def deploy_active_profile(game: "BaseGame", profile: str, log: LogFn) -> bool:
    """Deploy *profile* exactly as pressing the Deploy button does.

    Delegates to the shared ``run_deploy_pipeline`` (the canonical deploy path)
    so the pre-launch deploy stays in lock-step with the Deploy button:
    incremental fast path, deferred runtime snapshot, mount checks, per-profile
    ``load_paths`` on the profile switch, root-flagged mods, etc. - none of which
    the old bespoke copy did.  Called from the synthesis wizard's launch worker
    thread.
    """
    from Utils.deploy_pipeline import run_deploy_pipeline

    log(f"Deploying profile '{profile}' before launch …")
    return run_deploy_pipeline(game, profile, log_fn=log)


def launch_synthesis(game: "BaseGame", proton_script: Path, profile: str,
                     log: LogFn) -> None:
    """Deploy the active profile, link My Games, run Synthesis.exe via
    ``proton run`` and wait for it to close.  Plugins.txt should already be
    linked by the caller (symlink_plugins); My-Games links are made here and
    both are cleaned up by the caller."""
    sdir = synthesis_dir(game)
    exe = sdir / EXE_NAME
    if not exe.is_file():
        log(f"Synthesis.exe missing at {exe}")
        return
    if not proton_script.is_file():
        log(f"Proton script missing at {proton_script}")
        return

    deploy_active_profile(game, profile, log)

    # Run via `proton run` so the Steam Linux Runtime sniper container provides
    # libicuuc/libicuin (Wine's icu.dll stub forwards into them).
    # Scrub AppImage loader/bundle pollution so the host Proton's Vulkan probe
    # can load libvulkan (see Utils.protontricks.strip_appimage_env).
    from Utils.protontricks import strip_appimage_env
    from Utils.steam_finder import proton_run_command
    env = strip_appimage_env(os.environ.copy())
    env["STEAM_COMPAT_DATA_PATH"] = str(synthesis_prefix_parent(game))
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(
        Path.home() / ".local" / "share" / "Steam")
    env["STEAM_COMPAT_INSTALL_PATH"] = str(sdir)
    env["WINEDEBUG"] = "-all"
    env.setdefault("DISPLAY", os.environ.get("DISPLAY", ":0"))
    env["NUGET_CERT_REVOCATION_MODE"] = "offline"
    # Retry NuGet trust-chain builds: the first chain build after a fresh cert
    # import can race and fail spuriously (NuGet/Home#11099) - and our cert
    # import may have happened moments earlier in the same setup run.
    env["NUGET_EXPERIMENTAL_CHAIN_BUILD_RETRY_POLICY"] = "10,1000"
    # VBCSCompiler's named-pipe IPC is unreliable under Wine, so every patcher
    # build falls back to spawning its own dotnet.exe.  Without a concurrency
    # cap, Synthesis's patcher scheduler (Parallel.* defaults to
    # Environment.ProcessorCount) can pile up dozens of unreaped compiler
    # processes and OOM the host on large patcher lists.
    env["UseSharedCompilation"] = "false"
    env["MSBUILDDISABLENODEREUSE"] = "1"
    env["DOTNET_PROCESSOR_COUNT"] = "8"

    log(f"Launching {exe} via {proton_script.parent.name} …")
    try:
        log_path = sdir / "synthesis.log"
        with log_path.open("w", encoding="utf-8", errors="replace") as log_f:
            proc = subprocess.Popen(
                proton_run_command(proton_script, "run", str(exe), env=env),
                env=env, cwd=str(sdir), stdout=log_f, stderr=subprocess.STDOUT)
            proc.wait()
        log(f"Synthesis closed. Output log: {log_path}")
    except Exception as exc:
        log(f"Launch error: {exc}")
