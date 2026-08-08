"""
profile_backup.py
Backup and restore modlist.txt, plugins.txt, and related JSON state for a profile.
Each backup is stored in its own folder under profile_dir/backups/<timestamp>/.
Used before deploy (create_backup) and via Restore backup UI (list_backups, restore_backup).

Backups fall into two groups:
  - Automated: created before each deploy; pruned to the newest _MAX_BACKUPS.
  - User-made: created via the New backup button (.manual marker) or marked
    kept (.keep marker). Never pruned; removable only via delete_backup.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from Utils.app_log import safe_log as _safe_log

_TIMESTAMP_FMT = "%Y%m%d_%H%M%S"
_MAX_BACKUPS = 20
_BACKUPS_SUBDIR = "backups"
_TIMESTAMP_PATTERN = re.compile(r"^\d{8}_\d{6}$")
_SEPARATOR_SUFFIX = "_separator"

# Files to backup/restore (in profile dir). Copy only if present.
_BACKUP_FILES = [
    "modlist.txt",
    "plugins.txt",
    "userlist.yaml",
    "profile_state.json",
    # Legacy individual files - kept so old backups can still be restored
    "collapsed_seps.json",
    "plugin_locks.json",
    "separator_locks.json",
    "disabled_plugins.json",
    "excluded_mod_files.json",
    "profile_settings.json",
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


_KEEP_MARKER = ".keep"


def is_backup_kept(backup_dir: Path) -> bool:
    """Return True if this backup is marked to be kept permanently."""
    return (backup_dir / _KEEP_MARKER).is_file()


def set_backup_kept(backup_dir: Path, keep: bool) -> None:
    """Mark or unmark a backup to be kept permanently (skip pruning)."""
    marker = backup_dir / _KEEP_MARKER
    if keep:
        marker.touch(exist_ok=True)
    elif marker.is_file():
        marker.unlink()


_MANUAL_MARKER = ".manual"


def is_backup_manual(backup_dir: Path) -> bool:
    """Return True if this backup was created by the user (New backup button)."""
    return (backup_dir / _MANUAL_MARKER).is_file()


def is_backup_user_made(backup_dir: Path) -> bool:
    """Return True if this backup belongs to the user-made group.

    User-made = created manually via the New backup button, or an automated
    backup the user marked kept. These are never pruned.
    """
    return is_backup_manual(backup_dir) or is_backup_kept(backup_dir)


_LABEL_MARKER = ".label"


def get_backup_label(backup_dir: Path) -> str:
    """Return the user-given label for this backup, or "" if none set.

    Stored in a ``.label`` marker file alongside the backup so the folder name
    stays a parseable timestamp (used for sorting and pruning).
    """
    marker = backup_dir / _LABEL_MARKER
    if not marker.is_file():
        return ""
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def set_backup_label(backup_dir: Path, label: str) -> None:
    """Set (or clear, when blank) the user-given label for this backup."""
    marker = backup_dir / _LABEL_MARKER
    label = (label or "").strip()
    if label:
        marker.write_text(label, encoding="utf-8")
    elif marker.is_file():
        marker.unlink()


def create_backup(profile_dir: Path, log_fn=None, manual: bool = False) -> None:
    """
    Create a new backup in profile_dir/backups/<timestamp>/ containing
    modlist.txt, plugins.txt, and (if present) profile_state.json.
    Automated backups (manual=False) are pruned to the newest _MAX_BACKUPS;
    user-made backups (manual=True, or later marked kept) are never pruned
    and don't count toward the limit.
    """
    _log = _safe_log(log_fn)
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

    if manual:
        (backup_folder / _MANUAL_MARKER).touch(exist_ok=True)

    # Prune to _MAX_BACKUPS: list subdirs by name (chronological order), remove oldest.
    # User-made backups are excluded from pruning (and from the count).
    def _prunable_backups():
        dirs = [
            p for p in backups_dir.iterdir()
            if p.is_dir() and _parse_timestamp_from_dirname(p.name) is not None
               and not is_backup_user_made(p)
        ]
        dirs.sort(key=lambda p: p.name)
        return dirs

    subdirs = _prunable_backups()
    while len(subdirs) > _MAX_BACKUPS:
        oldest = subdirs.pop(0)
        try:
            shutil.rmtree(oldest)
            _log(f"Backup: removed oldest {oldest.name}")
        except OSError:
            pass
        subdirs = _prunable_backups()


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


def backup_stats(backup_dir: Path) -> dict:
    """Summarise a backup folder's contents for display in the restore UI.

    Returns a dict with:
      - mods_total / mods_enabled: real mods in modlist.txt (separators excluded)
      - separators: number of separator entries
      - plugins: number of plugin entries in plugins.txt (comments excluded)

    Counting is tolerant of missing files (returns 0 for that section) so it
    can be called on any backup, including legacy ones.
    """
    mods_total = mods_enabled = separators = plugins = 0

    modlist = backup_dir / "modlist.txt"
    if modlist.is_file():
        for line in modlist.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            prefix, name = line[0], line[1:]
            if prefix not in "+-*" or not name:
                continue
            if name.endswith(_SEPARATOR_SUFFIX):
                separators += 1
                continue
            mods_total += 1
            # '+' and '*' are enabled; '-' is disabled (non-separator).
            if prefix in "+*":
                mods_enabled += 1

    plugins_txt = backup_dir / "plugins.txt"
    if plugins_txt.is_file():
        for line in plugins_txt.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            plugins += 1

    return {
        "mods_total": mods_total,
        "mods_enabled": mods_enabled,
        "separators": separators,
        "plugins": plugins,
    }


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


def delete_backup(backup_dir: Path) -> None:
    """Permanently delete one backup folder.

    Guards against deleting anything that isn't a timestamp-named folder
    directly inside a ``backups`` dir, so a bad path can't wipe a profile.
    """
    backup_dir = Path(backup_dir)
    if (backup_dir.parent.name != _BACKUPS_SUBDIR
            or _parse_timestamp_from_dirname(backup_dir.name) is None):
        raise ValueError(f"Not a backup folder: {backup_dir}")
    shutil.rmtree(backup_dir)
