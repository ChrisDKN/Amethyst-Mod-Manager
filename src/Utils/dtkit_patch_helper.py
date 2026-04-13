"""
dtkit_patch_helper.py
Non-GUI helpers for downloading, installing, and running dtkit-patch.

Shared between the dtkit-patch wizard (wizards/dtkit_patch.py) and the
Darktide game handler (Games/Darktide/darktide.py) so that deploy/restore
can invoke dtkit-patch automatically without needing the wizard UI.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from Utils.app_log import safe_log as _safe_log
from Utils.config_paths import get_config_dir

try:
    import py7zr
except ImportError:
    py7zr = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GITHUB_API_URL = "https://api.github.com/repos/manshanko/dtkit-patch/releases/latest"
_ARCHIVE_EXTS   = {".zip", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".7z"}

# Persistent storage location for the downloaded binary
_TOOLS_DIR = get_config_dir() / "tools" / "dtkit-patch"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_archive(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in _ARCHIVE_EXTS)


def _fetch_latest_linux_asset(api_url: str) -> tuple[str, str, str]:
    """Return (version_tag, asset_name, download_url) for the latest Linux asset."""
    req = urllib.request.Request(
        api_url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ModManager/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    tag = data.get("tag_name", "unknown")
    for asset in data.get("assets", []):
        name: str = asset.get("name", "")
        if "linux" in name.lower():
            return tag, name, asset["browser_download_url"]
    raise RuntimeError(
        f"No Linux asset found in the latest dtkit-patch release ({tag}).\n"
        "Check https://github.com/manshanko/dtkit-patch/releases manually."
    )


def _extract_to_dir(archive: Path, dest: Path) -> None:
    name_lower = archive.name.lower()
    if name_lower.endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(dest)
    elif name_lower.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tar")):
        with tarfile.open(archive, "r:*") as tf:
            tf.extractall(dest)
    elif name_lower.endswith(".7z"):
        extracted = False
        try:
            subprocess.run(
                ["7z", "x", str(archive), f"-o{dest}", "-y"],
                check=True, capture_output=True,
            )
            extracted = True
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
        if not extracted:
            if py7zr is None:
                raise RuntimeError(
                    "Cannot extract .7z: 7z command not found and py7zr is not installed."
                )
            with py7zr.SevenZipFile(archive, "r") as zf:
                zf.extractall(dest)
    else:
        raise RuntimeError(f"Unsupported archive format: {archive.name}")


def _find_dtkit_binary_in_dir(directory: Path) -> Path | None:
    """Find the dtkit-patch executable inside *directory* (recursive)."""
    for candidate in directory.rglob("dtkit-patch*"):
        low = candidate.name.lower()
        if not candidate.is_file():
            continue
        # Skip Windows .exe files on Linux.
        if low.endswith(".exe"):
            continue
        if "dtkit" in low:
            return candidate
    return None


def _install_from_archive(archive: Path, tools_dir: Path) -> Path:
    """Extract *archive* into a temp dir, find the dtkit-patch binary,
    copy it to *tools_dir*, chmod +x, and return the installed path."""
    tools_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    try:
        _extract_to_dir(archive, tmp)
        binary = _find_dtkit_binary_in_dir(tmp)
        if binary is None:
            raise RuntimeError("Could not find dtkit-patch binary inside the archive.")
        dest = tools_dir / "dtkit-patch"
        shutil.copy2(binary, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest


def _install_bare_binary(src: Path, tools_dir: Path) -> Path:
    """Copy a bare binary file to *tools_dir*, chmod +x, and return the path."""
    tools_dir.mkdir(parents=True, exist_ok=True)
    dest = tools_dir / "dtkit-patch"
    shutil.copy2(src, dest)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_installed_dtkit_path() -> Path | None:
    """Return the path to the installed dtkit-patch binary, or None."""
    candidate = _TOOLS_DIR / "dtkit-patch"
    if candidate.is_file():
        return candidate
    return None


def ensure_dtkit_binary(log_fn=None) -> Path:
    """Return the path to dtkit-patch, downloading it from GitHub if needed.

    Raises RuntimeError if the binary cannot be obtained.
    """
    _log = _safe_log(log_fn)

    existing = get_installed_dtkit_path()
    if existing is not None:
        _log(f"dtkit-patch: using cached binary at {existing}")
        return existing

    _log("dtkit-patch: not found locally, fetching latest release from GitHub...")
    tag, asset_name, url = _fetch_latest_linux_asset(_GITHUB_API_URL)
    _log(f"dtkit-patch: downloading {tag} ({asset_name})...")

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        filename = url.split("/")[-1]
        dest_archive = tmp_dir / filename
        urllib.request.urlretrieve(url, dest_archive)
        _log(f"dtkit-patch: installing {filename}...")
        binary = _install_from_archive(dest_archive, _TOOLS_DIR)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    _log(f"dtkit-patch: installed to {binary}")
    return binary


def run_dtkit_patch(game_path: Path, flag: str, log_fn=None) -> bool:
    """Ensure dtkit-patch is installed, then run it with *flag* against
    *game_path*/bundle (e.g. '--patch' or '--unpatch').

    Returns True on success, False on failure (errors are logged, not raised).
    """
    _log = _safe_log(log_fn)

    try:
        binary = ensure_dtkit_binary(log_fn=_log)
    except Exception as exc:
        _log(f"dtkit-patch: could not obtain binary: {exc}")
        return False

    _log(f"dtkit-patch: running {binary} {flag} bundle (cwd={game_path})")
    try:
        result = subprocess.run(
            [str(binary), flag, "bundle"],
            capture_output=True,
            text=True,
            cwd=str(game_path),
        )
    except Exception as exc:
        _log(f"dtkit-patch: failed to run: {exc}")
        return False

    for line in result.stdout.strip().splitlines():
        _log(f"dtkit-patch: {line}")
    for line in result.stderr.strip().splitlines():
        _log(f"dtkit-patch [stderr]: {line}")

    if result.returncode == 0:
        _log("dtkit-patch: done.")
        return True
    else:
        _log(f"dtkit-patch: exited with code {result.returncode}.")
        return False
