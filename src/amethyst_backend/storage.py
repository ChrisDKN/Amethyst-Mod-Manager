from __future__ import annotations

import os
import stat
from pathlib import Path

from .jobs import JobCancelled, JobError


def _counter():
    return {"files": 0, "logical_bytes": 0, "allocated_bytes": 0,
            "unique_files": 0, "unique_logical_bytes": 0, "unique_allocated_bytes": 0}


def _add(counter, seen, info):
    allocated = getattr(info, "st_blocks", 0) * 512
    counter["files"] += 1
    counter["logical_bytes"] += info.st_size
    counter["allocated_bytes"] += allocated
    identity = (info.st_dev, info.st_ino)
    if identity not in seen:
        seen.add(identity)
        counter["unique_files"] += 1
        counter["unique_logical_bytes"] += info.st_size
        counter["unique_allocated_bytes"] += allocated


def measure(folder, records, cancel, totals, total_seen):
    status = {}
    archive_members = 0
    for record in records:
        if cancel.is_set():
            raise JobCancelled("Storage report cancelled.")
        if record.provider_kind == "archive_member":
            archive_members += 1
            continue
        key = os.fsdecode(record.source_rel).replace("\\", "/")
        values = status.setdefault(key, set())
        if record.enabled and record.deployable and record.winning:
            values.add("winning")
        elif record.enabled and record.conflict_status < 0:
            values.add("conflict_losing")
        else:
            values.add("inactive")
    counts, seen = _counter(), set()
    groups = {key: _counter() for key in ("winning", "conflict_losing", "inactive", "unmapped")}
    group_seen = {key: set() for key in groups}
    errors, symlinks, special = [], 0, 0
    pending = [folder]
    while pending:
        if cancel.is_set():
            raise JobCancelled("Storage report cancelled.")
        directory = pending.pop()
        try:
            with os.scandir(directory) as children:
                for child in children:
                    if cancel.is_set():
                        raise JobCancelled("Storage report cancelled.")
                    try:
                        info = child.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            symlinks += 1
                        elif stat.S_ISDIR(info.st_mode):
                            pending.append(Path(child.path))
                        elif stat.S_ISREG(info.st_mode):
                            relative = Path(child.path).relative_to(folder).as_posix()
                            flags = status.get(relative, set())
                            category = next((key for key in groups if key in flags), "unmapped")
                            _add(counts, seen, info)
                            _add(groups[category], group_seen[category], info)
                            _add(totals, total_seen, info)
                        else:
                            special += 1
                    except OSError as exc:
                        errors.append({"path": child.path, "reason": str(exc)})
        except OSError as exc:
            errors.append({"path": str(directory), "reason": str(exc)})
    return {"totals": counts, "categories": groups, "archive_member_count": archive_members,
            "skipped_symlinks": symlinks, "skipped_special_files": special, "errors": errors,
            "complete": not errors and not symlinks and not special}


def runner(game, profile, mods, include_overwrite, include_root_folder, cancel):
    def run(log, progress):
        from Utils.deployment.locking import coordinated_game_mutation
        from Utils.filegraph.constants import OVERWRITE_NAME, ROOT_FOLDER_NAME
        from Utils.mods.files import _mod_dir_for
        from .conflicts import _selection, profile_snapshot, snapshot_revision

        with coordinated_game_mutation(game, "profiles.storage.get"):
            with profile_snapshot(game, profile, log) as (selected, entries, snapshot):
                entries = [entry for entry in entries if not entry.is_group_header]
                names = _selection(entries, mods)
                enabled = {entry.name: entry.enabled for entry in entries}
                for name, include in ((OVERWRITE_NAME, include_overwrite), (ROOT_FOLDER_NAME, include_root_folder)):
                    folder = _mod_dir_for(selected, name)
                    if include and name not in names and folder is not None and folder.exists():
                        names.append(name)
                items, totals, seen = [], _counter(), set()
                for index, name in enumerate(names):
                    if cancel.is_set():
                        raise JobCancelled("Storage report cancelled.")
                    folder = _mod_dir_for(selected, name)
                    if folder is None:
                        raise JobError("mod_storage_unavailable", "The mod storage path is unavailable.", {"mod": name})
                    item = measure(folder, snapshot.iter_mod_files(name), cancel, totals, seen)
                    items.append({"mod": name, "enabled": enabled.get(name, True), "path": str(folder),
                                  "linked": folder.is_symlink(), **item})
                    progress(index + 1, len(names), "Measuring installed storage")
                return {"ok": True, "profile": profile.name, "revision": snapshot_revision(snapshot),
                        "mods": items, "totals": totals, "complete": all(item["complete"] for item in items),
                        "allocation_supported": hasattr(os.stat(profile), "st_blocks"),
                        "scope": "installed_regular_files", "archive_members_measured": False,
                        "reclaimable_bytes": None}
    return run
