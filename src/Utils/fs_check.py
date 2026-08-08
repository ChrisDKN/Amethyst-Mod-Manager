"""Windows-filesystem detection for deploy folders (GUI-agnostic).

NTFS/exFAT/FAT mounts on Linux have weak write guarantees: NTFS journals
metadata only, ntfs-3g is a FUSE driver with no write barriers, and a drive
shared with Windows can be left dirty by Fast Startup/hibernation. Any of
these can leave files that *exist with their attributes* but contain 0 bytes
after an unclean unmount - GH#307: a runtime-recreated NVSE DLL came back as
a 0 KB read-only file, which the restore rescue walk then propagated into
staging (that half is guarded in deploy_standard now).

This module holds the pure detection - find deploy-relevant folders (mod
staging + the game's hardlink deploy targets) that sit on a Windows
filesystem. The GUI layer owns the actual prompt, mirroring
:mod:`Utils.cet_check`.
"""

from __future__ import annotations

import os
from pathlib import Path

# fstype (as reported in /proc/self/mounts) → human label. ``fuseblk`` is any
# FUSE-mounted block device but is overwhelmingly ntfs-3g in practice (udisks
# used it for NTFS for years); the label says so rather than hiding the hit.
WINDOWS_FS_LABELS: dict[str, str] = {
    "ntfs":   "NTFS",
    "ntfs3":  "NTFS",
    "fuseblk": "NTFS (ntfs-3g)",
    "exfat":  "exFAT",
    "vfat":   "FAT32",
    "msdos":  "FAT",
}


def _unescape_mount(field: str) -> str:
    """Undo /proc/mounts octal escapes (\\040 space, \\011 tab, \\134 backslash)."""
    if "\\" not in field:
        return field
    out: list[str] = []
    i = 0
    while i < len(field):
        c = field[i]
        if c == "\\" and i + 3 < len(field) + 1 and field[i + 1:i + 4].isdigit():
            try:
                out.append(chr(int(field[i + 1:i + 4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(c)
        i += 1
    return "".join(out)


def _iter_mounts() -> list[tuple[str, str]]:
    """(mountpoint, fstype) pairs from /proc/self/mounts; [] off Linux."""
    result: list[tuple[str, str]] = []
    try:
        with open("/proc/self/mounts", encoding="utf-8",
                  errors="surrogateescape") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    result.append((_unescape_mount(parts[1]), parts[2]))
    except OSError:
        pass
    return result


def mount_fs_type(path: "Path | str") -> "tuple[str, str] | None":
    """Return (fstype, mountpoint) for the mount holding *path*, or None.

    A not-yet-created path is anchored to its nearest existing ancestor (same
    approach as hardlink_check._device_of) so a fresh staging folder still
    resolves to a meaningful mount.
    """
    p = Path(path)
    real: str | None = None
    for cand in (p, *p.parents):
        try:
            if cand.exists():
                real = os.path.realpath(str(cand))
                break
        except OSError:
            continue
    if real is None:
        return None
    best: "tuple[str, str] | None" = None
    for mnt, fstype in _iter_mounts():
        if real == mnt or real.startswith(mnt.rstrip("/") + "/"):
            if best is None or len(mnt) > len(best[1]):
                best = (fstype, mnt)
    return best


def windows_fs_targets(game) -> list[tuple[str, str, str]]:
    """Return (folder label, filesystem label, mountpoint) for every deploy
    folder of *game* that sits on a Windows filesystem - the mod staging
    folder plus ``get_hardlink_deploy_targets()`` (game directory, and the
    Proton prefix for games that deploy there). Empty list means nothing to
    warn about (or nothing could be probed)."""
    folders: list[tuple[str, "Path | None"]] = []
    try:
        folders.append(("Mod staging folder", game.get_mod_staging_path()))
    except Exception:
        pass
    try:
        folders.extend(game.get_hardlink_deploy_targets())
    except Exception:
        pass

    hits: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for label, path in folders:
        if path is None:
            continue
        info = mount_fs_type(path)
        if info is None:
            continue
        fstype, mnt = info
        fs_label = WINDOWS_FS_LABELS.get(fstype.lower())
        if fs_label is None:
            continue
        key = (label, mnt)
        if key in seen:
            continue
        seen.add(key)
        hits.append((label, fs_label, mnt))
    return hits


def fs_ack_fingerprint(hits: list[tuple[str, str, str]]) -> str:
    """Stable fingerprint of a windows_fs_targets() result, used to remember
    that the user acknowledged the warning for this exact drive layout (the
    prompt re-arms if the folders move to different mounts)."""
    return ";".join("|".join(h) for h in sorted(hits))
