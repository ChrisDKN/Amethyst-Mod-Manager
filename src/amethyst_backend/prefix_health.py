from __future__ import annotations

import copy
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

from .jobs import JobCancelled, JobError
from .wine_overrides import _selected_game as _wine_game


def _selected_game(game, profile):
    from .mod_properties import _record

    settings = _record(profile)["settings"]
    if (("prefix_path" in settings and not isinstance(settings["prefix_path"], str))
            or ("prefix_path_cleared" in settings and not isinstance(settings["prefix_path_cleared"], bool))):
        raise JobError("invalid_profile_state", "The profile's saved prefix choice is invalid.")
    return _wine_game(game, profile, prefix_fallback=False)


def _identity(path):
    if path is None:
        return None
    try:
        link = path.lstat()
        file = path.stat()
    except FileNotFoundError:
        return None
    return [[value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]
            for value in (link, file)]


def _state(game, profile):
    from Utils.wine.prefix import resolve_compat_data
    from Utils.wine.registry import normalize_pfx

    configured = game.get_prefix_path()
    prefix = normalize_pfx(Path(configured)) if configured else None
    paths = [profile / "profile_state.json", profile / "profile_settings.json"]
    saved = getattr(game, "_paths_file", None)
    if saved is not None:
        paths.append(Path(saved))
    if prefix is not None:
        paths.extend(prefix / name for name in ("user.reg", "system.reg", "userdef.reg"))
        paths.append(resolve_compat_data(prefix) / "config_info")
    return {
        "configuration": [str(game.get_game_path()), str(configured), str(prefix)],
        "prefix_identity": _identity(prefix),
        "files": {str(path): _identity(path) for path in paths},
    }


def _checkpoint(stop, repairs=None):
    if stop.is_set():
        raise JobCancelled("Prefix health operation cancelled.",
                           {"repairs": list(repairs or [])})


def _report(game, profile, check_proton, stop, log, progress):
    from Utils.wine.health import HealthStatus, run_prefix_health
    from Utils.wine.proton import resolve_proton_env
    from Utils.wine.repair import health_installers, prefix_usage
    from Utils.wine.registry import normalize_pfx

    check = lambda: _checkpoint(stop)
    check()
    before = _state(game, profile)
    configured = game.get_prefix_path()
    prefix = normalize_pfx(Path(configured)) if configured else None
    probe = copy.copy(game)
    if prefix is not None:
        probe._prefix_path = prefix
        probe.get_prefix_path = lambda: prefix
    script = None
    runner_error = None
    if check_proton:
        progress(0, 0, "Resolving the installed prefix runner")
        try:
            script, _env = resolve_proton_env(probe, log, read_only=True)
        except Exception as exc:
            runner_error = str(exc)
            log(f"Could not resolve the prefix runner: {exc}")
    check()
    progress(0, 0, "Checking prefix health")
    checks = run_prefix_health(probe, proton_script=script, check_proton=check_proton, checkpoint=check)
    installers = health_installers()
    rows = [{"id": row.check_id, "status": row.status.value, "label": row.label,
             "detail": row.detail, "fix_token": row.fix_token, "evidence": row.evidence,
             "fixable": row.status in (HealthStatus.MISSING, HealthStatus.WARN)
                        and row.fix_token in installers} for row in checks]
    pids = prefix_usage(prefix) if prefix is not None and prefix.is_dir() else None
    after = _state(game, profile)
    check()
    if before != after:
        raise JobError("stale_prefix_health", "Prefix or configuration changed during the check; scan again.")
    result = {
        "profile": profile.name, "scope": "prefix", "prefix_path": str(configured) if configured else None,
        "normalized_prefix": str(prefix) if prefix else None, "check_proton": check_proton,
        "runner_path": str(script) if script else None, "runner_error": runner_error,
        "checks": rows, "summary": {status.value: sum(row["status"] == status.value for row in rows)
                                   for status in HealthStatus},
        "repairable_checks": [row["id"] for row in rows if row["fixable"]],
        "process_scan_available": pids is not None, "prefix_pids": sorted(pids) if pids is not None else None,
    }
    result["revision"] = hashlib.sha256(json.dumps(
        [before, {key: value for key, value in result.items()
                  if key not in ("process_scan_available", "prefix_pids")}],
        sort_keys=True).encode()).hexdigest()
    return result


def _target(game):
    from Utils.wine.registry import normalize_pfx

    configured = game.get_prefix_path()
    prefix = normalize_pfx(Path(configured)) if configured else None
    info = prefix.stat() if prefix is not None and prefix.exists() else None
    return (str(game.get_game_path()), str(configured), str(prefix.resolve()) if prefix else None,
            (info.st_dev, info.st_ino) if info else None)


def _check_target(game, expected, profile, repairs):
    if _target(_selected_game(game, profile)) != expected:
        raise JobError("stale_prefix_health", "The configured game or prefix changed.",
                       {"repairs": list(repairs)})


