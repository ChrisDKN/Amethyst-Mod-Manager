from __future__ import annotations

import copy
import hashlib
import json
import stat
import tarfile
import zipfile
from pathlib import Path

from .jobs import JobCancelled, JobError


def _checkpoint(cancel):
    if cancel.is_set():
        raise JobCancelled("Tool installation cancelled.")


def _token(path):
    try:
        value = Path(path).lstat()
    except FileNotFoundError:
        return None
    return [value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns]


def _selected_game(game, profile):
    from Utils.profiles.state import read_only_profile_state

    if profile.is_symlink() or not profile.is_dir():
        raise JobError("profile_not_found", "The selected profile is unavailable or unsafe.")
    selected = copy.copy(game)
    selected._game_path = None
    selected._staging_path = None
    selected.set_active_profile_dir(profile)
    with read_only_profile_state():
        selected.load_paths(read_only=True, prefix_fallback=False)
    return selected


def _archive_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise JobError("invalid_tool_archive", "Choose an absolute archive path.")
    try:
        if path.is_symlink() or not path.is_file():
            raise JobError("invalid_tool_archive", "The tool archive must be a regular, non-symlink file.")
        resolved = path.resolve(strict=True)
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise JobError("invalid_tool_archive", "The tool archive must be a regular file.")
        from Utils.wizards.archives import is_archive
        if not is_archive(resolved.name):
            raise JobError("invalid_tool_archive", "The tool archive type is unsupported.")
        return resolved
    except (OSError, RuntimeError) as exc:
        raise JobError("invalid_tool_archive", "The tool archive is unavailable.",
                       {"reason": str(exc)}) from exc


def _digest_archive(path, cancel):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _checkpoint(cancel)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _source_identity(path, tool_id, version_index, version, archive_digest):
    source = version["source"]
    tool_sidecar = path.with_suffix(path.suffix + ".tool.json")
    nexus_sidecar = path.with_suffix(path.suffix + ".nexus.json")
    if tool_sidecar.is_symlink() or nexus_sidecar.is_symlink():
        raise JobError("tool_source_mismatch", "An archive identity file is a symbolic link.")
    if tool_sidecar.exists() and nexus_sidecar.exists():
        raise JobError("tool_source_mismatch", "The archive has conflicting source identities.")
    if tool_sidecar.exists():
        try:
            sidecar_info = tool_sidecar.lstat()
            if not stat.S_ISREG(sidecar_info.st_mode) or sidecar_info.st_size > 65536:
                raise ValueError("Unsafe tool identity")
            saved = json.loads(tool_sidecar.read_text(encoding="utf-8"))
            matched = (source["kind"] in ("direct", "github_release")
                       and isinstance(saved, dict) and saved.get("version") == 1
                       and saved.get("tool_id") == tool_id
                       and saved.get("version_index") == version_index
                       and saved.get("source_kind") == source["kind"]
                       and saved.get("sha256") == archive_digest
                       and saved.get("size_bytes") == path.stat().st_size)
            if matched and source["kind"] == "direct":
                matched = saved.get("source_url") == source["url"]
            if matched and source["kind"] == "github_release":
                from urllib.parse import urlsplit
                api_parts = urlsplit(source["url"]).path.strip("/").split("/")
                asset = urlsplit(str(saved.get("source_url") or ""))
                matched = (len(api_parts) >= 3 and api_parts[0] == "repos"
                           and asset.scheme == "https" and asset.hostname == "github.com"
                           and asset.path.startswith(f"/{api_parts[1]}/{api_parts[2]}/releases/download/"))
        except (OSError, ValueError, TypeError):
            matched = False
        if not matched:
            raise JobError("tool_source_mismatch", "The tool archive identity does not match this choice.")
        return "tool_download"
    if nexus_sidecar.exists():
        try:
            sidecar_info = nexus_sidecar.lstat()
            if not stat.S_ISREG(sidecar_info.st_mode) or sidecar_info.st_size > 65536:
                raise ValueError("Unsafe Nexus identity")
            saved = json.loads(nexus_sidecar.read_text(encoding="utf-8"))
            nexus_source = source["kind"] in ("nexus", "manual") and "mod_id" in source and "domain" in source
            matched = (nexus_source and isinstance(saved, dict)
                       and saved.get("version") == 1
                       and str(saved.get("game_domain") or "").casefold() == source["domain"].casefold()
                       and int(saved.get("mod_id") or 0) == source["mod_id"]
                       and int(saved.get("file_id") or 0) > 0
                       and ("file_id" not in source or int(saved.get("file_id") or 0) == source["file_id"]))
        except (OSError, ValueError, TypeError):
            matched = False
        if not matched:
            raise JobError("tool_source_mismatch", "The Nexus archive identity does not match this choice.")
        return "nexus_download"
    return None


