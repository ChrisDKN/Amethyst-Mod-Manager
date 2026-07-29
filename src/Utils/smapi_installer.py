"""Neutral (GUI-free) helpers for the SMAPI install wizard.

Extracted from the Tk ``sdv_smapi`` plugin so the download / install logic can
be reused by the Qt view and unit-tested without a GUI toolkit.  No tkinter or
Qt imports here.

Pipeline:
  * ``fetch_latest_smapi_asset()`` — latest installer-zip URL from GitHub.
  * ``download_smapi(url, dest, reporthook)`` — HTTPS download via the app CA
    bundle.
  * ``install_smapi(game, archive, mode, ...)`` — unattended install into the
    game folder / Root_Folder staging / a managed root-flagged mod.

Why we don't run SMAPI's own installer
--------------------------------------
The bundled ``internal/linux/SMAPI.Installer`` is an interactive .NET console
app: it asks the player to pick install-vs-uninstall and confirm the game
folder, then writes into that folder directly.  Driving it needed a terminal
emulator (konsole/xterm/…), which is fragile from a Flatpak/AppImage sandbox,
required user input, and can only ever target the real game folder — so it
could not honour our Root_Folder / managed-mod destinations.

Its payload, however, is just ``internal/linux/install.dat`` — a plain zip (the
SMAPI README documents renaming it to ``.zip``).  The installer's remaining
work is three documented steps we reproduce natively here:

  1. Unpack ``install.dat`` into the game folder.
  2. Copy ``Stardew Valley.deps.json`` → ``StardewModdingAPI.deps.json``.
  3. Rename the launcher: ``StardewValley`` → ``StardewValley-original`` and
     ``StardewModdingAPI`` → ``StardewValley``, so launching the game normally
     starts SMAPI.

Steps 2 and 3 touch *vanilla* game files, so they behave differently per
destination — see :func:`install_smapi` and :func:`_write_launcher_shim`.
"""

from __future__ import annotations

import json as _json
import os
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame

LogFn = Callable[[str], None]

_GITHUB_API_URL = "https://api.github.com/repos/Pathoschild/SMAPI/releases/latest"

#: Payload zip inside the installer archive, in preference order.  SMAPI has
#: shipped this under both ``unix`` (older) and ``linux`` (current) folders.
_PAYLOAD_CANDIDATES = ("internal/linux/install.dat", "internal/unix/install.dat")

#: Vanilla launcher (no extension) that SMAPI displaces.
_GAME_LAUNCHER = "StardewValley"
_GAME_LAUNCHER_BACKUP = "StardewValley-original"
_SMAPI_LAUNCHER = "StardewModdingAPI"

#: deps.json copy — SMAPI reuses the game's dependency manifest.
_GAME_DEPS = "Stardew Valley.deps.json"
_SMAPI_DEPS = "StardewModdingAPI.deps.json"


def _noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Fetch + download
# ---------------------------------------------------------------------------

def fetch_latest_smapi_asset() -> tuple[str, str]:
    """Return (version_tag, download_url) for the latest SMAPI installer zip."""
    req = urllib.request.Request(
        _GITHUB_API_URL,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "ModManager/1.0"},
    )
    from Utils.ca_bundle import get_ssl_context
    with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
        data = _json.loads(resp.read().decode())
    tag = data.get("tag_name", "unknown")
    assets = data.get("assets", [])
    for asset in assets:
        nl = asset.get("name", "").lower()
        if (nl.endswith(".zip") and "smapi" in nl and "installer" in nl
                and "double" not in nl):
            return tag, asset["browser_download_url"]
    for asset in assets:
        nl = asset.get("name", "").lower()
        if nl.endswith(".zip") and "smapi" in nl and "double" not in nl:
            return tag, asset["browser_download_url"]
    raise RuntimeError("No SMAPI installer zip found in the latest GitHub release.")


def download_smapi(url: str, dest: Path, reporthook=None) -> None:
    """Download *url* to *dest* over HTTPS using the app's resolved CA bundle."""
    from Utils.ca_bundle import download_file
    download_file(url, dest, reporthook=reporthook)


# ---------------------------------------------------------------------------
# Payload extraction
# ---------------------------------------------------------------------------

