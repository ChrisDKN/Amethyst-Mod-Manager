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
            from Utils.filegraph.service import FileGraphService
            library = FileGraphService.open_library(
                game, profile_dir, log_fn=log)

            def refresh_progress(value) -> None:
                label = "Indexing mod files"
                if value.current_mod:
                    label += f": {value.current_mod}"
                progress(value.mods_scanned, value.mods_total, label)

            library.ensure_ready(profile_dir, progress=refresh_progress)
            profile = library.open_profile(profile_dir)
            reconciled = profile.ensure_reconciled(operation_hint={
                "kind": "toggle",
                "mods": [name for name, _enabled in requested],
            })
            generation = profile.snapshot().generation
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