def _tree_state(root, digest, cancel):
    count = 0
    stack = [root]
    while stack:
        _checkpoint(cancel)
        parent = stack.pop()
        for path in sorted(parent.iterdir(), key=lambda item: item.name):
            _checkpoint(cancel)
            value = _token(path)
            digest.update(json.dumps([str(path.relative_to(root)), value], ensure_ascii=True).encode())
            count += 1
            if stat.S_ISDIR(value[2]):
                stack.append(path)
    return count


def _destination_state(selected, payload, mode, cancel, source_mod_id,
                       mod_name_override=None, allow_restore_links=False):
    from Utils.wizards.payload import destination, replacement_trees, tree_identity
    from Utils.profiles.state import read_only_profile_state

    with read_only_profile_state():
        target, label, mod_name = destination(
            selected, payload.archive, mode, mod_fallback_name="Script Extender",
            nexus_mod_id=source_mod_id, mod_name_override=mod_name_override)
    if payload.archive.is_relative_to(target):
        raise JobError("tool_archive_in_destination", "Move the archive outside the installation destination.")
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise JobError("unsafe_tool_destination", "The tool destination is a link or file.")

    records = {str(target): _token(target)}
    overwritten = []
    existing_entries = 0
    if mode == "mod":
        if target.parent.is_symlink() or (target.parent.exists() and not target.parent.is_dir()):
            raise JobError("unsafe_tool_destination", "The mod staging folder is unsafe.")
        records[str(target.parent)] = _token(target.parent)
        if target.parent.is_dir() and any(
                path.name.casefold() == mod_name.casefold() and path.name != mod_name
                for path in target.parent.iterdir()):
            raise JobError("tool_mod_name_conflict", "A staged folder differs only in letter case.")
        modlist = selected._active_profile_dir / "modlist.txt"
        if modlist.is_symlink():
            raise JobError("unsafe_tool_destination", "The profile mod list is a symbolic link.")
        records[str(modlist)] = _token(modlist)
        tree = hashlib.sha256()
        if target.is_dir():
            existing_entries = _tree_state(target, tree, cancel)
        records["existing_mod_tree"] = tree.hexdigest()
    else:
        trees = replacement_trees(payload)
        for relative in (*trees, *payload.files, *payload.remove_files):
            _checkpoint(cancel)
            if relative not in trees and any(relative.is_relative_to(tree) for tree in trees):
                continue
            output = target / relative
            parent = output.parent
            while parent != target:
                records[str(parent)] = _token(parent)
                if ((parent.is_symlink() and not allow_restore_links)
                        or (parent.exists() and not parent.is_dir())):
                    raise JobError("unsafe_tool_destination", "A tool output directory is a link or file.",
                                   {"path": str(parent)})
                parent = parent.parent
            if relative in trees:
                current = tree_identity(output, lambda: _checkpoint(cancel))
                if current is not None:
                    if not stat.S_ISDIR(current[0][2]):
                        raise JobError("unsafe_tool_destination", "A replacement directory is a link or file.",
                                       {"path": str(output)})
                    overwritten.append(str(relative))
                    existing_entries += current[2]
                records[str(output)] = current
                continue
            before = _token(output)
            if before is not None and not (stat.S_ISREG(before[2]) or stat.S_ISLNK(before[2])):
                raise JobError("unsafe_tool_destination", "A tool output collides with a directory or special file.",
                               {"path": str(output)})
            records[str(output)] = before
            if before is not None and not (relative in payload.keep_existing and stat.S_ISREG(before[2])):
                overwritten.append(str(relative))
    return target, label, mod_name, records, overwritten, existing_entries


