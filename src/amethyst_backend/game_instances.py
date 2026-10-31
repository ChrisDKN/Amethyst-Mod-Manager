from __future__ import annotations

import json
import shutil
from pathlib import Path

from Utils.games.instances import InstanceError, registry_lock, validate_name, validate_paths

from .jobs import JobError


def create_instance(game, game_path, prefix_path, staging_path, deploy_mode,
                    runtime_mode, log, *, games, save_path_override=None,
                    appimage_path=None, openmw_cfg_path=None, launcher_ids=None,
                    initialize=None):
    from Utils.atomic_write import write_atomic_text
    from Utils.config_paths import get_download_cache_dir, link_game_download_cache
    from Utils.games.installations import matching_executables
    from .game_configuration import configure_game

    if not getattr(game, "_instance_draft", False):
        raise JobError("invalid_instance", "Create an instance draft before saving it.")
    original_state = dict(game.__dict__)
    game_path, staging_path = Path(game_path).expanduser(), Path(staging_path).expanduser()
    prefix_path = Path(prefix_path).expanduser() if prefix_path is not None else None
    for path in (game_path, staging_path, prefix_path, save_path_override, appimage_path, openmw_cfg_path):
        if path is not None and not Path(path).is_absolute():
            raise JobError("invalid_instance_path", "Instance locations must be absolute paths.")
    config = game._paths_file.parent
    staging = Path(staging_path)
    config_owned = staging_owned = False
    cache_link = cache_target = None
    cache_target_existed = True
    staging_existed = staging.exists()
    try:
        with registry_lock():
            validate_name(game.name, games, draft=game)
            if not matching_executables(game, game_path):
                raise InstanceError("game_executable_unavailable", "The selected installation does not contain an executable supported by this handler.")
            if staging.is_symlink() or (staging.exists() and (not staging.is_dir() or any(staging.iterdir()))):
                raise InstanceError("instance_staging_not_empty", "Choose an empty staging folder for a fresh instance.")
            if config.exists():
                raise InstanceError("instance_exists", "This instance already has saved configuration.")
            game._game_path = Path(game_path)
            game._prefix_path = prefix_path
            game._staging_path = staging
            game._save_path_override = save_path_override
            if game.game_id == "baldurs_gate_3" and runtime_mode is None:
                runtime_mode = game._infer_runtime_mode(game_path)
            if runtime_mode is not None:
                game._runtime_mode = runtime_mode
            if openmw_cfg_path is not None and game.game_id == "morrowind_openmw":
                game._openmw_cfg_path = Path(openmw_cfg_path)
            validate_paths(game, games)
            if game.get_mod_data_path() is None:
                raise InstanceError("prefix_path_required", "Select the prefix used by this installation.")
            game._game_path = None
            game._instance_draft = False
            config.mkdir(parents=True, exist_ok=False)
            config_owned = True
            cache_name = game._instance_record.get("download_cache_name")
            if cache_name:
                cache_root = get_download_cache_dir()
                cache_target = cache_root / cache_name
                cache_target_existed = cache_target.exists()
                candidate = cache_root / game.name
                if (candidate.exists() or candidate.is_symlink()) and not candidate.is_dir():
                    raise InstanceError("instance_cache_unavailable", "The instance's download cache path is not an available folder. Choose a different instance name.")
                if not candidate.exists() and not candidate.is_symlink():
                    cache_link = link_game_download_cache(game.name, cache_name)
            staging_owned = True
            result = configure_game(game, Path(game_path), prefix_path, staging,
                                    deploy_mode, runtime_mode, log,
                                    save_path_override=save_path_override,
                                    appimage_path=appimage_path,
                                    openmw_cfg_path=openmw_cfg_path)
            if launcher_ids:
                game.set_launcher_ids(**launcher_ids)
            if initialize is not None:
                initialize(game)
            validate_paths(game, games)
            write_atomic_text(config / "instance.json", json.dumps(game._instance_record, indent=2))
            games[game.name] = game
            return {**result, "id": game.instance_id, "name": game.name,
                    "handler_id": game.game_id, "is_instance": True}
    except Exception as exc:
        if cache_link is not None and cache_link.is_symlink():
            cache_link.unlink()
        if cache_target is not None and not cache_target_existed:
            try:
                cache_target.rmdir()
            except OSError:
                pass
        if config_owned:
            shutil.rmtree(config)
        if staging_owned:
            if staging_existed:
                for child in staging.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            else:
                shutil.rmtree(staging, ignore_errors=True)
        game.__dict__.clear()
        game.__dict__.update(original_state)
        if isinstance(exc, InstanceError):
            raise JobError(exc.name, str(exc), exc.details) from exc
        raise


def remove_instance(game, log):
    from Utils.config_paths import remove_game_download_cache_link
    from Utils.deployment.locking import game_mutation_lock
    from Utils.deployment.pipeline import run_restore_pipeline
    from Utils.operations import acquire_game_operation

    with registry_lock():
        if game.get_deploy_active():
            restored = run_restore_pipeline(
                game, game.get_last_deployed_profile(), log_fn=log,
                use_recorded_paths=bool(game.get_deployment_context()), raise_busy=True)
            if not restored or game.get_deploy_active():
                raise JobError("instance_restore_failed", "Restoration failed. The instance and its recovery data have been kept.")
        with acquire_game_operation(game, "instance.remove"), game_mutation_lock(game):
            validate_paths(game, staging_only=True)
            root = game.get_profile_root()
            if getattr(game, "is_instance", False):
                from Utils.games.instances import reserve_retained_storage
                reserve_retained_storage(game._instance_record, root)
            for child in root.iterdir() if root.is_dir() else ():
                if child.name in {"mods", "profiles", "overwrite", "Applications", ".amethyst-operation.lock"}:
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            if getattr(game, "is_instance", False):
                remove_game_download_cache_link(game.name)
            shutil.rmtree(game._paths_file.parent)
