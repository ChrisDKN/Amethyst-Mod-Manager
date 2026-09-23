from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from .jobs import JobError


def _stamp(path: Path) -> list[int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise JobError("resource_unavailable", "Could not inspect a mod folder.",
                       {"path": str(path), "reason": str(exc)}) from exc
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns]


def _bytes(path: Path) -> bytes:
    try:
        return path.read_bytes() if path.is_file() else b""
    except OSError as exc:
        raise JobError("resource_unavailable", "Could not read profile state.",
                       {"path": str(path), "reason": str(exc)}) from exc


def _source_folder(profile: Path, staging: Path, name: str, grouped: bool) -> Path:
    from Utils.profiles.groups import owner_of

    folder = staging / name
    if not folder.is_dir():
        raise JobError("mod_files_missing", "The selected mod folder is missing.",
                       {"mod": name, "path": str(folder)})
    if folder.is_symlink():
        owner = owner_of(profile, name) if grouped else None
        member = profile.parent / owner[0] if owner is not None else None
        expected = (member / "mods" / owner[1]
                    if member is not None else None)
        if (expected is None or member.is_symlink()
                or expected.parent.is_symlink() or expected.is_symlink()
                or not expected.is_dir()
                or folder.resolve() != expected.resolve()):
            raise JobError("unsafe_mod_path", "The selected mod is an unsafe link.",
                           {"mod": name, "path": str(folder)})
    return folder


def _shared_users(target: Path, name: str) -> list[str]:
    from Utils.mods.modlist import read_modlist
    from Utils.profiles.groups import is_group
    from Utils.profiles.state import profile_uses_specific_mods

    if profile_uses_specific_mods(target):
        return []
    users = []
    for profile in (target.parent).iterdir():
        if (not profile.is_dir() or profile.is_symlink()
                or profile == target or is_group(profile)
                or profile_uses_specific_mods(profile)):
            continue
        if any(entry.name == name and not entry.is_separator
               for entry in read_modlist(profile / "modlist.txt")):
            users.append(profile.name)
    return sorted(users)