def plan(game, profile, tool_id, version_index, version, catalogue_revision,
         archive, mode, payload, cancel):
    from Utils.wizards.payload import archive_token

    _checkpoint(cancel)
    selected = _selected_game(game, profile)
    if not selected.is_configured():
        raise JobError("game_not_configured", "The game is not configured for tool installation.")
    archive_identity = archive_token(archive)
    archive_digest = _digest_archive(archive, cancel)
    if archive_token(archive) != archive_identity:
        raise JobError("tool_archive_changed", "The archive changed during inspection.")
    payload.check_archive()
    source_identity = _source_identity(archive, tool_id, version_index, version, archive_digest)
    source_mod_id = version["source"].get("mod_id", 0) if source_identity == "nexus_download" else 0
    target, label, mod_name, records, overwritten, existing_entries = _destination_state(
        selected, payload, mode, cancel, source_mod_id)
    snapshot = {
        "game_id": str(selected.game_id), "profile": profile.name,
        "tool_id": tool_id, "version_index": version_index,
        "catalogue_revision": catalogue_revision,
        "archive": str(archive), "archive_identity": archive_identity,
        "archive_sha256": archive_digest,
        "source_identity": source_identity,
        "mode": mode, "destination": str(target), "mod_name": mod_name,
        "files": [str(path) for path in payload.files],
        "configuration_state": {
            "paths": _token(selected._paths_file),
            "profile": _token(profile / "profile_state.json"),
            "legacy_profile": _token(profile / "profile_settings.json"),
        },
        "destination_state": records,
    }
    revision = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return {
        "revision": revision, "game": str(selected.game_id), "profile": profile.name,
        "tool_id": tool_id, "version_index": version_index,
        "version": version["label"], "mode": mode,
        "archive": {"path": str(archive), "name": archive.name,
                    "size_bytes": archive.stat().st_size, "sha256": archive_digest,
                    "source_verified": source_identity is not None,
                    "source_identity": source_identity},
        "destination": {"path": str(target), "label": label,
                        "mod_name": mod_name, "exists": target.exists(),
                        "existing_entries": existing_entries,
                        "overwrites": overwritten[:100],
                        "overwrite_count": len(overwritten)},
        "file_count": len(payload.files), "restore_before_install": mode == "game",
        "archive_retained": True,
    }


def inspect_runner(game, profile, tool_id, version_index, version,
                   catalogue_revision, archive_path, mode, cancel):
    def run(log, progress):
        from Utils.wizards.payload import PayloadInstallError, prepare_payload

        archive = _archive_path(archive_path)
        _checkpoint(cancel)
        progress(0, 1, "inspect_tool")
        try:
            with prepare_payload(archive, log_fn=log, checkpoint=lambda: _checkpoint(cancel),
                                 strip_top_dir=version.get("strip_top_dir", True)) as payload:
                result = plan(game, profile, tool_id, version_index, version,
                              catalogue_revision, archive, mode, payload, cancel)
        except PayloadInstallError as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except (JobError, JobCancelled):
            raise
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise JobError("tool_inspection_failed", "Could not inspect the tool archive and destination.",
                           {"reason": str(exc)}) from exc
        progress(1, 1, "inspect_tool")
        return result
    return run


