"""bg3le, the Script Extender for the native Linux build of Baldur's Gate 3.

Norbyte's BG3SE is a Windows DLL the game loads as DWrite.dll; the native
build loads no DLLs, so mods that need a Script Extender run there through
bg3le instead (https://github.com/lenonk/bg3le): a library preloaded into
bin/bg3 by a wrapper in the game's Steam Launch Options.  It installs outside
the game folder, into ~/.local/share/bg3le, with its own installer that also
writes the launch option.  Amethyst never bundles it (the same policy as me3):
this module finds an install, checks that the game will actually load it, and
runs bg3le's installer from the newest release.

Toolkit-neutral: the Qt wizard (wizards_qt.bg3le_install_view) and the BG3
handler both use it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

PROJECT_URL = "https://github.com/lenonk/bg3le"
NEXUS_URL = "https://www.nexusmods.com/baldursgate3/mods/25431"
RELEASES_URL = PROJECT_URL + "/releases/latest"
_LATEST_API = "https://api.github.com/repos/lenonk/bg3le/releases/latest"
_ASSET_SUFFIX = "-linux-x86_64.zip"

# Nexus mod id of Norbyte's BG3SE: the requirement bg3le stands in for.
BG3SE_NEXUS_ID = 2172

WRAPPER_NAME = "bg3le-launch"
_LIBRARY = Path("lib") / "libbg3le.so"


def _noop(_msg: str) -> None:
    pass


def _in_flatpak() -> bool:
    return Path("/.flatpak-info").exists()


def _data_dir_candidates() -> "list[Path]":
    """Data roots bg3le could be installed under, the host's included.

    Inside our sandbox XDG_DATA_HOME is the app's private data dir, not the
    host's ~/.local/share bg3le installs into (see me3_runtime).
    """
    seen: list[Path] = []
    for raw in (os.environ.get("HOST_XDG_DATA_HOME", ""),
                os.environ.get("XDG_DATA_HOME", ""),
                str(Path.home() / ".local" / "share")):
        if raw and Path(raw) not in seen:
            seen.append(Path(raw))
    return seen


def install_dir() -> "Path | None":
    """The bg3le install (the directory holding lib/libbg3le.so), or None."""
    for base in _data_dir_candidates():
        root = base / "bg3le"
        if (root / _LIBRARY).is_file():
            return root
    return None


def is_installed() -> bool:
    return install_dir() is not None


def library_path() -> "Path | None":
    root = install_dir()
    return root / _LIBRARY if root is not None else None


def loads_bg3le(options: str) -> bool:
    """Whether a launch-options string runs the game through bg3le's wrapper."""
    return WRAPPER_NAME in options and "%command%" in options


def wrapper_option() -> str:
    """The launch option bg3le's installer writes, for telling the user."""
    root = install_dir() or (Path.home() / ".local" / "share" / "bg3le")
    return f'"{root / "bin" / WRAPPER_NAME}" %command%'


def launch_problem(game) -> "str | None":
    """Why a launch of *game* would start it without bg3le, or None.

    Only meaningful once bg3le is installed. Amethyst's own Launch Options
    for the game replace Steam's (and force a direct launch), so they need
    the wrapper themselves; otherwise Steam's must carry it.
    """
    if not is_installed():
        return None
    try:
        from Utils.executables.launch import (
            effective_steam_id, game_exe_key, load_launch_options,
        )
        manager = load_launch_options(game, game_exe_key(game)).strip()
        if manager and not loads_bg3le(manager):
            return ("bg3le is installed, but Amethyst's Launch Options for "
                    "Baldur's Gate 3 replace Steam's and don't load it. Add "
                    f"{wrapper_option()} to them, or clear them.")
        if manager:
            return None
        from Utils.launchers.steam import steam_launch_options
        steam_id = effective_steam_id(game) or getattr(game, "steam_id", "")
        if steam_id and not loads_bg3le(steam_launch_options(steam_id)):
            return ("bg3le is installed, but Baldur's Gate 3's Steam Launch "
                    "Options don't load it. Run the Install bg3le wizard to "
                    f"repair it, or add {wrapper_option()} to them.")
    except Exception:
        return None
    return None


# ---- install ----------------------------------------------------------------

def _fetch_latest(log: LogFn) -> "tuple[str, str] | None":
    """(tag, zip url) of the newest release, or None."""
    from Utils.ca_bundle import get_ssl_context
    try:
        req = urllib.request.Request(
            _LATEST_API, headers={"User-Agent": "Amethyst-Mod-Manager"})
        with urllib.request.urlopen(req, timeout=30,
                                    context=get_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log(f"could not reach the bg3le release feed: {exc}")
        return None
    for asset in data.get("assets") or []:
        name = asset.get("name") or ""
        if name.endswith(_ASSET_SUFFIX):
            return data.get("tag_name") or "", asset.get("browser_download_url") or ""
    log(f"the latest bg3le release has no {_ASSET_SUFFIX} asset")
    return None


def _extract(archive: Path, dest: Path) -> "Path | None":
    """Unpack the release zip; returns the folder holding install.py."""
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest.resolve()) + os.sep):
                raise ValueError(f"unsafe path in archive: {member.filename}")
        z.extractall(dest)
    for installer in sorted(dest.glob("*/install.py")) + [dest / "install.py"]:
        if installer.is_file():
            return installer.parent
    return None


def _run_installer(folder: Path, log: LogFn) -> bool:
    """Run bg3le's own install.py; it reports Steam running and exits."""
    # Zip extraction drops the executable bits the installer copies over.
    for rel in ("build/libbg3le.so", "installer/" + WRAPPER_NAME, "client/bg3lua"):
        path = folder / rel
        if path.is_file():
            path.chmod(0o755)
    python = sys.executable or shutil.which("python3") or "python3"
    proc = subprocess.Popen(
        [python, str(folder / "install.py")], cwd=folder,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout or ():
        log(line.rstrip())
    return proc.wait() == 0


def install_bg3le(log_fn: "LogFn | None" = None) -> bool:
    """Download the newest bg3le release and run its installer."""
    log = log_fn or _noop
    if _in_flatpak():
        log("Amethyst is running as a Flatpak; bg3le has to be installed on the "
            f"host. Download it from {RELEASES_URL} and run ./install.py.")
        return False
    latest = _fetch_latest(log)
    if latest is None:
        return False
    tag, url = latest
    log(f"downloading bg3le {tag}")
    from Utils.ca_bundle import download_file
    with tempfile.TemporaryDirectory(prefix="amethyst-bg3le-") as tmp:
        archive = Path(tmp) / url.rsplit("/", 1)[-1]
        try:
            download_file(url, archive)
            folder = _extract(archive, Path(tmp) / "release")
        except Exception as exc:
            log(f"could not download or unpack bg3le: {exc}")
            return False
        if folder is None:
            log("the bg3le release has no install.py")
            return False
        return _run_installer(folder, log)