def _chmod_exec(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode
                   | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def find_payload(installer_root: Path) -> Path:
    """Return the Linux ``install.dat`` payload zip inside an unpacked installer.

    The release zip wraps everything in a top-level ``SMAPI x.y.z installer/``
    folder, so match on the path *suffix* rather than an exact relative path.
    The archive also ships ``internal/windows`` and ``internal/macOS``
    payloads — picking either of those would install the wrong platform's
    binaries, so only the documented Linux/unix locations are accepted.
    """
    matches = sorted(installer_root.rglob("install.dat"))
    for rel in _PAYLOAD_CANDIDATES:
        for cand in matches:
            if cand.is_file() and cand.as_posix().endswith("/" + rel):
                return cand
    # No wrapper dir at all (payload sitting at the root of the search path).
    for rel in _PAYLOAD_CANDIDATES:
        cand = installer_root / rel
        if cand.is_file():
            return cand
    raise RuntimeError(
        "Could not find 'internal/linux/install.dat' inside the SMAPI "
        "archive — the archive may be corrupt or not a SMAPI installer.")


def extract_smapi_payload(archive: Path, dest: Path,
                          log_fn: LogFn = _noop) -> int:
    """Unpack the SMAPI payload from installer *archive* into *dest*.

    *archive* is the downloaded ``SMAPI-x.y.z-installer.zip``; the real files
    live in a nested ``install.dat`` zip.  Returns the number of files written.
    Marks the SMAPI launcher and ``unix-launcher.sh`` executable — the zip
    stores POSIX modes but Python's zipfile drops them on extract.
    """
    cache_root = Path.home() / ".cache" / "amethyst-smapi"
    cache_root.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="smapi_", dir=str(cache_root)))
    try:
        if not archive.is_file():
            raise RuntimeError("Archive not found.")
        if not archive.name.lower().endswith(".zip"):
            raise RuntimeError(f"Unsupported archive format: {archive.name}")

        log_fn(f"SMAPI Wizard: unpacking {archive.name}")
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(tmp_dir)

        payload = find_payload(tmp_dir)
        log_fn(f"SMAPI Wizard: extracting payload {payload.name} → {dest}")

        dest.mkdir(parents=True, exist_ok=True)
        count = 0
        with zipfile.ZipFile(payload, "r") as zf:
            zf.extractall(dest)
            count = sum(1 for i in zf.infolist() if not i.is_dir())

        # Restore the executable bit the zip module discards.
        for name in (_SMAPI_LAUNCHER, "unix-launcher.sh"):
            p = dest / name
            if p.is_file():
                _chmod_exec(p)
        return count
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Launcher wiring
# ---------------------------------------------------------------------------

def wire_game_folder(game_dir: Path, log_fn: LogFn = _noop) -> None:
    """Apply the in-place launcher swap inside a real *game_dir*.

    Copies the game's deps.json for SMAPI, then renames the vanilla launcher
    aside and puts SMAPI's in its place. Idempotent: re-running after SMAPI is
    already installed will not clobber ``StardewValley-original`` with the
    SMAPI launcher.
    """
    deps_src = game_dir / _GAME_DEPS
    deps_dst = game_dir / _SMAPI_DEPS
    if deps_src.is_file():
        shutil.copy2(deps_src, deps_dst)
        log_fn(f"SMAPI Wizard: copied {_GAME_DEPS} → {_SMAPI_DEPS}")
    else:
        log_fn(f"SMAPI Wizard: warning — {_GAME_DEPS} not found in the game "
               "folder; SMAPI may fail to start.")

    vanilla = game_dir / _GAME_LAUNCHER
    backup = game_dir / _GAME_LAUNCHER_BACKUP
    smapi = game_dir / _SMAPI_LAUNCHER

    if not smapi.is_file():
        raise RuntimeError(
            f"'{_SMAPI_LAUNCHER}' is missing after extraction — "
            "the SMAPI payload did not unpack correctly.")

    if backup.is_file():
        # Already installed once: `vanilla` is a previous SMAPI launcher, so
        # overwrite it and leave the real backup untouched.
        log_fn(f"SMAPI Wizard: {_GAME_LAUNCHER_BACKUP} already present — "
               "reusing the existing vanilla backup.")
    elif vanilla.is_file():
        shutil.move(str(vanilla), str(backup))
        log_fn(f"SMAPI Wizard: renamed {_GAME_LAUNCHER} → {_GAME_LAUNCHER_BACKUP}")
    else:
        log_fn(f"SMAPI Wizard: warning — no {_GAME_LAUNCHER} launcher found "
               "to back up.")

    shutil.copy2(smapi, vanilla)
    _chmod_exec(vanilla)
    log_fn(f"SMAPI Wizard: installed {_SMAPI_LAUNCHER} as {_GAME_LAUNCHER}")


def _write_launcher_shim(dest: Path, log_fn: LogFn = _noop) -> None:
    """Write a ``StardewValley`` launcher into a *staged* payload.

    For staging destinations (Root_Folder / managed mod) we cannot rename the
    vanilla launcher — it lives in the game folder and is restored on every
    deploy cycle.  Instead we ship our own ``StardewValley`` script that the
    deploy overlays on top of the vanilla one; it execs SMAPI from the same
    folder.  Deploy backs the vanilla launcher up to the _Core folder, so the
    original is recovered on restore.
    """
    shim = dest / _GAME_LAUNCHER
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "# Installed by Amethyst Mod Manager — launches SMAPI instead of the\n"
        "# vanilla game. The original launcher is preserved by the mod\n"
        "# manager's deploy backup (Stardew Valley restore puts it back).\n"
        'cd "$(dirname "$0")" || exit $?\n'
        'exec ./StardewModdingAPI "$@"\n',
        encoding="utf-8",
    )
    _chmod_exec(shim)
    log_fn(f"SMAPI Wizard: wrote {_GAME_LAUNCHER} launcher shim into the payload.")