def install_runner(game, profile, tool_id, version_index, version,
                   catalogue_revision, archive_path, mode, revision,
                   allow_unverified_source, replace_existing, cancel):
    def run(log, progress):
        from Utils.wizards.payload import PayloadInstallError, install_prepared_payload, prepare_payload
        from .tools import catalogue

        archive = _archive_path(archive_path)
        _checkpoint(cancel)
        progress(0, 1, "prepare_tool")
        restore_started = False

        def install_checkpoint():
            if cancel.is_set():
                details = {"restore_attempted": True, "state_persisted": True} if restore_started else {}
                raise JobCancelled("Tool installation cancelled.", details)

        def mark_restore():
            nonlocal restore_started
            restore_started = True

        try:
            with prepare_payload(archive, log_fn=log, checkpoint=lambda: _checkpoint(cancel),
                                 strip_top_dir=version.get("strip_top_dir", True)) as payload:
                selected = _selected_game(game, profile)

                def verify_plan():
                    listing = catalogue(game, profile)
                    if listing["revision"] != catalogue_revision:
                        raise JobError("stale_tool_catalogue", "The tool choices changed; inspect again.")
                    current = plan(game, profile, tool_id, version_index, version,
                                   catalogue_revision, archive, mode, payload, cancel)
                    if current["revision"] != revision:
                        raise JobError("stale_tool_plan", "The archive or destination changed; inspect again.")
                    if not current["archive"]["source_verified"] and not allow_unverified_source:
                        raise JobError("unverified_tool_source", "Confirm this manually supplied archive with allow_unverified_source.")
                    if mode == "mod" and current["destination"]["exists"] and not replace_existing:
                        raise JobError("tool_mod_already_exists", "Confirm replacement of the existing managed mod.")
                    return current

                current = verify_plan()
                progress(0, 1, "install_tool")
                source_mod_id = (version["source"].get("mod_id", 0)
                                 if current["archive"]["source_identity"] == "nexus_download" else 0)
                synced_groups = []

                def sync_groups():
                    if mode != "mod":
                        return
                    from Utils.profiles.groups import materialize_group, member_of_groups

                    failures = []
                    try:
                        group_names = member_of_groups(game, profile.name)
                    except Exception as exc:
                        group_names = []
                        failures.append({"profile": None, "reason": str(exc)})
                    for group_name in group_names:
                        group_dir = profile.parent / group_name
                        try:
                            if group_dir.is_symlink() or not group_dir.is_dir():
                                raise OSError("The profile group is unavailable or unsafe.")
                            materialize_group(_selected_game(game, group_dir), group_dir, log_fn=log)
                            synced_groups.append(group_name)
                        except Exception as exc:
                            failures.append({"profile": group_name, "reason": str(exc)})
                    if failures:
                        raise JobError("profile_group_refresh_failed",
                                       "The tool was installed but a profile group could not be refreshed.",
                                       {"mod": current["destination"]["mod_name"],
                                        "synced_groups": synced_groups, "failures": failures,
                                        "state_persisted": True, "archive_retained": True})

                label, count, name = install_prepared_payload(
                    selected, payload, mode, mod_fallback_name="Script Extender",
                    nexus_mod_id=source_mod_id, modlist_path=profile / "modlist.txt",
                    restore_first=True, delete_archive=False, log_fn=log,
                    checkpoint=install_checkpoint, before_write=verify_plan,
                    on_restore_start=mark_restore, after_write=sync_groups,
                    strict_index=True)
        except PayloadInstallError as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except (JobError, JobCancelled):
            raise
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise JobError("tool_install_failed", "Could not install the selected tool archive.",
                           {"reason": str(exc), "restore_attempted": restore_started,
                            "state_persisted": restore_started}) from exc
        progress(1, 1, "install_tool")
        return {"ok": True, "game": str(game.game_id), "profile": profile.name,
                "tool_id": tool_id, "version_index": version_index,
                "archive": {"path": str(archive), "retained": True},
                "destination": {"label": label, "mod_name": name},
                "file_count": count, "restore_attempted": mode == "game",
                "synced_groups": synced_groups}
    return run
