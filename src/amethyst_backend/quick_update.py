from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .jobs import JobCancelled, JobError
from .mod_versions import _archive, _context, _selected_target, change_runner, versions_runner
from .nexus import _call, download_runner


def _selection(profile, mods):
    from Utils.mods.groups import group_edit_state, group_state_revision, normalize_groups, with_group_headers
    from .mod_groups import _selection as expand

    with group_edit_state(profile / "modlist.txt") as (raw, _state, entries, groups):
        original = groups
        groups = normalize_groups(groups, entries)
        entries = with_group_headers(entries, groups)
        names = ([entry.name for entry in entries if not entry.is_separator and not entry.is_group_header]
                 if mods is None else expand(entries, groups, mods))
        selected = [entry.name for entry in entries if entry.name in names and not entry.is_group_header]
        if len(selected) != len(set(selected)):
            raise JobError("ambiguous_mod", "The profile has duplicate Quick Update targets.")
        return selected, group_state_revision(raw, original)


def _error(exc):
    try:
        def fail():
            raise exc
        _call(fail)
    except JobError as error:
        return {"name": error.name, "message": str(error), "details": error.details}
    except Exception as error:
        return {"name": "quick_update_failed", "message": "The update could not be completed.",
                "details": {"exception": type(error).__name__}}


def _checkpoint(control, summary=None):
    from Utils.downloads.control import wait_if_paused

    if wait_if_paused(control):
        raise JobCancelled("Quick Update cancelled.", {"quick_update": summary} if summary is not None else {})


