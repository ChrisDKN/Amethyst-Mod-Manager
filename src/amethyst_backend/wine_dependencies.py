from __future__ import annotations

import copy
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

from .jobs import JobCancelled, JobError
from .prefix_health import _identity, _selected_game, _state, _target


def _checkpoint(stop, installations=()):
    if stop.is_set():
        raise JobCancelled("Dependency operation cancelled.", {"installations": list(installations)})


def _configuration(game, profile):
    paths = [profile / "profile_state.json", profile / "profile_settings.json"]
    saved = getattr(game, "_paths_file", None)
    if saved is not None:
        paths.append(Path(saved))
    return [_target(game), {str(path): _identity(path) for path in paths}]


def _check_configuration(game, profile, expected):
    try:
        current = _configuration(_selected_game(game, profile), profile)
    except JobError as exc:
        raise JobError("stale_wine_dependencies", "The selected configuration is no longer available.") from exc
    if current != expected:
        raise JobError("stale_wine_dependencies", "The configured game, prefix, or profile changed; list dependencies again.")


def _initialized(prefix):
    return bool(prefix and prefix.is_dir() and (prefix / "drive_c").is_dir()
                and all((prefix / name).is_file() for name in ("user.reg", "system.reg")))


def _row(token, prefix, initialized, declared):
    from Utils.wine.health import COMPONENT_SPECS, HealthStatus, check_component
    from Utils.wine.protontricks import read_installed_deps, winetricks_verb_dep_key

    spec = COMPONENT_SPECS.get(token)
    row = {"id": token, "label": spec.label if spec else "XAudio2 / XACT (32-bit and 64-bit)",
           "declared": token in declared, "installable": initialized,
           "verification": "live" if spec else "installer_marker"}
    check = check_component(token, prefix) if initialized and spec else None
    if check is not None:
        row.update(status=check.status.value, detail=check.detail, evidence=check.evidence)
    elif initialized and token == "xact":
        markers = read_installed_deps(prefix)
        evidence = {verb: winetricks_verb_dep_key(verb) in markers for verb in ("xact", "xact_x64")}
        ready = all(evidence.values())
        row.update(status="ok" if ready else "missing",
                   detail="Both XACT installers are recorded as completed." if ready else
                          "One or both XACT installers have no completion marker.", evidence=evidence)
    else:
        row.update(status="unknown", detail="The selected prefix is not initialized.", evidence={})
    row["installed"] = row["status"] == HealthStatus.OK.value
    return row


def _report(game, profile, stop, progress):
    from Utils.wine.protontricks import _deps_file
    from Utils.wine.registry import normalize_pfx
    from Utils.wine.repair import dependency_installers, prefix_usage

    def state():
        snapshot = _state(game, profile)
        if prefix is not None:
            marker = _deps_file(prefix)
            snapshot["files"][str(marker)] = _identity(marker)
        return snapshot

    _checkpoint(stop)
    configured = game.get_prefix_path()
    prefix = normalize_pfx(Path(configured)) if configured else None
    before = state()
    initialized = _initialized(prefix)
    declared = set(getattr(game, "auto_install_deps", ()) or ()) | set(
        getattr(game, "prefix_health_extras", ()) or ())
    tokens = sorted(dependency_installers())
    rows = []
    for index, token in enumerate(tokens):
        _checkpoint(stop)
        progress(index, len(tokens), f"Checking dependency: {token}")
        rows.append(_row(token, prefix, initialized, declared))
    pids = prefix_usage(prefix) if initialized else None
    if before != state():
        raise JobError("stale_wine_dependencies", "Prefix state changed while listing dependencies; scan again.")
    _checkpoint(stop)
    result = {"profile": profile.name, "scope": "prefix",
              "prefix_path": str(configured) if configured else None,
              "normalized_prefix": str(prefix) if prefix else None,
              "initialized": initialized, "dependencies": rows,
              "process_scan_available": pids is not None,
              "prefix_pids": sorted(pids) if pids is not None else None}
    result["revision"] = hashlib.sha256(json.dumps(
        [before, {key: value for key, value in result.items()
                  if key not in ("process_scan_available", "prefix_pids")}], sort_keys=True).encode()).hexdigest()
    progress(len(tokens), len(tokens), "Dependency report finished")
    return result


