"""
App update check: fetch latest version from repo and compare.
Used by App. No dependency on other gui modules.
"""

import re
import urllib.request

_APP_UPDATE_VERSION_URL = "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/version.py"
_APP_UPDATE_RELEASES_URL = "https://github.com/ChrisDKN/Amethyst-Mod-Manager/releases"
_APP_UPDATE_INSTALLER_URL = "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/appimage/Amethyst-MM-installer.sh"


def _parse_version(s: str) -> tuple[int, ...]:
    """Convert a version string like '0.3.0' to a tuple of ints for comparison."""
    out = []
    for part in s.strip().split("."):
        part = re.sub(r"[^0-9].*$", "", part)
        out.append(int(part) if part.isdigit() else 0)
    return tuple(out) if out else (0,)


def _fetch_latest_version() -> str | None:
    """Fetch the latest __version__ from the repo; return None on error."""
    try:
        req = urllib.request.Request(
            _APP_UPDATE_VERSION_URL,
            headers={"User-Agent": "Amethyst-Mod-Manager"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', body)
        return m.group(1).strip() if m else None
    except Exception:
        return None


def _is_newer_version(current: str, latest: str) -> bool:
    """Return True if latest is newer than current (strictly greater)."""
    try:
        return _parse_version(latest) > _parse_version(current)
    except (ValueError, TypeError):
        return False
