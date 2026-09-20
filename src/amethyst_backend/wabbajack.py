from __future__ import annotations

import copy
import hashlib
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from .jobs import JobCancelled, JobError


def package_summary(package):
    return {
        "package_path": str(package.path),
        "identity": package.identity,
        "name": package.name,
        "version": package.version,
        "game": package.game,
        "profiles": package.profiles,
        "selected_profile": package.selected_profile or None,
        "archive_count": len(package.archives),
        "archive_bytes": sum(
            archive.size for archive in package.archives.values()),
        "directive_count": len(package.directives),
    }


def _gallery_entry(entry_id, *, log=None):
    from Utils.wabbajack.gallery import load_gallery

    gallery = load_gallery(cached_only=True, log=log)
    entry = next((item for item in gallery.entries if item.id == entry_id), None)
    if entry is None:
        raise JobError("wabbajack_gallery_entry_not_found",
                       "The gallery entry was not found in cached feeds.",
                       {"entry_id": entry_id})
    return entry


def _gallery_package_path(entry):
    from Utils.wabbajack.gallery import cache_root

    return (cache_root() /
            (hashlib.sha256(entry.download.encode()).hexdigest() + ".wabbajack"))


def run_gallery(game, refresh, cached_only, stop, log, progress):
    from Utils.wabbajack.gallery import load_gallery
    from Utils.wabbajack.games import matches_game

    try:
        progress(0, 0, "Loading Wabbajack gallery")
        result = load_gallery(refresh=refresh, cached_only=cached_only,
                              stop=stop, log=log)
        if stop.is_set():
            raise JobCancelled("Gallery loading cancelled.")
        entries = [asdict(entry) for entry in result.entries
                   if game is None or matches_game(game, entry.game)]
        progress(len(entries), len(entries), "Gallery loaded")
        return {"entries": entries, "warnings": result.warnings,
                "cached": result.cached}
    except InterruptedError as exc:
        raise JobCancelled("Gallery loading cancelled.") from exc
    except JobCancelled:
        raise
    except Exception as exc:
        raise JobError("wabbajack_gallery_failed",
                       str(exc) or type(exc).__name__,
                       {"exception": type(exc).__name__}) from exc


def download_gallery_package(entry_id, expected_version, stop, log, progress):
    from Utils.wabbajack.acquire import download_package
    from Utils.wabbajack.gallery import load_gallery
    from Utils.wabbajack.manifest import inspect_package

    try:
        progress(0, 0, "Loading cached Wabbajack gallery")
        gallery = load_gallery(cached_only=True, stop=stop, log=log)
        if stop.is_set():
            raise JobCancelled("Package download cancelled.")
        entry = next((item for item in gallery.entries if item.id == entry_id), None)
        if entry is None:
            raise JobError("wabbajack_gallery_entry_not_found",
                           "The gallery entry was not found in cached feeds.",
                           {"entry_id": entry_id})
        if entry.unavailable or not entry.download:
            raise JobError("wabbajack_package_unavailable",
                           "This gallery entry has no available package download.",
                           {"entry_id": entry_id})
        if expected_version is not None and entry.version != expected_version:
            raise JobError("wabbajack_gallery_entry_changed",
                           "The gallery entry version changed since selection.",
                           {"entry_id": entry_id, "version": entry.version})
        if urlparse(entry.download).scheme != "https":
            raise JobError("wabbajack_package_unavailable",
                           "Gallery package downloads must use HTTPS.",
                           {"entry_id": entry_id})
        target = _gallery_package_path(entry)
        progress(0, entry.package_size, "Downloading Wabbajack package")
        download_package(
            entry.download, target, stop=stop, size=entry.package_size,
            expected=entry.package_hash,
            progress=lambda current, total:
                progress(current, total, "Downloading Wabbajack package"),
            log=log,
        )
        if stop.is_set():
            raise JobCancelled("Package download cancelled.")
        progress(0, 1, "Inspecting Wabbajack package")
        package = inspect_package(target, log=log)
        progress(1, 1, "Package inspected")
        return {"gallery_id": entry_id, "gallery_version": entry.version,
                **package_summary(package)}
    except InterruptedError as exc:
        raise JobCancelled("Package download cancelled.") from exc
    except (JobCancelled, JobError):
        raise
    except Exception as exc:
        raise JobError("wabbajack_package_download_failed",
                       str(exc) or type(exc).__name__,
                       {"entry_id": entry_id,
                        "exception": type(exc).__name__}) from exc


