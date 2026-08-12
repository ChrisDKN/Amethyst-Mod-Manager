"""
GUI-neutral archive download/locate/extract primitives for wizard tools.

Moved out of wizards/script_extender.py (which imports customtkinter) so the
Qt wizard views can share them. These are deliberately generic - the script
extender, BepInEx, Wrye Bash, DynDOLOD, TTW … wizards all follow the same
"fetch archive → find it in ~/Downloads → extract to game/root/mod" shape,
and Morrowind's MGE XE / MCP wizards already use them as a library.

Everything here is pure stdlib (+ optional py7zr fallback); the only project
imports are lazy (ca_bundle for TLS, _install_as_mod for the managed-mod
registration inside install_archive_payload).
"""

from __future__ import annotations

import json as _json
import os
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from Utils.xdg import xdg_download_dir

try:
    import py7zr
except ImportError:
    py7zr = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from Games.base_game import BaseGame

ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz"}


def _noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# GitHub release fetch
# ---------------------------------------------------------------------------

def fetch_latest_github_asset(api_url: str, archive_keywords: list[str]) -> tuple[str, str]:
    """Return (version_tag, download_url) for the latest release asset matching *archive_keywords*."""
    req = urllib.request.Request(
        api_url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ModManager/1.0"},
    )
    from Utils.ca_bundle import get_ssl_context
    with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
        data = _json.loads(resp.read().decode())
    tag = data.get("tag_name", "unknown")
    for asset in data.get("assets", []):
        name: str = asset.get("name", "").lower()
        if not any(name.endswith(ext) for ext in ARCHIVE_EXTS):
            continue
        if all(kw in name for kw in archive_keywords):
            return tag, asset["browser_download_url"]
    raise RuntimeError(f"No matching asset found in the latest GitHub release ({tag}).")


# ---------------------------------------------------------------------------
# Locate
# ---------------------------------------------------------------------------

def get_downloads_dir() -> Path:
    return xdg_download_dir()


def is_archive(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in ARCHIVE_EXTS)


def find_archive(directory: Path, keywords: list[str]) -> Path | None:
    """Search *directory* for the most-recently-modified archive matching all *keywords*."""
    if not directory.is_dir() or not keywords:
        return None
    for entry in sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not entry.is_file() or not is_archive(entry.name):
            continue
        low = entry.name.lower()
        if all(kw in low for kw in keywords):
            return entry
    return None


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------

