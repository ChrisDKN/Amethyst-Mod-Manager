from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any, Callable


LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, str | None], None]


class ProfileError(RuntimeError):
    def __init__(self, name: str, message: str,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


def _profiles_dir(game: Any) -> Path:
    return Path(game.get_profile_root()) / "profiles"


def _profile_dirs(game: Any) -> list[Path]:
    try:
        root = _profiles_dir(game)
        if not root.is_dir():
            return []
        return [path for path in root.iterdir() if path.is_dir()]
    except OSError as exc:
        raise ProfileError(
            "resource_unavailable",
            "Could not read the game's profiles directory.",
            {"reason": str(exc)},
        ) from exc


def _validate_name(name: str) -> None:
    if (not isinstance(name, str) or not name.strip()
            or name != name.strip()
            or name in (".", "..")
            or Path(name).name != name
            or "\\" in name
            or any(char in name for char in "\x00\r\n")):
        raise ProfileError(
            "invalid_profile_name",
            "The profile name is not a safe folder name.",
            {"profile": name},
        )


def _require_profile(game: Any, name: str) -> Path:
    _validate_name(name)
    matches = [path for path in _profile_dirs(game) if path.name == name]
    if not matches:
        raise ProfileError(
            "profile_not_found",
            f"Profile '{name}' was not found.",
            {"profile": name},
        )
    profile_dir = matches[0]
    if profile_dir.is_symlink():
        raise ProfileError(
            "unsafe_profile_path",
            f"Profile '{name}' is a symbolic link and cannot be modified.",
            {"profile": name, "path": str(profile_dir)},
        )
    return profile_dir


def _ensure_new_name(game: Any, name: str, *, old_name: str | None = None) -> None:
    _validate_name(name)
    if name.casefold() == "default":
        raise ProfileError(
            "reserved_profile_name",
            "The profile name 'default' is reserved.",
            {"profile": name},
        )
    for path in _profile_dirs(game):
        if old_name is not None and path.name == old_name:
            continue
        if path.name.casefold() == name.casefold():
            raise ProfileError(
                "profile_exists",
                f"Profile '{path.name}' already exists.",
                {"profile": name, "existing": path.name},
            )


def _is_original_default(profile_dir: Path) -> bool:
    from Utils.profiles.state import read_profile_settings

    settings = read_profile_settings(profile_dir, None)
    return bool(profile_dir.name == "default"
                or settings.get("original_default", False))


def _is_locked(profile_dir: Path) -> bool:
    from Utils.profiles.state import read_profile_settings

    return bool(read_profile_settings(profile_dir, None).get(
        "profile_locked", False))


def _deployed_use(game: Any, profile_dir: Path) -> str | None:
    try:
        if not game.get_deploy_active():
            return None
        deployed = str(game.get_last_deployed_profile() or "default")
    except Exception as exc:
        raise ProfileError(
            "resource_unavailable",
            "Could not read the game's deployment state.",
            {"profile": profile_dir.name, "reason": str(exc)},
        ) from exc
    if deployed == profile_dir.name:
        return deployed
    try:
        from Utils.profiles.groups import is_group, member_of_groups
        if not is_group(profile_dir) and deployed in member_of_groups(
                game, profile_dir.name):
            return deployed
    except Exception as exc:
        raise ProfileError(
            "resource_unavailable",
            "Could not verify the profile's deployed group membership.",
            {
                "profile": profile_dir.name,
                "deployed_profile": deployed,
                "reason": str(exc),
            },
        ) from exc
    return None


def _reject_deployed_use(game: Any, profile_dir: Path) -> None:
    deployed = _deployed_use(game, profile_dir)
    if deployed is None:
        return
    if deployed == profile_dir.name:
        message = (
            f"Profile '{profile_dir.name}' is currently deployed; restore "
            "the game before changing or removing it."
        )
    else:
        message = (
            f"Profile '{profile_dir.name}' belongs to deployed group "
            f"'{deployed}'; restore the game before changing or removing it."
        )
    raise ProfileError(
        "profile_deployed",
        message,
        {"profile": profile_dir.name, "deployed_profile": deployed},
    )


def _generated_copy_entries(source: Path, directory: str,
                            names: list[str]) -> set[str]:
    if Path(directory) != source:
        return set()
    exact = {
        "backups",
        "bsa_index.bin",
        "deploy_snapshot.txt",
        "filemap.txt",
        "filemap_root.txt",
        "modindex.bin",
    }
    return {
        name for name in names
        if name in exact
        or name.startswith("filegraph.")
        or name.startswith(".filegraph.")
    }


def _copy_profile(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        copy_function=shutil.copy2,
        symlinks=True,
        ignore=lambda directory, names: _generated_copy_entries(
            source, directory, names),
    )


def create_profile(
    game: Any,
    name: str,
    *,
    profile_specific_mods: bool = False,
    copy_from: str | None = None,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
    mutation_coordinated: bool = False,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    lease = None
    if not mutation_coordinated:
        lease = acquire_game_operation(
            game, "profiles.create", owner=operation_owner or "profiles.create")
        lease.acquire()
    try:
        progress(0, 3, "Validating profile")
        _ensure_new_name(game, name)
        profiles_dir = _profiles_dir(game)
        profiles_dir.mkdir(parents=True, exist_ok=True)
        target = profiles_dir / name
        if target.exists() or target.is_symlink():
            raise ProfileError(
                "profile_exists",
                f"Profile '{name}' already exists.",
                {"profile": name},
            )

        source = None
        copied_from = None
        if copy_from is not None:
            source = _require_profile(game, copy_from)
            from Utils.profiles.groups import is_group
            if is_group(source):
                raise ProfileError(
                    "profile_copy_unsupported",
                    "Profile groups cannot be copied as ordinary profiles.",
                    {"profile": copy_from, "reason": "profile_group"},
                )
            from Utils.profiles.state import profile_uses_specific_mods
            profile_specific_mods = profile_uses_specific_mods(source)
            copied_from = source.name
        elif not profile_specific_mods:
            default_dir = profiles_dir / "default"
            if default_dir.is_dir() and not default_dir.is_symlink():
                source = default_dir
                copied_from = default_dir.name

        temp = profiles_dir / f".amethyst-profile-{uuid.uuid4().hex}"
        try:
            progress(1, 3, "Copying profile" if source else "Creating profile")
            if source is not None:
                _copy_profile(source, temp)
            else:
                temp.mkdir()
                (temp / "modlist.txt").touch()
                (temp / "plugins.txt").touch()
                (temp / "loadorder.txt").touch()
                if profile_specific_mods:
                    (temp / "mods").mkdir()
                    (temp / "overwrite").mkdir()
                    (temp / "Root_Folder").mkdir()

            from Utils.profiles.state import (
                read_profile_settings,
                write_profile_settings,
            )
            settings = read_profile_settings(temp, None)
            for key in ("original_default", "profile_locked"):
                settings.pop(key, None)
            if profile_specific_mods:
                settings["profile_specific_mods"] = True
                (temp / "mods").mkdir(exist_ok=True)
                (temp / "overwrite").mkdir(exist_ok=True)
                (temp / "Root_Folder").mkdir(exist_ok=True)
            else:
                settings.pop("profile_specific_mods", None)
            write_profile_settings(temp, settings)

            from Utils.games.registry import _ensure_profile_primary_plugin_order
            _ensure_profile_primary_plugin_order(game, temp)
            progress(2, 3, "Publishing profile")
            temp.rename(target)
        except Exception:
            if temp.is_dir() and not temp.is_symlink():
                shutil.rmtree(temp, ignore_errors=True)
            raise

        progress(3, 3, "Profile created")
        log(f"Profile '{name}' created.")
        return {
            "ok": True,
            "profile": name,
            "profile_specific_mods": profile_specific_mods,
            "copied_from": copied_from,
        }
    except ProfileError:
        raise
    except Exception as exc:
        raise ProfileError(
            "profile_create_failed",
            f"Could not create profile '{name}'.",
            {"profile": name, "reason": str(exc)},
        ) from exc
    finally:
        if lease is not None:
            lease.release()


def create_profile_group(
    game: Any,
    name: str,
    members: list[str],
    *,
    ini_source: str | None = None,
    overwrite_excluded: list[str] | None = None,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    lease = acquire_game_operation(
        game, "profiles.groups.create",
        owner=operation_owner or "profiles.groups.create")
    lease.acquire()
    try:
        progress(0, 2, "Validating profile group")
        _validate_name(name)
        _ensure_new_name(game, name)
        target = _profiles_dir(game) / name
        if target.exists() or target.is_symlink():
            raise ProfileError(
                "profile_exists", f"Profile '{name}' already exists.",
                {"profile": name},
            )
        from Utils.profiles.groups import GroupValidationError, create_group
        progress(1, 2, "Creating profile group")
        try:
            create_group(
                game, name, members, ini_source=ini_source,
                overwrite_excluded=overwrite_excluded, log_fn=log,
            )
        except GroupValidationError as exc:
            raise ProfileError(
                "profile_group_invalid", str(exc), {"profile": name},
            ) from exc
        progress(2, 2, "Profile group created")
        log(f"Profile group '{name}' created.")
        return {"ok": True, "profile": name, "members": members,
                "ini_source": ini_source,
                "overwrite_excluded": overwrite_excluded or []}
    except ProfileError:
        raise
    except Exception as exc:
        raise ProfileError(
            "profile_group_create_failed",
            f"Could not create profile group '{name}'.",
            {"profile": name, "reason": str(exc)},
        ) from exc
    finally:
        lease.release()


def set_profile_group_members(
    game: Any,
    profile: str,
    members: list[str],
    *,
    expected_members: list[str],
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation
    from Utils.profiles.groups import (
        GroupValidationError, _validate_member, get_members,
        group_build_lock, is_group, materialize_group, set_members,
    )

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    lease = acquire_game_operation(
        game, "profiles.groups.set_members",
        owner=operation_owner or "profiles.groups.set_members")
    lease.acquire()
    try:
        progress(0, 2, "Validating profile group")
        group_dir = _require_profile(game, profile)
        if not is_group(group_dir):
            raise ProfileError(
                "profile_not_group", f"Profile '{profile}' is not a group.",
                {"profile": profile},
            )
        _reject_deployed_use(game, group_dir)
        try:
            for member in members:
                _validate_member(game, group_dir.parent, member)
        except GroupValidationError as exc:
            raise ProfileError(
                "profile_group_invalid", str(exc), {"profile": profile},
            ) from exc
        if len(members) != len(set(members)):
            raise ProfileError(
                "profile_group_invalid", "Group members must be distinct.",
                {"profile": profile},
            )

        with group_build_lock(group_dir):
            previous = get_members(group_dir)
            if previous != expected_members:
                raise ProfileError(
                    "profile_group_changed",
                    f"Profile group '{profile}' changed since it was read.",
                    {"profile": profile, "current_members": previous},
                )
            if previous == members:
                progress(2, 2, "Profile group unchanged")
                return {"ok": True, "changed": False, "profile": profile,
                        "members": members}
            progress(1, 2, "Updating profile group")
            try:
                set_members(group_dir, members)
                materialize_group(game, group_dir, log_fn=log)
            except Exception as exc:
                membership_restored = False
                try:
                    set_members(group_dir, previous)
                    materialize_group(game, group_dir, log_fn=log)
                    membership_restored = True
                except Exception as rollback_exc:
                    log(f"Profile group rollback warning: {rollback_exc}")
                raise ProfileError(
                    "profile_group_update_failed",
                    f"Could not update profile group '{profile}'.",
                    {"profile": profile, "reason": str(exc),
                     "membership_restored": membership_restored},
                ) from exc
        progress(2, 2, "Profile group updated")
        log(f"Profile group '{profile}' members updated.")
        return {"ok": True, "changed": True, "profile": profile,
                "members": members, "previous_members": previous}
    finally:
        lease.release()


def convert_profile(
    game: Any,
    profile: str,
    *,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.deployment.locking import DeploymentBusy, game_mutation_lock
    from Utils.operations import acquire_game_operation
    from Utils.profiles.convert import ConvertError, convert_profile_to_specific

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    lease = acquire_game_operation(
        game, "profiles.convert", owner=operation_owner or "profiles.convert")
    lease.acquire()
    try:
        try:
            with game_mutation_lock(game):
                progress(0, 1, "Validating profile")
                profile_dir = _require_profile(game, profile)
                if _is_locked(profile_dir):
                    raise ProfileError(
                        "profile_locked",
                        f"Profile '{profile}' is locked and cannot be converted.",
                        {"profile": profile},
                    )
                _reject_deployed_use(game, profile_dir)
                try:
                    cloned = convert_profile_to_specific(
                        game, profile_dir, log_fn=log,
                        progress_fn=lambda done, total: progress(
                            done, total, "Cloning listed mods"),
                    )
                except ConvertError as exc:
                    raise ProfileError(
                        "profile_convert_invalid", str(exc),
                        {"profile": profile},
                    ) from exc
                active_dir = getattr(game, "_active_profile_dir", None)
                if active_dir is not None and Path(active_dir) == profile_dir:
                    try:
                        game.load_paths()
                    except Exception as exc:
                        log(f"Profile conversion state warning: {exc}")
                progress(1, 1, "Profile converted")
                return {"ok": True, "profile": profile,
                        "profile_specific_mods": True,
                        "cloned_count": len(cloned)}
        except DeploymentBusy as exc:
            raise ProfileError(
                "operation_busy", str(exc), {"profile": profile},
            ) from exc
    except ProfileError:
        raise
    except Exception as exc:
        raise ProfileError(
            "profile_convert_failed", f"Could not convert profile '{profile}'.",
            {"profile": profile, "reason": str(exc)},
        ) from exc
    finally:
        lease.release()


def rename_profile(
    game: Any,
    profile: str,
    new_name: str,
    *,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    lease = acquire_game_operation(
        game, "profiles.rename", owner=operation_owner or "profiles.rename")
    lease.acquire()
    try:
        progress(0, 3, "Validating profile")
        profile_dir = _require_profile(game, profile)
        if new_name == profile:
            progress(3, 3, "Profile unchanged")
            return {
                "ok": True,
                "changed": False,
                "old_name": profile,
                "new_name": new_name,
                "groups_updated": [],
            }
        _ensure_new_name(game, new_name, old_name=profile)
        _reject_deployed_use(game, profile_dir)
        new_dir = profile_dir.parent / new_name
        was_original = _is_original_default(profile_dir)

        if was_original:
            try:
                from Utils.profiles.state import merge_profile_settings
                merge_profile_settings(profile_dir, {"original_default": True})
            except Exception as exc:
                raise ProfileError(
                    "profile_rename_failed",
                    f"Could not preserve protection for profile '{profile}'.",
                    {"profile": profile, "reason": str(exc)},
                ) from exc

        progress(1, 3, "Renaming profile")
        try:
            profile_dir.rename(new_dir)
        except OSError as exc:
            raise ProfileError(
                "profile_rename_failed",
                f"Could not rename profile '{profile}' to '{new_name}'.",
                {
                    "profile": profile,
                    "new_name": new_name,
                    "reason": str(exc),
                },
            ) from exc

        progress(2, 3, "Updating profile groups")
        try:
            from Utils.profiles.groups import rename_profile_everywhere
            groups = rename_profile_everywhere(
                game, profile, new_name, log_fn=log)
        except Exception as exc:
            rollback_succeeded = False
            try:
                new_dir.rename(profile_dir)
                rollback_succeeded = True
            except OSError:
                pass
            if rollback_succeeded:
                try:
                    rename_profile_everywhere(
                        game, new_name, profile, log_fn=log)
                except Exception:
                    rollback_succeeded = False
            raise ProfileError(
                "profile_rename_failed",
                "The profile folder was renamed, but profile-group updates "
                "failed.",
                {
                    "profile": profile,
                    "new_name": new_name,
                    "reason": str(exc),
                    "rollback_succeeded": rollback_succeeded,
                },
            ) from exc

        try:
            if str(game.get_last_active_profile()) == profile:
                game.save_last_active_profile(new_name)
            active_dir = getattr(game, "_active_profile_dir", None)
            if active_dir is not None and Path(active_dir).name == profile:
                game.set_active_profile_dir(new_dir)
                game.load_paths()
        except Exception as exc:
            log(f"Profile rename state warning: {exc}")

        progress(3, 3, "Profile renamed")
        log(f"Profile '{profile}' renamed to '{new_name}'.")
        return {
            "ok": True,
            "changed": True,
            "old_name": profile,
            "new_name": new_name,
            "groups_updated": groups,
        }
    finally:
        lease.release()


def remove_profile(
    game: Any,
    profile: str,
    *,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    lease = acquire_game_operation(
        game, "profiles.remove", owner=operation_owner or "profiles.remove")
    lease.acquire()
    try:
        progress(0, 3, "Validating profile")
        profile_dir = _require_profile(game, profile)
        if _is_original_default(profile_dir):
            raise ProfileError(
                "profile_protected",
                f"Profile '{profile}' is the original default and cannot be "
                "removed.",
                {"profile": profile},
            )
        if _is_locked(profile_dir):
            raise ProfileError(
                "profile_locked",
                f"Profile '{profile}' is locked and cannot be removed.",
                {"profile": profile},
            )
        _reject_deployed_use(game, profile_dir)

        progress(1, 3, "Updating profile groups")
        try:
            from Utils.profiles.groups import remove_profile_everywhere
            groups = remove_profile_everywhere(game, profile, log_fn=log)
        except Exception as exc:
            raise ProfileError(
                "profile_remove_failed",
                "Could not remove the profile from its profile groups.",
                {"profile": profile, "reason": str(exc)},
            ) from exc

        progress(2, 3, "Removing profile")
        try:
            shutil.rmtree(profile_dir)
        except OSError as exc:
            raise ProfileError(
                "profile_remove_failed",
                f"Could not remove profile '{profile}'.",
                {
                    "profile": profile,
                    "groups_updated": groups,
                    "reason": str(exc),
                },
            ) from exc

        remaining = [path.name for path in _profile_dirs(game)]
        fallback = "default" if "default" in remaining else (
            sorted(remaining, key=lambda value: (value.casefold(), value))[0]
            if remaining else None)
        try:
            if str(game.get_last_active_profile()) == profile and fallback:
                game.save_last_active_profile(fallback)
            active_dir = getattr(game, "_active_profile_dir", None)
            if active_dir is not None and Path(active_dir).name == profile:
                game.set_active_profile_dir(
                    _profiles_dir(game) / fallback if fallback else None)
                game.load_paths()
        except Exception as exc:
            log(f"Profile removal state warning: {exc}")

        try:
            from Utils.wabbajack.profiles import cleanup_unreferenced
            cleanup_unreferenced(Path(game.get_profile_root()))
        except Exception as exc:
            log(f"Wabbajack storage cleanup warning: {exc}")

        progress(3, 3, "Profile removed")
        log(f"Profile '{profile}' removed.")
        return {
            "ok": True,
            "removed": profile,
            "groups_updated": groups,
            "fallback_profile": fallback,
        }
    finally:
        lease.release()


def select_profile(
    game: Any,
    profile: str,
    *,
    log_fn: LogFn | None = None,
    progress_fn: ProgressFn | None = None,
    operation_owner: str | None = None,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    log = log_fn or (lambda _message: None)
    progress = progress_fn or (lambda _done, _total, _phase=None: None)
    lease = acquire_game_operation(
        game, "profiles.select", owner=operation_owner or "profiles.select")
    lease.acquire()
    try:
        progress(0, 2, "Loading profile")
        profile_dir = _require_profile(game, profile)
        previous = str(game.get_last_active_profile() or "default")
        previous_dir = _profiles_dir(game) / previous
        try:
            game.set_active_profile_dir(profile_dir)
            game.load_paths()
            from Utils.profiles.groups import is_group, materialize_group
            grouped = is_group(profile_dir)
            if grouped:
                materialize_group(game, profile_dir, log_fn=log)
            game.save_last_active_profile(profile)
            if str(game.get_last_active_profile() or "default") != profile:
                raise OSError("The last-active profile setting was not saved.")
        except Exception as exc:
            try:
                game.set_active_profile_dir(
                    previous_dir if previous_dir.is_dir() else None)
                game.load_paths()
            except Exception:
                pass
            raise ProfileError(
                "profile_select_failed",
                f"Could not select profile '{profile}'.",
                {"profile": profile, "reason": str(exc)},
            ) from exc

        progress(2, 2, "Profile selected")
        log(f"Profile '{profile}' selected.")
        return {
            "ok": True,
            "changed": previous != profile,
            "profile": profile,
            "previous_profile": previous,
            "profile_group": grouped,
        }
    finally:
        lease.release()