def prepare_request(game, options, *, log=None):
    from Utils.config_paths import get_download_cache_dir_for_game
    from Utils.wabbajack.games import source_roots
    from Utils.wabbajack.installed import installation_by_id
    from Utils.wabbajack.manifest import inspect_package
    from Utils.wabbajack.models import InstallRequest
    from Utils.wabbajack.store import installation_info

    package = inspect_package(Path(options["package_path"]), log=log)
    expected = options.get("expected_identity")
    if expected and package.identity != expected:
        raise JobError("wabbajack_package_changed",
                       "The Wabbajack package changed after inspection.",
                       {"expected_identity": expected,
                        "actual_identity": package.identity})
    mode = options["mode"]
    if mode == "install":
        directory = (Path(game.get_profile_root()) / ".wabbajack"
                     / options["installation_name"])
        info = None
    else:
        directory = installation_by_id(game, options["installation_id"])
        info = installation_info(directory, log)
        if not info or str(info.get("id") or "") != options["installation_id"]:
            raise JobError("wabbajack_installation_changed",
                           "The managed installation changed before preflight.")

    profiles = options.get("profiles")
    if profiles is None:
        profiles = list(info.get("selected_profiles") or []) if info else []
    fixes = options.get("fixes")
    if fixes is None:
        fixes = list(info.get("fixes") or []) if info else []
    setup_options = options.get("setup_options")
    if setup_options is None:
        setup_options = dict(info.get("setup_options") or {}) if info else {}
    downloads = options.get("downloads_path") or (
        info.get("downloads") if info else None) or get_download_cache_dir_for_game(game.name)
    roots = source_roots(package, game)
    if info:
        for name, path in info.get("source_roots", {}).items():
            if name not in roots or not roots[name].is_dir():
                roots[name] = Path(path)
    roots.update(options.get("game_roots") or {})

    gallery_id = options.get("gallery_id") or (
        str(info.get("gallery_id") or "") if info else "")
    gallery_metadata = dict(info.get("gallery_metadata") or {}) if info else {}
    if options.get("gallery_id"):
        from Utils.wabbajack.hashes import canonical_hash, file_hash

        entry = _gallery_entry(gallery_id, log=log)
        cached_package = _gallery_package_path(entry).resolve() == package.path.resolve()
        if (not cached_package
                and (not info or str(info.get("gallery_id") or "") != gallery_id)):
            raise JobError("wabbajack_gallery_package_mismatch",
                           "The package path does not match the selected gallery entry.",
                           {"gallery_id": gallery_id})
        if cached_package and (
            entry.package_size and package.path.stat().st_size != entry.package_size
            or entry.package_hash and file_hash(package.path) != canonical_hash(entry.package_hash)
        ):
            raise JobError("wabbajack_gallery_package_mismatch",
                           "The package does not match the gallery's size or hash.",
                           {"gallery_id": gallery_id})
        gallery_metadata = {key: getattr(entry, key) for key in (
            "author", "image", "readme", "community", "download", "nsfw", "tags")}

    return InstallRequest(
        package, copy.copy(game), directory, downloads, profiles, roots,
        mode=mode, gallery_id=gallery_id,
        gallery_metadata=gallery_metadata,
        fixes=fixes, setup_options=setup_options,
        clear_archives=options.get("clear_archives"),
    )