def extract_to_dir(archive: Path, dest: Path) -> None:
    """Extract *archive* into *dest* (low-level, no flattening)."""
    name_lower = archive.name.lower()

    if name_lower.endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(dest)

    elif name_lower.endswith(".7z"):
        extracted_via_cli = False
        # Prefer a native 7-zip binary - the Flatpak bundles `7zz` at
        # /app/bin and the AppImage bundles `7zzs`. py7zr is a last resort:
        # it can't decode the BCJ2 filter that SKSE-style archives use.
        _7z_bin = (
            shutil.which("7zzs") or shutil.which("7zz")
            or shutil.which("7z") or shutil.which("7za")
        )
        if _7z_bin:
            result = subprocess.run(
                [_7z_bin, "x", str(archive), f"-o{dest}", "-y"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            extracted_via_cli = result.returncode == 0

        # bsdtar (libarchive) also handles BCJ2 and is broadly available.
        if not extracted_via_cli:
            _bsdtar_bin = shutil.which("bsdtar")
            if _bsdtar_bin:
                result = subprocess.run(
                    [_bsdtar_bin, "-xf", str(archive), "-C", str(dest)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                extracted_via_cli = result.returncode == 0

        if not extracted_via_cli:
            if py7zr is None:
                raise RuntimeError(
                    "Cannot extract .7z archive: no native 7z/bsdtar command "
                    "was found and py7zr is not installed."
                )
            with py7zr.SevenZipFile(archive, "r") as zf:
                zf.extractall(dest)

    elif name_lower.endswith((".tar.zst", ".tzst")):
        _extract_tar_zst(archive, dest)

    elif name_lower.endswith((".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
        with tarfile.open(archive, "r:*") as tf:
            tf.extractall(dest, filter="data")
    else:
        raise RuntimeError(f"Unsupported archive format: {archive.name}")


def _zstd_module():
    """Return a module exposing ``open(path, "rb")`` for zstd, or None.

    ``compression.zstd`` is stdlib from 3.14; ``backports.zstd`` is the
    same API on older interpreters and is already vendored (it ships in the
    AppImage and the flatpak), so this is the portable path - unlike bsdtar
    or 7z, it needs nothing on PATH.
    """
    try:
        from compression import zstd            # Python 3.14+
        return zstd
    except ImportError:
        pass
    try:
        from backports import zstd              # vendored backport
        return zstd
    except ImportError:
        return None


def _extract_tar_zst(archive: Path, dest: Path) -> None:
    """Extract a zstd-compressed tar into *dest*.

    Python's ``tarfile`` gained no zstd support of its own, so the stream is
    decompressed first. Falls back to bsdtar (bundled in the AppImage,
    libarchive links libzstd) and then to 7-Zip, which only unwraps the
    ``.zst`` container and needs a second pass over the inner ``.tar``.
    """
    zstd = _zstd_module()
    if zstd is not None:
        # Streaming mode ("r|"): a zstd stream isn't seekable, and the whole
        # tar is walked once anyway.
        with zstd.open(archive, "rb") as zf, \
                tarfile.open(fileobj=zf, mode="r|") as tf:
            tf.extractall(dest, filter="data")
        return

    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        result = subprocess.run(
            [bsdtar, "-xf", str(archive), "-C", str(dest)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        if result.returncode == 0:
            return

    _7z_bin = (shutil.which("7zzs") or shutil.which("7zz")
               or shutil.which("7z") or shutil.which("7za"))
    if _7z_bin:
        import tempfile
        with tempfile.TemporaryDirectory() as stage:
            # 7z treats .tar.zst as a zstd container around a .tar, so the
            # first pass yields the tar and the second unpacks it.
            r1 = subprocess.run(
                [_7z_bin, "x", str(archive), f"-o{stage}", "-y"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            inner = [p for p in Path(stage).iterdir() if p.is_file()]
            if r1.returncode == 0 and inner:
                with tarfile.open(inner[0], "r:") as tf:
                    tf.extractall(dest, filter="data")
                return

    raise RuntimeError(
        "Cannot extract .tar.zst: no zstd module (compression.zstd / "
        "backports.zstd) and neither bsdtar nor 7z is available.")


def _strip_single_top_dir(tmp: Path) -> Path:
    """If *tmp* contains a single top-level directory, return it so the
    caller can copy its *contents* instead of the wrapper folder."""
    entries = [e for e in tmp.iterdir() if e.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return tmp


def extract_archive(archive: Path, dest: Path) -> list[Path]:
    """Extract *archive* into *dest*, stripping a single top-level wrapper
    directory if present (e.g. ``f4se_0_07_07/`` -> contents go straight
    into *dest*).

    Returns created paths in **reverse depth order** (deepest first) so
    callers can delete files before their parent directories.
    """
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    try:
        extract_to_dir(archive, tmp)
        src = _strip_single_top_dir(tmp)

        created: list[Path] = []
        for root, _dirs, files in os.walk(src):
            for f in files:
                src_file = Path(root) / f
                rel = src_file.relative_to(src)
                dst_file = dest / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_file), str(dst_file))
                created.append(dst_file)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    dirs: set[Path] = set()
    for p in created:
        rel = p.relative_to(dest)
        for parent in rel.parents:
            if parent != Path("."):
                dirs.add(dest / parent)

    return list(created) + sorted(dirs, key=lambda p: len(p.parts), reverse=True)


# ---------------------------------------------------------------------------
# Install orchestrator (extract to game / Root_Folder / managed mod)
# ---------------------------------------------------------------------------

def install_archive_payload(
    game: "BaseGame",
    archive: Path,
    mode: str,
    *,
    mod_fallback_name: str,
    modlist_path: "Path | None" = None,
    restore_first: bool = True,
    delete_archive: bool = True,
    log_fn: Callable[[str], None] = _noop,
) -> tuple[str, int, "str | None"]:
    """Extract *archive* into the wizard-standard destination for *mode*.

    mode - "game" (game root, restoring to vanilla first when *restore_first*),
    "root" (Root_Folder staging), or "mod" (a managed root-flagged mod named
    via derive_mod_name, registered in the modlist AND indexed so it deploys
    without a manual Refresh - the Tk wizards relied on the mod panel's
    reload for that).

    Returns (dest_label, file_count, mod_name-or-None). Raises on failure.
    Blocking; call from a worker thread. Does NO UI work - the caller reloads
    the modlist on the GUI thread afterwards when mode == "mod".
    """
    from Utils.install_as_mod import (
        derive_mod_name, index_installed_mod, register_as_mod_neutral,
    )

    if archive is None or not archive.is_file():
        raise RuntimeError("Archive not found.")

    mod_name: "str | None" = None
    if mode == "mod":
        staging = game.get_effective_mod_staging_path()
        if staging is None:
            raise RuntimeError("Mod staging path is not configured.")
        mod_name = derive_mod_name(archive, fallback=mod_fallback_name)
        dest = staging / mod_name
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
    elif mode == "root":
        dest = game.get_effective_root_folder_path()
        dest.mkdir(parents=True, exist_ok=True)
    else:
        dest = game.get_game_path()
        if dest is None:
            raise RuntimeError("Game path is not configured.")
        if restore_first:
            # Revert to vanilla so the extractor writes onto clean files
            # (mirrors the Tk wizard's pre-extract restore).
            log_fn("Wizard: restoring game to vanilla state…")
            try:
                game.restore(log_fn=log_fn)
            except Exception as exc:
                log_fn(f"Wizard: restore skipped or failed: {exc}")

    dest_label = {
        "mod": f"mod folder ({mod_name})",
        "root": "Root_Folder (staging)",
        "game": "game folder",
    }[mode if mode in ("mod", "root") else "game"]
    log_fn(f"Wizard: extracting {archive.name} → {dest}")

    paths = extract_archive(archive, dest)
    file_count = len([p for p in paths if p.is_file()])
    log_fn(f"Wizard: extracted {file_count} file(s).")

    if mode == "mod" and mod_name is not None:
        register_as_mod_neutral(
            game, mod_name, archive,
            modlist_path=modlist_path, log_fn=log_fn, root_folder=True)
        # Files are on disk now - index them so the next deploy sees the mod.
        index_installed_mod(game, mod_name, log_fn=log_fn)

    if delete_archive:
        try:
            archive.unlink()
            log_fn(f"Wizard: deleted {archive.name} from Downloads.")
        except OSError as exc:
            log_fn(f"Wizard: could not delete archive: {exc}")

    return dest_label, file_count, mod_name
