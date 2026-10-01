"""Migrate name-keyed per-mod state when a mod is renamed.

Tkinter-free port of the disk-backed half of gui/modlist_panel.py
`_migrate_mod_name_state` (6618-6690): strip prefixes, disabled plugins,
excluded mod files and mod notes are all keyed by mod name in the profile's
state files - a rename must re-key them or the settings silently detach from
the mod. (modindex.bin migration lives in Utils.filemap.rename_in_mod_index;
the in-memory renderer sets are Tk-only and rebuilt on reload in Qt.)
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path

from Utils.profiles.state import (
    read_mod_strip_prefixes, read_disabled_plugins, read_excluded_mod_files,
    read_root_mod_files, read_mod_notes, read_ignored_mod_updates,
    read_mod_groups, read_profile_state, write_profile_state, _lock_for,
    profile_uses_specific_mods,
)


@contextmanager
def remap_group_mod_name(profile_dir, old_name, new_name):
    from Utils.mods.modlist import read_modlist
    from Utils.profiles.groups import (
        is_group, get_members, group_build_lock, owner_of,
        _read_identity_map, _write_identity_map, _merge_members,
        _mod_identity_and_version,
    )
    if not profile_uses_specific_mods(profile_dir):
        yield lambda: None
        return
    with ExitStack() as locks:
        affected = []
        for group_dir in sorted(profile_dir.parent.iterdir()):
            if not group_dir.is_dir() or not is_group(group_dir):
                continue
            locks.enter_context(group_build_lock(group_dir))
            if profile_dir.name not in get_members(group_dir):
                continue
            owner = (profile_dir.name, old_name)
            entries = read_modlist(group_dir / "modlist.txt")
            names = [e.name for e in entries
                     if not e.is_separator and owner_of(group_dir, e.name) == owner]
            if not names:
                continue
            target = group_dir / "mods" / new_name
            if ((target.exists() or target.is_symlink()
                 or any(e.name == new_name for e in entries))
                    and owner_of(group_dir, new_name) != owner):
                raise ValueError(f"Cannot restore the name '{new_name}'; another mod in '{group_dir.name}' already uses it.")
            affected.append((group_dir, names, _read_identity_map(group_dir)))
        committed = []

        def remap():
            for group_dir, names, original in affected:
                _, records = _merge_members(group_dir.parent, get_members(group_dir),
                                             lambda m: None, False)
                key = next((key for key, record in records.items()
                            if record["member"] == profile_dir.name
                            and record["folder"] == new_name), None)
                if key is None:
                    key, _ = _mod_identity_and_version(profile_dir / "mods", new_name)
                updated = dict(original)
                updated.update((name, key) for name in names)
                _write_identity_map(group_dir, updated)
                committed.append((group_dir, original))

        try:
            yield remap
        except Exception:
            for group_dir, original in reversed(committed):
                _write_identity_map(group_dir, original)
            raise


def migrate_mod_state(profile_dir: Path | None, old_name: str,
                      new_name: str, log_fn=None, *, strict=False) -> None:
    """Re-key every disk-backed per-mod setting from *old_name* to *new_name*.
    Settings absent for the old name are left untouched."""
    log = log_fn or (lambda m: None)
    if profile_dir is None or not old_name or not new_name or old_name == new_name:
        return
    try:
        from Utils.mods.groups import rename_group_mod
        with _lock_for(profile_dir):
            state = read_profile_state(profile_dir)
            changed = False
            groups = read_mod_groups(profile_dir, state)
            renamed = rename_group_mod(groups, old_name, new_name)
            if renamed != groups:
                state["mod_groups"] = renamed
                changed = True
            for key, reader in (
                    ("mod_strip_prefixes", read_mod_strip_prefixes),
                    ("disabled_plugins", read_disabled_plugins),
                    ("excluded_mod_files", read_excluded_mod_files),
                    ("root_mod_files", read_root_mod_files),
                    ("mod_notes", read_mod_notes),
                    ("ignored_mod_updates", read_ignored_mod_updates)):
                data = reader(profile_dir, state)
                if old_name in data:
                    data[new_name] = data.pop(old_name)
                    state[key] = data
                    changed = True
            if changed:
                write_profile_state(profile_dir, state)
    except Exception as exc:
        if strict:
            raise
        log(f"Rename: failed to migrate mod state: {exc}")