def report_runner(game, profile, check_proton, stop):
    def run(log, progress):
        from Utils.operations import OperationCancelled, acquire_game_operation, acquire_resource_operation
        from Utils.wine.registry import normalize_pfx

        try:
            selected = _selected_game(game, profile)
            target = _target(selected)
            with acquire_game_operation(selected, "wine.prefix.health"), ExitStack() as locks:
                prefix = selected.get_prefix_path()
                if prefix is not None:
                    normalized = normalize_pfx(Path(prefix))
                    locks.enter_context(acquire_resource_operation(
                        f"wine-prefix:{normalized.resolve()}", "wine.prefix.health", cancel=stop))
                result = _report(selected, profile, check_proton, stop, log, progress)
                _check_target(game, target, profile, [])
                return result
        except OperationCancelled as exc:
            raise JobCancelled("Prefix health check cancelled.") from exc
        except (OSError, TypeError, ValueError) as exc:
            raise JobError("prefix_health_unavailable", str(exc)) from exc
    return run


def repair_runner(game, profile, check_proton, stop, revision, checks, all_checks):
    def run(log, progress):
        from Utils.operations import OperationCancelled
        from Utils.wine.health import HealthStatus, run_prefix_health
        from Utils.wine.repair import PrefixRepairError, health_installers, prefix_repair_operation

        repairs = []
        try:
            selected = _selected_game(game, profile)
            target = _target(selected)
            with prefix_repair_operation(selected, stop=stop) as prefix:
                before = _report(selected, profile, check_proton, stop, log, progress)
                if before["revision"] != revision:
                    raise JobError("stale_prefix_health", "Prefix health changed; scan again.",
                                   {"revision": before["revision"]})
                requested = before["repairable_checks"] if all_checks else checks
                available = {row["id"]: row for row in before["checks"]}
                invalid = [name for name in requested if name not in available or not available[name]["fixable"]]
                if invalid:
                    raise JobError("prefix_health_not_repairable", "Only the report's fixable checks can be repaired.",
                                   {"checks": invalid})
                installers = health_installers()
                installed = copy.copy(selected)
                installed._prefix_path = prefix
                installed.get_prefix_path = lambda: prefix
                for index, name in enumerate(requested):
                    _checkpoint(stop, repairs)
                    _check_target(game, target, profile, repairs)
                    progress(index, len(requested), f"Repairing: {available[name]['label']}")
                    current = {row.check_id: row for row in run_prefix_health(installed)}
                    row = current.get(name)
                    if row is not None and row.status is HealthStatus.OK:
                        repairs.append({"id": name, "status": "already_healthy", "attempted": False,
                                        "installer_succeeded": None, "check_status": row.status.value})
                        continue
                    token = available[name]["fix_token"]
                    if row is None or row.fix_token != token:
                        raise JobError("stale_prefix_health", "A selected repair is no longer applicable.",
                                       {"check": name, "repairs": list(repairs)})
                    log(f"Repairing {row.label}.")
                    error = None
                    try:
                        succeeded = bool(installers[token](installed, log))
                    except (JobCancelled, JobError):
                        raise
                    except Exception as exc:
                        succeeded, error = False, str(exc)
                        log(f"{row.label}: {exc}")
                    current = {item.check_id: item for item in run_prefix_health(installed)}
                    verified = current.get(name)
                    healthy = verified is not None and verified.status is HealthStatus.OK
                    outcome = {"id": name, "status": "repaired" if succeeded and healthy else "failed",
                               "attempted": True, "installer_succeeded": succeeded,
                               "check_status": verified.status.value if verified else "unknown",
                               "detail": verified.detail if verified else "The check is no longer available."}
                    if error is not None:
                        outcome["error"] = error
                    repairs.append(outcome)
                    if not succeeded or not healthy:
                        log(f"{row.label}: repair could not be verified; continuing with the remaining checks.")
                    _checkpoint(stop, repairs)
                _check_target(game, target, profile, repairs)
                after = _report(selected, profile, check_proton, stop, log, progress)
                progress(len(requested), len(requested), "Prefix health repair finished")
                return {"profile": profile.name, "prefix_path": str(prefix), "repairs": repairs,
                        "repaired": sum(row["status"] == "repaired" for row in repairs),
                        "failed": sum(row["status"] == "failed" for row in repairs),
                        "already_healthy": sum(row["status"] == "already_healthy" for row in repairs),
                        "no_op": not repairs, "health": after}
        except PrefixRepairError as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except OperationCancelled as exc:
            raise JobCancelled("Prefix repair cancelled.", {"repairs": repairs}) from exc
        except JobCancelled as exc:
            raise JobCancelled(str(exc), {**exc.details, "repairs": repairs}) from exc
        except JobError as exc:
            raise JobError(exc.name, str(exc), {**exc.details, "repairs": repairs}) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise JobError("prefix_repair_failed", str(exc), {"repairs": repairs}) from exc
    return run
