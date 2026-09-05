from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, str | None], None]


class PluginStateError(RuntimeError):
    def __init__(self, name: str, message: str,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


@contextmanager
def _preserve_active_profile(game: Any, log: LogFn):
    previous = getattr(game, "_active_profile_dir", None)
    try:
        yield
    finally:
        if getattr(game, "_active_profile_dir", None) != previous:
            try:
                game.set_active_profile_dir(previous)
                game.load_paths()
            except Exception as exc:
                log(f"WARN previous profile context could not be restored: {exc}")


def _load_profile(
    game: Any,
    profile_dir: Path,
) -> tuple[list[Any], set[str], Any]:
    profile_dir = Path(profile_dir)
    if not profile_dir.is_dir():
        raise PluginStateError(
            "profile_not_found",
            f"Profile '{profile_dir.name}' is no longer available.",
            {"profile": profile_dir.name},
        )
    try:
        game.set_active_profile_dir(profile_dir)
        game.load_paths()
    except Exception as exc:
        raise PluginStateError(
            "resource_unavailable",
            f"Could not load profile '{profile_dir.name}'.",
            {"profile": profile_dir.name, "reason": str(exc)},
        ) from exc

    try:
        from Utils.filegraph.service import active_snapshot
        snapshot = active_snapshot(game)
    except Exception as exc:
        raise PluginStateError(
            "plugin_state_unavailable",
            "The profile's plugin inventory could not be resolved.",
            {"profile": profile_dir.name, "reason": str(exc)},
        ) from exc

    try:
        from Utils.plugins.state import load_plugins
        rows = load_plugins(
            game,
            profile_dir.name,
            snapshot=snapshot,
            include_bos_sp=False,
            persist_repairs=False,
        )
    except Exception as exc:
        raise PluginStateError(
            "plugin_state_unavailable",
            "The profile's plugin state could not be read.",
            {"profile": profile_dir.name, "reason": str(exc)},
        ) from exc
    if rows is None:
        raise PluginStateError(
            "plugin_state_unavailable",
            "The profile's plugin scan did not complete.",
            {"profile": profile_dir.name},
        )

    try:
        from Utils.profiles.state import read_plugin_locks
        raw_locks = read_plugin_locks(profile_dir) or {}
    except Exception as exc:
        raise PluginStateError(
            "plugin_state_unavailable",
            "The profile's plugin locks could not be read.",
            {"profile": profile_dir.name, "reason": str(exc)},
        ) from exc
    locks = {
        str(name).casefold() for name, locked in raw_locks.items()
        if isinstance(name, str) and bool(locked)
    }
    return rows, locks, snapshot


def _row_flags(row: Any) -> list[str]:
    from Utils.plugins.state import (
        PF_BLUEPRINT,
        PF_DIRTY,
        PF_ESL,
        PF_ESL_SAFE,
        PF_ESL_UNSAFE,
        PF_GROUNDCOVER,
        PF_LATE,
        PF_LOOT,
        PF_MASTER,
        PF_MISSING,
        PF_TAGS,
        PF_UL_CYCLE,
        PF_USERLIST,
        PF_VMM,
    )
    names = (
        (PF_MISSING, "missing_masters"),
        (PF_LATE, "late_masters"),
        (PF_VMM, "version_mismatched_masters"),
        (PF_ESL, "light"),
        (PF_LOOT, "loot_messages"),
        (PF_DIRTY, "dirty"),
        (PF_TAGS, "bash_tags"),
        (PF_MASTER, "master"),
        (PF_USERLIST, "userlist"),
        (PF_UL_CYCLE, "userlist_cycle"),
        (PF_ESL_SAFE, "esl_eligible"),
        (PF_ESL_UNSAFE, "esl_unsafe"),
        (PF_BLUEPRINT, "blueprint"),
        (PF_GROUNDCOVER, "groundcover"),
    )
    return [name for bit, name in names if row.flags & bit]


def _counts(rows: list[Any], locks: set[str]) -> dict[str, int]:
    enabled = sum(bool(row.enabled) for row in rows)
    warnings = sum(bool(
        row.missing_masters or row.late_masters or row.vmm_masters)
        for row in rows)
    return {
        "total": len(rows),
        "enabled": enabled,
        "disabled": len(rows) - enabled,
        "vanilla": sum(bool(row.vanilla) for row in rows),
        "locked": sum(row.name.casefold() in locks for row in rows),
        "with_dependency_warnings": warnings,
    }


def _inventory(
    game: Any,
    profile_dir: Path,
    rows: list[Any],
    locks: set[str],
    snapshot: Any,
) -> dict[str, Any]:
    from Utils.plugins.state import (
        compute_esl_eligibility,
        compute_game_indexes,
        master_block_enabled,
        resolve_plugin_paths_for_game,
    )

    resolved = resolve_plugin_paths_for_game(game, snapshot=snapshot)
    try:
        data_dir = (game.get_vanilla_plugins_path()
                    if hasattr(game, "get_vanilla_plugins_path") else None)
    except Exception:
        data_dir = None
    eligibility = compute_esl_eligibility(
        [row.name for row in rows], resolved, data_dir, game)
    for row in rows:
        row.flags |= eligibility.get(row.name.lower(), 0)

    indexes = compute_game_indexes(rows)
    plugins = []
    for position, (row, game_index) in enumerate(zip(rows, indexes)):
        plugins.append({
            "name": row.name,
            "enabled": bool(row.enabled),
            "vanilla": bool(row.vanilla),
            "locked": row.name.casefold() in locks,
            "position": position,
            "game_index": game_index or None,
            "masters": list(row.masters) if row.masters is not None else None,
            "flags": _row_flags(row),
            "warnings": {
                "missing_masters": list(row.missing_masters or ()),
                "late_masters": list(row.late_masters or ()),
                "version_mismatched_masters": list(row.vmm_masters or ()),
            },
            "loot": dict(row.loot_info) if row.loot_info else None,
        })
    try:
        from LOOT.loot_sorter import read_loot_info
        loot_info = read_loot_info(profile_dir)
    except Exception:
        loot_info = {}
    if not isinstance(loot_info, dict):
        loot_info = {}
    general_messages = loot_info.get("general_messages")
    if not isinstance(general_messages, list):
        general_messages = []
    return {
        "ok": True,
        "profile": {"name": profile_dir.name, "path": str(profile_dir)},
        "order": "first_loaded_first",
        "master_block_enforced": master_block_enabled(game),
        "filegraph_generation": int(snapshot.generation),
        "counts": _counts(rows, locks),
        "plugins": plugins,
        "loot": {
            "generated_at": loot_info.get("generated_at"),
            "game_id": loot_info.get("game_id") or None,
            "general_messages": list(general_messages),
        },
    }


def list_profile_plugins(
    game: Any,
    profile_dir: Path,
    *,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    profile_dir = Path(profile_dir)
    with acquire_game_operation(
        game, "plugins.list", owner=operation_owner or "plugins.list",
    ), _preserve_active_profile(game, log):
        progress(0, 2, "Resolving plugin inventory")
        rows, locks, snapshot = _load_profile(game, profile_dir)
        progress(1, 2, "Reading plugin metadata")
        result = _inventory(game, profile_dir, rows, locks, snapshot)
        progress(2, 2, "Plugin inventory ready")
        return result


def _resolve_row(rows: list[Any], name: str) -> Any:
    matches = [row for row in rows if row.name == name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise PluginStateError(
            "ambiguous_plugin",
            f"Plugin name '{name}' appears more than once in the profile.",
            {"plugin": name, "matches": len(matches)},
        )
    casing = [row.name for row in rows if row.name.casefold() == name.casefold()]
    details: dict[str, Any] = {"plugin": name}
    if len(casing) == 1:
        details["expected_name"] = casing[0]
    raise PluginStateError(
        "plugin_not_found",
        f"Plugin '{name}' was not found in the profile.",
        details,
    )


def _validate_names(names: list[str], *, kind: str) -> None:
    folded = [name.casefold() for name in names]
    if not names or len(set(folded)) != len(folded):
        raise PluginStateError(
            f"invalid_plugin_{kind}",
            "Plugin names must be non-empty and unique.",
        )


def _save(game: Any, profile_dir: Path, rows: list[Any],
          *, changed: list[str]) -> None:
    try:
        from Utils.plugins.state import save_plugins
        save_plugins(
            game, profile_dir.name, rows, stamp_load_order=False)
    except Exception as exc:
        raise PluginStateError(
            "plugin_state_write_failed",
            "The plugin state could not be fully persisted.",
            {
                "profile": profile_dir.name,
                "plugins": list(changed),
                "files_may_have_changed": True,
                "reason": str(exc),
            },
        ) from exc


def set_plugins_enabled(
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
    _validate_names([name for name, _enabled in changes], kind="changes")
    with acquire_game_operation(
        game,
        "plugins.set_enabled",
        owner=operation_owner or "plugins.set_enabled",
    ), _preserve_active_profile(game, log):
        progress(0, 3, "Resolving plugin inventory")
        rows, locks, _snapshot = _load_profile(game, profile_dir)
        progress(1, 3, "Validating plugin changes")
        resolved = [(_resolve_row(rows, name), enabled)
                    for name, enabled in changes]
        for row, enabled in resolved:
            if row.vanilla and row.enabled != enabled:
                raise PluginStateError(
                    "plugin_required",
                    f"Plugin '{row.name}' is game-managed and cannot be disabled.",
                    {"plugin": row.name, "enabled": bool(row.enabled)},
                )

        changed = [(row, enabled) for row, enabled in resolved
                   if bool(row.enabled) != enabled]
        unchanged = [(row, enabled) for row, enabled in resolved
                     if bool(row.enabled) == enabled]
        for row, enabled in changed:
            row.enabled = enabled
        if changed:
            progress(2, 3, "Saving plugin state")
            _save(
                game,
                profile_dir,
                rows,
                changed=[row.name for row, _enabled in changed],
            )
            log(f"Plugin state updated for {len(changed)} plugin(s).")
        else:
            log("Plugin state already matched the requested values.")
        progress(3, 3, "Plugin state updated")
        return {
            "ok": True,
            "changed": [
                {"name": row.name, "enabled": enabled}
                for row, enabled in changed
            ],
            "unchanged": [
                {"name": row.name, "enabled": enabled}
                for row, enabled in unchanged
            ],
            "counts": _counts(rows, locks),
        }


def _crossed_plugins(
    old_order: list[str],
    new_order: list[str],
    moved_names: list[str],
) -> list[str]:
    old_positions = {name: index for index, name in enumerate(old_order)}
    new_positions = {name: index for index, name in enumerate(new_order)}
    crossed: set[str] = set()
    for moved in moved_names:
        for name in old_positions.keys() & new_positions.keys():
            if name == moved:
                continue
            if ((old_positions[name] < old_positions[moved])
                    != (new_positions[name] < new_positions[moved])):
                crossed.add(name)
    return sorted(crossed, key=lambda name: new_positions[name])


def move_plugins(
    game: Any,
    profile_dir: Path,
    plugin_names: list[str],
    *,
    anchor: str,
    after: bool,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation
    from Utils.plugins import enforce_primary_plugin_order
    from Utils.plugins.state import (
        dependency_bounds,
        master_block_enabled,
        movable_bounds,
        plugin_rank,
    )

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    profile_dir = Path(profile_dir)
    _validate_names(plugin_names, kind="move")
    if anchor.casefold() in {name.casefold() for name in plugin_names}:
        raise PluginStateError(
            "invalid_plugin_move",
            "The move anchor cannot also be a moved plugin.",
            {"anchor": anchor},
        )

    with acquire_game_operation(
        game, "plugins.move", owner=operation_owner or "plugins.move",
    ), _preserve_active_profile(game, log):
        progress(0, 3, "Resolving plugin inventory")
        rows, locks, _snapshot = _load_profile(game, profile_dir)
        progress(1, 3, "Validating plugin move")
        selected = [_resolve_row(rows, name) for name in plugin_names]
        anchor_row = _resolve_row(rows, anchor)
        selected_ids = {id(row) for row in selected}
        indices = [index for index, row in enumerate(rows)
                   if id(row) in selected_ids]
        if indices[-1] - indices[0] != len(indices) - 1:
            raise PluginStateError(
                "plugin_move_not_contiguous",
                "Moved plugins must be contiguous in the current load order.",
                {"plugins": [row.name for row in selected]},
            )
        for row in selected:
            if row.vanilla:
                raise PluginStateError(
                    "plugin_not_movable",
                    f"Plugin '{row.name}' is game-managed and cannot be moved.",
                    {"plugin": row.name, "reason": "vanilla"},
                )
            if row.name.casefold() in locks:
                raise PluginStateError(
                    "plugin_locked",
                    f"Plugin '{row.name}' is locked and cannot be moved.",
                    {"plugin": row.name},
                )

        block_enabled = master_block_enabled(game)
        ranks = {plugin_rank(row) for row in selected}
        if block_enabled and len(ranks) != 1:
            raise PluginStateError(
                "plugin_move_mixed_regions",
                "A moved block cannot span engine load-order regions.",
                {"plugins": [row.name for row in selected]},
            )

        first, last = indices[0], indices[-1]
        block = rows[first:last + 1]
        old_order = [row.name for row in rows]
        anchor_index = rows.index(anchor_row)
        destination = anchor_index + (1 if after else 0)
        rest = rows[:first] + rows[last + 1:]
        desired = destination if destination <= first else destination - len(block)
        desired = max(0, min(desired, len(rest)))
        if block_enabled:
            lower, upper = movable_bounds(rest, next(iter(ranks)), True)
        else:
            lower, upper = movable_bounds(rest, 0, False)
        insert_at = max(lower, min(desired, upper))
        dep_lower, dep_upper = dependency_bounds(
            rest, block, moving_up=(insert_at <= first))
        insert_at = max(dep_lower, min(insert_at, dep_upper))
        constrained = insert_at != desired

        reordered = rest[:insert_at] + block + rest[insert_at:]
        reordered, primary_changed = enforce_primary_plugin_order(game, reordered)
        constrained = constrained or primary_changed
        changed = reordered != rows
        moved = [row.name for row in block]
        if changed:
            progress(2, 3, "Saving plugin order")
            _save(game, profile_dir, reordered, changed=moved)
            log(f"Moved {len(moved)} plugin(s) in the load order.")
        else:
            log("Plugin order already matched the constrained move.")
        progress(3, 3, "Plugin order updated")

        new_order = [row.name for row in reordered]
        positions = {name: index for index, name in enumerate(new_order)}
        placement = "after" if after else "before"
        return {
            "ok": True,
            "changed": changed,
            "moved": moved,
            "crossed": _crossed_plugins(old_order, new_order, moved),
            "requested_position": {placement: anchor_row.name},
            "constrained": constrained,
            "positions": [
                {"name": name, "position": positions[name]}
                for name in moved
            ],
        }


def _raise_loot_error(exc: Exception) -> None:
    from LOOT.loot_sorter import (
        LootConfigurationError,
        LootCycleError,
        LootMasterlistError,
        LootUnavailableError,
    )
    if isinstance(exc, LootUnavailableError):
        name = "loot_unavailable"
        message = "The LOOT library is not available."
    elif isinstance(exc, LootConfigurationError):
        name = "loot_configuration_invalid"
        message = "LOOT is not configured for this game."
    elif isinstance(exc, LootMasterlistError):
        name = "loot_masterlist_unavailable"
        message = "No compatible LOOT masterlist is available."
    elif isinstance(exc, LootCycleError):
        name = "loot_cycle"
        message = "LOOT found a cyclic plugin dependency."
    else:
        name = "loot_sort_failed"
        message = "LOOT could not sort the profile's plugins."
    raise PluginStateError(
        name,
        message,
        {"reason": str(exc), "exception": type(exc).__name__},
    ) from exc


def sort_profile_plugins(
    game: Any,
    profile_dir: Path,
    *,
    apply_order: bool = True,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    profile_dir = Path(profile_dir)
    with acquire_game_operation(
        game, "plugins.sort", owner=operation_owner or "plugins.sort",
    ), _preserve_active_profile(game, log):
        progress(0, 4, "Resolving plugin inventory")
        rows, locks, snapshot = _load_profile(game, profile_dir)
        old_order = [row.name for row in rows]
        locked_indices = {
            index: row for index, row in enumerate(rows)
            if row.name.casefold() in locks
        }
        unlocked = [row for index, row in enumerate(rows)
                    if index not in locked_indices]
        if not unlocked:
            progress(4, 4, "No unlocked plugins to sort")
            return {
                "ok": True,
                "order_applied": apply_order,
                "changed": False,
                "would_change": False,
                "sorted_count": 0,
                "filegraph_generation": int(snapshot.generation),
                "locked": [row.name for row in rows],
                "moved": [],
                "order": old_order,
                "warnings": ["No unlocked plugins were available to sort."],
                "metadata": {
                    "saved": False,
                    "plugin_count": 0,
                    "general_messages": [],
                },
                "userlist_source": None,
            }

        userlist_path = profile_dir / "userlist.yaml"
        userlist_source = "profile" if userlist_path.is_file() else None
        if userlist_source is None:
            try:
                from Utils.config_paths import get_loot_game_dir
                global_userlist = (
                    get_loot_game_dir(str(getattr(game, "game_id", "")))
                    / "userlist.yaml"
                )
                if global_userlist.is_file():
                    userlist_path = global_userlist
                    userlist_source = "game"
                else:
                    userlist_path = None
            except Exception:
                userlist_path = None

        try:
            from Utils.filegraph.service import plugin_source_paths
            from LOOT.game_view import ProfileSources
            winner_paths = plugin_source_paths(snapshot, game)
            profile_sources = ProfileSources.from_game(snapshot, game)
            raw_game_path = game.get_game_path()
            raw_staging_root = game.get_effective_mod_staging_path()
            if not raw_game_path or not raw_staging_root:
                raise ValueError("game or staging path is not configured")
            game_path = Path(raw_game_path)
            staging_root = Path(raw_staging_root)
            raw_data_dir = (game.get_vanilla_plugins_path()
                            if hasattr(game, "get_vanilla_plugins_path")
                            else None)
            data_dir = Path(raw_data_dir) if raw_data_dir else None
        except Exception as exc:
            raise PluginStateError(
                "plugin_state_unavailable",
                "The plugin paths required by LOOT could not be resolved.",
                {"profile": profile_dir.name, "reason": str(exc)},
            ) from exc

        progress(1, 4, "Running LOOT")
        try:
            from LOOT.loot_sorter import sort_plugins
            result = sort_plugins(
                plugin_names=[row.name for row in rows],
                enabled_set={row.name for row in rows if row.enabled},
                game_name=str(getattr(game, "name", "") or ""),
                game_path=game_path,
                staging_root=staging_root,
                log_fn=lambda message: log(f"[loot] {message}"),
                game_type_attr=str(
                    getattr(game, "loot_game_type", "") or ""),
                game_id=str(getattr(game, "game_id", "") or ""),
                masterlist_url=str(
                    getattr(game, "loot_masterlist_url", "") or ""),
                masterlist_repo=str(
                    getattr(game, "loot_masterlist_repo", "") or ""),
                game_data_dir=data_dir,
                userlist_path=userlist_path,
                plugin_winner_paths=winner_paths,
                profile_sources=profile_sources,
                locked_positions={
                    row.name: index for index, row in locked_indices.items()
                },
            )
        except Exception as exc:
            _raise_loot_error(exc)

        expected = {row.name.casefold() for row in rows}
        returned = [str(name) for name in result.sorted_names]
        if (len(returned) != len(rows)
                or {name.casefold() for name in returned} != expected):
            raise PluginStateError(
                "loot_sort_invalid_result",
                "LOOT returned a different plugin set than it received.",
                {
                    "expected_count": len(rows),
                    "returned_count": len(returned),
                },
            )

        progress(2, 4, "Saving LOOT metadata")
        warnings = list(result.warnings or ())
        metadata_saved = True
        try:
            from LOOT.loot_sorter import write_loot_info
            write_loot_info(
                profile_dir,
                result.plugin_info,
                result.general_messages,
                game_id=str(getattr(game, "game_id", "") or ""),
            )
        except Exception as exc:
            metadata_saved = False
            warnings.append(f"LOOT metadata could not be saved: {exc}")

        from Utils.plugins import enforce_primary_plugin_order
        from Utils.plugins.state import (
            apply_loot_sort,
            enforce_master_block,
            master_block_enabled,
        )
        new_rows, _moved = apply_loot_sort(
            rows,
            locked_indices,
            returned,
            bool(getattr(game, "plugins_include_vanilla", False)),
        )
        if master_block_enabled(game):
            flags = {row.name.casefold(): row.flags for row in rows}
            for row in new_rows:
                row.flags = flags.get(row.name.casefold(), row.flags)
            new_rows, _changed = enforce_master_block(new_rows)
        new_rows, _primary_changed = enforce_primary_plugin_order(game, new_rows)
        new_order = [row.name for row in new_rows]
        old_positions = {name: index for index, name in enumerate(old_order)}
        moved = [
            name for index, name in enumerate(new_order)
            if old_positions.get(name) != index
        ]
        would_change = bool(moved)

        progress(
            3,
            4,
            "Saving plugin order" if apply_order else "Preparing sort preview",
        )
        if apply_order and would_change:
            _save(game, profile_dir, new_rows, changed=moved)
        progress(4, 4, "LOOT sort complete")
        return {
            "ok": True,
            "order_applied": apply_order,
            "changed": apply_order and would_change,
            "would_change": would_change,
            "sorted_count": len(unlocked),
            "filegraph_generation": int(snapshot.generation),
            "locked": [row.name for row in locked_indices.values()],
            "moved": moved,
            "order": new_order,
            "warnings": warnings,
            "metadata": {
                "saved": metadata_saved,
                "plugin_count": len(result.plugin_info or {}),
                "general_messages": list(result.general_messages or ()),
            },
            "userlist_source": userlist_source,
        }
