from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

from .jobs import JobCancelled, JobError
from .prefix_health import _selected_game, _state, _target
from .xedit_run import _identity


def _checkpoint(cancel):
    if cancel.is_set():
        raise JobCancelled("Game registry inspection cancelled.")


def _report(game, profile, cancel, log, *, target=None):
    from Utils.bethesda.registry import _marker_path, registration_state
    from Utils.profiles.state import read_only_profile_state
    from Utils.wine.prefix import resolve_compat_data
    from Utils.wine.proton import resolve_proton_env
    from Utils.wine.registry import normalize_pfx
    from Utils.wine.repair import prefix_usage

    with read_only_profile_state():
        _checkpoint(cancel)
        selected = _selected_game(game, profile)
        if target is not None and _target(selected) != target:
            raise JobError("stale_game_registry", "The configured game or prefix changed while waiting.")
        name = getattr(selected, "synthesis_registry_name", None)
        if not isinstance(name, str) or not name:
            raise JobError("game_registry_unavailable", "This game has no Bethesda registry registration.")
        game_path = selected.get_game_path()
        if game_path is None or not game_path.is_dir():
            raise JobError("game_not_configured", "Configure the game before inspecting its registry registration.")
        configured = selected.get_prefix_path()
        prefix = normalize_pfx(Path(configured)) if configured else None
        before = _state(selected, profile)
        state = registration_state(prefix, name, game_path) if prefix else None
        initialized = bool(prefix and (prefix / "drive_c").is_dir()
                           and all((prefix / n).is_file() for n in ("user.reg", "system.reg")))
        script, env = resolve_proton_env(selected, log, read_only=True) if initialized else (None, None)
        pids = prefix_usage(prefix) if initialized else None
        compat = prefix if script and Path(script).name in ("wine", "wine64") else resolve_compat_data(prefix) if prefix else None
        marker = _marker_path(compat, name) if compat else None
        snapshot = {"state": before, "game_path_identity": _identity(game_path), "registry_name": name,
                    "runner": [str(script), _identity(Path(script))] if script else None,
                    "runner_environment": {key: env.get(key) for key in (
                        "WINEPREFIX", "STEAM_COMPAT_DATA_PATH", "STEAM_COMPAT_CLIENT_INSTALL_PATH",
                        "STEAM_COMPAT_INSTALL_PATH", "SteamAppId", "SteamGameId")} if env is not None else None,
                    "marker": [str(marker), _identity(marker)] if marker else None,
                    "registration": state}
        if before != _state(_selected_game(game, profile), profile):
            raise JobError("stale_game_registry", "The configured game or prefix changed during inspection.")
        _checkpoint(cancel)
        return {"revision": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
                "game": str(game.game_id), "profile": profile.name, "registry_name": name,
                "game_path": str(game_path), "prefix_path": str(prefix) if prefix else None,
                "runner_path": str(script) if script else None, "initialized": initialized,
                "process_scan_available": pids is not None, "prefix_pids": sorted(pids) if pids is not None else None,
                "can_write": bool(initialized and script and Path(script).is_file() and env is not None and pids == set()),
                "registration": state, "marker_path": str(marker) if marker else None}, selected, script, env


def inspect_runner(game, profile, cancel):
    def run(log, progress):
        from Utils.operations import OperationBusy, OperationCancelled, acquire_game_operation, acquire_resource_operation
        from Utils.profiles.state import read_only_profile_state
        from Utils.wine.registry import normalize_pfx

        try:
            with read_only_profile_state():
                selected = _selected_game(game, profile)
                target = _target(selected)
                with acquire_game_operation(selected, "wine.game_registry.inspect"), ExitStack() as locks:
                    prefix = selected.get_prefix_path()
                    if prefix is not None:
                        locks.enter_context(acquire_resource_operation(f"wine-prefix:{normalize_pfx(prefix).resolve()}",
                                                                        "wine.game_registry.inspect", cancel=cancel))
                    progress(0, 1, "inspect_game_registry")
                    result, _selected, _script, _env = _report(game, profile, cancel, log, target=target)
                    progress(1, 1, "inspect_game_registry")
                    return result
        except OperationBusy as exc:
            raise JobError("operation_busy", str(exc), exc.details) from exc
        except OperationCancelled as exc:
            raise JobCancelled("Game registry inspection cancelled.") from exc
        except OSError as exc:
            raise JobError("game_registry_unavailable", "Could not read the game registry.", {"reason": str(exc)}) from exc
    return run


def write_runner(game, profile, revision, force, cancel):
    def run(log, progress):
        from Utils.bethesda.registration import write_game_registration
        from Utils.bethesda.registry import RegistryRegistrationCancelled, RegistryRegistrationError
        from Utils.deployment.locking import DeploymentBusy
        from Utils.operations import OperationBusy, OperationCancelled
        from Utils.wine.repair import PrefixRepairError

        try:
            initial, selected, script, env = _report(game, profile, cancel, log)
            if initial["revision"] != revision:
                raise JobError("stale_game_registry", "Registry, prefix, game or runner changed; inspect again.")
            if not initial["initialized"]:
                raise JobError("wine_prefix_unavailable", "The selected prefix is not initialized.")
            if script is None or env is None or not Path(script).is_file():
                raise JobError("game_registry_runner_unavailable", "The configured prefix has no available Wine/Proton runner.")
            binding = _target(selected)
            runner_identity = _identity(Path(script))
            game_identity = _identity(Path(initial["game_path"]))
            config_paths = [profile / "profile_state.json", profile / "profile_settings.json"]
            if getattr(selected, "_paths_file", None) is not None:
                config_paths.append(Path(selected._paths_file))
            configuration = {str(path): _identity(path) for path in config_paths}

            def check():
                latest = _selected_game(game, profile)
                if (_target(latest) != binding or _identity(Path(script)) != runner_identity
                        or _identity(Path(initial["game_path"])) != game_identity
                        or {str(path): _identity(path) for path in config_paths} != configuration):
                    raise JobError("stale_game_registry", "The configured target or runner changed during registration.")

            def verify():
                if _report(game, profile, cancel, log)[0]["revision"] != revision:
                    raise JobError("stale_game_registry", "Registry, prefix, game or runner changed; inspect again.")

            result = write_game_registration(selected, Path(script), env, force=force, cancel=cancel,
                                             before_write=verify, checkpoint=check, log_fn=log, progress_fn=progress)
            return {**result, "game": str(game.game_id), "profile": profile.name, "registry_name": initial["registry_name"],
                    "game_path": initial["game_path"], "prefix_path": initial["prefix_path"], "needs_deployment": False}
        except RegistryRegistrationCancelled as exc:
            raise JobCancelled(str(exc), exc.details) from exc
        except (RegistryRegistrationError, PrefixRepairError) as exc:
            raise JobError(exc.name, str(exc), exc.details) from exc
        except (OperationBusy, DeploymentBusy) as exc:
            raise JobError("operation_busy", str(exc), getattr(exc, "details", {})) from exc
        except OperationCancelled as exc:
            raise JobCancelled("Game registry registration cancelled.") from exc
        except OSError as exc:
            raise JobError("game_registry_write_failed", "Could not write the game registry.", {"reason": str(exc)}) from exc
    return run
