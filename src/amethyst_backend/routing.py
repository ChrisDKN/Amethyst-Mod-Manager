from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from Utils.deployment.shared import CustomRule
from Utils.games.routing_rules import (
    RoutingEntry, load_entries, new_entry, rule_values, save_entries,
    validate_rule,
)

from .jobs import JobError


def _revision(entries: list[RoutingEntry]) -> str:
    data = [
        {"id": entry.key, "defaults": entry.defaults,
         "edits": entry.edits, "disabled": entry.disabled,
         "moved": entry.moved}
        for entry in entries
    ]
    raw = json.dumps(data, sort_keys=True, ensure_ascii=True).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _snapshot(entries: list[RoutingEntry]) -> dict[str, Any]:
    return {
        "revision": _revision(entries),
        "rules": [
            {"id": entry.key, "builtin": entry.builtin,
             "enabled": not entry.disabled,
             "values": rule_values(entry.rule),
             "defaults": copy.deepcopy(entry.defaults)}
            for entry in entries
        ],
    }


def get_routing(game: Any) -> dict[str, Any]:
    try:
        return _snapshot(load_entries(game))
    except (OSError, ValueError) as exc:
        raise JobError("routing_rules_unavailable",
                       "Could not read the game's routing rules.",
                       {"reason": str(exc)}) from exc


def _prepare(current: list[RoutingEntry], rows: list[dict[str, Any]]) -> list[RoutingEntry]:
    if not isinstance(rows, list):
        raise JobError("invalid_routing_rules", "Rules must be an array.")
    if len(rows) > 512:
        raise JobError("invalid_routing_rules", "At most 512 rules are allowed.")
    current_by_id = {entry.key: entry for entry in current}
    seen: set[str] = set()
    submitted: list[RoutingEntry] = []
    known_order: list[str] = []
    fields = set(rule_values(CustomRule(dest="")))
    for index, row in enumerate(rows):
        if (not isinstance(row, dict)
                or set(row) - {"id", "enabled", "values", "builtin", "defaults"}
                or "id" not in row or "enabled" not in row
                or "values" not in row or type(row["enabled"]) is not bool):
            raise JobError("invalid_routing_rules",
                           "Every rule needs an ID, enabled flag, and values.",
                           {"index": index})
        values = row["values"]
        if not isinstance(values, dict) or set(values) != fields:
            raise JobError("invalid_routing_rules",
                           "Every rule must contain all routing fields.",
                           {"index": index, "fields": sorted(fields)})
        key = row["id"]
        if key is None:
            try:
                entry = new_entry(validate_rule(values))
            except ValueError as exc:
                raise JobError("invalid_routing_rules", str(exc),
                               {"index": index}) from exc
        elif isinstance(key, str) and key in current_by_id and key not in seen:
            seen.add(key)
            known_order.append(key)
            entry = copy.deepcopy(current_by_id[key])
            old_values = rule_values(entry.rule)
            if values != old_values:
                try:
                    normalized = rule_values(validate_rule(values))
                except ValueError as exc:
                    raise JobError("invalid_routing_rules", str(exc),
                                   {"index": index}) from exc
                changed = {name for name in fields
                           if values[name] != old_values[name]}
                desired = {name: (normalized[name] if name in changed
                                  else old_values[name]) for name in fields}
                entry.edit(CustomRule(**desired))
        else:
            raise JobError("invalid_routing_rules",
                           "A rule ID is unknown or repeated.",
                           {"index": index, "id": key})
        entry.disabled = not row["enabled"]
        submitted.append(entry)

    missing_builtins = [entry.key for entry in current
                        if entry.builtin and entry.key not in seen]
    if missing_builtins:
        raise JobError("invalid_routing_rules",
                       "Built-in rules must remain in the list; disable them instead.",
                       {"missing": missing_builtins})
    old_order = [entry.key for entry in current if entry.key in seen]
    for position, key in enumerate(known_order):
        if key != old_order[position]:
            next(entry for entry in submitted if entry.key == key).moved = True
    return submitted


def set_routing(
    game: Any, profile_dir: Path | None, revision: str,
    rows: list[dict[str, Any]],
    *, log_fn: Callable[[str], None] | None = None,
    progress_fn: Callable[[int, int, str | None], None] | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    with acquire_game_operation(
        game, "games.routing.set", owner="headless:games.routing.set",
    ):
        try:
            current = load_entries(game)
        except (OSError, ValueError) as exc:
            raise JobError("routing_rules_unavailable",
                           "Could not read the game's routing rules.",
                           {"reason": str(exc)}) from exc
        if _revision(current) != revision:
            raise JobError("stale_routing_rules",
                           "The routing rules changed; get them again.",
                           {"routing": _snapshot(current)})
        proposed = _prepare(current, rows)
        if proposed == current:
            return {"changed": False, "routing": _snapshot(current),
                    "reconciled_profile": None}
        progress(0, 2, "Saving routing rules")
        try:
            latest = load_entries(game)
            if _revision(latest) != revision:
                raise JobError("stale_routing_rules",
                               "The routing rules changed; get them again.",
                               {"routing": _snapshot(latest)})
            save_entries(game, proposed)
        except JobError:
            raise
        except (OSError, ValueError) as exc:
            raise JobError("routing_rules_save_failed",
                           "Could not save the routing rules.",
                           {"reason": str(exc)}) from exc
        saved = _snapshot(proposed)
        progress(1, 2, "Refreshing the selected profile")
        if profile_dir is not None:
            previous = getattr(game, "_active_profile_dir", None)
            errors: list[str] = []
            try:
                from Utils.mods.state import _reconcile_filegraph
                game.set_active_profile_dir(profile_dir)
                game.load_paths(read_only=True)
                _reconcile_filegraph(
                    game, profile_dir, kind="routing", mod_names=[],
                    log=log, progress=progress,
                )
            except Exception as exc:
                errors.append(str(exc))
            finally:
                try:
                    game.set_active_profile_dir(previous)
                    game.load_paths(read_only=True)
                except Exception as exc:
                    errors.append(f"restore active profile: {exc}")
            if errors:
                raise JobError(
                    "routing_sync_failed",
                    "The rules were saved, but the selected profile did not refresh.",
                    {"saved": True, "routing": saved,
                     "profile": profile_dir.name, "errors": errors},
                )
        progress(2, 2, "Routing rules saved")
        log("Saved per-game routing rules.")
        return {"changed": True, "routing": saved,
                "reconciled_profile": profile_dir.name if profile_dir else None}
