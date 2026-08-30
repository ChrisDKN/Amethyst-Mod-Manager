"""Hard-link cross-device validation (GUI-agnostic).

Hardlinks (`os.link`) require the source and destination to live on the same
filesystem. When the deploy mode is Hardlink, every directory the game deploys
into must sit on the same device as the mod staging folder - otherwise deploy
would fail at link time. This module holds the pure detection so the config
UIs (Tk add-game dialog, Qt configure-game view) can block the save and warn.

The set of directories that must match is game-specific and lives in
``game.get_hardlink_deploy_targets()`` - the game directory by default, plus
the Proton prefix / native data dir for games that deploy there (BG3, The
Sims 4, Jagged Alliance 3, Dragon Age Origins). BG3 with an empty prefix
(native Linux Larian build) reports only the game dir, so only that matters.

Ported from the Tk ``gui.add_game_dialog`` save-time check.
"""

from __future__ import annotations

import os
from pathlib import Path


def hardlink_device_mismatches(game, log_fn=None) -> list[str]:
    """Return the labels of deploy targets that are on a different filesystem
    than *game*'s mod staging folder.

    The caller must have already applied the pending game/prefix/staging paths
    to *game* (so ``get_mod_staging_path`` and ``get_hardlink_deploy_targets``
    resolve against the values being saved). An empty list means all targets
    share the staging device (or nothing could be probed) - i.e. hardlinks are
    safe. Returns labels like ``["Proton prefix"]`` when they don't.

    Paths that don't exist yet are anchored to their nearest existing parent so
    a not-yet-created staging/prefix still gets a meaningful device.
    """
    if log_fn is None:
        try:
            from Utils.app_log import app_log
            log_fn = app_log
        except Exception:
            log_fn = lambda _message: None
    target_log = log_fn

    def log_fn(message):
        try:
            target_log(message)
        except Exception:
            pass
    try:
        staging = game.get_mod_staging_path()
    except Exception as exc:
        log_fn(f"Hardlink preflight: staging path lookup failed: {exc}")
        return []
    staging_dev = _device_of(staging)
    if staging_dev is None:
        log_fn(f"Hardlink preflight: could not determine a device for staging: "
               f"{staging}")
        return []

    try:
        targets = game.get_hardlink_deploy_targets()
    except Exception as exc:
        log_fn(f"Hardlink preflight: deploy target lookup failed: {exc}")
        return []

    mismatched: list[str] = []
    for label, path in targets:
        if path is None:
            continue
        dev = _device_of(path)
        if dev is None:
            log_fn(f"Hardlink preflight: device unknown for {label}: {path}")
        elif dev != staging_dev:
            mismatched.append(label)
    return mismatched


def _device_of(path) -> "int | None":
    """st_dev of *path*, or of its nearest existing ancestor. None if nothing
    on the way up can be stat'd."""
    p = Path(path)
    for cand in (p, *p.parents):
        try:
            return os.stat(cand).st_dev
        except OSError:
            continue
    return None
