"""
plugin_mtimes.py
Stamp ascending mtimes on deployed plugins to enforce timestamp load order.

Oblivion, Fallout 3 and Fallout NV order plugins by the mtime of the files
in Data/ — plugins.txt only selects the active set. Tools like Wrye Bash
display that mtime order, so without stamping the panel's order is never
the one the game (or WB) actually uses. Earlier in the list = older mtime,
1-second spacing, same scheme as morrowind_ini.py.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Mapping

# Spacing between mtime values (seconds).
_MTIME_STEP = 1


def _data_dir_files(data_dir: Path) -> "dict[str, Path]":
    """Map lowercased filename → path for the top-level files in data_dir."""
    out: dict[str, Path] = {}
    try:
        with os.scandir(data_dir) as it:
            for de in it:
                if not de.is_dir(follow_symlinks=False):
                    out[de.name.lower()] = Path(de.path)
    except OSError:
        pass
    return out


def _staging_plugin_map(
    staging_root: "Path | Mapping[str, Path] | None",
    overwrite_dir: "Path | None",
    wanted: "set[str]",
) -> "dict[str, list[Path]]":
    """Map lowercased plugin filename → candidate staging/overwrite paths.

    staging_root is a plain Path for a regular profile (its mod folders are
    scanned directly) or a {mod_name: real_mod_dir} mapping for a Profile
    Group (every mod's location is already known) — see resolve_mod_dir in
    deploy_shared.py.
    """
    out: dict[str, list[Path]] = {}
    roots: list[Path] = []
    if overwrite_dir is not None and overwrite_dir.is_dir():
        roots.append(overwrite_dir)
    if staging_root is not None:
        if isinstance(staging_root, Path):
            if staging_root.is_dir():
                roots.extend(d for d in staging_root.iterdir() if d.is_dir())
        else:
            roots.extend(d for d in staging_root.values() if d.is_dir())
    for root in roots:
        for d in (root, root / "Data"):
            try:
                scan_it = os.scandir(d)
            except OSError:
                continue
            with scan_it:
                for de in scan_it:
                    name_lower = de.name.lower()
                    if name_lower in wanted and de.is_file(follow_symlinks=False):
                        out.setdefault(name_lower, []).append(Path(de.path))
    return out


def stamp_plugin_load_order(
    ordered: "list[str]",
    data_dir: Path,
    staging_root: "Path | Mapping[str, Path] | None" = None,
    overwrite_dir: "Path | None" = None,
    log_fn=None,
) -> int:
    """Set ascending mtimes on deployed plugins so mtime order matches ordered."""
    _log = log_fn or (lambda _: None)
    deployed = _data_dir_files(data_dir)

    present: list[tuple[str, Path]] = []
    for name in ordered:
        path = deployed.get(name.lower())
        if path is not None:
            present.append((name, path))
    if not present:
        return 0

    # Skip when the deployed mtimes are already strictly ascending in the
    # desired order — keeps repeated saves from rewriting timestamps.
    try:
        mtimes = [p.stat().st_mtime for _n, p in present]
        if all(b > a for a, b in zip(mtimes, mtimes[1:])):
            return 0
    except OSError:
        pass

    staging_map: "dict[str, list[Path]] | None" = None
    base_time = time.time() - len(present) * _MTIME_STEP
    stamped = 0
    for i, (name, path) in enumerate(present):
        target_mtime = base_time + i * _MTIME_STEP
        try:
            st = os.lstat(path)
            # Follows symlinks, so symlink-mode deploys stamp the staging
            # target; hardlink-mode shares the inode with staging anyway.
            os.utime(path, (target_mtime, target_mtime))
            stamped += 1
        except OSError as exc:
            _log(f"  WARN: could not set mtime on {name}: {exc}")
            continue
        # Copy-mode deploys: stamp the staging copy too so restore's
        # size+mtime comparison still sees an unmodified deployed file.
        if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
            if staging_map is None:
                staging_map = _staging_plugin_map(
                    staging_root, overwrite_dir,
                    {n.lower() for n, _p in present},
                )
            for cand in staging_map.get(name.lower(), []):
                try:
                    if cand.stat().st_size == st.st_size:
                        os.utime(cand, (target_mtime, target_mtime))
                except OSError:
                    pass
    return stamped
