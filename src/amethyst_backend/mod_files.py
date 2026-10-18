from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .jobs import JobCancelled, JobError


def snapshot(game, profile, mod, cancel, log):
    from Utils.mods.files import _mod_dir_for, ancestor_paths
    from Utils.profiles.state import read_profile_state, read_excluded_mod_files, read_root_mod_files, read_mod_strip_prefixes, read_only_profile_state
    from .conflicts import _selection, profile_snapshot, snapshot_revision

    with read_only_profile_state(), profile_snapshot(game, profile, log) as (selected, entries, graph):
        _selection([entry for entry in entries if not entry.is_group_header], [mod])
        folder = _mod_dir_for(selected, mod)
        if folder is None or not folder.is_dir():
            raise JobError("mod_files_missing", "The installed mod folder is unavailable.")
        state = read_profile_state(profile, read_only=True)
        excluded = set(read_excluded_mod_files(profile, state).get(mod, []))
        roots = set(read_root_mod_files(profile, state).get(mod, []))
        stripped = read_mod_strip_prefixes(profile, state).get(mod, [])
        files, directories, identities = {}, {}, []
        for record in graph.iter_mod_files(mod):
            if cancel.is_set():
                raise JobCancelled("Mod file inspection cancelled.")
            if record.provider_kind == "archive_member":
                continue
            raw = os.fsdecode(record.source_rel).replace("\\", "/")
            parts = Path(raw).parts
            if not parts or Path(raw).is_absolute() or ".." in parts:
                raise JobError("unsafe_mod_path", "The file inventory contains an unsafe path.")
            if raw in files:
                continue
            path = folder / raw
            try:
                if not path.resolve().is_relative_to(folder.resolve()) or path.is_symlink():
                    raise JobError("unsafe_mod_path", "A mod file resolves outside its folder or is linked.", {"path": raw})
                info = path.stat()
                if not path.is_file():
                    raise OSError("Not a regular file")
            except OSError as exc:
                raise JobError("mod_files_changed", "The indexed file is unavailable; refresh the mod inventory.",
                               {"path": raw, "reason": str(exc)}) from exc
            key = raw.lower()
            files[raw] = {"path": raw, "enabled": key not in excluded, "root": key in roots,
                          "destination": record.destination, "target": record.target,
                          "winning": record.winning, "deployable": record.deployable, "size_bytes": info.st_size}
            identities.append((raw, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
            for parent in ancestor_paths(raw):
                directories[parent.lower()] = parent
        keys = [path.lower() for path in files]
        if len(keys) != len(set(keys)):
            raise JobError("ambiguous_mod_path", "The mod contains filenames differing only in letter case.")
        revision = hashlib.sha256(json.dumps({"profile": profile.name, "mod": mod,
            "graph": snapshot_revision(graph), "files": sorted(identities),
            "state": state, "excluded": sorted(excluded), "roots": sorted(roots), "stripped": stripped},
            sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        return {"profile": profile.name, "mod": mod, "revision": revision,
                "files": sorted(files.values(), key=lambda item: item["path"]),
                "directories": sorted(directories.values()), "strip_prefixes": stripped}


def runner(game, profile, mod, options, cancel, *, edit):
    def run(log, progress):
        from Utils.deployment.locking import coordinated_game_mutation
        from Utils.mods.modlist import modlist_lock
        from Utils.profiles.manage import _reject_deployed_use
        from Utils.profiles.state import _lock_for, read_profile_state, write_profile_state, read_profile_settings
        from .profile_backups import refresh_profile

        with coordinated_game_mutation(game, "mods.files.edit" if edit else "mods.files.get"):
            report = snapshot(game, profile, mod, cancel, log)
            if options.get("revision") is not None and report["revision"] != options["revision"]:
                raise JobError("stale_mod_files", "The files or profile state changed; inspect them again.")
            if not edit:
                offset, limit = options.get("offset", 0), options.get("limit", 500)
                files = report.pop("files")
                return {**report, "files": files[offset:offset + limit], "total": len(files),
                        "offset": offset, "limit": limit,
                        "next_offset": offset + limit if offset + limit < len(files) else None}
            _reject_deployed_use(game, profile)
            if read_profile_settings(profile, read_only=True).get("profile_locked", False):
                raise JobError("profile_locked", "Unlock the profile before editing its files.")
            files = {item["path"]: item for item in report["files"]}
            for change in options.get("changes", []):
                if change["path"] not in files:
                    raise JobError("mod_file_not_found", "Choose an exact raw file path from the inventory.",
                                   {"path": change["path"]})
            if "strip_prefixes" in options:
                unknown = set(options["strip_prefixes"]) - set(report["directories"])
                if unknown:
                    raise JobError("mod_directory_not_found", "Choose exact directory paths from the inventory.",
                                   {"paths": sorted(unknown)})
            with modlist_lock(profile / "modlist.txt"), _lock_for(profile):
                current = snapshot(game, profile, mod, cancel, log)
                if current["revision"] != report["revision"]:
                    raise JobError("stale_mod_files", "The files or profile state changed; inspect them again.")
                state = read_profile_state(profile)
                before = json.dumps(state, sort_keys=True)
                if options.get("reset", False):
                    for section in ("excluded_mod_files", "root_mod_files", "mod_strip_prefixes"):
                        state.setdefault(section, {}).pop(mod, None)
                else:
                    for section, field, inverse in (("excluded_mod_files", "enabled", True), ("root_mod_files", "root", False)):
                        values = set(state.get(section, {}).get(mod, []))
                        for change in options.get("changes", []):
                            if field in change:
                                key = change["path"].lower()
                                if change[field] != inverse:
                                    values.add(key)
                                else:
                                    values.discard(key)
                        if values:
                            state.setdefault(section, {})[mod] = sorted(values)
                        else:
                            state.setdefault(section, {}).pop(mod, None)
                    if "strip_prefixes" in options:
                        if options["strip_prefixes"]:
                            state.setdefault("mod_strip_prefixes", {})[mod] = options["strip_prefixes"]
                        else:
                            state.setdefault("mod_strip_prefixes", {}).pop(mod, None)
                changed = before != json.dumps(state, sort_keys=True)
                if changed:
                    write_profile_state(profile, state)
            if changed:
                try:
                    refresh_profile(game, profile, log, progress)
                except Exception as exc:
                    raise JobError("mod_files_sync_failed", "File settings were saved but the profile index could not refresh.",
                                   {"state_persisted": True, "mod": mod, "reason": str(exc)}) from exc
            return {"ok": True, "profile": profile.name, "mod": mod, "changed": changed,
                    "state_persisted": changed, "needs_deployment": changed}
    return run