def _preflight(game, options, session, stop, log, progress):
    from Utils.ui.config import load_force_manual_install, load_nexus_last_premium
    from Utils.wabbajack.preflight import preflight

    progress(0, 0, "Inspecting Wabbajack package")
    request = prepare_request(game, options, log=log)
    if stop.is_set():
        raise JobCancelled("Wabbajack operation cancelled.")
    try:
        request.api = session.require_api()
    except Exception as exc:
        log(f"Nexus account unavailable for automatic downloads: {exc}")
    if request.api:
        try:
            request.premium = bool(request.api.validate().is_premium)
        except Exception as exc:
            log(f"Nexus account validation failed: {exc}")
            request.premium = bool(load_nexus_last_premium())
    if load_force_manual_install():
        request.premium = False
    report = preflight(
        request, stop, log=log,
        progress=lambda phase, done, total, detail:
            progress(done, total, phase + (f": {detail}" if detail else "")),
    )
    if stop.is_set():
        raise JobCancelled("Wabbajack operation cancelled.")
    return request, report


def _known_issues(package):
    from Utils.wabbajack.broken_lists import matching_broken_lists
    return [{"id": item.id, "reason": item.reason,
             "recovery": item.recovery, "links": list(item.links)}
            for item in matching_broken_lists(package)]


def _manual_archives(request, report):
    from Utils.wabbajack.games import nexus_domain
    from Utils.wabbajack.hosts import source_url

    manual_keys = set(report.manual_archive_keys)
    result = []
    for archive in request.package.archives.values():
        if archive.key not in manual_keys or archive.key in report.cached:
            continue
        url = source_url(archive)
        if archive.kind == "Nexus":
            state = archive.state
            try:
                domain = nexus_domain(state.get("GameName", state.get("Game", request.package.game)))
                mod_id = int(state["ModID"])
                file_id = int(state["FileID"])
                url = f"https://www.nexusmods.com/{domain}/mods/{mod_id}?tab=files&file_id={file_id}"
            except (KeyError, TypeError, ValueError):
                pass
        result.append({"hash": archive.key, "name": archive.name,
                       "size": archive.size, "source": archive.kind,
                       "url": url or None,
                       "instructions": str(archive.state.get("Prompt") or "")})
    return result


def run_preflight(game, options, session, stop, log, progress):
    try:
        request, report = _preflight(game, options, session, stop, log, progress)
        checks = [asdict(check) for check in report.checks]
        for check in checks:
            if check["name"] == "Manual download":
                check["resolution"] = (
                    "Add the exact archive to the shared download cache, then run "
                    "preflight again. Amethyst verifies its size and hash.")
        return {
            "ok": report.ok,
            "package_identity": request.package.identity,
            "package_name": request.package.name,
            "package_version": request.package.version,
            "gallery_id": request.gallery_id or None,
            "mode": request.mode,
            "directory": str(request.directory),
            "downloads": str(request.downloads),
            "profiles": request.profiles,
            "fixes": request.fixes,
            "setup_options": request.setup_options,
            "clear_archives": request.clear_archives,
            "game_roots": {name: str(path) for name, path in request.game_roots.items()},
            "premium": request.premium,
            "checks": checks,
            "manual_archives": _manual_archives(request, report),
            "known_issues": _known_issues(request.package),
            "setup_tasks": [asdict(task) for task in report.setup_tasks],
            "required_archive_count": len(report.required_archives or []),
            "cached_archive_count": len(report.cached),
            "game_file_count": len(report.game_files),
            "prepared_game_file_count": len(report.prepared_game_files),
            "download_bytes": report.download_bytes,
            "install_bytes": report.install_bytes,
            "archive_budget_bytes": report.archive_budget_bytes,
            "install_work": report.install_work,
            "timings": report.timings,
        }
    except InterruptedError as exc:
        raise JobCancelled("Preflight cancelled.") from exc
    except JobCancelled:
        raise
    except JobError:
        raise
    except Exception as exc:
        raise JobError("wabbajack_preflight_failed",
                       str(exc) or type(exc).__name__,
                       {"exception": type(exc).__name__}) from exc


