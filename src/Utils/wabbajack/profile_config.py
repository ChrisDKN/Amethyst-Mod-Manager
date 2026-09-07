from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from Utils.atomic_write import write_atomic_text
from .games import nexus_domain
from .paths import WabbajackError, within

WITCHER_SETTINGS = {"user.settings", "dx12user.settings", "input.settings"}


def _save(journal, rows):
    from .store import Store
    write_atomic_text(journal, json.dumps(rows))
    with journal.open("rb") as stream:
        os.fsync(stream.fileno())
    Store._sync_directory(journal.parent)


def extra_profile_files(package):
    return WITCHER_SETTINGS if nexus_domain(package.game) == "witcher3" else set()


def _locations(game):
    prefix = game.get_prefix_path()
    if not prefix:
        raise WabbajackError("Configure the Witcher 3 prefix before deploying profile settings")
    destination = Path(prefix) / "drive_c/users/steamuser/Documents/The Witcher 3"
    journal = Path(game.get_profile_root()) / "wabbajack-settings-links.json"
    return destination, journal


def restore_settings(game, log):
    from .store import Store
    destination, journal = _locations(game)
    if not journal.is_file():
        return
    rows = json.loads(journal.read_text())
    profiles = Path(game.get_profile_root()) / "profiles"
    while rows:
        row = rows[0]
        if str(row.get("name", "")).casefold() not in WITCHER_SETTINGS:
            raise WabbajackError("Invalid managed game-settings journal")
        if row.get("destination") != str(destination.resolve()):
            raise WabbajackError("Restore the previous Witcher 3 prefix's profile settings before changing its prefix path")
        source = within(profiles, row["source"])
        parts = Path(row["source"]).parts
        if len(parts) != 3 or parts[1] != "ini files" or parts[2].casefold() != row["name"].casefold():
            raise WabbajackError("Invalid managed profile-settings source")
        target = destination / row["name"]
        backup = destination / row["backup"] if row.get("backup") else None
        if backup and (backup.parent != destination or not backup.name.startswith(".amethyst-settings-")):
            raise WabbajackError("Invalid game-settings backup path")
        backup_exists = backup and (backup.exists() or backup.is_symlink())
        if target.is_symlink():
            if target.resolve() != source.resolve():
                if not (backup and not backup_exists and (not row.get("linked") or row.get("restoring"))):
                    raise WabbajackError(f"Game settings link changed: {target}")
            else:
                row["restoring"] = True
                _save(journal, rows)
                target.unlink()
        elif target.is_file() and row.get("linked") and not row.get("restoring"):
            Store._copy(target, source)
            row["restoring"] = True
            _save(journal, rows)
            target.unlink()
        elif target.exists() and backup_exists:
            if not row.get("restoring"):
                raise WabbajackError(f"Game settings changed during interrupted deployment: {target}")
            target.unlink()
        elif target.is_file() and row.get("restoring") and not backup:
            target.unlink()
        if backup_exists:
            backup.replace(target)
        Store._sync_directory(destination)
        log(f"Restored game settings: {row['name']}")
        rows.pop(0)
        _save(journal, rows)
    journal.unlink()
    Store._sync_directory(journal.parent)


def link_settings(game, profile, log):
    from Utils.profiles.state import read_profile_settings
    from .store import Store
    profiles = Path(game.get_profile_root()) / "profiles"
    profile_dir = within(profiles, profile)
    if (Path(game.get_profile_root()) / "wabbajack-settings-links.json").is_file():
        restore_settings(game, log)
    settings = read_profile_settings(profile_dir)
    if not (settings.get("wabbajack_install_id") or settings.get("is_group")) or not settings.get("profile_ini_files"):
        return
    destination, journal = _locations(game)
    restore_settings(game, log)
    files = {}
    for path in (profile_dir / "ini files").glob("*"):
        name = path.name.casefold()
        if name not in WITCHER_SETTINGS:
            continue
        if name in files:
            raise WabbajackError(f"Conflicting profile-settings filenames: {path}")
        within(profiles, path.relative_to(profiles).as_posix())
        if path.is_file():
            files[name] = path
    if not files:
        return
    destination.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, source in files.items():
        target = destination / name
        present = [p for p in destination.iterdir() if p.name.casefold() == name]
        if len(present) > 1:
            raise WabbajackError(f"Conflicting game-settings filenames at {destination}: {name}")
        if present:
            target = present[0]
        backup = ".amethyst-settings-" + uuid.uuid4().hex if target.exists() or target.is_symlink() else ""
        row = {"name": target.name, "source": source.relative_to(profiles).as_posix(), "backup": backup,
               "destination": str(destination.resolve()), "linked": False}
        rows.append(row)
        _save(journal, rows)
        if backup:
            target.replace(destination / backup)
            Store._sync_directory(destination)
        target.symlink_to(os.path.relpath(source, destination))
        Store._sync_directory(destination)
        row["linked"] = True
        _save(journal, rows)
        log(f"Linked profile settings: {name}")
