"""
Mutagen Synthesis wizard plugin for Bethesda games.

Self-contained plugin port of the former built-in Synthesis wizard. Lives
in the Plugins/ folder so the heavy Wine/.NET bootstrap logic isn't bundled
into the AppImage (it tripped Nexus's malware heuristics — silent .exe
installers, certutil cert-store writes, Windows registry edits and a
"native,builtin" DLL override list all read like a dropper).

Pipeline mirrors the original wizard:
  1. Download the latest Synthesis release zip from GitHub and extract it.
  2. Let the user pick a Proton version (saved per-game in synthesis.ini).
  3. Run setup_synthesis_prefix() in a worker — per-step marker files under
     <pfx>/.synthesis_setup/<step>.done skip work already done.
  4. Symlink the active profile's plugins.txt into the prefix's AppData
     dir(s), then launch Synthesis.exe via `proton run` so the Steam Linux
     Runtime sniper container provides icu/vkd3d/etc.
"""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.request
import json as _json
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import customtkinter as ctk

from Utils.config_paths import (
    get_dotnet_cache_dir,
    get_game_config_dir,
    get_vcredist_cache_path,
)
from Utils.steam_finder import list_installed_proton

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL, BORDER,
    TEXT_ON_ACCENT,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD, FONT_SMALL,
)


PLUGIN_INFO = {
    "id":           "bethesda_synthesis",
    "label":        "Run Synthesis",
    "description":  "Install and run Mutagen Synthesis patcher in its own prefix.",
    "game_ids": [
        "skyrim_se",
        "Fallout3",
        "Fallout3GOTY",
        "Fallout4",
        "Fallout4VR",
        "Oblivion",
        "skyrim",
        "skyrimvr",
        "Starfield",
        "enderal",
        "enderalse",
    ],
    "all_games":    False,
    "dialog_class": "SynthesisWizard",
}


_APP_DIR_NAME = "Synthesis"
_EXE_NAME = "Synthesis.exe"
_GITHUB_API = "https://api.github.com/repos/Mutagen-Modding/Synthesis/releases/latest"
_INI_SECTION = "synthesis"
_INI_PROTON_KEY = "proton"


# ---------------------------------------------------------------------------
# Download URLs (kept in sync with .NET LTS / current releases)
# ---------------------------------------------------------------------------

_DOTNET9_SDK_URL = (
    "https://builds.dotnet.microsoft.com/dotnet/Sdk/9.0.310/"
    "dotnet-sdk-9.0.310-win-x64.exe"
)
_DOTNET9_SDK_FILENAME = "dotnet-sdk-9.0.310-win-x64.exe"

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

_DIGICERT_CERT_URL = "https://cacerts.digicert.com/DigiCertTrustedRootG4.crt.pem"
_DIGICERT_CERT_FILENAME = "DigiCertTrustedRootG4.crt.pem"

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
# Inlined zip extraction (formerly wizards.script_extender)
# ===========================================================================