def _plan(session, game, profile, mods, log, progress, control=None):
    from Nexus.nexus_meta import read_meta
    from Utils.mods.copy import resolve_target_staging
    from Utils.mods.metadata import meta_file_identity, meta_file_lock

    selected, selection_revision = _selection(profile, mods)
    staging = Path(resolve_target_staging(game, profile))
    updates, skipped, failed, fingerprints, owners = [], [], [], [], {}
    for index, mod in enumerate(selected):
        _checkpoint(control)
        progress(index, len(selected), f"Checking Quick Update: {mod}")
        path = staging / mod / "meta.ini"
        try:
            with meta_file_lock(path):
                before = meta_file_identity(path) if path.is_file() else None
                meta = read_meta(path)
                after = meta_file_identity(path) if path.is_file() else None
            if before != after:
                raise JobError("stale_mod_version", "The mod changed while Quick Update read its metadata.")
            fingerprints.append({"mod": mod, "metadata": after})
            reason = ("no_nexus_identity" if not meta.mod_id else
                      "updates_ignored" if meta.ignore_update else
                      "no_pending_update" if mods is None and not meta.has_update else None)
            if reason is not None:
                skipped.append({"mod": mod, "reason": reason})
                continue
            result = versions_runner(session, game, profile, mod, operation_locked=True,
                                     ignore_download_history=True)(log, lambda *_args: None)
            with meta_file_lock(path):
                if (meta_file_identity(path) if path.is_file() else None) != after:
                    raise JobError("stale_mod_version", "The mod changed while Quick Update read its versions.")
            catalog = result["version_catalog"]
            file_id = catalog["recommended_file_id"]
            if file_id is None:
                skipped.append({"mod": mod, "reason": "no_name_matched_update"})
                continue
            file = next(row for row in catalog["files"] if row["file_id"] == file_id)
            if meta.ignored_version and file["version"] == meta.ignored_version:
                skipped.append({"mod": mod, "reason": "updates_ignored"})
                continue
            owner = (catalog["owner_profile"], catalog["owner_mod"])
            if owner in owners:
                skipped.append({"mod": mod, "reason": "duplicate_owner", "primary_mod": owners[owner]})
                continue
            owners[owner] = mod
            updates.append({key: catalog[key] for key in (
                "requested_profile", "requested_mod", "owner_profile", "owner_mod", "affected_profiles",
                "revision", "domain", "mod_id", "installed_file_id", "installed_version")}
                | {"mod": mod, "file_id": file_id, "version": file["version"], "file_name": file["file_name"],
                   "name": file["name"], "size_bytes": file["size_in_bytes"] or file["size_kb"] * 1024})
        except Exception as exc:
            failed.append({"mod": mod, "error": _error(exc)})
    payload = {"profile": profile.name, "source": "nexus", "requested_mods": mods,
               "mods": selected, "updates": updates, "skipped": skipped, "failed": failed}
    revision = hashlib.sha256(json.dumps(
        {**payload, "selection_revision": selection_revision, "metadata": fingerprints},
        sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    progress(len(selected), len(selected), "Quick Update plan ready")
    return {**payload, "revision": revision,
            "counts": {"selected": len(selected), "updates": len(updates), "skipped": len(skipped), "failed": len(failed)}}


def plan_runner(session, game, profile, mods):
    def run(log, progress):
        from Utils.operations import acquire_game_operation

        with acquire_game_operation(game, "mods.quick_update.plan"):
            return {"plan": _plan(session, game, profile, mods, log, progress)}
    return run


def _summary(plan, downloaded, updated, failed, remaining, download_only):
    return {"profile": plan["profile"], "source": "nexus", "plan_revision": plan["revision"],
            "download_only": download_only, "downloaded": downloaded, "updated": updated,
            "skipped": plan["skipped"], "failed": failed, "remaining": remaining,
            "counts": {"downloaded": len(downloaded), "updated": len(updated),
                       "skipped": len(plan["skipped"]), "failed": len(failed), "remaining": len(remaining)}}


def update_runner(session, game, profile, mods, revision, options, per_mod, nxm_links, control):
    def run(log, progress):
        from Utils.deployment.locking import coordinated_game_mutation
        from Utils.ui.config import load_collection_settings

        with coordinated_game_mutation(game, "mods.quick_update"):
            plan = _plan(session, game, profile, mods, log, progress, control)
            if plan["revision"] != revision:
                raise JobError("quick_update_plan_changed", "Quick Update targets changed; read and approve a new plan.",
                               {"plan": plan})
            targets = plan["updates"]
            named = {row["mod"] for row in targets}
            unsupported = sorted((set(per_mod) | set(nxm_links)) - named)
            if unsupported:
                raise JobError("quick_update_option_target_unavailable", "Options must refer to planned updates.",
                               {"mods": unsupported})
            for row in targets:
                link = nxm_links.get(row["mod"])
                if link is not None and (link.game_domain, link.mod_id, link.file_id) != (row["domain"], row["mod_id"], row["file_id"]):
                    raise JobError("mod_version_source_mismatch", "An NXM link does not identify its planned update.",
                                   {"mod": row["mod"]})
            download_only = options["download_only"]
            failed = list(plan["failed"])
            acquired, downloaded, updated = {}, [], []
            remaining = [row["mod"] for row in targets]

            def summary():
                return _summary(plan, downloaded, updated, failed, list(remaining), download_only)

            slots = {row["mod"]: [0, row["size_bytes"]] for row in targets}
            lock = threading.Lock()
            def download_progress(mod, done, total, _phase):
                with lock:
                    slots[mod] = [done, total or slots[mod][1]]
                    progress(sum(value[0] for value in slots.values()), sum(value[1] for value in slots.values()),
                             "Acquiring Quick Update archives")

            def acquire(row):
                try:
                    _checkpoint(control)
                    selection = per_mod.get(row["mod"], {})
                    archive = _archive(game, profile, row["domain"], row["mod_id"], row["file_id"], selection.get("archive_id"))
                    if archive is None:
                        result = download_runner(session, game.name, row["domain"], row["mod_id"], row["file_id"],
                                                 nxm_link=nxm_links.get(row["mod"]), cancel=control)(
                            log, lambda done, total, phase: download_progress(row["mod"], done, total, phase))
                        archive = _archive(game, profile, row["domain"], row["mod_id"], row["file_id"],
                                           downloaded=Path(result["archive"]["path"]))
                    _item, _paths, tokens = archive
                    download_progress(row["mod"], tokens[0][3], tokens[0][3], "archive ready")
                    return archive, None
                except JobCancelled:
                    return None, {"name": "job_cancelled", "message": "Update acquisition cancelled.", "details": {}}
                except Exception as exc:
                    return None, _error(exc)

            try:
                workers = min(32, max(1, int(load_collection_settings().get("max_concurrent", 8))))
            except (TypeError, ValueError):
                workers = 8
            if targets:
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="headless-quick-update") as pool:
                    futures = {pool.submit(acquire, row): row for row in targets}
                    errors = {}
                    for future in as_completed(futures):
                        row = futures[future]
                        archive, error = future.result()
                        if error is None:
                            acquired[row["mod"]] = archive
                        else:
                            errors[row["mod"]] = error
                for row in targets:
                    mod = row["mod"]
                    if mod in acquired:
                        item = acquired[mod][0]
                        downloaded.append({"mod": mod, "domain": row["domain"], "mod_id": row["mod_id"],
                                           "file_id": row["file_id"], "archive_id": item["id"],
                                           "name": item["name"], "path": item["path"], "size_bytes": item["size_bytes"]})
                    elif errors[mod]["name"] != "job_cancelled":
                        failed.append({"mod": mod, "phase": "acquire", "error": errors[mod]})
                        remaining.remove(mod)
            _checkpoint(control, summary())
            for index, row in enumerate(targets):
                mod = row["mod"]
                if mod not in acquired:
                    continue
                _checkpoint(control, summary())
                progress(index, len(targets), f"{'Verifying' if download_only else 'Installing'} Quick Update: {mod}")
                try:
                    context = _context(game, profile, mod, row["revision"], ignore_download_history=True)
                    _selected_target(game, context)
                    archive = _archive(game, profile, row["domain"], row["mod_id"], row["file_id"], acquired[mod][0]["id"])
                    if archive[1:] != acquired[mod][1:]:
                        raise JobError("mod_version_archive_changed", "The update archive changed after acquisition.")
                    if not download_only:
                        install_options = {**options["installer_options"], **per_mod.get(mod, {}), "archive_id": archive[0]["id"]}
                        result = change_runner(session, game, profile, mod, row["revision"], row["file_id"], install_options,
                                               operation_locked=True, ignore_download_history=True)(log, lambda done, total, phase: progress(
                            index + (done / total if total else 0), len(targets), f"Quick Update {mod}: {phase}"))
                        updated.append({"mod": mod, "result": result})
                    remaining.remove(mod)
                except Exception as exc:
                    error = _error(exc)
                    failed.append({"mod": mod, "phase": "verify" if download_only else "install", "error": error})
                    remaining.remove(mod)
                    if error["details"].get("state_persisted") or error["name"] in {
                        "install_rollback_failed", "mod_version_rollback_failed", "mod_version_change_failed",
                    }:
                        break
            _checkpoint(control, summary())
            result = summary()
            progress(len(targets) - len(remaining), len(targets), "Quick Update finished")
            if failed:
                raise JobError("quick_update_incomplete", "Some mods could not be quick-updated.",
                               {"quick_update": result, "state_persisted": bool(updated) or any(
                                   item["error"]["details"].get("state_persisted") for item in failed)})
            return {"quick_update": result}
    return run
