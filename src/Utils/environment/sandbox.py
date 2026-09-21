"""Detect user-supplied paths that the Flatpak sandbox cannot see.

The manager's flatpak grants --filesystem=home, /run/media, /media, /mnt,
/var/mnt and the Steam/Heroic flatpak data dirs (see
flatpak/io.github.Amethyst.ModManager.yml).
A game or staging path outside those trees (e.g. /data/SteamLibrary, /opt/...)
simply doesn't exist from inside the sandbox - indistinguishable from a typo -
so the UI should tell the user it's a sandbox grant problem, not a bad path.

No UI imports here (Utils stays gui-free).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

# ~/.var/app is excluded from --filesystem=home; these are granted
# explicitly in the manifest.
_GRANTED_VAR_APPS = ("com.valvesoftware.Steam", "com.heroicgameslauncher.hgl",
                     "net.lutris.Lutris", "io.github.Faugus.faugus-launcher")

# Non-home trees granted in the manifest. /var/mnt is included because on
# Fedora Atomic / Bazzite (and similar) /mnt is a symlink to /var/mnt, so the
# --filesystem=/mnt grant actually exposes /var/mnt, and Path.resolve() rewrites
# any /mnt/... path the user picks to its /var/mnt/... target. Without it such a
# resolved path looks unreachable and triggers a false "not visible" warning.
_GRANTED_ROOTS = ("/run/media", "/media", "/mnt", "/var/mnt")


def in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def _has_writable_ancestor(path: Path, stop_at: Path | None = None) -> bool:
    sandbox_only = {Path("/"), Path("/var"), Path("/run"), Path("/tmp")}
    for parent in path.parents:
        try:
            if parent == stop_at or parent in sandbox_only:
                return False
            if parent.exists():
                return parent.is_dir() and os.access(
                    parent, os.W_OK | os.X_OK)
        except OSError:
            return False
    return False


def _flatpak_required_grant(path) -> Path | None:
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
            if _has_writable_ancestor(p, home / ".var" / "app"):
                return None
        else:
            for root in _GRANTED_ROOTS:
                if p == Path(root) or str(p).startswith(root + "/"):
                    return None
            # Flatseal may grant any user-selected directory, including paths
            # not listed in the manifest. A missing leaf is usable when it can
            # be created below a visible, writable ancestor.
            if _has_writable_ancestor(p):
                return None
        # Grant the top-level tree, not the leaf, so sibling paths
        # (other games in the same library) come along.
        if rel is not None:
            app = rel.parts[2] if len(rel.parts) > 2 else ""
            grant = home / rel.parts[0] / rel.parts[1] / app
        else:
            grant_parts = p.parts[1:3] if p.parts[1:2] == ("var",) \
                else p.parts[1:2]
            grant = Path("/").joinpath(*grant_parts) if grant_parts else p
        return grant
    except Exception:
        return None


def flatpak_blocked_path_hint(path) -> str | None:
    """Return a `flatpak override` command when *path* looks sandbox-blocked."""
    grant = _flatpak_required_grant(path)
    if grant is None:
        return None
    app_id = os.environ.get("FLATPAK_ID", "io.github.Amethyst.ModManager")
    filesystem = shlex.quote(f"--filesystem={grant}")
    return f"flatpak override --user {filesystem} {shlex.quote(app_id)}"


def grant_flatpak_path_access(
    paths: Iterable[Path],
) -> tuple[bool, list[Path], str]:
    """Persist missing path grants for this app through the host Flatpak CLI."""
    grants: list[Path] = []
    for path in paths:
        grant = _flatpak_required_grant(path)
        if grant is None or any(grant == old or old in grant.parents
                                for old in grants):
            continue
        grants = [old for old in grants if grant not in old.parents]
        grants.append(grant)
    if not grants:
        return True, [], ""

    app_id = os.environ.get("FLATPAK_ID", "io.github.Amethyst.ModManager")
    filesystems = " ".join(
        shlex.quote(f"--filesystem={grant}") for grant in grants)
    manual = (f"flatpak override --user {filesystems} "
              f"{shlex.quote(app_id)}")
    if shutil.which("flatpak-spawn") is None:
        return False, grants, manual
    try:
        result = subprocess.run(
            ["flatpak-spawn", "--host", "--directory=/", "flatpak",
             "override", "--user",
             *(f"--filesystem={grant}" for grant in grants), app_id],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, grants, f"{exc}\n{manual}"
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "").strip()
        return False, grants, f"{error}\n{manual}".strip()
    return True, grants, ""
