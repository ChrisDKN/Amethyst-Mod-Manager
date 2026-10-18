from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from Utils.mods.state import ModStateError, _reconcile_filegraph


def modlist_revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def separator_state_revision(settings: dict[str, Any]) -> str:
    raw = json.dumps(settings, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def separator_settings(profile_dir: Path, separator: str,
                       state: dict | None = None) -> dict[str, Any]:
    from Utils.profiles import state as profile_state

    display = separator.removesuffix("_separator")
    return {
        "collapsed": display in profile_state.read_collapsed_seps(profile_dir, state),
        "locked": bool(profile_state.read_separator_locks(
            profile_dir, state).get(display, False)),
        "color": profile_state.read_separator_colors(
            profile_dir, state).get(separator),
        "deploy_path": profile_state.read_separator_deploy_paths(
            profile_dir, state).get(separator),
    }


def set_separator_settings(
    game: Any,
    profile_dir: Path,
    separator: str,
    *,
    revision: str,
    state_revision: str,
    changes: dict[str, Any],
    log_fn: Callable[[str], None] | None = None,
    progress_fn: Callable[[int, int, str | None], None] | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.mods.modlist import modlist_lock, parse_modlist_text
    from Utils.operations import acquire_game_operation
    from Utils.profiles import state as profile_state

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    profile_dir = Path(profile_dir)
    modlist_path = profile_dir / "modlist.txt"
    with acquire_game_operation(
        game, "separators.settings.set",
        owner=operation_owner or "separators.settings.set",
    ):
        try:
            game.set_active_profile_dir(profile_dir)
            game.load_paths()
        except Exception as exc:
            raise ModStateError(
                "resource_unavailable",
                f"Could not load profile '{profile_dir.name}'.",
                {"profile": profile_dir.name, "reason": str(exc)},
            ) from exc

        progress(0, 2, "Updating separator settings")
        with modlist_lock(modlist_path):
            try:
                raw = modlist_path.read_bytes() if modlist_path.is_file() else b""
            except OSError as exc:
                raise ModStateError(
                    "resource_unavailable", "Could not read the mod list.",
                    {"path": str(modlist_path), "reason": str(exc)},
                ) from exc
            current_revision = modlist_revision(raw)
            if current_revision != revision:
                raise ModStateError(
                    "stale_modlist", "The mod list changed; list it again.",
                    {"revision": current_revision},
                )
            matches = [entry for entry in parse_modlist_text(raw.decode(
                "utf-8", errors="surrogateescape"))
                if entry.name == separator and entry.is_separator]
            if len(matches) != 1:
                raise ModStateError(
                    "separator_not_found" if not matches else "ambiguous_separator",
                    f"Separator '{separator}' is not unique in the mod list.",
                    {"separator": separator, "matches": len(matches)},
                )

            with profile_state._lock_for(profile_dir):
                state = profile_state.read_profile_state(profile_dir)
                current = separator_settings(profile_dir, separator, state)
                current_state_revision = separator_state_revision(current)
                if current_state_revision != state_revision:
                    raise ModStateError(
                        "stale_separator_state",
                        "The separator settings changed; list the mods again.",
                        {"state_revision": current_state_revision,
                         "separator": separator},
                    )
                updated = {**current, **changes}
                changed = updated != current
                if changed:
                    display = separator.removesuffix("_separator")
                    if "collapsed" in changes:
                        collapsed = profile_state.read_collapsed_seps(
                            profile_dir, state)
                        if updated["collapsed"]:
                            collapsed.add(display)
                        else:
                            collapsed.discard(display)
                        state["collapsed_seps"] = sorted(collapsed)
                    if "locked" in changes:
                        locks = dict(profile_state.read_separator_locks(
                            profile_dir, state))
                        if updated["locked"]:
                            locks[display] = True
                        else:
                            locks.pop(display, None)
                        state["separator_locks"] = locks
                    if "color" in changes:
                        colors = profile_state.read_separator_colors(
                            profile_dir, state)
                        if updated["color"] is not None:
                            colors[separator] = updated["color"]
                        else:
                            colors.pop(separator, None)
                        state["separator_colors"] = colors
                    if "deploy_path" in changes:
                        paths = profile_state.read_separator_deploy_paths(
                            profile_dir, state)
                        if updated["deploy_path"] is not None:
                            paths[separator] = updated["deploy_path"]
                        else:
                            paths.pop(separator, None)
                        state["separator_deploy_paths"] = paths
                    try:
                        profile_state.write_profile_state(profile_dir, state)
                    except OSError as exc:
                        raise ModStateError(
                            "resource_unavailable",
                            "Could not save separator settings.",
                            {"separator": separator, "reason": str(exc)},
                        ) from exc
                updated_state_revision = separator_state_revision(updated)

        deploy_changed = updated["deploy_path"] != current["deploy_path"]
        if deploy_changed:
            progress(1, 2, "Reconciling file graph")
            try:
                reconciled, generation = _reconcile_filegraph(
                    game, profile_dir, kind="full", mod_names=[],
                    log=log, progress=progress,
                )
            except Exception as exc:
                raise ModStateError(
                    "separator_sync_failed",
                    "The settings were saved, but Filegraph reconciliation failed.",
                    {"separator": separator, "state_revision": updated_state_revision,
                     "reason": str(exc)},
                ) from exc
        else:
            reconciled, generation = False, None
        progress(2, 2, "Separator settings updated")
        if changed:
            log(f"Updated settings for separator '{separator}'.")
        return {
            "ok": True,
            "separator": separator,
            "changed": changed,
            "revision": current_revision,
            "state_revision": updated_state_revision,
            "state": updated,
            "filegraph_reconciled": reconciled,
            "filegraph_generation": generation,
        }


def edit_separator(
    game: Any,
    profile_dir: Path,
    action: str,
    *,
    revision: str,
    separator: str | None = None,
    new_name: str | None = None,
    position: dict[str, str] | None = None,
    log_fn: Callable[[str], None] | None = None,
    progress_fn: Callable[[int, int, str | None], None] | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.atomic_write import write_atomic
    from Utils.mods.groups import normalize_groups
    from Utils.mods.modlist import (
        ModEntry, modlist_lock, parse_modlist_text, write_modlist,
    )
    from Utils.operations import acquire_game_operation
    from Utils.profiles.state import read_mod_groups, rekey_separator_state

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    if action not in {"create", "rename", "remove"}:
        raise ValueError(f"Unknown separator action: {action}")
    if action in {"create", "rename"} and new_name is None:
        raise ValueError("A separator display name is required.")
    if action in {"rename", "remove"} and separator is None:
        raise ValueError("An existing separator name is required.")
    profile_dir = Path(profile_dir)
    modlist_path = profile_dir / "modlist.txt"
    with acquire_game_operation(
        game, f"separators.{action}",
        owner=operation_owner or f"separators.{action}",
    ):
        try:
            game.set_active_profile_dir(profile_dir)
            game.load_paths()
        except Exception as exc:
            raise ModStateError(
                "resource_unavailable",
                f"Could not load profile '{profile_dir.name}'.",
                {"profile": profile_dir.name, "reason": str(exc)},
            ) from exc

        progress(0, 2, "Editing separators")
        with modlist_lock(modlist_path):
            try:
                existed = modlist_path.is_file()
                raw = modlist_path.read_bytes() if existed else b""
            except OSError as exc:
                raise ModStateError(
                    "resource_unavailable", "Could not read the mod list.",
                    {"path": str(modlist_path), "reason": str(exc)},
                ) from exc
            current_revision = modlist_revision(raw)
            if current_revision != revision:
                raise ModStateError(
                    "stale_modlist", "The mod list changed; list it again.",
                    {"revision": current_revision},
                )
            entries = parse_modlist_text(raw.decode(
                "utf-8", errors="surrogateescape"))
            original_entries = list(entries)
            names = [entry.name for entry in entries]
            updated_name = separator

            if action == "create":
                updated_name = new_name + "_separator"
                _require_unused(entries, updated_name, new_name)
                insert_at = 0
                if position:
                    placement, anchor = next(iter(position.items()))
                    matches = [i for i, entry in enumerate(entries)
                               if entry.name == anchor]
                    if len(matches) != 1:
                        raise ModStateError(
                            "separator_anchor_not_found" if not matches
                            else "ambiguous_separator_anchor",
                            f"Separator anchor '{anchor}' is not unique in the mod list.",
                            {"anchor": anchor, "matches": len(matches)},
                        )
                    insert_at = matches[0] + (placement == "after")
                entries.insert(insert_at, ModEntry(updated_name, True, False, True))
                groups = normalize_groups(
                    read_mod_groups(profile_dir), original_entries,
                )
                if normalize_groups(groups, entries) != groups:
                    raise ModStateError(
                        "separator_splits_mod_group",
                        "The separator would split a mod group; choose an anchor outside it.",
                        {"position": position},
                    )
            else:
                matches = [i for i, entry in enumerate(entries)
                           if entry.name == separator]
                if len(matches) != 1:
                    raise ModStateError(
                        "separator_not_found" if not matches
                        else "ambiguous_separator",
                        f"Separator '{separator}' is not unique in the mod list.",
                        {"separator": separator, "matches": len(matches)},
                    )
                index = matches[0]
                if not entries[index].is_separator:
                    raise ModStateError(
                        "not_a_separator", f"'{separator}' is a mod, not a separator.",
                        {"separator": separator},
                    )
                if action == "rename":
                    updated_name = new_name + "_separator"
                    if updated_name != separator:
                        _require_unused(entries, updated_name, new_name,
                                        exclude=separator)
                        entries[index].name = updated_name
                elif action == "remove":
                    entries.pop(index)

            changed = names != [entry.name for entry in entries]
            if changed:
                try:
                    write_modlist(modlist_path, entries)
                    if action == "create":
                        rekey_separator_state(profile_dir, updated_name)
                    elif action == "rename":
                        rekey_separator_state(profile_dir, separator, updated_name)
                    else:
                        rekey_separator_state(profile_dir, separator)
                except Exception as exc:
                    try:
                        if existed:
                            write_atomic(modlist_path, raw)
                        else:
                            modlist_path.unlink(missing_ok=True)
                    except OSError:
                        raise ModStateError(
                            "separator_state_sync_failed",
                            "Separator state could not be saved and the mod list could not be restored.",
                            {"reason": str(exc), "modlist_changed": True},
                        ) from exc
                    raise ModStateError(
                        "separator_state_sync_failed",
                        "Separator state could not be saved; the mod list was restored.",
                        {"reason": str(exc), "modlist_changed": False},
                    ) from exc
            resulting_revision = modlist_revision(
                modlist_path.read_bytes() if modlist_path.is_file() else b"")

        if not changed:
            progress(2, 2, "Separator already matched")
            return {
                "ok": True, "action": action, "separator": updated_name,
                "changed": False, "revision": resulting_revision,
                "filegraph_reconciled": False,
                "filegraph_generation": None,
            }

        progress(1, 2, "Reconciling file graph")
        try:
            reconciled, generation = _reconcile_filegraph(
                game, profile_dir, kind="full", mod_names=[],
                log=log, progress=progress,
            )
        except Exception as exc:
            raise ModStateError(
                "separator_sync_failed",
                "The separator edit was saved, but Filegraph reconciliation failed.",
                {"modlist_changed": changed, "revision": resulting_revision,
                 "reason": str(exc)},
            ) from exc
        progress(2, 2, "Separator edit complete")
        log(f"Separator {action}: {updated_name}.")
        return {
            "ok": True,
            "action": action,
            "separator": updated_name,
            "changed": changed,
            "revision": resulting_revision,
            "filegraph_reconciled": reconciled,
            "filegraph_generation": generation,
        }


def _require_unused(entries: list[Any], internal: str, display: str,
                    *, exclude: str | None = None) -> None:
    collisions = [entry.name for entry in entries
                  if entry.name != exclude and (
                      entry.name.casefold() == internal.casefold()
                      or entry.display_name.casefold() == display.casefold())]
    if collisions:
        raise ModStateError(
            "separator_name_exists",
            f"A mod or separator named '{display}' already exists.",
            {"name": display, "collisions": collisions},
        )
