"""On-demand install of ``umu-run`` into the manager's tools folder.

A Steam-less machine (Heroic/Lutris/GOG only, GH#320) cannot run a raw
``python3 proton <verb>``: there is no Steam runtime behind it, so Wine starts
bare and dies in a wall of missing-FreeType errors. ``proton_run_command``
already reroutes those launches through ``umu-run``, which starts Proton inside
the Steam Linux Runtime container with no Steam client at all — but only if the
box happens to have a umu from Heroic, Lutris or Faugus. When it doesn't, every
Proton path dead-ends on :data:`~Utils.steam_finder.STEAMLESS_NO_UMU_MESSAGE`
and the user is told to go install another launcher.

umu ships as a single ~410 KB zipapp with a ``#!/usr/bin/env python3`` shebang,
so we can just fetch it the way we already fetch winetricks and cabextract:
into ``~/.config/AmethystModManager/tools/``, never into the AppImage/Flatpak
bundle. Bundling would be worse on every axis — it needs a *host* python3
anyway (we strip the bundle's loader env before every Proton call), its real
payload is the ~1 GB Steam Linux Runtime it downloads on first run regardless,
under Flatpak it must live on host-visible disk to be executed via
flatpak-spawn, and an in-bundle copy could only update when we cut a release.

Nothing here runs for the majority of users: :func:`ensure_umu_run` early-outs
as soon as it sees a Steam client or an existing umu.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

_LATEST_API = ("https://api.github.com/repos/Open-Wine-Components/"
               "umu-launcher/releases/latest")

# Upstream publishes one arch-independent zipapp tarball per release
# ("umu-launcher-<ver>-zipapp.tar") holding a single executable "umu/umu-run".
_ZIPAPP_SUFFIX = "-zipapp.tar"

# Don't re-check GitHub on every launch once we own a copy.
_UPDATE_INTERVAL = 7 * 24 * 3600

# One download attempt per session: a box that is offline (or rate-limited)
# must not re-try on every tool launch.
_attempted = False
_attempt_lock = threading.Lock()


def _noop(_msg: str) -> None:
    pass


def _log_fn(log_fn: "LogFn | None") -> LogFn:
    if log_fn is not None:
        return log_fn
    try:
        from Utils.app_log import app_log
        return lambda m: app_log(f"umu: {m}")
    except Exception:
        return _noop


def bundled_umu_run() -> Path:
    """Path our downloaded copy lives at (may not exist)."""
    from Utils.config_paths import get_tools_dir
    return get_tools_dir() / "umu-run"


def _state_path() -> Path:
    return bundled_umu_run().with_suffix(".json")


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(tag: str, checked: float) -> None:
    try:
        _state_path().write_text(
            json.dumps({"tag": tag, "checked": checked}, indent=2),
            encoding="utf-8")
    except OSError:
        pass


def installed_tag() -> str:
    """Release tag of our downloaded copy ('' when we don't own one)."""
    return _read_state().get("tag", "") if bundled_umu_run().is_file() else ""


def _fetch_latest() -> "tuple[str, str] | None":
    """``(tag, zipapp_url)`` for the newest upstream release, or None."""
    from Utils.ca_bundle import get_ssl_context
    req = urllib.request.Request(
        _LATEST_API, headers={"User-Agent": "Amethyst-Mod-Manager"})
    try:
        with urllib.request.urlopen(req, timeout=15,
                                    context=get_ssl_context()) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    tag = data.get("tag_name") or ""
    for asset in data.get("assets", []):
        name = asset.get("name") or ""
        if name.endswith(_ZIPAPP_SUFFIX):
            url = asset.get("browser_download_url")
            if tag and url:
                return tag, url
    return None


def _extract_zipapp(tar_bytes: bytes, dest: Path) -> bool:
    """Write the single ``umu-run`` member of *tar_bytes* to *dest*.

    Pulls the one member out by name rather than calling ``extractall`` — a
    tarball off the network must never be allowed to choose its own paths.
    Written to a temp file and renamed, so a copy that is currently executing
    keeps its own inode and a failed write can't leave a truncated launcher.
    """
    tmp = dest.with_suffix(".part")
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r|") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                if Path(member.name).name != "umu-run":
                    continue
                src = tf.extractfile(member)
                if src is None:
                    continue
                tmp.write_bytes(src.read())
                break
            else:
                return False
    except (OSError, tarfile.TarError):
        tmp.unlink(missing_ok=True)
        return False

    try:
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, dest)
    except OSError:
        tmp.unlink(missing_ok=True)
        return False
    return True


def install_umu_run(log_fn: "LogFn | None" = None) -> bool:
    """Download the latest umu-run zipapp into the tools folder.

    Blocking (small — ~0.4 MB); call from a worker thread. Returns True when a
    usable launcher is in place afterwards.
    """
    log = _log_fn(log_fn)
    latest = _fetch_latest()
    if latest is None:
        log("could not reach the umu-launcher release feed.")
        return False
    tag, url = latest

    dest = bundled_umu_run()
    if dest.is_file() and installed_tag() == tag:
        _write_state(tag, time.time())
        return True

    log(f"downloading umu-launcher {tag} …")
    from Utils.ca_bundle import get_ssl_context
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Amethyst-Mod-Manager"})
        with urllib.request.urlopen(req, timeout=60,
                                    context=get_ssl_context()) as resp:
            payload = resp.read()
    except Exception as exc:
        log(f"umu-launcher download failed: {exc}")
        return False

    if not _extract_zipapp(payload, dest):
        log("no umu-run found inside the downloaded zipapp archive.")
        return False

    _write_state(tag, time.time())
    log(f"umu-launcher {tag} installed to {dest}.")
    return True


def _update_due() -> bool:
    state = _read_state()
    if not state.get("tag"):
        return False
    return (time.time() - float(state.get("checked", 0))) > _UPDATE_INTERVAL


def maybe_update_umu_run(log_fn: "LogFn | None" = None) -> None:
    """Refresh our copy in the background, at most once a week.

    Only ever touches a launcher we installed ourselves — a umu belonging to
    Heroic, Lutris or the distro package is that owner's to update. Runs
    detached so a launch never waits on GitHub.
    """
    if not _update_due():
        return
    # Stamp before starting so a slow/failed check doesn't queue another
    # thread on the next launch.
    _write_state(installed_tag(), time.time())
    threading.Thread(
        target=lambda: install_umu_run(log_fn),
        name="umu-update", daemon=True,
    ).start()


def ensure_umu_run(log_fn: "LogFn | None" = None) -> "Path | None":
    """Return a usable ``umu-run``, installing one if this box has no launcher.

    Early-outs (no network, a few stat calls) whenever a Steam client is
    present or some umu already exists, so the common case pays almost nothing.
    Call before resolving a Proton launch; safe from any worker thread.
    """
    global _attempted
    from Utils.steam_finder import steam_client_installed

    if steam_client_installed():
        # Steam's runtime is there — proton_run_command uses the proton script
        # directly and never needs umu.
        return None

    from Utils.lutris_finder import find_umu_run
    found = find_umu_run()
    if found is not None:
        if found == bundled_umu_run():
            maybe_update_umu_run(log_fn)
        return found

    with _attempt_lock:
        if _attempted:
            return None
        _attempted = True

    log = _log_fn(log_fn)
    log("no Steam client and no umu-run on this system — fetching a copy so "
        "Proton can run inside the Steam Linux Runtime.")
    if not install_umu_run(log):
        return None
    return find_umu_run()
