"""
GUI-neutral archive primitives for wizard tools.

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

from Utils.environment.xdg import xdg_download_dir

try:
    import py7zr
except ImportError:
    py7zr = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from Games.base_game import BaseGame

ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz"}


def _noop(_msg: str) -> None:
    pass


_tool_versions: dict[str, str] = {}


def _extract_log(log_fn):
    target = log_fn
    if target is None:
        try:
            from Utils.app_log import app_log
            target = app_log
        except Exception:
            target = _noop

    def emit(message: str) -> None:
        try:
            target(message)
        except Exception:
            pass
    return emit


def _output_tail(text: str, limit: int = 1200) -> str:
    try:
        from Utils.processes.watch import redact_text
        text = redact_text(text)
    except Exception:
        text = str(text)
    clean = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    if not clean:
        return "no output"
    return clean if len(clean) <= limit else clean[-limit:]


def _tool_description(path: str) -> str:
    cached = _tool_versions.get(path)
    if cached is not None:
        return cached
    args = [path, "--version"] if Path(path).name == "bsdtar" else [path]
    version = ""
    try:
        result = subprocess.run(
            args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=3, check=False)
        version = next((line.strip() for line in (result.stdout or "").splitlines()
                        if line.strip()), "")
        version = version[:300]
    except (OSError, subprocess.SubprocessError):
        pass
    description = path + (f" ({version})" if version else "")
    _tool_versions[path] = description
    return description


def _run_extractor(args: list[str]):
    try:
        return subprocess.run(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            check=False), ""
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


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


def fetch_newest_github_asset(api_url: str, asset_keywords: list[str], *,
                              extensions: "set[str] | None" = None,
                              max_pages: int = 10) -> tuple[str, str]:
    """Return the newest matching asset across a repository's releases.

    Unlike :func:`fetch_latest_github_asset`, *api_url* points at the releases
    collection (``.../releases``), not ``.../releases/latest``. Releases are
    inspected newest-first and the first asset whose filename contains every
    keyword is returned. This suits multi-tool repositories where the newest
    release may not publish the requested application.

    When *extensions* is supplied, only filenames ending in one of those
    suffixes are considered. Up to *max_pages* of 100 releases are searched.
    """
    keywords = [str(keyword).lower() for keyword in asset_keywords]
    suffixes = ({str(extension).lower() for extension in extensions}
                if extensions is not None else None)

    for page in range(1, max(1, max_pages) + 1):
        separator = "&" if "?" in api_url else "?"
        page_url = f"{api_url}{separator}per_page=100&page={page}"
        req = urllib.request.Request(
            page_url,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "ModManager/1.0"},
        )
        from Utils.ca_bundle import get_ssl_context
        with urllib.request.urlopen(
                req, timeout=15, context=get_ssl_context()) as resp:
            releases = _json.loads(resp.read().decode())
        if not isinstance(releases, list):
            raise RuntimeError("GitHub returned an invalid releases response.")

        for release in releases:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            tag = release.get("tag_name", "unknown")
            for asset in release.get("assets", []):
                name = str(asset.get("name", "")).lower()
                if suffixes is not None and not any(
                        name.endswith(extension) for extension in suffixes):
                    continue
                if all(keyword in name for keyword in keywords):
                    url = asset.get("browser_download_url", "")
                    if url:
                        return str(tag), str(url)

        if len(releases) < 100:
            break

    wanted = ", ".join(asset_keywords)
    raise RuntimeError(
        f"No GitHub release asset matching '{wanted}' was found.")


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

def extract_to_dir(archive: Path, dest: Path, log_fn=None) -> None:
    """Extract *archive* into *dest* (low-level, no flattening)."""
    log = _extract_log(log_fn)
    name_lower = archive.name.lower()

    if name_lower.endswith(".zip"):
        log(f"Wizard extraction: using Python zipfile for {archive.name}.")
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
        failures: list[str] = []
        if _7z_bin:
            log(f"Wizard extraction: trying {_tool_description(_7z_bin)}.")
            result, spawn_error = _run_extractor(
                [_7z_bin, "x", str(archive), f"-o{dest}", "-y"])
            extracted_via_cli = result is not None and result.returncode == 0
            if extracted_via_cli:
                log(f"Wizard extraction: extracted with {_7z_bin}.")
            else:
                rc = result.returncode if result is not None else "not started"
                detail = (_output_tail(spawn_error) if result is None
                          else _output_tail(result.stderr or ""))
                failures.append(f"{_7z_bin} rc={rc}: {detail}")
                log(f"Wizard extraction: {_7z_bin} failed (rc={rc}): {detail}")

        # bsdtar (libarchive) also handles BCJ2 and is broadly available.
        if not extracted_via_cli:
            _bsdtar_bin = shutil.which("bsdtar")
            if _bsdtar_bin:
                log(f"Wizard extraction: trying {_tool_description(_bsdtar_bin)}.")
                result, spawn_error = _run_extractor(
                    [_bsdtar_bin, "-xf", str(archive), "-C", str(dest)])
                extracted_via_cli = result is not None and result.returncode == 0
                if extracted_via_cli:
                    log(f"Wizard extraction: extracted with {_bsdtar_bin}.")
                else:
                    rc = result.returncode if result is not None else "not started"
                    detail = (_output_tail(spawn_error) if result is None
                              else _output_tail(result.stderr or ""))
                    failures.append(
                        f"{_bsdtar_bin} rc={rc}: {detail}")
                    log(f"Wizard extraction: {_bsdtar_bin} failed "
                        f"(rc={rc}): {detail}")

        if not extracted_via_cli:
            if py7zr is None:
                if failures:
                    raise RuntimeError(
                        "Cannot extract .7z archive; available command-line "
                        "extractors failed: " + " || ".join(failures))
                raise RuntimeError("Cannot extract .7z archive: no native "
                                   "7z/bsdtar command or py7zr module was found.")
            log(f"Wizard extraction: trying py7zr {getattr(py7zr, '__version__', '?')}.")
            try:
                with py7zr.SevenZipFile(archive, "r") as zf:
                    zf.extractall(dest)
                log("Wizard extraction: extracted with py7zr.")
            except Exception as exc:
                detail = _output_tail(f"{type(exc).__name__}: {exc}")
                failures.append(f"py7zr: {detail}")
                raise RuntimeError("Cannot extract .7z archive; all available "
                                   "extractors failed: " + " || ".join(failures)) from exc

    elif name_lower.endswith((".tar.zst", ".tzst")):
        _extract_tar_zst(archive, dest, log_fn=log)

    elif name_lower.endswith((".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
        log(f"Wizard extraction: using Python tarfile for {archive.name}.")
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


def _extract_tar_zst(archive: Path, dest: Path, log_fn=None) -> None:
    """Extract a zstd-compressed tar into *dest*.

    Python's ``tarfile`` gained no zstd support of its own, so the stream is
    decompressed first. Falls back to bsdtar (bundled in the AppImage,
    libarchive links libzstd) and then to 7-Zip, which only unwraps the
    ``.zst`` container and needs a second pass over the inner ``.tar``.
    """
    log = _extract_log(log_fn)
    failures: list[str] = []
    zstd = _zstd_module()
    if zstd is not None:
        try:
            # Streaming mode ("r|"): a zstd stream isn't seekable, and the whole
            # tar is walked once anyway.
            with zstd.open(archive, "rb") as zf, \
                    tarfile.open(fileobj=zf, mode="r|") as tf:
                tf.extractall(dest, filter="data")
            log(f"Wizard extraction: extracted {archive.name} with "
                f"{zstd.__name__}.")
            return
        except Exception as exc:
            detail = _output_tail(f"{type(exc).__name__}: {exc}")
            failures.append(f"{zstd.__name__}: {detail}")
            log(f"Wizard extraction: {zstd.__name__} failed: {detail}")

    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        log(f"Wizard extraction: trying {_tool_description(bsdtar)}.")
        result, spawn_error = _run_extractor(
            [bsdtar, "-xf", str(archive), "-C", str(dest)])
        if result is not None and result.returncode == 0:
            log(f"Wizard extraction: extracted with {bsdtar}.")
            return
        rc = result.returncode if result is not None else "not started"
        detail = (_output_tail(spawn_error) if result is None
                  else _output_tail(result.stderr or ""))
        failures.append(f"{bsdtar} rc={rc}: {detail}")
        log(f"Wizard extraction: {bsdtar} failed (rc={rc}): {detail}")

    _7z_bin = (shutil.which("7zzs") or shutil.which("7zz")
               or shutil.which("7z") or shutil.which("7za"))
    if _7z_bin:
        import tempfile
        log(f"Wizard extraction: trying {_tool_description(_7z_bin)}.")
        with tempfile.TemporaryDirectory() as stage:
            # 7z treats .tar.zst as a zstd container around a .tar, so the
            # first pass yields the tar and the second unpacks it.
            r1, spawn_error = _run_extractor(
                [_7z_bin, "x", str(archive), f"-o{stage}", "-y"])
            inner = [p for p in Path(stage).iterdir() if p.is_file()]
            if r1 is not None and r1.returncode == 0 and inner:
                with tarfile.open(inner[0], "r:") as tf:
                    tf.extractall(dest, filter="data")
                log(f"Wizard extraction: extracted with {_7z_bin} + Python tarfile.")
                return
            rc = r1.returncode if r1 is not None else "not started"
            detail = (_output_tail(spawn_error) if r1 is None
                      else _output_tail(r1.stderr or ""))
            failures.append(f"{_7z_bin} rc={rc}: {detail}")
            log(f"Wizard extraction: {_7z_bin} failed (rc={rc}): {detail}")

    if failures:
        raise RuntimeError("Cannot extract .tar.zst; available extractors failed: "
                           + " || ".join(failures))
    raise RuntimeError("Cannot extract .tar.zst: no zstd module "
                       "(compression.zstd / backports.zstd), bsdtar or 7z was found.")


def _strip_single_top_dir(tmp: Path) -> Path:
    """If *tmp* contains a single top-level directory, return it so the
    caller can copy its *contents* instead of the wrapper folder."""
    entries = [e for e in tmp.iterdir() if e.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return tmp


def extract_archive(archive: Path, dest: Path, log_fn=None) -> list[Path]:
    """Extract *archive* into *dest*, stripping a single top-level wrapper
    directory if present (e.g. ``f4se_0_07_07/`` -> contents go straight
    into *dest*).

    Returns created paths in **reverse depth order** (deepest first) so
    callers can delete files before their parent directories.
    """
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    try:
        extract_to_dir(archive, tmp, log_fn=log_fn)
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
    from Utils.mods.install_as_mod import (
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

    paths = extract_archive(archive, dest, log_fn=log_fn)
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
