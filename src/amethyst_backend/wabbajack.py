from __future__ import annotations

import copy
from dataclasses import asdict
from pathlib import Path

from .jobs import JobCancelled, JobError


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


def run_preflight(game, options, session, stop, log, progress):
    from Utils.ui.config import load_force_manual_install, load_nexus_last_premium
    from Utils.wabbajack.preflight import preflight

    try:
        progress(0, 0, "Inspecting Wabbajack package")
        request = prepare_request(game, options, log=log)
        if stop.is_set():
            raise JobCancelled("Preflight cancelled.")
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
            raise JobCancelled("Preflight cancelled.")
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
            "game_roots": {name: str(path) for name, path in request.game_roots.items()},
            "premium": request.premium,
            "checks": [asdict(check) for check in report.checks],
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
