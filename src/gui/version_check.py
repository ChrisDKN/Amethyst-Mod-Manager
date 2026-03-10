"""
App update check: fetch latest version from repo and compare.
Used by App. No dependency on other gui modules.
"""

import os
import re
import urllib.request

_APP_UPDATE_RELEASES_API_URL = "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/releases/latest"
_APP_UPDATE_RELEASES_URL = "https://github.com/ChrisDKN/Amethyst-Mod-Manager/releases"
_APP_UPDATE_INSTALLER_URL = "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/appimage/Amethyst-MM-installer.sh"
_AUR_API_URL = "https://aur.archlinux.org/rpc/v5/info/amethyst-mod-manager"
_AUR_PACKAGE_URL = "https://aur.archlinux.org/packages/amethyst-mod-manager"


def is_appimage() -> bool:
    """Return True if we are running inside an AppImage."""
    return bool(os.environ.get("APPIMAGE"))


def _parse_version(s: str) -> tuple[int, ...]:
    """Convert a version string like '0.3.0' to a tuple of ints for comparison."""
    out = []
    for part in s.strip().split("."):
        part = re.sub(r"[^0-9].*$", "", part)
        out.append(int(part) if part.isdigit() else 0)
    return tuple(out) if out else (0,)


def _fetch_latest_version() -> str | None:
    """Fetch the latest version from the GitHub Releases API tag; return None on error."""
    import json
    try:
        req = urllib.request.Request(
            _APP_UPDATE_RELEASES_API_URL,
            headers={"User-Agent": "Amethyst-Mod-Manager"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        tag = data.get("tag_name", "").lstrip("v")
        return tag if tag else None
    except Exception:
        return None


def _fetch_aur_version() -> str | None:
    """Fetch the current AUR package version; return None on error.

    The AUR version string includes a pkgrel suffix (e.g. '0.7.9-1').
    We strip everything from the first '-' onwards so callers get a plain
    version number comparable with __version__.
    """
    import json
    try:
        req = urllib.request.Request(
            _AUR_API_URL,
            headers={"User-Agent": "Amethyst-Mod-Manager"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        results = data.get("results", [])
        if not results:
            return None
        ver = results[0].get("Version", "")
        # Strip pkgrel: '0.7.9-1' -> '0.7.9'
        ver = ver.split("-")[0]
        return ver if ver else None
    except Exception:
        return None


def _is_newer_version(current: str, latest: str) -> bool:
    """Return True if latest is newer than current (strictly greater)."""
    try:
        return _parse_version(latest) > _parse_version(current)
    except (ValueError, TypeError):
        return False