def _stage_deps_json(game: "BaseGame", dest: Path, log_fn: LogFn = _noop) -> None:
    """Copy ``StardewModdingAPI.deps.json`` into a staged payload.

    Sourced from the game folder's ``Stardew Valley.deps.json`` (vanilla file,
    always present in a real install).  Staged rather than generated so the
    deployed SMAPI sees the same manifest the official installer would create.
    """
    game_dir = game.get_game_path()
    if game_dir is None:
        log_fn("SMAPI Wizard: warning — game path not configured, cannot stage "
               f"{_SMAPI_DEPS}.")
        return
    src = Path(game_dir) / _GAME_DEPS
    if not src.is_file():
        log_fn(f"SMAPI Wizard: warning — {_GAME_DEPS} not found in the game "
               f"folder; {_SMAPI_DEPS} was not staged.")
        return
    shutil.copy2(src, dest / _SMAPI_DEPS)
    log_fn(f"SMAPI Wizard: staged {_SMAPI_DEPS} from the game folder.")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def install_smapi(
    game: "BaseGame",
    archive: Path,
    mode: str = "game",
    *,
    mod_name: str = "SMAPI",
    modlist_path: "Path | None" = None,
    restore_first: bool = True,
    delete_archive: bool = True,
    log_fn: LogFn = _noop,
) -> tuple[str, int, "str | None"]:
    """Install SMAPI from *archive* with no user interaction.

    mode — ``"game"`` (game folder, restoring to vanilla first when
    *restore_first*), ``"root"`` (Root_Folder staging) or ``"mod"`` (a managed
    root-flagged mod, registered in the modlist and indexed so it deploys
    without a manual Refresh).

    Returns ``(dest_label, file_count, mod_name-or-None)``.  Blocking — call
    from a worker thread; does no UI work.  The caller reloads the modlist on
    the GUI thread when mode == "mod".
    """
    from Utils.install_as_mod import index_installed_mod, register_as_mod_neutral

    if archive is None or not archive.is_file():
        raise RuntimeError("Archive not found.")

    installed_mod: "str | None" = None

    if mode == "mod":
        staging = game.get_effective_mod_staging_path()
        if staging is None:
            raise RuntimeError("Mod staging path is not configured.")
        installed_mod = mod_name
        dest = Path(staging) / mod_name
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        dest_label = f"mod folder ({mod_name})"
    elif mode == "root":
        dest = game.get_effective_root_folder_path()
        dest.mkdir(parents=True, exist_ok=True)
        dest_label = "Root_Folder (staging)"
    else:
        game_dir = game.get_game_path()
        if game_dir is None:
            raise RuntimeError("Game path is not configured.")
        dest = Path(game_dir)
        dest_label = "game folder"
        if restore_first:
            # Revert to vanilla so we swap the REAL launcher, not a deployed
            # one. A failed restore must abort the install: swapping inside a
            # deployed root leaves post-snapshot files that the next restore
            # sweeps into overwrite/. (The Qt wizard restores via the app's
            # restore machinery instead and passes restore_first=False.)
            log_fn("SMAPI Wizard: restoring game to vanilla state…")
            game.restore(log_fn=log_fn)

    file_count = extract_smapi_payload(archive, dest, log_fn=log_fn)
    log_fn(f"SMAPI Wizard: extracted {file_count} file(s).")

    if mode == "game":
        wire_game_folder(dest, log_fn=log_fn)
    else:
        # Staged: the vanilla launcher isn't ours to rename, so ship a shim
        # that the deploy overlays, plus SMAPI's deps.json.
        _stage_deps_json(game, dest, log_fn=log_fn)
        _write_launcher_shim(dest, log_fn=log_fn)

    if mode == "mod" and installed_mod is not None:
        register_as_mod_neutral(
            game, installed_mod, archive,
            modlist_path=modlist_path, log_fn=log_fn, root_folder=True)
        index_installed_mod(game, installed_mod, log_fn=log_fn)

    if delete_archive:
        try:
            archive.unlink()
            log_fn(f"SMAPI Wizard: deleted {archive.name} from Downloads.")
        except OSError as exc:
            log_fn(f"SMAPI Wizard: could not delete archive: {exc}")

    log_fn(f"SMAPI Wizard: SMAPI installed into the {dest_label}.")
    return dest_label, file_count, installed_mod
