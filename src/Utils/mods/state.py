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
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    profile_dir = Path(profile_dir)
    lease = acquire_game_operation(
        game, "mods.set_enabled", owner=operation_owner or "mods.set_enabled")
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
                profile_dir / "modlist.txt", changes)
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
) -> tuple[bool, list[str], list[str]]:
    from Utils.mods.modlist import modlist_lock, read_modlist, write_modlist

    if not mod_names or len(set(mod_names)) != len(mod_names):
        raise ModStateError(
            "invalid_move",
            "Moved mod names must be non-empty and unique.",
        )
    moving = set(mod_names)
    with modlist_lock(modlist_path):
        entries = read_modlist(modlist_path)
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
                    "mod_not_movable",
                    f"'{name}' is a separator, not a movable mod.",
                    {"mod": name, "reason": "separator"},
                )
            if entry.locked:
                raise ModStateError(
                    "mod_locked",
                    f"Mod '{name}' is locked and cannot be moved.",
                    {"mod": name},
                )

        anchor_matches = [entry for entry in entries if entry.name == anchor]
        if not anchor_matches:
            raise ModStateError(
                "move_anchor_not_found",
                f"Move anchor '{anchor}' was not found in the profile.",
                {"anchor": anchor},
            )
        if len(anchor_matches) > 1:
            raise ModStateError(
                "ambiguous_move_anchor",
                f"Move anchor '{anchor}' appears more than once in the profile.",
                {"anchor": anchor, "matches": len(anchor_matches)},
            )
        if anchor in moving:
            raise ModStateError(
                "invalid_move",
                "The move anchor cannot also be a moved mod.",
                {"anchor": anchor},
            )

        block = [entry for entry in entries if entry.name in moving]
        remaining = [entry for entry in entries if entry.name not in moving]
        anchor_index = next(
            index for index, entry in enumerate(remaining)
            if entry.name == anchor)
        insert_at = anchor_index + (1 if after else 0)
        reordered = remaining[:insert_at] + block + remaining[insert_at:]
        old_order = [entry.name for entry in entries if not entry.is_separator]
        new_order = [entry.name for entry in reordered if not entry.is_separator]
        moved = [entry.name for entry in block]
        changed = reordered != entries
        if changed:
            write_modlist(modlist_path, reordered)
        return changed, moved, _crossed_mods(old_order, new_order, moved)


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
