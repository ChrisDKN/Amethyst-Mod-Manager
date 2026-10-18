from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .jobs import JobError


def _files(folder, *, markers=False):
    from Utils.profiles.backup import _BACKUP_FILES

    if folder.is_symlink() or not folder.is_dir():
        raise JobError("unsafe_backup_path", "The profile or backup folder is unavailable or linked.")
    names = list(_BACKUP_FILES)
    if markers:
        names += [".missing.json", ".manual", ".keep", ".label"]
    result = {}
    for name in names:
        path = folder / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise JobError("unsafe_backup_path", "A backup state file is not a regular file.", {"path": str(path)})
        if path.exists() and path.stat().st_size > 32 * 1024 * 1024:
            raise JobError("backup_file_too_large", "A profile state file exceeds 32 MiB.", {"path": str(path)})
        result[name] = path.read_bytes() if path.exists() else None
    return result


def _revision(files):
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        digest.update(name.encode() + b"\0")
        digest.update(b"missing\0" if data is None else str(len(data)).encode() + b":" + data)
    return digest.hexdigest()


def _root(profile):
    _files(profile)
    root = profile / "backups"
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise JobError("unsafe_backup_path", "The backup root is not a regular directory.")
    return root


def _backup(profile, backup_id):
    from Utils.profiles.backup import _parse_timestamp_from_dirname

    if not isinstance(backup_id, str) or _parse_timestamp_from_dirname(backup_id) is None:
        raise JobError("invalid_backup_id", "Choose a backup ID returned by the inventory.")
    folder = _root(profile) / backup_id
    files = _files(folder, markers=True)
    if files["modlist.txt"] is None:
        raise JobError("invalid_backup", "The backup has no mod list.")
    return folder, files


def _summary(folder, files):
    from Utils.profiles.backup import backup_stats

    return {"id": folder.name, "revision": _revision(files),
            "label": (files[".label"] or b"").decode("utf-8", errors="replace").strip(),
            "manual": files[".manual"] is not None, "kept": files[".keep"] is not None,
            "size_bytes": sum(len(data) for data in files.values() if data is not None),
            "files": [name for name, data in files.items() if data is not None and not name.startswith(".")],
            "counts": backup_stats(folder)}


def inventory(profile):
    from Utils.profiles.backup import _parse_timestamp_from_dirname

    root = _root(profile)
    backups, unavailable = [], []
    for folder in sorted(root.iterdir(), reverse=True) if root.exists() else []:
        if _parse_timestamp_from_dirname(folder.name) is None:
            continue
        try:
            folder, files = _backup(profile, folder.name)
            backups.append(_summary(folder, files))
        except (JobError, OSError) as exc:
            unavailable.append({"id": folder.name, "reason": str(exc)})
    return {"profile": profile.name, "revision": _revision(_files(profile)),
            "backups": backups, "unavailable": unavailable}


def _settings(files):
    state = json.loads(files.get("profile_state.json") or b"{}")
    if not isinstance(state, dict):
        raise JobError("invalid_backup", "Profile state must be an object.")
    settings = (state["profile_settings"] if "profile_settings" in state
                else json.loads(files.get("profile_settings.json") or b"{}"))
    if not isinstance(settings, dict):
        raise JobError("invalid_backup", "Profile settings must be an object.")
    return settings


def refresh_profile(game, profile, log, progress):
    from Utils.mods.state import _reconcile_filegraph
    from Utils.profiles.groups import is_group, materialize_group, member_of_groups

    targets = [profile] + [profile.parent / name for name in member_of_groups(game, profile.name)]
    synced = []
    for target in targets:
        selected = copy.copy(game)
        selected.set_active_profile_dir(target)
        selected.load_paths(read_only=True)
        if is_group(target):
            materialize_group(selected, target, log_fn=log)
        _reconcile_filegraph(selected, target, kind="full", mod_names=[], log=log, progress=progress)
        synced.append(target.name)
    return synced


def runner(game, profile, action, options):
    def run(log, progress):
        from Utils.deployment.locking import coordinated_game_mutation
        from Utils.mods.modlist import modlist_lock
        from Utils.profiles import backup
        from Utils.profiles.manage import _is_locked, _reject_deployed_use
        from Utils.profiles.state import _lock_for, read_only_profile_state

        with coordinated_game_mutation(game, "profiles.backups." + action), read_only_profile_state():
            with modlist_lock(profile / "modlist.txt"), _lock_for(profile):
                if action == "list":
                    return inventory(profile)
                _root(profile)
                current = _files(profile)
                if action in {"create", "restore"} and _revision(current) != options["revision"]:
                    raise JobError("stale_profile_backup", "Profile state changed; list backups again.")
                if action == "create":
                    if current["modlist.txt"] is None:
                        raise JobError("invalid_profile", "The profile has no mod list to back up.")
                    folder = backup.create_backup(profile, log_fn=log, manual=True,
                                                  label=options.get("label", ""), record_missing=True)
                    return {"ok": True, "backup": _summary(folder, _files(folder, markers=True))}
                folder, files = _backup(profile, options["backup_id"])
                if _revision(files) != options["backup_revision"]:
                    raise JobError("stale_profile_backup", "The selected backup changed; list backups again.")
                if action == "remove":
                    backup.delete_backup(folder)
                    return {"ok": True, "removed": folder.name}
                if action == "edit":
                    if "label" in options:
                        backup.set_backup_label(folder, options["label"])
                    if "kept" in options:
                        backup.set_backup_kept(folder, options["kept"])
                    return {"ok": True, "backup": _summary(folder, _files(folder, markers=True))}
                _reject_deployed_use(game, profile)
                if _is_locked(profile):
                    raise JobError("profile_locked", "Unlock the profile before restoring its state.")
                missing = json.loads(files[".missing.json"] or b"[]")
                if not isinstance(missing, list) or any(name not in backup._BACKUP_FILES for name in missing):
                    raise JobError("invalid_backup", "The backup has an invalid missing-file manifest.")
                restored = {name: files.get(name) if files.get(name) is not None or name in missing else data
                            for name, data in current.items()}
                old_settings, new_settings = _settings(current), _settings(restored)
                for key in ("profile_specific_mods", "is_group", "group_members", "original_default"):
                    if old_settings.get(key) != new_settings.get(key):
                        raise JobError("backup_layout_mismatch", "The backup uses a different profile storage or group layout.",
                                       {"setting": key})
                recovery = backup.create_backup(profile, log_fn=log, manual=True,
                                                label="Before backup restore", record_missing=True)
                try:
                    backup.restore_backup(profile, folder)
                except Exception as exc:
                    try:
                        backup.restore_backup(profile, recovery)
                    except Exception as recovery_error:
                        raise JobError("backup_restore_rollback_failed", "Restore failed and recovery was incomplete.",
                                       {"recovery_backup_id": recovery.name, "state_persisted": True,
                                        "reason": str(exc), "recovery_error": str(recovery_error)}) from exc
                    raise JobError("backup_restore_failed", "Restore failed; previous profile state was recovered.",
                                   {"recovery_backup_id": recovery.name, "rolled_back": True, "reason": str(exc)}) from exc
            result = {"ok": True, "profile": profile.name, "backup_id": folder.name,
                      "recovery_backup_id": recovery.name, "state_persisted": True}
            try:
                result["synced_profiles"] = refresh_profile(game, profile, log, progress)
            except Exception as exc:
                raise JobError("backup_restore_sync_failed", "Profile state was restored but its file index could not refresh.",
                               {**result, "reason": str(exc)}) from exc
            result["revision"] = _revision(_files(profile))
            return result
    return run
