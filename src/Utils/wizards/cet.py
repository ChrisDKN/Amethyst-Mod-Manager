"""GUI-agnostic Cyber Engine Tweaks symlink-mode detection.

CET's ASI loader refuses to load a symlinked ``cyber_engine_tweaks.asi``, so the
mod silently fails when Cyberpunk 2077 is deployed in SYMLINK mode. This module
holds the pure detection - query the resolved snapshot for the ASI and report
whether a warning is warranted. The GUI layer owns the actual prompt.

Ported from the Tk ``gui.dialogs.confirm_cet_symlink`` (its detection half).
"""

from __future__ import annotations

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
      :mod:`Utils.deployment.shared`). The user never picked symlink, but CET breaks
      all the same.

    Any missing attribute, non-Cyberpunk game, hardlink mode with matching
    devices, or unavailable catalog returns False (nothing to warn about)."""
    if getattr(game, "name", "") != "Cyberpunk 2077":
        return False
    try:
        from Utils.deployment import LinkMode
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
        from Utils.filegraph.service import active_snapshot
        return active_snapshot(game).winner_by_suffix(CET_ASI) is not None
    except Exception:
        return False