def _plan(
    game: Any, source: Path, target: Path, mods: list[str],
    separator: str | None, renames: dict[str, str], if_exists: str,
    *, move: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from Utils.mods.copy import resolve_target_staging
    from Utils.mods.groups import expand_leaders
    from Utils.mods.modlist import modlist_lock, parse_modlist_text
    from Utils.profiles.groups import is_group, member_of_groups
    from Utils.profiles.state import read_mod_groups

    if (not source.is_dir() or source.is_symlink()
            or not target.is_dir() or target.is_symlink()):
        raise JobError("profile_not_found", "A selected profile is unavailable.")
    if source.resolve() == target.resolve():
        raise JobError("invalid_target_profile",
                       "Source and target profiles must differ.")
    if is_group(target):
        raise JobError("profile_group_target",
                       "Copy into a group member instead of the group profile.",
                       {"target_profile": target.name})
    if move and is_group(source):
        raise JobError("profile_group_move_unsupported",
                       "Moving out of a Profile Group requires member-owner cleanup.",
                       {"source_profile": source.name})
    affected_groups = (sorted(set(member_of_groups(game, source.name)) |
                              set(member_of_groups(game, target.name)))
                       if move else [])
    if move:
        try:
            deployed_profile = (game.get_last_deployed_profile()
                                if game.get_deploy_active() else None)
        except Exception as exc:
            raise JobError("resource_unavailable",
                           "Could not check the deployed profile.",
                           {"reason": str(exc)}) from exc
        if deployed_profile is not None and not deployed_profile:
            raise JobError("resource_unavailable",
                           "Could not identify the deployed profile.")
        if deployed_profile in affected_groups:
            raise JobError("profile_group_deployed",
                           "Restore the deployed Profile Group before moving member mods.",
                           {"group": deployed_profile})
        if deployed_profile is not None and deployed_profile != source.name:
            raise JobError("other_profile_deployed",
                           "Restore the other deployed profile before moving mods.",
                           {"deployed_profile": deployed_profile})

    source_modlist = source / "modlist.txt"
    target_modlist = target / "modlist.txt"
    for profile in (source, target):
        for path in (profile / "modlist.txt", profile / "profile_state.json",
                     profile / "fomod", profile / "bain"):
            if path.is_symlink():
                raise JobError("unsafe_mod_path",
                               "A profile file or installer-choice directory is a link.",
                               {"path": str(path)})
    with modlist_lock(source_modlist):
        source_raw = _bytes(source_modlist)
        source_entries = parse_modlist_text(source_raw.decode(
            "utf-8", errors="surrogateescape"))
    with modlist_lock(target_modlist):
        target_raw = _bytes(target_modlist)
        target_entries = parse_modlist_text(target_raw.decode(
            "utf-8", errors="surrogateescape"))
    source_groups = read_mod_groups(source)
    selected = expand_leaders(mods, source_groups)
    available = {entry.name: entry for entry in source_entries
                 if not entry.is_separator}
    missing = sorted(selected - available.keys())
    if missing:
        raise JobError("mod_not_found", "Selected mods are missing from the source.",
                       {"mods": missing})
    target_names = {renames.get(name, name) for name in selected}
    for entries, profile, relevant in ((source_entries, source, selected),
                                       (target_entries, target, target_names)):
        names = [entry.name for entry in entries
                 if not entry.is_separator and entry.name in relevant]
        if len(names) != len(set(names)):
            raise JobError("invalid_modlist",
                           "A selected mod name occurs more than once in a profile.",
                           {"profile": profile.name})
    if set(renames) - selected:
        raise JobError("invalid_mod_copy",
                       "A rename refers to an unselected mod.",
                       {"mods": sorted(set(renames) - selected)})
    if separator is not None and not any(
            entry.is_separator and entry.name == separator
            for entry in source_entries):
        raise JobError("separator_not_found",
                       "The selected separator is missing from the source.",
                       {"separator": separator})
    if not selected and separator is None:
        raise JobError("invalid_mod_copy",
                       "Select at least one mod or a separator.")
    if move and not selected:
        raise JobError("invalid_mod_move", "Select at least one mod to move.")
    if move:
        locked = [entry.name for entry in source_entries
                  if not entry.is_separator and entry.name in selected
                  and entry.locked]
        if locked:
            raise JobError("mod_locked", "Locked source mods cannot be moved.",
                           {"mods": locked, "source_profile": source.name})

    source_staging = Path(resolve_target_staging(game, source))
    target_staging = Path(resolve_target_staging(game, target))
    target_by_name = {entry.name: entry for entry in target_entries
                      if not entry.is_separator}
    seen_targets: set[str] = set()
    public: list[dict[str, Any]] = []
    private: list[dict[str, Any]] = []
    fingerprints: list[dict[str, Any]] = []
    for entry in source_entries:
        if entry.is_separator or entry.name not in selected:
            continue
        name = entry.name
        out = renames.get(name, name)
        if out.endswith("_separator"):
            raise JobError("invalid_mod_copy",
                           "A target mod name cannot be a separator.",
                           {"target_mod": out})
        if out in seen_targets:
            raise JobError("mod_copy_target_conflict",
                           "Multiple selected mods have the same target name.",
                           {"target_mod": out})
        seen_targets.add(out)
        src = _source_folder(source, source_staging, name, is_group(source))
        dst = target_staging / out
        if dst.is_symlink():
            raise JobError("unsafe_mod_path", "A target mod is a symbolic link.",
                           {"mod": out, "path": str(dst)})
        if dst.exists() and not dst.is_dir():
            raise JobError("unsafe_mod_path", "A target mod is not a directory.",
                           {"mod": out, "path": str(dst)})
        source_choices = _choice_paths(source, name)
        target_choices = _choice_paths(target, out)
        if any(path.is_symlink() for path in (*source_choices, *target_choices)):
            raise JobError("unsafe_mod_path", "An installer-choice file is a link.",
                           {"mod": name})
        if any(path.exists() and not path.is_file()
               for path in (*source_choices, *target_choices)):
            raise JobError("unsafe_mod_path",
                           "An installer-choice path is not a regular file.",
                           {"mod": name})
        same_folder = (src.resolve() == dst.resolve()
                       if dst.exists() else False)
        source_users = _shared_users(source, name) if move else []
        choice_collisions = [str(path) for path in target_choices
                             if path.is_file()]
        collision = not same_folder and (
            dst.exists() or out in target_by_name or bool(choice_collisions))
        shared_users = _shared_users(target, out) if collision else []
        if collision and if_exists == "replace" and shared_users:
            raise JobError("shared_mod_in_use",
                           "Replacing this shared mod would affect other profiles.",
                           {"mod": out, "profiles": shared_users})
        if collision and if_exists == "replace" and (
                out in target_by_name and target_by_name[out].locked):
            raise JobError("mod_locked", "A locked target mod cannot be replaced.",
                           {"mod": out, "target_profile": target.name})
        src_stamp = _stamp(src)
        dst_stamp = _stamp(dst)
        public.append({
            "source_mod": name, "target_mod": out,
            "enabled": entry.enabled, "source_path": str(src),
            "target_path": str(dst), "same_folder": same_folder,
            "collision": collision, "shared_users": shared_users,
            "source_shared_users": source_users,
            "choice_collisions": choice_collisions,
            "action": ("skip" if collision and if_exists == "skip" else
                       "replace" if collision and if_exists == "replace" else
                       "share" if same_folder else "copy"),
        })
        private.append({"source": src, "target": dst, "name": name,
                        "out": out, "enabled": entry.enabled,
                        "same_folder": same_folder,
                        "source_stamp": src_stamp,
                        "target_stamp": dst_stamp,
                        "source_shared_users": source_users,
                        "collision": collision})
        fingerprints.append({"name": name, "out": out,
                             "source_stamp": src_stamp,
                             "source_target_stamp": _stamp(src.resolve()),
                             "source_shared_users": source_users,
                             "target_stamp": dst_stamp,
                             "source_choices": [_stamp(path) for path in source_choices],
                             "target_choices": [_stamp(path) for path in target_choices]})
    requested_groups = [leader for leader, data in source_groups.items()
                        if {leader, *data["members"]} <= selected]
    payload = {
        "source": str(source.resolve()), "target": str(target.resolve()),
        "source_modlist": hashlib.sha256(source_raw).hexdigest(),
        "target_modlist": hashlib.sha256(target_raw).hexdigest(),
        "source_state": hashlib.sha256(_bytes(source / "profile_state.json")).hexdigest(),
        "target_state": hashlib.sha256(_bytes(target / "profile_state.json")).hexdigest(),
        "separator": separator, "renames": renames, "if_exists": if_exists,
        "move": move, "affected_groups": affected_groups,
        "folders": fingerprints,
    }
    revision = hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()
    return ({"revision": revision, "source_profile": source.name,
             "target_profile": target.name, "separator": separator,
             "move": move,
             "affected_groups": affected_groups,
             "mods": public, "requested_groups": requested_groups,
             "collisions": [item["target_mod"] for item in public
                            if item["collision"]],
             "source_staging": str(source_staging),
             "target_staging": str(target_staging)}, private)


def plan_copy(
    game: Any, source: Path, target: Path, mods: list[str],
    separator: str | None, renames: dict[str, str], if_exists: str,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    with acquire_game_operation(game, "mods.copy.plan",
                                owner="headless:mods.copy.plan"):
        plan, _private = _plan(
            game, source, target, mods, separator, renames, if_exists)
        return plan


def plan_move(
    game: Any, source: Path, target: Path, mods: list[str],
    separator: str | None, renames: dict[str, str], if_exists: str,
) -> dict[str, Any]:
    from Utils.operations import acquire_game_operation

    with acquire_game_operation(game, "mods.move_profile.plan",
                                owner="headless:mods.move_profile.plan"):
        plan, _private = _plan(
            game, source, target, mods, separator, renames, if_exists,
            move=True)
        return plan


def _choice_paths(profile: Path, name: str) -> list[Path]:
    return [profile / sub / f"{name}.json" for sub in ("fomod", "bain")]


def _restore_choices(saved: list[tuple[Path, bytes | None]]) -> None:
    from Utils.atomic_write import write_atomic

    for path, data in saved:
        if data is None:
            path.unlink(missing_ok=True)
        else:
            write_atomic(path, data)


def _remove_source_item(game: Any, source: Path, staging: Path,
                        name: str, *, retain_folder: bool,
                        log: Callable[..., None]) -> None:
    from Utils.mods.groups import reconcile_profile_groups
    from Utils.mods.modlist import modlist_lock, read_modlist, write_modlist
    from Utils.mods.state import _drop_removed_mod_state

    modlist = source / "modlist.txt"
    with modlist_lock(modlist):
        entries = read_modlist(modlist)
        matches = [entry for entry in entries
                   if entry.name == name and not entry.is_separator]
        if len(matches) != 1 or matches[0].locked:
            raise JobError("mod_move_source_changed",
                           "The source mod list changed during the move.",
                           {"mod": name, "source_profile": source.name})
        game.set_active_profile_dir(source)
        game.load_paths()
        if retain_folder:
            from Utils.downloads.core import record_download_install
            from Utils.filegraph.service import FileGraphService
            from Utils.mods.remove import (
                _remove_plugins_for_mods, undeploy_catalog_mods,
            )

            library = FileGraphService.open_library(game, source, log_fn=log)
            profile = library.open_profile(source)
            record_download_install(source, staging / name, log_fn=log)
            try:
                deploy_active = bool(game.get_deploy_active())
            except Exception:
                deploy_active = True
            if deploy_active:
                undeploy_catalog_mods(
                    game, profile, staging, [name], log_fn=log, strict=True)
            _remove_plugins_for_mods(
                game, source, staging, [name], log, preserve_remaining=True)
            profile.forget_deployed_mods([name])
        else:
            from Utils.mods.remove import remove_mods
            remove_mods(game, source, [name], log_fn=log,
                        staging_root=staging, strict=True)
        _drop_removed_mod_state(source, [name])
        kept = [entry for entry in entries if entry.name != name]
        write_modlist(modlist, kept)
        reconcile_profile_groups(source, kept)
        for path in _choice_paths(source, name):
            path.unlink(missing_ok=True)


def copy_runner(
    game: Any, source: Path, target: Path, mods: list[str],
    separator: str | None, renames: dict[str, str], if_exists: str,
    revision: str, *, move: bool = False,
) -> Callable:
    def run(log: Callable[..., None], progress: Callable[..., None]) -> dict:
        from Utils.atomic_write import write_atomic
        from Utils.mods.copy import (
            copy_fomod_choice, copy_separator_state,
            register_mods_in_modlist, register_separator_block_in_modlist,
        )
        from Utils.mods.groups import copy_complete_groups
        from Utils.operations import acquire_game_operation
        from Utils.profiles.state import read_mod_groups
        from Utils.mods.state import _reconcile_filegraph

        operation = "mods.move_profile" if move else "mods.copy"
        with acquire_game_operation(game, operation,
                                    owner=f"headless:{operation}"):
            plan, items = _plan(
                game, source, target, mods, separator, renames, if_exists,
                move=move)
            if plan["revision"] != revision:
                raise JobError("mod_move_plan_changed" if move else
                               "mod_copy_plan_changed",
                               "The source or target changed; inspect the plan again.",
                               {"plan": plan})
            if plan["collisions"] and if_exists == "fail":
                raise JobError("mod_copy_target_exists",
                               "The target already contains selected mods.",
                               {"mods": plan["collisions"]})

            active = [item for item in items
                      if not (item["collision"] and if_exists == "skip")]
            source_modlist_before = _bytes(source / "modlist.txt")
            source_state_before = _bytes(source / "profile_state.json")
            target_modlist = target / "modlist.txt"
            previous_modlist = _bytes(target_modlist)
            modlist_existed = target_modlist.is_file()
            previous_state = _bytes(target / "profile_state.json")
            state_existed = (target / "profile_state.json").is_file()
            staged: list[tuple[dict[str, Any], Path]] = []
            committed: list[tuple[dict[str, Any], Path | None,
                                  list[tuple[Path, bytes | None]]]] = []
            total = max(1, len(active))
            try:
                for index, item in enumerate(active):
                    progress(index, total, f"Copying {item['name']}")
                    if item["same_folder"]:
                        continue
                    item["target"].parent.mkdir(parents=True, exist_ok=True)
                    temp_root = Path(tempfile.mkdtemp(
                        prefix=".amm-copy-", dir=item["target"].parent))
                    staged.append((item, temp_root))
                    shutil.copytree(item["source"], temp_root / "mod")
                    if not (temp_root / "mod").is_dir():
                        raise OSError("The staged mod copy is incomplete.")

                latest, _unused = _plan(
                    game, source, target, mods, separator, renames, if_exists,
                    move=move)
                if latest["revision"] != revision:
                    raise JobError("mod_move_plan_changed" if move else
                                   "mod_copy_plan_changed",
                                   "The source or target changed during copying.",
                                   {"plan": latest})

                for item, temp_root in staged:
                    dst = item["target"]
                    src = item["source"]
                    backup: Path | None = None
                    if (_stamp(item["source"]) != item["source_stamp"]
                            or _stamp(dst) != item["target_stamp"]):
                        raise JobError("mod_copy_plan_changed",
                                       "A mod folder changed during copying.",
                                       {"source": str(src), "target": str(dst)})
                    if dst.exists():
                        if if_exists != "replace" or dst.is_symlink():
                            raise JobError("mod_copy_plan_changed",
                                           "A target mod changed during copying.",
                                           {"path": str(dst)})
                        backup_root = Path(tempfile.mkdtemp(
                            prefix=".amm-copy-backup-", dir=dst.parent))
                        backup = backup_root / "mod"
                        dst.rename(backup)
                    try:
                        (temp_root / "mod").rename(dst)
                    except BaseException:
                        if backup is not None:
                            backup.rename(dst)
                            shutil.rmtree(backup.parent, ignore_errors=True)
                        raise
                    choice_paths = _choice_paths(target, item["out"])
                    saved = [(path, path.read_bytes() if path.is_file() else None)
                             for path in choice_paths]
                    committed.append((item, backup, saved))
                    copy_fomod_choice(source, target, item["name"], item["out"])
                    if if_exists == "replace":
                        for src_choice, dst_choice in zip(
                            _choice_paths(source, item["name"]), choice_paths,
                        ):
                            if not src_choice.is_file():
                                dst_choice.unlink(missing_ok=True)

                for item in active:
                    if not item["same_folder"]:
                        continue
                    missing_choices = [
                        (src_choice, dst_choice)
                        for src_choice, dst_choice in zip(
                            _choice_paths(source, item["name"]),
                            _choice_paths(target, item["out"]),
                        )
                        if src_choice.is_file() and not dst_choice.exists()
                    ]
                    if not missing_choices:
                        continue
                    committed.append((item, None, [(dst_choice, None)
                                                   for _, dst_choice in missing_choices]))
                    for src_choice, dst_choice in missing_choices:
                        dst_choice.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_choice, dst_choice)

                registered = [(item["out"], item["enabled"])
                              for item in active]
                if separator is not None:
                    created = register_separator_block_in_modlist(
                        target_modlist, separator, registered)
                    if created:
                        copy_separator_state(source, target, separator)
                elif registered:
                    register_mods_in_modlist(target_modlist, registered)

                name_map = {item["name"]: item["out"] for item in active}
                source_groups = read_mod_groups(source)
                preserved = (copy_complete_groups(
                    source, target, name_map, source_groups=source_groups)
                    if source_groups and name_map else set())
                if move and set(plan["requested_groups"]) - preserved:
                    raise JobError(
                        "mod_move_group_conflict",
                        "The target could not preserve every complete source group.",
                        {"groups": sorted(set(plan["requested_groups"]) - preserved)})
            except BaseException as exc:
                rollback_errors: list[str] = []
                try:
                    if modlist_existed:
                        write_atomic(target_modlist, previous_modlist)
                    else:
                        target_modlist.unlink(missing_ok=True)
                    if state_existed:
                        write_atomic(target / "profile_state.json", previous_state)
                    else:
                        (target / "profile_state.json").unlink(missing_ok=True)
                except OSError as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
                for item, backup, saved in reversed(committed):
                    try:
                        _restore_choices(saved)
                        if not item["same_folder"] and item["target"].exists():
                            shutil.rmtree(item["target"])
                        if backup is not None and backup.exists():
                            backup.rename(item["target"])
                            shutil.rmtree(backup.parent, ignore_errors=True)
                    except OSError as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
                if rollback_errors:
                    raise JobError("mod_copy_rollback_failed",
                                   "Could not restore the target after a failed copy.",
                                   {"reason": str(exc),
                                    "rollback_errors": rollback_errors}) from exc
                if isinstance(exc, JobError):
                    raise
                raise JobError("mod_copy_failed", "Could not copy selected mods.",
                               {"reason": str(exc)}) from exc
            finally:
                for _item, temp_root in staged:
                    shutil.rmtree(temp_root, ignore_errors=True)

            backups_retained: list[str] = []
            for _item, backup, _saved in committed:
                if backup is not None:
                    shutil.rmtree(backup.parent, ignore_errors=True)
                    if backup.parent.exists():
                        backups_retained.append(str(backup.parent))
            copied = [{"source_mod": item["name"], "target_mod": item["out"],
                       "same_folder": item["same_folder"]}
                      for item in active]
            progress(total, total, "Reconciling target profile")
            try:
                game.set_active_profile_dir(target)
                game.load_paths()
                if active:
                    from Utils.filegraph.service import FileGraphService
                    library = FileGraphService.open_library(
                        game, target, log_fn=log)
                    library.refresh(target, mod_names=[item["out"]
                                                       for item in active])
                reconciled, generation = _reconcile_filegraph(
                    game, target, kind="copy", mod_names=[item["out"]
                                                          for item in active],
                    log=log, progress=progress)
            except Exception as exc:
                raise JobError("mod_move_sync_failed" if move else
                               "mod_copy_sync_failed",
                               "Mods were copied but target indexing failed.",
                               {"copied": copied, "stage": "filegraph",
                                "reason": str(exc)}) from exc
            try:
                from Utils.mods.modlist import read_modlist
                from Utils.plugins.sync import sync_plugins_for_mods
                target_enabled = {
                    entry.name: entry.enabled
                    for entry in read_modlist(target_modlist)
                    if not entry.is_separator
                }
                enabled_additions = [
                    (item["out"], True) for item in active
                    if target_enabled[item["out"]]
                ]
                plugins_changed = (sync_plugins_for_mods(
                    game, target, Path(plan["target_staging"]),
                    enabled_additions, log_fn=log)
                    if enabled_additions else False)
            except Exception as exc:
                raise JobError("mod_move_sync_failed" if move else
                               "mod_copy_sync_failed",
                               "Mods were copied but plugin synchronization failed.",
                               {"copied": copied, "stage": "plugins",
                                "reason": str(exc)}) from exc
            moved: list[str] = []
            source_reconciled = False
            source_generation: int | None = None
            if move and active:
                progress(0, len(active), "Removing source profile entries")
                stage = "source_cleanup"
                try:
                    if (_bytes(source / "modlist.txt") != source_modlist_before
                            or _bytes(source / "profile_state.json")
                            != source_state_before):
                        raise JobError("mod_move_source_changed",
                                       "The source profile changed during copying.")
                    source_staging = Path(plan["source_staging"])
                    for index, item in enumerate(active):
                        progress(index, len(active), f"Moving {item['name']}")
                        if _stamp(item["source"]) != item["source_stamp"]:
                            raise JobError("mod_move_source_changed",
                                           "A source mod folder changed during copying.",
                                           {"mod": item["name"]})
                        retain_folder = (item["same_folder"] or bool(
                            _shared_users(source, item["name"])))
                        _remove_source_item(
                            game, source, source_staging, item["name"],
                            retain_folder=retain_folder, log=log)
                        moved.append(item["name"])
                    stage = "source_filegraph"
                    source_reconciled, source_generation = _reconcile_filegraph(
                        game, source, kind="move", mod_names=moved,
                        log=log, progress=progress)
                    if plan["affected_groups"]:
                        stage = "group_materialization"
                        from Utils.profiles.groups import materialize_group
                        for group_name in plan["affected_groups"]:
                            group = source.parent / group_name
                            game.set_active_profile_dir(group)
                            game.load_paths()
                            materialize_group(game, group, log_fn=log)
                    stage = "target_filegraph"
                    game.set_active_profile_dir(target)
                    game.load_paths()
                    reconciled, generation = _reconcile_filegraph(
                        game, target, kind="move", mod_names=[item["out"]
                                                              for item in active],
                        log=log, progress=progress)
                except Exception as exc:
                    from Utils.mods.modlist import read_modlist
                    try:
                        remaining = [entry.name for entry in read_modlist(
                            source / "modlist.txt") if not entry.is_separator
                            and entry.name in {item["name"] for item in active}]
                    except Exception:
                        remaining = [item["name"] for item in active
                                     if item["name"] not in moved]
                    raise JobError(
                        ("mod_move_source_cleanup_failed"
                         if stage == "source_cleanup" else "mod_move_sync_failed"),
                        "The target copy succeeded but the move did not finish.",
                        {"copied": copied, "moved": moved,
                         "source_remaining": remaining, "stage": stage,
                         "reason": str(exc)}) from exc
            verb = "Moved" if move else "Copied"
            log(f"{verb} {len(moved) if move else len(copied)} mod(s) to '{target.name}'.")
            return {"copied": copied, "count": len(copied),
                    "backups_retained": backups_retained,
                    "skipped": [item["name"] for item in items
                                if item not in active],
                    "groups_preserved": sorted(preserved),
                    "groups_not_preserved": sorted(
                        set(plan["requested_groups"]) - preserved),
                    "plugins_changed": bool(plugins_changed),
                    "filegraph_reconciled": reconciled,
                    "filegraph_generation": generation,
                    **({"moved": moved, "moved_count": len(moved),
                        "source_filegraph_reconciled": source_reconciled,
                        "source_filegraph_generation": source_generation}
                       if move else {})}
    return run
