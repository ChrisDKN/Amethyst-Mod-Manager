"""Cyber Engine Tweaks symlink-mode detection (GUI-agnostic).

CET's ASI loader refuses to load a symlinked ``cyber_engine_tweaks.asi``, so the
mod silently fails when Cyberpunk 2077 is deployed in SYMLINK mode. This module
holds the pure detection - scan the effective filemap for the asi and report
whether a warning is warranted. The GUI layer owns the actual prompt.

Ported from the Tk ``gui.dialogs.confirm_cet_symlink`` (its detection half).
"""

from __future__ import annotations

from pathlib import Path


CET_ASI = "cyber_engine_tweaks.asi"


def cet_symlink_conflict(game) -> bool:
    """Return True when *game* is Cyberpunk 2077, CET's ``cyber_engine_tweaks.asi``
    is staged, and the deploy will symlink it - i.e. the situation where CET
    would silently fail to load.

    Two ways the asi ends up symlinked:

    * The deploy mode is set to SYMLINK outright.
    * The deploy mode is HARDLINK but the game folder is on a different
      filesystem than the mod staging folder, so ``os.link`` hits EXDEV and the
      deploy silently falls back to symlinks (see
      :mod:`Utils.deploy_shared`). The user never picked symlink, but CET breaks
      all the same.

    Any missing attribute, non-Cyberpunk game, hardlink mode with matching
    devices, or unreadable filemap returns False (nothing to warn about)."""
    if getattr(game, "name", "") != "Cyberpunk 2077":
        return False
    try:
        from Utils.deploy import LinkMode
        if not hasattr(game, "get_deploy_mode"):
            return False
        mode = game.get_deploy_mode()
        if mode == LinkMode.SYMLINK:
            pass  # symlink chosen outright - always a conflict
        elif mode == LinkMode.HARDLINK:
            # Hardlink chosen, but if the game folder and staging are on
            # different filesystems the deploy silently symlinks instead.
            from Utils.hardlink_check import hardlink_device_mismatches
            if not hardlink_device_mismatches(game):
                return False
        else:
            return False
    except Exception:
        return False
    try:
        filemap_path = game.get_effective_filemap_path()
    except Exception:
        return False
    if not filemap_path or not Path(filemap_path).is_file():
        return False
    try:
        with Path(filemap_path).open(encoding="utf-8") as f:
            for line in f:
                if "\t" not in line:
                    continue
                rel_str, _ = line.rstrip("\n").split("\t", 1)
                if rel_str.lower().endswith(CET_ASI):
                    return True
    except Exception:
        return False
    return False
