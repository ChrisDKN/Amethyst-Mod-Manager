"""
profile_backup.py
Backup and restore modlist.txt, plugins.txt, and related JSON state for a profile.
Each backup is stored in its own folder under profile_dir/backups/<timestamp>/.
Used before deploy (create_backup) and via Restore backup UI (list_backups, restore_backup).
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

_TIMESTAMP_FMT = "%Y%m%d_%H%M%S"
_MAX_BACKUPS = 10
_BACKUPS_SUBDIR = "backups"
_TIMESTAMP_PATTERN = re.compile(r"^\d{8}_\d{6}$")

# Files to backup/restore (in profile dir). Copy only if present.
_BACKUP_FILES = [
    "modlist.txt",
    "plugins.txt",
    "collapsed_seps.json",
    "plugin_locks.json",
    "separator_locks.json",
]


def _timestamp_str() -> str:
    return datetime.now().strftime(_TIMESTAMP_FMT)


def _parse_timestamp_from_dirname(name: str) -> datetime | None:
    """Parse timestamp from a backup folder name like '20250225_143022'."""
    if not _TIMESTAMP_PATTERN.fullmatch(name):
        return None
    try:
        return datetime.strptime(name, _TIMESTAMP_FMT)
    except ValueError:
        return None


def create_backup(profile_dir: Path, log_fn=None) -> None:
    """
    Create a new backup in profile_dir/backups/<timestamp>/ containing
    modlist.txt, plugins.txt, and (if present) collapsed_seps.json,
    plugin_locks.json, separator_locks.json.
    Keep at most _MAX_BACKUPS backup folders; delete oldest when over limit.
    """
    _log = log_fn or (lambda _: None)
    backups_dir = profile_dir / _BACKUPS_SUBDIR
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = _timestamp_str()
    backup_folder = backups_dir / ts
    backup_folder.mkdir(parents=True, exist_ok=True)

    for name in _BACKUP_FILES:
        src = profile_dir / name
        if src.is_file():
            dst = backup_folder / name
            shutil.copy2(src, dst)
            _log(f"Backup: {name}")

    # Prune to _MAX_BACKUPS: list subdirs by name (chronological order), remove oldest
    subdirs = [
        p for p in backups_dir.iterdir()
        if p.is_dir() and _parse_timestamp_from_dirname(p.name) is not None
    ]
    subdirs.sort(key=lambda p: p.name)
    while len(subdirs) > _MAX_BACKUPS:
        oldest = subdirs.pop(0)
        try:
            shutil.rmtree(oldest)
            _log(f"Backup: removed oldest {oldest.name}")
        except OSError:
            pass
        subdirs = [
            p for p in backups_dir.iterdir()
            if p.is_dir() and _parse_timestamp_from_dirname(p.name) is not None
        ]
        subdirs.sort(key=lambda p: p.name)


def list_backups(profile_dir: Path) -> list[tuple[datetime, Path]]:
    """
    List backup folders from profile_dir/backups/, newest first.
    Returns list of (timestamp, backup_folder_path).
    Only includes folders that contain modlist.txt (valid backup).
    """
    backups_dir = profile_dir / _BACKUPS_SUBDIR
    if not backups_dir.is_dir():
        return []

    result: list[tuple[datetime, Path]] = []
    for p in backups_dir.iterdir():
        if not p.is_dir():
            continue
        dt = _parse_timestamp_from_dirname(p.name)
        if dt is None:
            continue
        if (p / "modlist.txt").is_file():
            result.append((dt, p))
    result.sort(key=lambda x: x[0], reverse=True)
    return result


def restore_backup(profile_dir: Path, backup_dir: Path) -> None:
    """
    Copy all backed-up files from backup_dir into profile_dir.
    Overwrites modlist.txt, plugins.txt, and any of the JSON state files
    that exist in the backup folder.
    """
    for name in _BACKUP_FILES:
        src = backup_dir / name
        if src.is_file():
            shutil.copy2(src, profile_dir / name)