def _fetch_latest_synthesis_asset() -> tuple[str, str]:
    """Return (version_tag, download_url) for the latest Synthesis zip."""
    req = urllib.request.Request(
        _GITHUB_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ModManager/1.0"},
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


def _extract_zip_flat(archive: Path, dest: Path) -> None:
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


# ===========================================================================
# Wine prefix bootstrap (formerly Utils.synthesis_setup)
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
    container wrapper. Without WINEDLLPATH+LD_LIBRARY_PATH, icu.dll's
    forwards to icuuc68.dll fail and .NET WPF apps crash.
    """
    files = _proton_files_dir(wine)
    env = os.environ.copy()
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
    env = os.environ.copy()
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


def _download_if_missing(url: str, dest: Path, log: Callable[[str], None]) -> bool:
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


def _ensure_prefix(pfx: Path, wine: Path, log: Callable[[str], None]) -> bool:
    if (pfx / "system.reg").is_file():
        return True
    pfx.mkdir(parents=True, exist_ok=True)
    log("Creating Wine prefix (this can take a minute on first run) …")
    result = subprocess.run(
        [str(wine), "wineboot", "-i"],
        env=_base_env(pfx, wine),
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        log(f"  wineboot exited with {result.returncode}: {result.stderr[:200]}")
        return False
    log("  prefix created.")
    return True


def _step_dotnet9_sdk(pfx: Path, wine: Path, log: Callable[[str], None]) -> bool:
    if _is_done(pfx, "dotnet9_sdk"):
        log("  .NET 9 SDK already installed, skipping.")
        return True
    installer = get_dotnet_cache_dir() / _DOTNET9_SDK_FILENAME
    if not _download_if_missing(_DOTNET9_SDK_URL, installer, log):
        return False
    log("Installing .NET 9 SDK (this can take several minutes) …")
    result = subprocess.run(
        [str(wine), str(installer), "/install", "/quiet", "/norestart"],
        env=_base_env(pfx, wine),
        capture_output=True, text=True, timeout=900,
    )
    if result.returncode not in (0, 3010):
        log(f"  .NET 9 SDK installer exited with {result.returncode}")
        return False
    _mark_done(pfx, "dotnet9_sdk")
    log("  .NET 9 SDK installed.")
    return True


def _step_dotnet10_desktop(pfx: Path, wine: Path, log: Callable[[str], None]) -> bool:
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
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode not in (0, 3010):
        log(f"  .NET 10 Desktop Runtime installer exited with {result.returncode}")
        return False
    _mark_done(pfx, "dotnet10_desktop")
    log("  .NET 10 Desktop Runtime installed.")
    return True


def _install_desktop_runtime(
    pfx: Path,
    wine: Path,
    log: Callable[[str], None],
    *,
    marker: str,
    url: str,
    filename: str,
    label: str,
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
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode not in (0, 3010):
        log(f"  {label} installer exited with {result.returncode}")
        return False
    _mark_done(pfx, marker)
    log(f"  {label} installed.")
    return True


def _step_dotnet8_desktop(pfx: Path, wine: Path, log: Callable[[str], None]) -> bool:
    return _install_desktop_runtime(
        pfx, wine, log,
        marker="dotnet8_desktop",
        url=_DOTNET8_DESKTOP_URL,
        filename=_DOTNET8_DESKTOP_FILENAME,
        label=".NET 8 Desktop Runtime",
    )


def _step_dotnet7_desktop(pfx: Path, wine: Path, log: Callable[[str], None]) -> bool:
    return _install_desktop_runtime(
        pfx, wine, log,
        marker="dotnet7_desktop",
        url=_DOTNET7_DESKTOP_URL,
        filename=_DOTNET7_DESKTOP_FILENAME,
        label=".NET 7 Desktop Runtime",
    )


def _step_dotnet6_desktop(pfx: Path, wine: Path, log: Callable[[str], None]) -> bool:
    return _install_desktop_runtime(
        pfx, wine, log,
        marker="dotnet6_desktop",
        url=_DOTNET6_DESKTOP_URL,
        filename=_DOTNET6_DESKTOP_FILENAME,
        label=".NET 6 Desktop Runtime",
    )


def _step_digicert_root(pfx: Path, wine: Path, log: Callable[[str], None]) -> bool:
    if _is_done(pfx, "digicert_root"):
        log("  DigiCert root cert already imported, skipping.")
        return True
    cert = get_dotnet_cache_dir() / _DIGICERT_CERT_FILENAME
    if not _download_if_missing(_DIGICERT_CERT_URL, cert, log):
        return False
    log("Importing DigiCert Trusted Root G4 into Wine cert store …")
    result = subprocess.run(
        [str(wine), "certutil", "-addstore", "Root", str(cert)],
        env=_base_env(pfx, wine),
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        log(f"  certutil exited with {result.returncode} (likely already present)")
    _mark_done(pfx, "digicert_root")
    log("  DigiCert root cert imported.")
    return True


def _step_win11_version(pfx: Path, wine: Path, log: Callable[[str], None]) -> bool:
    """Set Windows version to Win11 directly via registry.

    Mirrors winetricks `w_set_winver win11` but avoids its wineserver-on-PATH
    dependency (Proton's wineserver isn't exported globally).
    """
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
            args, env=env, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log(f"  reg add {name} failed: {result.stderr[:200].strip()}")
            all_ok = False

    subprocess.run(
        [str(wine), "reg", "delete",
         r"HKLM\System\CurrentControlSet\Control\Windows",
         "/v", "CSDVersion", "/f"],
        env=env, capture_output=True, text=True, timeout=30,
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
        "Pandora Behaviour Engine+.exe\\X11 Driver]"
    )
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


def _step_regedit(pfx: Path, wine: Path, log: Callable[[str], None]) -> bool:
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
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            log(f"  wine regedit exited with {result.returncode}: {result.stderr[:200].strip()}")
            return False
    finally:
        try:
            os.unlink(reg_path)
        except OSError:
            pass
    _mark_done(pfx, "regedit_v2")
    log("  Registry patches applied.")
    return True


def _step_game_path(
    pfx: Path,
    wine: Path,
    game_path: Path,
    registry_game_name: str,
    log: Callable[[str], None],
) -> bool:
    """Register the game's install path under HKLM so Synthesis discovers it."""
    marker = f"game_path_{registry_game_name}".replace(" ", "_")
    if _is_done(pfx, marker):
        log("  Game install path already registered, skipping.")
        return True

    wine_value = _posix_to_wine_path(game_path)
    key = (
        r"HKLM\Software\Wow6432Node\Bethesda Softworks"
        + "\\" + registry_game_name
    )
    log(f"Registering {registry_game_name} install path: {wine_value}")
    result = subprocess.run(
        [
            str(wine), "reg", "add", key,
            "/v", "Installed Path",
            "/t", "REG_SZ",
            "/d", wine_value,
            "/f",
        ],
        env=_base_env(pfx, wine),
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        log(f"  reg add exited with {result.returncode}: {result.stderr[:200]}")
        return False
    _mark_done(pfx, marker)
    log("  Game path registered.")
    return True


def _step_fonts(pfx: Path, wine: Path, log: Callable[[str], None]) -> bool:
    """Symlink Proton's bundled fonts into the prefix's Fonts dir.

    Proton's prefix-init normally links them in; our wineboot-only prefix
    skips that step, so WPF text shaping FailFasts on the first measure.
    """
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


def _step_vcredist(pfx: Path, wine: Path, log: Callable[[str], None]) -> bool:
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
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode not in (0, 1638, 3010):
        log(f"  vc_redist exited with {result.returncode}")
        return False
    _mark_done(pfx, "vcredist")
    log("  VC++ Redistributable installed.")
    return True


def _step_nuget_config(pfx: Path, wine: Path, log: Callable[[str], None]) -> bool:
    """Write NuGet.Config that accepts expired-timestamp signed packages.

    Mutagen's deps include 2020-era packages whose timestamping certs have
    rolled past validity. ``allowUntrustedRoot="true"`` on the listed
    fingerprints (nuget.org repo + Microsoft author) lets NuGet accept them.
    """
    if _is_done(pfx, "nuget_config_v6"):
        return True
    cfg = (
        pfx / "drive_c" / "users" / "steamuser"
        / "AppData" / "Roaming" / "NuGet" / "NuGet.Config"
    )
    cfg.parent.mkdir(parents=True, exist_ok=True)
    content = (
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
        '    <repository name="nuget.org" serviceIndex="https://api.nuget.org/v3/index.json">\n'
        '      <certificate fingerprint="0E5F38F57DC1BCC806D8494F4F90FBCEDD988B46760709CBEEC6F4219AA6157D" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
        '      <certificate fingerprint="5A2901D6ADA3D18260B9C6DFE2133C95D74B9EEF6AE0E5DC334C8454D1477DF4" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
        '      <certificate fingerprint="1F4B311D9ACC115C8DC8018B5A49E00FCE6DA8E2855F9F014CA6F34570BC482D" hashAlgorithm="SHA256" allowUntrustedRoot="true" />\n'
        '    </repository>\n'
        '  </trustedSigners>\n'
        '</configuration>\n'
    )
    cfg.write_text(content, encoding="utf-8")
    _mark_done(pfx, "nuget_config_v6")
    log("  NuGet.Config written with trustedSigners (allowUntrustedRoot).")
    return True


def _step_mscoree_cleanup(pfx: Path, wine: Path, log: Callable[[str], None]) -> bool:
    if _is_done(pfx, "mscoree_cleanup"):
        return True
    subprocess.run(
        [
            str(wine), "reg", "delete",
            r"HKCU\Software\Wine\DllOverrides",
            "/v", "*mscoree", "/f",
        ],
        env=_base_env(pfx, wine),
        capture_output=True, text=True, timeout=30,
    )
    _mark_done(pfx, "mscoree_cleanup")
    return True


def setup_synthesis_prefix(
    synthesis_dir: Path,
    proton_script: Path,
    game_path: Path,
    log_fn: Callable[[str], None],
    prefix_parent: Path | None = None,
    registry_game_name: str = "Skyrim Special Edition",
) -> bool:
    """Prepare the Synthesis prefix. Returns True on full success."""
    if prefix_parent is None:
        prefix_parent = synthesis_dir / "prefix"
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
    ok &= _step_dotnet9_sdk(pfx, wine, log_fn)
    ok &= _step_dotnet10_desktop(pfx, wine, log_fn)
    ok &= _step_dotnet8_desktop(pfx, wine, log_fn)
    ok &= _step_dotnet7_desktop(pfx, wine, log_fn)
    ok &= _step_dotnet6_desktop(pfx, wine, log_fn)
    ok &= _step_digicert_root(pfx, wine, log_fn)
    ok &= _step_regedit(pfx, wine, log_fn)
    ok &= _step_fonts(pfx, wine, log_fn)
    ok &= _step_nuget_config(pfx, wine, log_fn)
    ok &= _step_game_path(pfx, wine, game_path, registry_game_name, log_fn)

    if ok:
        log_fn("Synthesis prefix ready.")
    else:
        log_fn("Synthesis prefix setup finished with errors — see log above.")
    return ok


# ===========================================================================
# Per-game path helpers
# ===========================================================================

def _synthesis_dir(game: "BaseGame") -> Path:
    return game.get_mod_staging_path().parent / "Applications" / _APP_DIR_NAME


def _synthesis_prefix_parent(game: "BaseGame") -> Path:
    return _synthesis_dir(game) / "prefix"


def _synthesis_pfx(game: "BaseGame") -> Path:
    return _synthesis_prefix_parent(game) / "pfx"


def _synthesis_exe(game: "BaseGame") -> Path:
    return _synthesis_dir(game) / _EXE_NAME


def _settings_path(game: "BaseGame") -> Path:
    return get_game_config_dir(game.name) / "synthesis.ini"


def _load_saved_proton(game: "BaseGame") -> str:
    ini = _settings_path(game)
    if not ini.is_file():
        return ""
    parser = configparser.ConfigParser()
    try:
        parser.read(ini)
    except configparser.Error:
        return ""
    return parser.get(_INI_SECTION, _INI_PROTON_KEY, fallback="")


def _save_proton(game: "BaseGame", proton_name: str) -> None:
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


# ============================================================================
# Wizard dialog
# ============================================================================

class SynthesisWizard(ctk.CTkFrame):
    """Multi-step wizard: download → proton → setup prefix → launch."""

    def __init__(
        self,
        parent,
        game: "BaseGame",
        log_fn=None,
        *,
        on_close=None,
        **_kwargs,
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close_cb = on_close or (lambda: None)
        self._game = game
        self._log = log_fn or (lambda msg: None)

        self._proton_candidates: list[Path] = []
        self._selected_proton: Path | None = None
        self._plugins_symlinks: list[Path] = []

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Run Synthesis — {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="✕", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_close_cb,
        ).pack(side="right", padx=4, pady=4)

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        if _synthesis_exe(self._game).is_file():
            self._show_step_proton()
        else:
            self._show_step_download()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    def _set_label(self, attr: str, text: str, color: str = TEXT_DIM):
        def _apply():
            widget = getattr(self, attr, None)
            if widget is not None and widget.winfo_exists():
                widget.configure(text=text, text_color=color)
        try:
            self.after(0, _apply)
        except Exception:
            pass

    def _append_log(self, msg: str):
        box = getattr(self, "_log_box", None)
        if box is None:
            self._log(msg)
            return

        def _apply():
            try:
                box.configure(state="normal")
                box.insert("end", msg + "\n")
                box.see("end")
                box.configure(state="disabled")
            except Exception:
                pass
        try:
            self.after(0, _apply)
        except Exception:
            pass
        self._log(msg)

    # ------------------------------------------------------------------
    # Step 1 — download
    # ------------------------------------------------------------------

    def _show_step_download(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download Synthesis",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._dl_status = ctk.CTkLabel(
            self._body, text="Fetching latest release from GitHub …",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
            wraplength=480,
        )
        self._dl_status.pack(pady=(0, 16))

        self._dl_progress = ctk.CTkProgressBar(self._body, width=400, mode="indeterminate")
        self._dl_progress.pack(pady=(0, 16))
        self._dl_progress.start()

        threading.Thread(target=self._do_download, daemon=True).start()

    def _do_download(self):
        try:
            self._set_label("_dl_status", "Fetching latest release from GitHub …")
            tag, url = _fetch_latest_synthesis_asset()
            self._set_label("_dl_status", f"Downloading Synthesis {tag} …")
            self._log(f"Synthesis: downloading {url}")

            tmpdir = Path(tempfile.mkdtemp(prefix="synthesis_dl_"))
            archive = tmpdir / url.split("/")[-1]

            def _reporthook(block_num, block_size, total_size):
                if total_size > 0:
                    pct = min(block_num * block_size / total_size, 1.0)
                    try:
                        self.after(0, lambda p=pct: (
                            self._dl_progress.configure(mode="determinate"),
                            self._dl_progress.set(p),
                        ))
                    except Exception:
                        pass

            urllib.request.urlretrieve(url, archive, reporthook=_reporthook)

            dest = _synthesis_dir(self._game)
            dest.mkdir(parents=True, exist_ok=True)
            self._set_label("_dl_status", f"Extracting Synthesis {tag} …")
            _extract_zip_flat(archive, dest)

            try:
                archive.unlink()
                archive.parent.rmdir()
            except OSError:
                pass

            if not _synthesis_exe(self._game).is_file():
                raise RuntimeError(
                    f"{_EXE_NAME} not found after extraction — "
                    "the release asset layout may have changed."
                )

            self._set_label("_dl_status", f"Installed Synthesis {tag}.", color="#6bc76b")
            self.after(0, lambda: self._dl_progress.stop())
            self.after(500, self._show_step_proton)

        except Exception as exc:
            self._log(f"Synthesis: download error: {exc}")
            self._set_label("_dl_status", f"Download failed: {exc}", color="#e06c6c")
            try:
                self.after(0, lambda: self._dl_progress.stop())
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Step 2 — Proton selection
    # ------------------------------------------------------------------

    def _show_step_proton(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 2: Select Proton Version",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))
        ctk.CTkLabel(
            self._body,
            text=(
                "Synthesis will run in its own Wine prefix next to Synthesis.exe.\n"
                "Pick a Proton version to create that prefix with."
            ),
            font=FONT_SMALL, text_color=TEXT_DIM, justify="center", wraplength=460,
        ).pack(pady=(0, 12))

        self._proton_candidates = list_installed_proton()
        if not self._proton_candidates:
            ctk.CTkLabel(
                self._body,
                text="No Proton installations found.\n"
                     "Install Proton (e.g. GE-Proton) via Steam and try again.",
                font=FONT_NORMAL, text_color="#e06c6c", justify="center",
            ).pack(pady=16)
            return

        saved = _load_saved_proton(self._game)
        preselect = self._proton_candidates[0]
        for p in self._proton_candidates:
            if p.parent.name == saved:
                preselect = p
                break

        scroll = ctk.CTkScrollableFrame(self._body, fg_color="transparent", height=240)
        scroll.pack(fill="x", pady=(0, 12))

        self._proton_var = ctk.StringVar(value=str(preselect))
        for script in self._proton_candidates:
            row = ctk.CTkFrame(scroll, fg_color=BG_PANEL, corner_radius=6)
            row.pack(fill="x", pady=4)
            ctk.CTkRadioButton(
                row, text=script.parent.name, variable=self._proton_var,
                value=str(script),
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
            ).pack(side="left", padx=12, pady=10)

        btn = ctk.CTkButton(
            self._body, text="Continue →", width=160, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_proton_chosen,
        )
        btn.pack(side="bottom", pady=(8, 0))

    def _on_proton_chosen(self):
        choice = self._proton_var.get()
        if not choice:
            return
        self._selected_proton = Path(choice)
        _save_proton(self._game, self._selected_proton.parent.name)
        self._show_step_setup()

    # ------------------------------------------------------------------
    # Step 3 — prefix setup
    # ------------------------------------------------------------------

    def _show_step_setup(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Prepare Prefix",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))

        self._setup_status = ctk.CTkLabel(
            self._body, text="Preparing …",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=480,
        )
        self._setup_status.pack(pady=(0, 8))

        self._log_box = ctk.CTkTextbox(
            self._body, width=540, height=220, font=FONT_SMALL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER, border_width=1,
        )
        self._log_box.pack(pady=(0, 12))
        self._log_box.configure(state="disabled")

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        self._launch_btn = ctk.CTkButton(
            btn_frame, text="Launch Synthesis", width=180, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_launch, state="disabled",
        )
        self._launch_btn.pack(side="right", padx=(8, 0))

        threading.Thread(target=self._do_setup, daemon=True).start()

    def _do_setup(self):
        game_path = self._game.get_game_path()
        if game_path is None:
            self._append_log("Game path is not configured; aborting.")
            self._set_label("_setup_status", "Game path not configured.", color="#e06c6c")
            return
        if self._selected_proton is None:
            self._append_log("No Proton selected; aborting.")
            return

        synthesis_dir = _synthesis_dir(self._game)
        self._append_log(f"Synthesis dir: {synthesis_dir}")
        self._append_log(f"Proton: {self._selected_proton.parent.name}")
        self._append_log(f"Game path: {game_path}")
        self._append_log("")

        try:
            ok = setup_synthesis_prefix(
                synthesis_dir=synthesis_dir,
                proton_script=self._selected_proton,
                game_path=Path(game_path),
                log_fn=self._append_log,
                prefix_parent=_synthesis_prefix_parent(self._game),
                registry_game_name=getattr(
                    self._game, "synthesis_registry_name", "Skyrim Special Edition",
                ),
            )
        except Exception as exc:
            self._append_log(f"Prefix setup raised: {exc}")
            ok = False

        if ok:
            self._set_label("_setup_status", "Prefix ready. Click Launch Synthesis.", color="#6bc76b")
        else:
            self._set_label(
                "_setup_status",
                "Setup completed with errors — launch may still work.",
                color="#e0a06c",
            )
        try:
            self.after(0, lambda: self._launch_btn.configure(state="normal"))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Step 4 — launch
    # ------------------------------------------------------------------

    def _current_profile(self) -> str:
        try:
            return self.winfo_toplevel()._topbar._profile_var.get() or "default"
        except Exception:
            return self._game.get_last_active_profile()

    def _symlink_plugins(self) -> None:
        pfx = _synthesis_pfx(self._game)
        targets = _plugins_appdata_targets(self._game, pfx)
        if not targets:
            self._append_log("Skipping plugins.txt link (game has no AppData subpath).")
            return
        profile = self._current_profile()
        source = _active_profile_plugins_source(self._game, profile)
        if not source.is_file():
            self._append_log(f"plugins.txt source not found: {source}")
            return
        self._append_log(f"Using profile: {profile}")
        self._plugins_symlinks = []
        for target in targets:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(source)
                self._plugins_symlinks.append(target)
                self._append_log(f"Linked plugins.txt → {target}")
            except OSError as exc:
                self._append_log(f"Failed to link plugins.txt at {target}: {exc}")

    def _remove_plugins_symlink(self) -> None:
        for link in getattr(self, "_plugins_symlinks", []):
            try:
                if link.is_symlink():
                    link.unlink()
                    self._log(f"Synthesis: removed plugins.txt symlink {link}")
            except OSError:
                pass
        self._plugins_symlinks = []

    def _on_launch(self):
        if self._selected_proton is None:
            return
        self._launch_btn.configure(state="disabled", text="Running …")
        self._symlink_plugins()
        threading.Thread(target=self._do_launch, daemon=True).start()

    def _deploy_active_profile(self) -> bool:
        """Run restore + filemap rebuild + deploy for the active profile."""
        from Utils.filemap import build_filemap
        from Utils.deploy import (
            LinkMode, deploy_root_folder, restore_root_folder,
            load_per_mod_strip_prefixes,
        )
        from Utils.profile_state import read_excluded_mod_files
        from Utils.wine_dll_config import deploy_game_wine_dll_overrides

        game = self._game
        profile = self._current_profile()
        game_root = game.get_game_path()
        self._append_log(f"Deploying profile '{profile}' before launch …")

        try:
            profile_root = game.get_profile_root()
            last_deployed = game.get_last_deployed_profile()
            if last_deployed:
                game.set_active_profile_dir(
                    profile_root / "profiles" / last_deployed
                )

            if getattr(game, "restore_before_deploy", True) and hasattr(game, "restore"):
                try:
                    game.restore(log_fn=self._append_log)
                except RuntimeError:
                    pass

            restore_rf_dir = game.get_effective_root_folder_path()
            if restore_rf_dir.is_dir() and game_root:
                restore_root_folder(restore_rf_dir, game_root, log_fn=self._append_log)

            game.set_active_profile_dir(
                profile_root / "profiles" / profile
            )

            staging = game.get_effective_mod_staging_path()
            modlist_path = profile_root / "profiles" / profile / "modlist.txt"
            filemap_out = game.get_effective_filemap_path()
            if modlist_path.is_file():
                try:
                    _exc_raw = read_excluded_mod_files(modlist_path.parent, None)
                    _exc = {k: set(v) for k, v in _exc_raw.items()} if _exc_raw else None
                    build_filemap(
                        modlist_path, staging, filemap_out,
                        strip_prefixes=game.mod_folder_strip_prefixes or None,
                        per_mod_strip_prefixes=load_per_mod_strip_prefixes(modlist_path.parent),
                        allowed_extensions=game.mod_install_extensions or None,
                        root_deploy_folders=game.mod_root_deploy_folders or None,
                        excluded_mod_files=_exc,
                        conflict_ignore_filenames=getattr(game, "conflict_ignore_filenames", None) or None,
                        exclude_dirs=getattr(game, "filemap_exclude_dirs", None) or None,
                    )
                except Exception as fm_err:
                    self._append_log(f"Filemap rebuild warning: {fm_err}")

            deploy_mode = game.get_deploy_mode() if hasattr(game, "get_deploy_mode") else LinkMode.HARDLINK
            game.deploy(log_fn=self._append_log, profile=profile, mode=deploy_mode)
            game.save_last_deployed_profile(profile)

            _pfx = game.get_prefix_path()
            if _pfx and _pfx.is_dir():
                deploy_game_wine_dll_overrides(
                    game.name, _pfx, game.wine_dll_overrides, log_fn=self._append_log,
                )

            target_rf_dir = game.get_effective_root_folder_path()
            rf_allowed = getattr(game, "root_folder_deploy_enabled", True)
            if rf_allowed and target_rf_dir.is_dir() and game_root:
                deploy_root_folder(target_rf_dir, game_root, mode=deploy_mode, log_fn=self._append_log)

            if hasattr(game, "swap_launcher"):
                game.swap_launcher(self._append_log)

            self._append_log("Deploy complete.")
            return True
        except Exception as exc:
            self._append_log(f"Deploy error: {exc}")
            return False

    def _do_launch(self):
        synthesis_dir = _synthesis_dir(self._game)
        exe = synthesis_dir / _EXE_NAME
        proton_script = self._selected_proton
        if not exe.is_file():
            self._append_log(f"Synthesis.exe missing at {exe}")
            return
        if not proton_script.is_file():
            self._append_log(f"Proton script missing at {proton_script}")
            return

        self._deploy_active_profile()

        # Run via `proton run` so the Steam Linux Runtime sniper container
        # provides libicuuc/libicuin (Wine's icu.dll stub forwards into them
        # — without the runtime, .NET 9 WPF crashes with
        # ``Cannot get symbol u_charsToUChars from libicuuc``).
        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"] = str(_synthesis_prefix_parent(self._game))
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(Path.home() / ".local" / "share" / "Steam")
        # Bind-mount synthesis_dir into the sniper container so `dotnet build`
        # under Flatpak can write obj/bin/ and read Synthesis' Data/ cache.
        env["STEAM_COMPAT_INSTALL_PATH"] = str(synthesis_dir)
        env["WINEDEBUG"] = "-all"
        env.setdefault("DISPLAY", os.environ.get("DISPLAY", ":0"))
        # Skip online CRL/OCSP checks — Wine's WinHTTP can't reach them
        # reliably and it amplifies signature-expiry failures from Mutagen's
        # 2020-era deps.
        env["NUGET_CERT_REVOCATION_MODE"] = "offline"

        self._append_log(f"Launching {exe} via {proton_script.parent.name} …")
        try:
            log_path = synthesis_dir / "synthesis.log"
            with log_path.open("w", encoding="utf-8", errors="replace") as log_f:
                proc = subprocess.Popen(
                    ["python3", str(proton_script), "run", str(exe)],
                    env=env,
                    cwd=str(synthesis_dir),
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                )
                proc.wait()
            self._append_log(f"Synthesis closed. Output log: {log_path}")
        except Exception as exc:
            self._append_log(f"Launch error: {exc}")
        finally:
            self._remove_plugins_symlink()
            try:
                self.after(0, lambda: self._launch_btn.configure(
                    state="normal", text="Launch Synthesis",
                ))
            except Exception:
                pass
