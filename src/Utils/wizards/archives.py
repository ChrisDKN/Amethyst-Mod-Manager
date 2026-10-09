"""
GUI-neutral archive primitives for wizard tools.

Moved out of wizards/script_extender.py (which imports customtkinter) so the
Qt wizard views can share them. These are deliberately generic - the script
extender, BepInEx, Wrye Bash, DynDOLOD, TTW … wizards all follow the same
"fetch archive → find it in ~/Downloads → extract to game/root/mod" shape,
and Morrowind's MGE XE / MCP wizards already use them as a library.

"""

from __future__ import annotations

import json as _json
import os
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from Utils.environment.xdg import xdg_download_dir
from Utils.archives.identify import ArchiveType, identify_archive
from Utils.archives.process import failure_kind, run_extractor, run_python_extractor

if TYPE_CHECKING:
    from Games.base_game import BaseGame

ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tar.bz2",
                ".tar.xz", ".tgz", ".tar.zst", ".tzst"}


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


def _low_priority_enabled() -> bool:
    try:
        from Utils.ui.config import load_extraction_settings
        return bool(load_extraction_settings().get("low_priority", False))
    except Exception:
        return False


def _run_extractor(args: list[str], *, priority_path=None, low_priority=None):
    if low_priority is None:
        low_priority = _low_priority_enabled()
    code, detail, _ = run_extractor(
        args, low_priority=low_priority, priority_path=priority_path)
    if code and failure_kind(detail, code, args[0]) != "archive":
        raise RuntimeError(detail)
    return subprocess.CompletedProcess(args, code, stderr=detail), ""


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
    """Extract into private staging before merging verified output into dest."""
    from Utils.mods.install import _extract_archive, _normalise_extracted_tree
    archive, dest = Path(archive), Path(dest)
    log = _extract_log(log_fn)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wizard-extract-", dir=dest.parent) as temporary:
        stage = Path(temporary) / "payload"
        stage.mkdir()
        if identify_archive(archive) == ArchiveType.ZSTD:
            log(f"Archive header: zstd; path: {archive}")
            _extract_tar_zst(archive, stage, log_fn=log,
                             low_priority=_low_priority_enabled())
            _normalise_extracted_tree(stage, log)
        else:
            errors = []
            if not _extract_archive(str(archive), str(stage), log, error_sink=errors):
                raise RuntimeError(f"Cannot extract {archive.name}: {'; '.join(errors)}")
        original_mode = dest.stat().st_mode & 0o7777 if dest.exists() else None
        try:
            shutil.copytree(stage, dest, dirs_exist_ok=True, symlinks=True,
                            copy_function=shutil.move)
        finally:
            if original_mode is not None:
                dest.chmod(original_mode)


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


def _extract_tar_zst(archive: Path, dest: Path, log_fn=None, *,
                     low_priority=False) -> None:
    """Extract a zstd-compressed tar into *dest*.

    Python's ``tarfile`` gained no zstd support of its own, so the stream is
    decompressed first. Falls back to bsdtar (bundled in the AppImage,
    libarchive links libzstd) and then to 7-Zip, which only unwraps the
    ``.zst`` container and needs a second pass over the inner ``.tar``.
    """
    log = _extract_log(log_fn)
    failures: list[str] = []
    def retry(error):
        if isinstance(error, tarfile.FilterError) or failure_kind(error) != "archive":
            raise RuntimeError(str(error)) from error
        shutil.rmtree(dest)
        dest.mkdir()
        log("Retrying extraction with a clean temporary directory.")

    zstd = _zstd_module()
    if zstd is not None:
        try:
            if low_priority:
                code, detail, _ = run_python_extractor(
                    "tar-zst", archive, dest, low_priority=True)
                if code:
                    raise RuntimeError(detail)
            else:
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
            retry(exc)

    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        log(f"Wizard extraction: trying {_tool_description(bsdtar)}.")
        result, spawn_error = _run_extractor(
            [bsdtar, "-xf", str(archive), "-C", str(dest)],
            priority_path=dest, low_priority=low_priority)
        if result is not None and result.returncode == 0:
            log(f"Wizard extraction: extracted with {bsdtar}.")
            return
        rc = result.returncode if result is not None else "not started"
        detail = (_output_tail(spawn_error) if result is None
                  else _output_tail(result.stderr or ""))
        failures.append(f"{bsdtar} rc={rc}: {detail}")
        log(f"Wizard extraction: {bsdtar} failed (rc={rc}): {detail}")
        retry(RuntimeError(detail))

    _7z_bin = (shutil.which("7zzs") or shutil.which("7zz")
               or shutil.which("7z") or shutil.which("7za"))
    if _7z_bin:
        log(f"Wizard extraction: trying {_tool_description(_7z_bin)}.")
        with tempfile.TemporaryDirectory() as stage:
            # 7z treats .tar.zst as a zstd container around a .tar, so the
            # first pass yields the tar and the second unpacks it.
            r1, spawn_error = _run_extractor(
                [_7z_bin, "x", str(archive), f"-o{stage}", "-y"],
                priority_path=stage, low_priority=low_priority)
            inner = [p for p in Path(stage).iterdir() if p.is_file()]
            if r1 is not None and r1.returncode == 0 and len(inner) == 1:
                if low_priority:
                    code, detail, _ = run_python_extractor(
                        "tar", inner[0], dest, low_priority=True)
                    if code:
                        raise RuntimeError(detail)
                else:
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


def extract_archive(archive: Path, dest: Path, log_fn=None,
                    *, strip_top_dir: bool = True) -> list[Path]:
    """Extract *archive* into *dest*, stripping a single top-level wrapper
    by default (e.g. ``f4se_0_07_07/``). Keep it when it is a destination
    folder such as BLSE's ``bin/``.

    Returns created paths in **reverse depth order** (deepest first) so
    callers can delete files before their parent directories.
    """
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    try:
        extract_to_dir(archive, tmp, log_fn=log_fn)
        src = _strip_single_top_dir(tmp) if strip_top_dir else tmp

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
    nexus_mod_id: int = 0,
    modlist_path: "Path | None" = None,
    restore_first: bool = True,
    delete_archive: bool = True,
    strip_top_dir: bool = True,
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
        if nexus_mod_id:
            from Nexus.nexus_download import _clean_nexus_stem
            mod_name = _clean_nexus_stem(mod_name, str(nexus_mod_id))
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

    paths = extract_archive(archive, dest, log_fn=log_fn,
                            strip_top_dir=strip_top_dir)
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