def import_manual_archive(game, package_path, expected_identity, archive_hash,
                          source_path, downloads_path, stop, log, progress):
    from Utils.config_paths import get_download_cache_dir_for_game
    from Utils.operations import OperationBusy, acquire_game_operation
    from Utils.wabbajack.acquire import ArchiveCacheIndex
    from Utils.wabbajack.hashes import canonical_hash, verify_file
    from Utils.wabbajack.manifest import inspect_package
    from Utils.wabbajack.paths import WabbajackError, auxiliary_path, cache_path
    from Utils.wabbajack.store import Store

    try:
        with acquire_game_operation(game, "headless.wabbajack.archive.import"):
            progress(0, 0, "Inspecting Wabbajack package")
            package = inspect_package(package_path, log=log)
            if stop.is_set():
                raise JobCancelled("Archive import cancelled.")
            if expected_identity and package.identity != expected_identity:
                raise JobError("wabbajack_package_changed",
                               "The Wabbajack package changed after inspection.",
                               {"expected_identity": expected_identity,
                                "actual_identity": package.identity})
            try:
                key = canonical_hash(archive_hash)
            except Exception as exc:
                raise JobError("wabbajack_archive_hash_invalid",
                               "The archive hash is invalid.") from exc
            archive = package.archives.get(key)
            if archive is None or archive.kind == "GameFileSource":
                raise JobError("wabbajack_archive_not_found",
                               "The package does not require this downloadable archive.",
                               {"hash": key})
            if not source_path.is_file() or source_path.stat().st_size != archive.size:
                raise JobError("wabbajack_archive_source_invalid",
                               "The selected file is unavailable or has the wrong size.",
                               {"source_path": str(source_path),
                                "expected_size": archive.size})
            downloads = (downloads_path or get_download_cache_dir_for_game(game.name))
            downloads.mkdir(parents=True, exist_ok=True)
            progress(0, archive.size, "Checking cached archives")
            cache = ArchiveCacheIndex([downloads], {archive.size}, log=log)
            cache.refresh(stop, force=True)
            for path in cache.candidates(archive.size):
                if (path.is_symlink()
                        or auxiliary_path(path, ".wabbajack-owned").exists()):
                    continue
                try:
                    matches = verify_file(path, key, archive.size, stop)
                except InterruptedError:
                    raise
                except (OSError, WabbajackError):
                    continue
                if matches:
                    if (source_path.resolve() != path.resolve()
                            and not verify_file(source_path, key, archive.size, stop)):
                        raise JobError("wabbajack_archive_source_invalid",
                                       "The selected file has the wrong archive hash.",
                                       {"source_path": str(source_path),
                                        "expected_hash": key})
                    progress(archive.size, archive.size, "Archive already cached")
                    return {"package_identity": package.identity, "hash": key,
                            "name": archive.name, "size": archive.size,
                            "cache_path": str(path), "already_cached": True}
            target = cache_path(downloads, uuid.uuid4().hex, archive.name)
            while target.exists() or target.is_symlink():
                target = cache_path(downloads, uuid.uuid4().hex, archive.name)
            progress(0, archive.size, "Importing archive")
            Store._copy(
                source_path, target, stop=stop, expected=key,
                progress=lambda done, total: progress(done, total, "Importing archive"),
            )
            if stop.is_set():
                raise JobCancelled("Archive import cancelled.")
            progress(archive.size, archive.size, "Archive imported")
            return {"package_identity": package.identity, "hash": key,
                    "name": archive.name, "size": archive.size,
                    "cache_path": str(target), "already_cached": False}
    except InterruptedError as exc:
        raise JobCancelled("Archive import cancelled.") from exc
    except (JobError, JobCancelled, OperationBusy):
        raise
    except Exception as exc:
        raise JobError("wabbajack_archive_import_failed",
                       str(exc) or type(exc).__name__,
                       {"exception": type(exc).__name__}) from exc


