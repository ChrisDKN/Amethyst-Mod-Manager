"""Detect user-supplied paths that the Flatpak sandbox cannot see.

The manager's flatpak grants --filesystem=home, /run/media, /media, /mnt and
the Steam/Heroic flatpak data dirs (see flatpak/io.github.Amethyst.ModManager.yml).
A game or staging path outside those trees (e.g. /data/SteamLibrary, /opt/...)
simply doesn't exist from inside the sandbox - indistinguishable from a typo -
so the UI should tell the user it's a sandbox grant problem, not a bad path.

No UI imports here (Utils stays gui-free).
"""

from __future__ import annotations

import os
from pathlib import Path

# ~/.var/app is excluded from --filesystem=home; these are granted
# explicitly in the manifest.
_GRANTED_VAR_APPS = ("com.valvesoftware.Steam", "com.heroicgameslauncher.hgl",
                     "net.lutris.Lutris", "io.github.Faugus.faugus-launcher")

# Non-home trees granted in the manifest.
_GRANTED_ROOTS = ("/run/media", "/media", "/mnt")


def in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def flatpak_blocked_path_hint(path) -> str | None:
    """Return a `flatpak override` command when *path* looks sandbox-blocked.

    Returns None when not sandboxed, when the path exists (thus reachable),
    or when the path lies inside a granted tree (a missing path there is a
    genuine missing path, not a permission problem). Otherwise returns the
    command the user can run (or replicate in Flatseal) to grant access.
    """
    if not in_flatpak():
        return None
    try:
        p = Path(path).expanduser()
        if not p.is_absolute() or p.exists():
            return None
        home = Path.home()
        try:
            rel = p.relative_to(home)
        except ValueError:
            rel = None
        if rel is not None:
            parts = rel.parts
            if parts[:2] != (".var", "app"):
                return None  # plain home path - granted, so genuinely missing
            app = parts[2] if len(parts) > 2 else ""
            # The app's own ~/.var/app/<FLATPAK_ID> tree is always visible to
            # itself; a missing path there is genuinely missing, not blocked.
            own_id = os.environ.get("FLATPAK_ID", "io.github.Amethyst.ModManager")
            if app in _GRANTED_VAR_APPS or app == own_id:
                return None
        else:
            for root in _GRANTED_ROOTS:
                if p == Path(root) or str(p).startswith(root + "/"):
                    return None
        app_id = os.environ.get("FLATPAK_ID", "io.github.Amethyst.ModManager")
        # Grant the top-level tree, not the leaf, so sibling paths
        # (other games in the same library) come along.
        if rel is not None:
            grant = home / rel.parts[0] / rel.parts[1] / (rel.parts[2] if len(rel.parts) > 2 else "")
        else:
            grant = Path("/") / p.parts[1] if len(p.parts) > 1 else p
        return f"flatpak override --user --filesystem={grant} {app_id}"
    except Exception:
        return None
