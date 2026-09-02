"""Migrate name-keyed per-mod state when a mod is renamed.

Tkinter-free port of the disk-backed half of gui/modlist_panel.py
`_migrate_mod_name_state` (6618-6690): strip prefixes, disabled plugins,
excluded mod files and mod notes are all keyed by mod name in the profile's
state files - a rename must re-key them or the settings silently detach from
the mod. (modindex.bin migration lives in Utils.filemap.rename_in_mod_index;
the in-memory renderer sets are Tk-only and rebuilt on reload in Qt.)
"""

from __future__ import annotations

from pathlib import Path

from Utils.profiles.state import (
    read_mod_strip_prefixes, write_mod_strip_prefixes,
    read_disabled_plugins, write_disabled_plugins,
    read_excluded_mod_files, write_excluded_mod_files,
    read_root_mod_files, write_root_mod_files,
    read_mod_notes, write_mod_notes,
)


def migrate_mod_state(profile_dir: Path | None, old_name: str,
                      new_name: str, log_fn=None) -> None:
    """Re-key every disk-backed per-mod setting from *old_name* to *new_name*.
    Settings absent for the old name are left untouched."""
    log = log_fn or (lambda m: None)
    if profile_dir is None or not old_name or not new_name:
        return
    for reader, writer, label in (
            (read_mod_strip_prefixes, write_mod_strip_prefixes, "strip prefixes"),
            (read_disabled_plugins, write_disabled_plugins, "disabled plugins"),
            (read_excluded_mod_files, write_excluded_mod_files, "excluded files"),
            (read_root_mod_files, write_root_mod_files, "root files"),
            (read_mod_notes, write_mod_notes, "mod notes")):
        try:
            data = reader(profile_dir)
            if old_name in data:
                data[new_name] = data.pop(old_name)
                writer(profile_dir, data)
        except Exception as exc:
            log(f"Rename: failed to migrate {label}: {exc}")