def report_runner(game, profile, stop):
    def run(log, progress):
        from Utils.operations import OperationCancelled, acquire_game_operation, acquire_resource_operation
        from Utils.wine.registry import normalize_pfx

        try:
            selected = _selected_game(game, profile)
            expected = _configuration(selected, profile)
            with acquire_game_operation(selected, "wine.dependencies.list"), ExitStack() as locks:
                configured = selected.get_prefix_path()
                if configured is not None:
                    prefix = normalize_pfx(Path(configured))
                    locks.enter_context(acquire_resource_operation(
                        f"wine-prefix:{prefix.resolve()}", "wine.dependencies.list", cancel=stop))
                result = _report(selected, profile, stop, progress)
                _check_configuration(game, profile, expected)
                return result
        except OperationCancelled as exc:
            raise JobCancelled("Dependency report cancelled.") from exc
        except (OSError, TypeError, ValueError) as exc:
            raise JobError("wine_dependencies_unavailable", str(exc)) from exc
    return run


def install_runner(game, profile, stop, revision, dependencies):
    def run(log, progress):
        from Utils.operations import OperationCancelled
        from Utils.wine.repair import (
            PrefixRepairError, dependency_installers, prefix_repair_operation, prefix_usage,
        )

        installations = []
        try:
            selected = _selected_game(game, profile)
            if not selected.is_configured():
                raise JobError("game_not_configured", "The configured game or staging path is unavailable.")
            expected = _configuration(selected, profile)
            _checkpoint(stop)
            with prefix_repair_operation(selected, stop=stop, operation="wine.dependencies.install") as prefix:
                before = _report(selected, profile, stop, progress)
                _check_configuration(game, profile, expected)
                if before["revision"] != revision:
                    raise JobError("stale_wine_dependencies", "The dependency report changed; list dependencies again.",
                                   {"revision": before["revision"]})
                available = {row["id"]: row for row in before["dependencies"]}
                invalid = [token for token in dependencies if token not in available or not available[token]["installable"]]
                if invalid:
                    raise JobError("wine_dependency_unavailable", "Select installable dependencies from the report.",
                                   {"dependencies": invalid})
                installers = dependency_installers()
                installed = copy.copy(selected)
                installed._prefix_path = prefix
                installed.get_prefix_path = lambda: prefix
                declared = {token for token, row in available.items() if row["declared"]}
                for index, token in enumerate(dependencies):
                    _checkpoint(stop, installations)
                    _check_configuration(game, profile, expected)
                    if not _initialized(prefix):
                        raise JobError("wine_prefix_unavailable", "The selected prefix is no longer initialized.")
                    current = _row(token, prefix, True, declared)
                    progress(index, len(dependencies), f"Installing: {current['label']}")
                    if current["installed"]:
                        installations.append({"id": token, "status": "already_installed", "attempted": False,
                                              "installer_succeeded": None, "dependency": current})
                        continue
                    pids = prefix_usage(prefix)
                    if pids is None:
                        raise JobError("wine_prefix_scan_unavailable", "Could not check whether the prefix is in use.")
                    if pids:
                        raise JobError("wine_prefix_in_use", "Close processes using this prefix first.",
                                       {"pids": sorted(pids)})
                    outcome = {"id": token, "status": "failed", "attempted": True,
                               "installer_succeeded": False}
                    installations.append(outcome)
                    try:
                        outcome["installer_succeeded"] = bool(installers[token](installed, log))
                    except (JobCancelled, JobError):
                        raise
                    except Exception as exc:
                        outcome["error"] = str(exc)
                        log(f"{current['label']}: {exc}")
                    _check_configuration(game, profile, expected)
                    verified = _row(token, prefix, _initialized(prefix), declared)
                    outcome["dependency"] = verified
                    if outcome["installer_succeeded"] and verified["installed"]:
                        outcome["status"] = "installed"
                    else:
                        log(f"{current['label']}: installation could not be verified.")
                    _checkpoint(stop, installations)
                _check_configuration(game, profile, expected)
                after = _report(selected, profile, stop, progress)
                progress(len(dependencies), len(dependencies), "Dependency installation finished")
                return {"profile": profile.name, "prefix_path": str(prefix), "installations": installations,
                        "installed": sum(row["status"] == "installed" for row in installations),
                        "failed": sum(row["status"] == "failed" for row in installations),
                        "already_installed": sum(row["status"] == "already_installed" for row in installations),
                        "report": after}
        except PrefixRepairError as exc:
            name = "stale_wine_dependencies" if exc.name == "stale_prefix_health" else exc.name
            raise JobError(name, str(exc), {**exc.details, "installations": installations}) from exc
        except (OperationCancelled, JobCancelled) as exc:
            raise JobCancelled("Dependency installation cancelled.", {"installations": installations}) from exc
        except JobError as exc:
            raise JobError(exc.name, str(exc), {**exc.details, "installations": installations}) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise JobError("wine_dependency_install_failed", str(exc), {"installations": installations}) from exc
    return run
