"""
GUI-neutral core of the Creation Kit wizard (CKPE install + gates).

Moved out of wizards/creationkit.py (which imports customtkinter) so the Qt
wizard view can share it.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame

EXE_NAME = "CreationKit.exe"
CKPE_MOD_NAME = "Creation Kit Platform Extended"
CKPE_GITHUB_API = (
    "https://api.github.com/repos/Perchik71/Creation-Kit-Platform-Extended/releases/latest"
)


def _noop(_msg: str) -> None:
    pass


def creationkit_exe_path(game: "BaseGame") -> Path | None:
    root = game.get_game_path()
    if root is None:
        return None
    p = root / EXE_NAME
    return p if p.is_file() else None


def ckpe_mod_installed(game: "BaseGame") -> bool:
    """True if a CKPE mod staging folder already exists."""
    try:
        staging = game.get_effective_mod_staging_path()
    except Exception:
        staging = None
    return staging is not None and (staging / CKPE_MOD_NAME).is_dir()


def pick_ckpe_asset(assets: list[dict]) -> "tuple[str, str] | None":
    """Pick the normal SSE CKPE archive (not the noavx variant).

    Returns (asset_name, download_url) or None. Prefers an archive whose name
    contains ``sse`` and not ``noavx``; falls back to the first non-noavx
    archive when no ``sse`` token is present.
    """
    from Utils.wizard_archives import is_archive

    archives = [
        a for a in assets
        if is_archive(a.get("name", "")) and "noavx" not in a.get("name", "").lower()
    ]
    if not archives:
        return None
    sse = [a for a in archives if "sse" in a.get("name", "").lower()]
    chosen = (sse or archives)[0]
    return chosen.get("name", ""), chosen.get("browser_download_url", "")


def install_ckpe_mod(game: "BaseGame", *,
                     status_fn: Callable[[str], None] = _noop,
                     log_fn: Callable[[str], None] = _noop) -> str:
    """Download the latest CKPE release and install it as a root-flagged
    managed mod.  Returns the release tag.  Blocking; call from a worker
    thread - does NO UI work (status_fn/log_fn only receive strings).

    Mirrors the Tk wizard's _do_ckpe_install: register the mod (meta.ini
    rootFolder=true + modlist entry), extract CKPE into it so the
    winhttp.dll loader sits at the staging root, add the empty CKPEPlugins/
    folder CKPE needs at startup, and index the mod so the next deploy sees
    the files (build_filemap reads modindex.bin).
    """
    import shutil
    import tempfile

    from Utils.ca_bundle import download_file, get_ssl_context
    from Utils.wizard_archives import extract_archive
    from Utils.install_as_mod import index_installed_mod, register_as_mod_neutral

    req = urllib.request.Request(
        CKPE_GITHUB_API,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "ModManager/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
        data = json.loads(resp.read().decode())

    tag = data.get("tag_name", "unknown")
    picked = pick_ckpe_asset(data.get("assets", []))
    if picked is None or not picked[1]:
        raise RuntimeError(
            f"No suitable SSE archive found in the latest CKPE release ({tag}).")
    asset_name, url = picked

    log_fn(f"downloading CKPE {tag} ({asset_name}) from {url}")
    status_fn(f"Downloading CKPE {tag}…")

    tmp_dir = Path(tempfile.mkdtemp())
    archive = tmp_dir / asset_name
    try:
        download_file(url, archive)

        mod_dir = register_as_mod_neutral(
            game, CKPE_MOD_NAME, archive, log_fn=log_fn, root_folder=True)

        status_fn("Extracting CKPE…")
        log_fn(f"extracting {archive.name} → {mod_dir}")
        paths = extract_archive(archive, mod_dir)
        file_count = len([p for p in paths if p.is_file()])
        log_fn(f"extracted {file_count} file(s).")

        # CKPE scans a CKPEPlugins/ folder at startup and crashes with a
        # null-deref if it is missing.  Ship an empty one in the mod payload
        # so it deploys into the game root with CKPE; the folder needs a file
        # inside it to survive staging/deploy.
        placeholder = mod_dir / "CKPEPlugins" / "CKPEPlugins.txt"
        placeholder.parent.mkdir(parents=True, exist_ok=True)
        placeholder.touch(exist_ok=True)
        log_fn("added empty CKPEPlugins/ folder (prevents CKPE startup crash).")

        # build_filemap reads modindex.bin (fast path) - index the mod now or
        # nothing reaches the game root on the deploy step.
        index_installed_mod(game, CKPE_MOD_NAME, log_fn=log_fn)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return tag
