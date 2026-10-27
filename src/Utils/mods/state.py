from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, str | None], None]


class ModStateError(RuntimeError):
    def __init__(self, name: str, message: str,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


def _apply_enabled_changes(
    modlist_path: Path,
    changes: list[tuple[str, bool]],
    *, game=None,
) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    from Utils.mods.modlist import modlist_lock, read_modlist, write_modlist

    with modlist_lock(modlist_path):
        entries = read_modlist(modlist_path)
        resolved = []
        for name, enabled in changes:
            matches = [entry for entry in entries if entry.name == name]
            if not matches:
                raise ModStateError(
                    "mod_not_found",
                    f"Mod '{name}' was not found in the profile.",
                    {"mod": name},
                )
            if len(matches) > 1:
                raise ModStateError(
                    "ambiguous_mod",
                    f"Mod name '{name}' appears more than once in the profile.",
                    {"mod": name, "matches": len(matches)},
                )
            entry = matches[0]
            if entry.is_separator:
                raise ModStateError(
                    "mod_not_toggleable",
                    f"'{name}' is a separator, not a mod.",
                    {"mod": name, "reason": "separator"},
                )
            if entry.locked and entry.enabled != enabled:
                raise ModStateError(
                    "mod_locked",
                    f"Mod '{name}' is locked and cannot be toggled.",
                    {"mod": name, "enabled": entry.enabled},
                )
            resolved.append((entry, enabled))

        changed: list[tuple[str, bool]] = []
        unchanged: list[tuple[str, bool]] = []
        for entry, enabled in resolved:
            target = changed if entry.enabled != enabled else unchanged
            target.append((entry.name, enabled))
        if changed:
            from amethyst_plugins.guards import validate_operation
            validate_operation(game, modlist_path.parent, "mods.set_enabled",
                               mods=[name for name, _ in changed],
                               changes=[{"name": name, "enabled": enabled} for name, enabled in changed])
            wanted = dict(changed)
            for entry in entries:
                if entry.name in wanted:
                    entry.enabled = wanted[entry.name]
            write_modlist(modlist_path, entries)
        return changed, unchanged


def _reconcile_filegraph(
    game: Any,
    profile_dir: Path,
    *,
    kind: str,
    mod_names: list[str],
    log: LogFn,
    progress: ProgressFn,
) -> tuple[bool, int]:
    from Utils.filegraph.service import FileGraphService

    library = FileGraphService.open_library(game, profile_dir, log_fn=log)

    def refresh_progress(value) -> None:
        label = "Indexing mod files"
        if value.current_mod:
            label += f": {value.current_mod}"
        progress(value.mods_scanned, value.mods_total, label)

    library.ensure_ready(profile_dir, progress=refresh_progress)
    profile = library.open_profile(profile_dir)
    reconciled = profile.ensure_reconciled(operation_hint={
        "kind": kind,
        "mods": list(mod_names),
    })
    return bool(reconciled), profile.snapshot().generation


def set_mods_enabled(
    game: Any,
    profile_dir: Path,
    changes: list[tuple[str, bool]],
    *,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
    operation_locked: bool = False,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    profile_dir = Path(profile_dir)
    lease = (None if operation_locked else acquire_game_operation(
        game, "mods.set_enabled", owner=operation_owner or "mods.set_enabled"))
    if lease is not None:
        lease.acquire()
    try:
        try:
            game.set_active_profile_dir(profile_dir)
            game.load_paths()
        except Exception as exc:
            raise ModStateError(
                "resource_unavailable",
                f"Could not load profile '{profile_dir.name}'.",
                {"profile": profile_dir.name, "reason": str(exc)},
            ) from exc

        progress(0, 3, "Updating mod state")
        try:
            changed, unchanged = _apply_enabled_changes(
                profile_dir / "modlist.txt", changes, game=game)
        except ModStateError:
            raise
        except OSError as exc:
            raise ModStateError(
                "resource_unavailable",
                f"Could not update profile '{profile_dir.name}'.",
                {
                    "profile": profile_dir.name,
                    "path": str(profile_dir / "modlist.txt"),
                    "reason": str(exc),
                },
            ) from exc

        requested = list(changes)
        committed = [{"name": name, "enabled": enabled}
                     for name, enabled in changed]
        progress(1, 3, "Synchronizing plugins")
        try:
            from Utils.plugins.sync import sync_plugins_for_mods
            staging_dir = Path(game.get_effective_mod_staging_path())
            plugins_changed = sync_plugins_for_mods(
                game, profile_dir, staging_dir, requested, log_fn=log)
        except Exception as exc:
            raise ModStateError(
                "mod_state_sync_failed",
                "The requested mod state is persisted, but plugin "
                "synchronization failed.",
                {
                    "stage": "plugins",
                    "modlist_changed": bool(changed),
                    "changes": committed,
                    "reason": str(exc),
                },
            ) from exc

        progress(2, 3, "Reconciling file graph")
        try:
            reconciled, generation = _reconcile_filegraph(
                game,
                profile_dir,
                kind="toggle",
                mod_names=[name for name, _enabled in requested],
                log=log,
                progress=progress,
            )
        except Exception as exc:
            raise ModStateError(
                "mod_state_sync_failed",
                "The requested mod state is persisted, but Filegraph "
                "reconciliation failed.",
                {
                    "stage": "filegraph",
                    "modlist_changed": bool(changed),
                    "changes": committed,
                    "reason": str(exc),
                },
            ) from exc

        progress(3, 3, "Mod state updated")
        if changed:
            enabled_count = sum(enabled for _name, enabled in changed)
            log(
                f"Mod state updated: {enabled_count} enabled, "
                f"{len(changed) - enabled_count} disabled."
            )
        else:
            log("Mod state already matched the requested values.")
        return {
            "ok": True,
            "changed": committed,
            "unchanged": [
                {"name": name, "enabled": enabled}
                for name, enabled in unchanged
            ],
            "plugins_changed": bool(plugins_changed),
            "filegraph_reconciled": bool(reconciled),
            "filegraph_generation": generation,
        }
    finally:
        if lease is not None:
            lease.release()


def _crossed_mods(
    old_order: list[str],
    new_order: list[str],
    moved_names: list[str],
) -> list[str]:
    old_positions = {name: index for index, name in enumerate(old_order)}
    new_positions = {name: index for index, name in enumerate(new_order)}
    crossed: set[str] = set()
    for moved in moved_names:
        old_moved = old_positions.get(moved)
        new_moved = new_positions.get(moved)
        if old_moved is None or new_moved is None:
            continue
        for name in old_positions.keys() & new_positions.keys():
            if name == moved:
                continue
            if ((old_positions[name] < old_moved)
                    != (new_positions[name] < new_moved)):
                crossed.add(name)
    return sorted(crossed)


def _apply_mod_move(
    modlist_path: Path,
    mod_names: list[str],
    *,
    anchor: str,
    after: bool,
    game=None,
) -> tuple[bool, list[str], list[str]]:
    from Utils.mods import groups as grouping

    if not mod_names or len(set(mod_names)) != len(mod_names):
        raise ModStateError(
            "invalid_move", "Moved mod names must be non-empty and unique.")
    with grouping.group_edit_state(modlist_path) as (raw, state, original, old_groups):
        groups = grouping.normalize_groups(old_groups, original)
        entries = grouping.with_group_headers(original, groups)
        moving = grouping.expand_leaders(mod_names, groups)
        for name in moving:
            matches = [entry for entry in entries if entry.name == name]
            if len(matches) != 1:
                raise ModStateError(
                    "mod_not_found" if not matches else "ambiguous_mod",
                    f"Mod or group '{name}' does not uniquely match a profile entry.",
                    {"mod": name, "matches": len(matches)})
            entry = matches[0]
            if entry.is_separator:
                raise ModStateError(
                    "mod_not_movable", f"'{name}' is a separator, not a movable mod.",
                    {"mod": name, "reason": "separator"})
            if entry.locked and not entry.is_group_header:
                raise ModStateError(
                    "mod_locked", f"Mod '{name}' is locked and cannot be moved.",
                    {"mod": name})

        anchors = [index for index, entry in enumerate(entries) if entry.name == anchor]
        if len(anchors) != 1:
            raise ModStateError(
                "move_anchor_not_found" if not anchors else "ambiguous_move_anchor",
                f"Move anchor '{anchor}' does not uniquely match a profile entry.",
                {"anchor": anchor, "matches": len(anchors)})
        if anchor in moving:
            raise ModStateError(
                "invalid_move", "The move anchor cannot also be in the moved group or selection.",
                {"anchor": anchor})

        slot = anchors[0] + (1 if after else 0)
        membership = grouping.owners(groups)
        source_owners = {membership.get(name) for name in moving}
        retain = next(iter(source_owners)) if len(source_owners) == 1 else None
        if retain in moving or membership.get(anchor) != retain:
            retain = None
        if retain:
            positions = [index for index, entry in enumerate(entries)
                         if membership.get(entry.name) == retain]
            if not min(positions) < slot <= max(positions) + 1:
                retain = None
        if retain is None and anchor in membership:
            target = membership[anchor]
            positions = [index for index, entry in enumerate(entries)
                         if membership.get(entry.name) == target]
            slot = max(positions) + 1 if after else min(positions)

        reordered, updated = grouping.relocate(entries, groups, mod_names, slot, retain=retain)
        from amethyst_plugins.guards import validate_operation
        before = [e.name for e in original if not e.is_separator]
        after_order = [e.name for e in reordered if not e.is_separator and not e.is_group_header]
        if before != after_order:
            validate_operation(game, modlist_path.parent, "mods.move",
                               mods=[name for name in moving if not name.startswith("@group:")],
                               old_order=before, new_order=after_order,
                               position={"after" if after else "before": anchor})
        reordered, updated, order_changed, groups_changed = grouping.save_group_edit(
            modlist_path, raw, state, reordered, updated)
        old_order = [entry.name for entry in original if not entry.is_separator]
        new_order = [entry.name for entry in reordered if not entry.is_separator]
        moved = [entry.name for entry in original
                 if entry.name in moving and not entry.is_separator]
        return order_changed or groups_changed, moved, _crossed_mods(old_order, new_order, moved)


def move_mods(
    game: Any,
    profile_dir: Path,
    mod_names: list[str],
    *,
    anchor: str,
    after: bool,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    profile_dir = Path(profile_dir)
    lease = acquire_game_operation(
        game, "mods.move", owner=operation_owner or "mods.move")
    lease.acquire()
    try:
        try:
            game.set_active_profile_dir(profile_dir)
            game.load_paths()
        except Exception as exc:
            raise ModStateError(
                "resource_unavailable",
                f"Could not load profile '{profile_dir.name}'.",
                {"profile": profile_dir.name, "reason": str(exc)},
            ) from exc

        progress(0, 2, "Moving mods")
        try:
            changed, moved, crossed = _apply_mod_move(
                profile_dir / "modlist.txt",
                mod_names,
                anchor=anchor,
                after=after,
                game=game,
            )
        except ModStateError:
            raise
        except OSError as exc:
            raise ModStateError(
                "resource_unavailable",
                f"Could not update profile '{profile_dir.name}'.",
                {
                    "profile": profile_dir.name,
                    "path": str(profile_dir / "modlist.txt"),
                    "reason": str(exc),
                },
            ) from exc

        placement = "after" if after else "before"
        progress(1, 2, "Reconciling file graph")
        try:
            reconciled, generation = _reconcile_filegraph(
                game,
                profile_dir,
                kind="move",
                mod_names=moved,
                log=log,
                progress=progress,
            )
        except Exception as exc:
            raise ModStateError(
                "mod_order_sync_failed",
                "The requested mod order is persisted, but Filegraph "
                "reconciliation failed.",
                {
                    "stage": "filegraph",
                    "order_changed": changed,
                    "moved": moved,
                    "position": {placement: anchor},
                    "reason": str(exc),
                },
            ) from exc

        progress(2, 2, "Mod order updated")
        if changed:
            log(
                f"Moved {len(moved)} mod(s) {placement} '{anchor}' "
                f"and crossed {len(crossed)} mod(s)."
            )
        else:
            log("Mod order already matched the requested move.")
        return {
            "ok": True,
            "changed": changed,
            "moved": moved,
            "crossed": crossed,
            "position": {placement: anchor},
            "filegraph_reconciled": reconciled,
            "filegraph_generation": generation,
        }
    finally:
        lease.release()


def _validate_removal(entries: list[Any], mod_names: list[str]) -> None:
    if not mod_names or len(set(mod_names)) != len(mod_names):
        raise ModStateError(
            "invalid_mod_removal",
            "Removed mod names must be non-empty and unique.",
        )
    for name in mod_names:
        matches = [entry for entry in entries if entry.name == name]
        if not matches:
            raise ModStateError(
                "mod_not_found",
                f"Mod '{name}' was not found in the profile.",
                {"mod": name},
            )
        if len(matches) > 1:
            raise ModStateError(
                "ambiguous_mod",
                f"Mod name '{name}' appears more than once in the profile.",
                {"mod": name, "matches": len(matches)},
            )
        entry = matches[0]
        if entry.is_separator:
            raise ModStateError(
                "mod_not_removable",
                f"'{name}' is a separator, not a removable mod.",
                {"mod": name, "reason": "separator"},
            )
        if entry.locked:
            raise ModStateError(
                "mod_locked",
                f"Mod '{name}' is locked and cannot be removed.",
                {"mod": name},
            )


def _drop_removed_mod_state(profile_dir: Path, mod_names: list[str]) -> None:
    from amethyst_plugins.metadata import update_mods
    update_mods(profile_dir, drops=mod_names)
    from Utils.profiles.state import (
        read_disabled_plugins,
        read_excluded_mod_files,
        read_mod_notes,
        read_mod_strip_prefixes,
        read_root_mod_files,
        write_disabled_plugins,
        write_excluded_mod_files,
        write_mod_notes,
        write_mod_strip_prefixes,
        write_root_mod_files,
    )

    removed = set(mod_names)
    for reader, writer in (
        (read_disabled_plugins, write_disabled_plugins),
        (read_excluded_mod_files, write_excluded_mod_files),
        (read_root_mod_files, write_root_mod_files),
        (read_mod_notes, write_mod_notes),
        (read_mod_strip_prefixes, write_mod_strip_prefixes),
    ):
        data = reader(profile_dir, None)
        kept = {name: value for name, value in data.items()
                if name not in removed}
        if kept != data:
            writer(profile_dir, kept)


def remove_profile_mods(
    game: Any,
    profile_dir: Path,
    mod_names: list[str],
    *,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    profile_dir = Path(profile_dir)
    lease = acquire_game_operation(
        game, "mods.remove", owner=operation_owner or "mods.remove")
    lease.acquire()
    try:
        try:
            game.set_active_profile_dir(profile_dir)
            game.load_paths()
            staging_dir = Path(game.get_effective_mod_staging_path())
        except Exception as exc:
            raise ModStateError(
                "resource_unavailable",
                f"Could not load profile '{profile_dir.name}'.",
                {"profile": profile_dir.name, "reason": str(exc)},
            ) from exc

        from Utils.mods.modlist import modlist_lock, read_modlist, write_modlist
        from Utils.profiles.groups import is_group, locked_owners
        from Utils.profiles.state import profile_uses_specific_mods

        grouped = is_group(profile_dir)
        shared_staging = not profile_uses_specific_mods(profile_dir)
        scope = ("profile_group_and_members" if grouped else
                 "shared_staging" if shared_staging else "profile")
        modlist_path = profile_dir / "modlist.txt"
        progress(0, 3, "Validating mod removal")
        with modlist_lock(modlist_path):
            try:
                entries = read_modlist(modlist_path)
            except Exception as exc:
                raise ModStateError(
                    "resource_unavailable",
                    f"Could not read profile '{profile_dir.name}'.",
                    {
                        "profile": profile_dir.name,
                        "path": str(modlist_path),
                        "reason": str(exc),
                    },
                ) from exc
            _validate_removal(entries, mod_names)

            if grouped:
                blocked = locked_owners(profile_dir, mod_names)
                if blocked:
                    first = next(name for name in mod_names if name in blocked)
                    raise ModStateError(
                        "mod_locked",
                        f"Mod '{first}' belongs to locked profile "
                        f"'{blocked[first]}' and cannot be removed through "
                        "the group.",
                        {
                            "mod": first,
                            "owner_profile": blocked[first],
                            "blocked": [
                                {"mod": name, "owner_profile": blocked[name]}
                                for name in mod_names if name in blocked
                            ],
                        },
                    )

            missing_folders = [
                name for name in mod_names
                if not (staging_dir / name).is_dir()
                and not (staging_dir / name).is_symlink()
            ]
            progress(1, 3, "Removing mod files")
            try:
                if grouped:
                    from Utils.profiles.groups import remove_mods_from_group
                    removed = remove_mods_from_group(
                        game, profile_dir, mod_names, log_fn=log, strict=True)
                else:
                    from Utils.mods.remove import remove_mods
                    remove_mods(
                        game, profile_dir, mod_names, log_fn=log, strict=True)
                    removed = list(mod_names)
            except Exception as exc:
                raise ModStateError(
                    "mod_removal_failed",
                    "Mod cleanup did not complete; the profile's mod list "
                    "was left intact so the operation can be retried.",
                    {
                        "stage": "cleanup",
                        "mods": list(mod_names),
                        "modlist_changed": False,
                        "reason": str(exc),
                    },
                ) from exc

            if removed != mod_names:
                raise ModStateError(
                    "mod_removal_failed",
                    "Not every requested mod was removed; the profile's mod "
                    "list was left intact.",
                    {
                        "stage": "cleanup",
                        "mods": list(mod_names),
                        "removed": list(removed),
                        "modlist_changed": False,
                    },
                )

            progress(2, 3, "Updating profile state")
            try:
                _drop_removed_mod_state(profile_dir, mod_names)
                removed_names = set(mod_names)
                write_modlist(
                    modlist_path,
                    [entry for entry in entries
                     if entry.name not in removed_names],
                )
            except Exception as exc:
                raise ModStateError(
                    "mod_removal_sync_failed",
                    "The mod folders were removed, but the profile state "
                    "could not be fully updated.",
                    {
                        "stage": "profile",
                        "mods_removed": list(mod_names),
                        "modlist_changed": False,
                        "reason": str(exc),
                    },
                ) from exc

        progress(3, 3, "Mods removed")
        log(f"Removed {len(mod_names)} mod(s) from '{profile_dir.name}'.")
        return {
            "ok": True,
            "removed": list(mod_names),
            "missing_folders": missing_folders,
            "scope": scope,
        }
    finally:
        lease.release()
