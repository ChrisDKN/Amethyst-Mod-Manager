from __future__ import annotations

import copy
from dataclasses import asdict
from pathlib import Path

from .jobs import JobCancelled, JobError


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

    return InstallRequest(
        package, copy.copy(game), directory, downloads, profiles, roots,
        mode=mode, gallery_id=str(info.get("gallery_id") or "") if info else "",
        gallery_metadata=dict(info.get("gallery_metadata") or {}) if info else {},
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
                           {"archives": manual})
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
            missing_manual.append(payload["name"])
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
                           {"archives": list(dict.fromkeys(missing_manual))})
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
                           {"archives": list(dict.fromkeys(missing_manual))}) from exc
        if control.stop.is_set():
            raise JobCancelled("Wabbajack installation cancelled.") from exc
        raise JobError("wabbajack_install_failed", str(exc)) from exc
    except (JobCancelled, JobError, OperationBusy):
        raise
    except Exception as exc:
        if missing_manual:
            raise JobError("wabbajack_manual_download_required",
                           "Required archives must be added to the download cache first.",
                           {"archives": list(dict.fromkeys(missing_manual))}) from exc
        raise JobError("wabbajack_install_failed",
                       str(exc) or type(exc).__name__,
                       {"exception": type(exc).__name__}) from exc
