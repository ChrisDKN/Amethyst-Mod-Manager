from __future__ import annotations

import copy
from pathlib import Path

from .jobs import JobError


def _profile_game(game, profile: Path, *, read_only: bool = False):
    selected = copy.copy(game)
    selected.set_active_profile_dir(profile)
    selected.load_paths(read_only=read_only)
    return selected


def _summary(option):
    result = {key: option[key] for key in (
        "key", "label", "kind", "value", "needs_reload")}
    if option["kind"] == "choice":
        result["choices"] = [
            {"value": value, "label": label}
            for value, label in option["choices"]]
    return result


def list_options(game, profile: Path):
    from Utils.games.quick_configure import build_quick_configure_options

    selected = _profile_game(game, profile, read_only=True)
    return [_summary(option) for option in build_quick_configure_options(selected)]


def _set_option(game, profile: Path, key: str, value, expected_value, log):
    from Utils.deployment.locking import coordinated_game_mutation
    from Utils.games.quick_configure import (
        build_quick_configure_options, deploy_mode_change_blocked)

    with coordinated_game_mutation(game, "headless.game_option"):
        if not profile.is_dir() or profile.is_symlink():
            raise JobError("profile_not_found", "The selected profile is unavailable.",
                           {"profile": profile.name})
        selected = _profile_game(game, profile)
        options = build_quick_configure_options(selected)
        option = next((item for item in options if item["key"] == key), None)
        if option is None:
            raise JobError("game_option_unavailable",
                           "This option is unavailable for the selected game.",
                           {"option": key})
        current = option["value"]
        if type(current) is not type(expected_value) or current != expected_value:
            raise JobError("game_option_changed",
                           "The game option changed since it was read.",
                           {"option": key, "current_value": current})
        if option["kind"] == "toggle":
            valid = isinstance(value, bool)
        else:
            valid = any(type(value) is type(choice) and value == choice
                        for choice, _label in option["choices"])
        if not valid:
            raise JobError("game_option_invalid_value",
                           "The value is not available for this game option.",
                           {"option": key, "choices": _summary(option).get("choices")})
        if value == current:
            return {"profile": profile.name, "option": _summary(option),
                    "changed": False}
        if key == "deploy_mode" and deploy_mode_change_blocked(selected, value):
            raise JobError("game_option_deployed",
                           "Restore the game before changing its deploy method.",
                           {"option": key})
        if key == "deploy_mode" and value == "hardlink":
            from Utils.hardlink_check import hardlink_device_mismatches

            mismatched = hardlink_device_mismatches(selected, log_fn=log)
            if mismatched:
                raise JobError("game_option_filesystem_mismatch",
                               "Hard links cannot reach every deploy target.",
                               {"targets": mismatched})
        if key == "prefer_appimage" and selected.get_deploy_active():
            raise JobError("game_option_deployed",
                           "Restore the game before changing the preferred OpenMW package.",
                           {"option": key})
        option["apply"](value)
        if option["needs_reload"]:
            selected.load_paths()
        active = getattr(game, "_active_profile_dir", None)
        if active is not None and Path(active).resolve() == profile.resolve():
            game.load_paths()
        updated = next((item for item in build_quick_configure_options(selected)
                        if item["key"] == key), None)
        if updated is None:
            raise JobError("game_option_unavailable",
                           "The changed option is no longer available.",
                           {"option": key})
        log(f"Saved {key} for {profile.name}.")
        return {"profile": profile.name, "option": _summary(updated),
                "changed": True}


def set_option(game, profile: Path, key: str, value, expected_value, log):
    from Utils.operations import OperationBusy

    try:
        return _set_option(game, profile, key, value, expected_value, log)
    except (JobError, OperationBusy):
        raise
    except Exception as exc:
        raise JobError("game_option_failed",
                       str(exc) or type(exc).__name__,
                       {"option": key, "exception": type(exc).__name__}) from exc
