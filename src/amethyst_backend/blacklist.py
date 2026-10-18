from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from Utils.games.conflict_blacklist import (
    BlacklistOverrides, builtin_rules, effective_rules, load_overrides,
    normalize_pattern, save_overrides,
)

from .jobs import JobError


_KEYS = ("files", "folders", "disabled_files", "disabled_folders")


def _snapshot(game: Any, overrides: BlacklistOverrides) -> dict[str, Any]:
    builtin_files, builtin_folders = builtin_rules(game)
    effective_files, effective_folders = effective_rules(game, overrides)
    defaults = {"files": sorted(builtin_files),
                "folders": sorted(builtin_folders)}
    saved = {key: sorted(getattr(overrides, key)) for key in _KEYS}
    payload = {"defaults": defaults, "overrides": saved}
    revision = hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()
    return {"revision": revision, **payload,
            "effective": {"files": sorted(effective_files),
                          "folders": sorted(effective_folders)}}


def get_blacklist(game: Any) -> dict[str, Any]:
    try:
        return _snapshot(game, load_overrides(game))
    except (OSError, ValueError) as exc:
        raise JobError("blacklist_unavailable",
                       "Could not read the game's conflict blacklist.",
                       {"reason": str(exc)}) from exc


def _prepare(game: Any, values: dict[str, Any],
             current: BlacklistOverrides) -> BlacklistOverrides:
    if not isinstance(values, dict) or set(values) != set(_KEYS):
        raise JobError("invalid_blacklist",
                       "Supply all four blacklist override lists.")
    normalized: dict[str, frozenset[str]] = {}
    for key in _KEYS:
        raw = values[key]
        if not isinstance(raw, list) or len(raw) > 512:
            raise JobError("invalid_blacklist",
                           "Each blacklist override must be an array of at most 512 patterns.",
                           {"field": key})
        try:
            normalized[key] = frozenset(normalize_pattern(value)
                                        for value in raw)
        except ValueError as exc:
            raise JobError("invalid_blacklist", str(exc),
                           {"field": key}) from exc
    builtin_files, builtin_folders = builtin_rules(game)
    for kind, defaults in (("files", builtin_files),
                           ("folders", builtin_folders)):
        disabled_key = "disabled_" + kind
        invalid = normalized[disabled_key] - defaults - getattr(current, disabled_key)
        if invalid:
            raise JobError("invalid_blacklist",
                           "Only built-in or previously disabled patterns can be disabled.",
                           {"field": disabled_key,
                            "patterns": sorted(invalid)})
        normalized[disabled_key] -= normalized[kind] & defaults
        normalized[kind] -= defaults
    return BlacklistOverrides(**normalized)


def set_blacklist(
    game: Any, profile_dir: Path | None, revision: str,
    values: dict[str, Any],
    *, log_fn: Callable[[str], None] | None = None,
    progress_fn: Callable[[int, int, str | None], None] | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    with acquire_game_operation(
        game, "games.blacklist.set", owner="headless:games.blacklist.set",
    ):
        try:
            current = load_overrides(game)
            current_snapshot = _snapshot(game, current)
        except (OSError, ValueError) as exc:
            raise JobError("blacklist_unavailable",
                           "Could not read the game's conflict blacklist.",
                           {"reason": str(exc)}) from exc
        if current_snapshot["revision"] != revision:
            raise JobError("stale_blacklist",
                           "The blacklist changed; get it again.",
                           {"blacklist": current_snapshot})
        proposed = _prepare(game, values, current)
        if proposed == current:
            return {"changed": False, "blacklist": current_snapshot,
                    "reconciled_profile": None}
        previous = effective_rules(game, current)
        progress(0, 2, "Saving conflict blacklist")
        try:
            latest = _snapshot(game, load_overrides(game))
            if latest["revision"] != revision:
                raise JobError("stale_blacklist",
                               "The blacklist changed; get it again.",
                               {"blacklist": latest})
            save_overrides(game, proposed)
        except JobError:
            raise
        except (OSError, ValueError) as exc:
            raise JobError("blacklist_save_failed",
                           "Could not save the conflict blacklist.",
                           {"reason": str(exc)}) from exc
        saved = _snapshot(game, proposed)
        progress(1, 2, "Refreshing the selected profile")
        generation = None
        if profile_dir is not None:
            active = getattr(game, "_active_profile_dir", None)
            errors: list[str] = []
            try:
                from Utils.filegraph.service import FileGraphService
                game.set_active_profile_dir(profile_dir)
                game.load_paths(read_only=True)
                library = FileGraphService.open_library(game, profile_dir,
                                                        log_fn=log)
                library.ensure_ready(profile_dir)
                session = library.open_profile(profile_dir)
                session.ensure_reconciled(operation_hint={
                    "kind": "blacklist", "mods": [],
                    "_blacklist_previous": [sorted(part) for part in previous],
                })
                generation = session.snapshot().generation
            except Exception as exc:
                errors.append(str(exc))
            finally:
                try:
                    game.set_active_profile_dir(active)
                    game.load_paths(read_only=True)
                except Exception as exc:
                    errors.append(f"restore active profile: {exc}")
            if errors:
                raise JobError(
                    "blacklist_sync_failed",
                    "The blacklist was saved, but the selected profile did not refresh.",
                    {"saved": True, "blacklist": saved,
                     "profile": profile_dir.name, "errors": errors},
                )
        progress(2, 2, "Conflict blacklist saved")
        log("Saved per-game conflict blacklist.")
        return {"changed": True, "blacklist": saved,
                "reconciled_profile": profile_dir.name if profile_dir else None,
                "filegraph_generation": generation}