def run_install_job(game, options, session, control, log, progress):
    from Utils.downloads.install import InstallCallbacks
    from Utils.operations import OperationBusy
    from Utils.wabbajack.install import run_install
    from Utils.wabbajack.paths import WabbajackError
    from Utils.wabbajack.store import installation_info

    missing_manual = []
    try:
        request, report = _preflight(
            game, options, session, control.stop, log, progress)
        if not report.ok:
            checks = [{"name": item.name, "detail": item.detail}
                      for item in report.checks if item.status == "error"]
            raise JobError("wabbajack_requirements_failed",
                           "Wabbajack requirements are not satisfied.",
                           {"checks": checks})
        manual = [item.detail for item in report.checks
                  if item.status == "manual" and item.name == "Manual download"]
        if manual:
            raise JobError("wabbajack_manual_download_required",
                           "Required archives must be added to the download cache first.",
                           {"archives": manual,
                            "manual_archives": _manual_archives(request, report)})
        issues = _known_issues(request.package)
        if issues and not options.get("accept_known_issues", False):
            raise JobError("wabbajack_known_issues",
                           "Known list issues require explicit acceptance.",
                           {"issues": issues})

        policy = options.get("conflict_policy", "reject")

        def resolve_conflicts(conflicts):
            if policy == "reject":
                raise WabbajackError(
                    f"{len(conflicts)} local changes require conflict_policy keep or author")
            return {item.path: policy for item in conflicts}

        def manual_required(payload):
            missing_manual.append({key: payload.get(key) for key in (
                "name", "hash", "size", "url", "source", "instructions")})
            control.stop.set()
            raise WabbajackError(
                f"Manual archive required: {payload['name']}. Add it to the download cache and resume.")

        request.resolve_conflicts = resolve_conflicts
        callbacks = InstallCallbacks(
            on_log=log,
            on_phase=lambda phase, done, total, detail:
                progress(done, total, phase + (f": {detail}" if detail else "")),
            on_manual_mod=manual_required,
        )
        result = run_install(request, callbacks=callbacks, control=control,
                             report=report)
        if missing_manual:
            raise JobError("wabbajack_manual_download_required",
                           "Required archives must be added to the download cache first.",
                           {"archives": list(dict.fromkeys(
                               item["name"] for item in missing_manual)),
                            "manual_archives": missing_manual})
        if result.status == "cancelled":
            raise JobCancelled("Wabbajack installation cancelled.")
        if result.status != "complete":
            raise JobError("wabbajack_install_incomplete", result.message,
                           {"status": result.status})
        info = installation_info(request.directory, log)
        if not info or not info.get("id"):
            raise JobError("wabbajack_install_state_unavailable",
                           "The completed installation record could not be read.")
        return {
            "status": result.status,
            "installation_id": str(info.get("id") or ""),
            "directory": str(request.directory),
            "profiles": [str(path) for path in result.profiles],
            "selected_profile": result.selected_profile,
            "installed_files": result.installed,
            "message": result.message,
            "remaining_instructions": list(info.get("remaining_instructions") or []),
        }
    except InterruptedError as exc:
        if missing_manual:
            raise JobError("wabbajack_manual_download_required",
                           "Required archives must be added to the download cache first.",
                           {"archives": list(dict.fromkeys(
                               item["name"] for item in missing_manual)),
                            "manual_archives": missing_manual}) from exc
        if control.stop.is_set():
            raise JobCancelled("Wabbajack installation cancelled.") from exc
        raise JobError("wabbajack_install_failed", str(exc)) from exc
    except (JobCancelled, JobError, OperationBusy):
        raise
    except Exception as exc:
        if missing_manual:
            raise JobError("wabbajack_manual_download_required",
                           "Required archives must be added to the download cache first.",
                           {"archives": list(dict.fromkeys(
                               item["name"] for item in missing_manual)),
                            "manual_archives": missing_manual}) from exc
        raise JobError("wabbajack_install_failed",
                       str(exc) or type(exc).__name__,
                       {"exception": type(exc).__name__}) from exc
