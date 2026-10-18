from __future__ import annotations

import copy
from pathlib import Path

from .jobs import JobError


def configure_game(game, game_path: Path, prefix_path: Path | None,
                   staging_path: Path, deploy_mode: str | None,
                   runtime_mode: str | None, log, *,
                   save_path_override: Path | None = None,
                   appimage_path: Path | None = None):
    from Utils.deployment import LinkMode
    from Utils.deployment.locking import game_mutation_lock
    from Utils.hardlink_check import hardlink_device_mismatches
    from Utils.mods.staging import staging_root_problem
    from Utils.operations import OperationBusy, acquire_game_operation
    from Utils.profiles.structure import create_profile_structure
    from Utils.wine.prefix import normalize_prefix_path

    try:
        if prefix_path is not None:
            prefix_path = normalize_prefix_path(prefix_path)
        selected = copy.copy(game)
        selected.set_active_profile_dir(None)
        selected._game_path = game_path
        selected._prefix_path = prefix_path
        selected._prefix_path_cleared = False
        selected._staging_path = staging_path
        selected._save_path_override = save_path_override
        if selected.game_id == "morrowind_openmw":
            selected._openmw_appimage_path = appimage_path
        if appimage_path is not None:
            if (selected.game_id != "morrowind_openmw"
                    or not callable(getattr(selected, "set_appimage_path", None))
                    or not callable(getattr(selected, "get_appimage_path", None))):
                raise JobError("appimage_path_unsupported",
                               "This game does not use an AppImage path.")
        if deploy_mode is not None and deploy_mode != "vfs":
            selected._deploy_mode = {
                "hardlink": LinkMode.HARDLINK,
                "symlink": LinkMode.SYMLINK,
                "copy": LinkMode.COPY,
            }[deploy_mode]
        with acquire_game_operation(selected, "headless.game_configure"):
            paths_file = game._paths_file
            if game.is_configured() or paths_file.exists():
                raise JobError("game_already_configured",
                               "This game already has saved paths; first-time setup cannot replace them.")
            if not game_path.is_dir():
                raise JobError("game_path_unavailable",
                               "The game installation path is not a directory.",
                               {"path": str(game_path)})
            if prefix_path is not None:
                if not prefix_path.is_dir():
                    raise JobError("prefix_path_unavailable",
                                   "The prefix path is not a directory.",
                                   {"path": str(prefix_path)})
            if (save_path_override is not None and save_path_override.exists()
                    and not save_path_override.is_dir()):
                raise JobError("save_path_unavailable",
                               "The manual saves path is not a directory.",
                               {"path": str(save_path_override)})
            if appimage_path is not None and not appimage_path.is_file():
                raise JobError("appimage_path_unavailable",
                               "The AppImage path is not a file.",
                               {"path": str(appimage_path)})
            if selected.game_id == "baldurs_gate_3":
                mode = runtime_mode or selected._infer_runtime_mode(game_path)
                if mode == "native":
                    if not (game_path / "bin/bg3").is_file():
                        raise JobError("game_runtime_unavailable",
                                       "The native BG3 executable was not found.")
                    if prefix_path is not None:
                        raise JobError("game_runtime_prefix_conflict",
                                       "A native BG3 configuration cannot use a Proton prefix.")
                    selected._prefix_path_cleared = True
                else:
                    if not any((game_path / path).is_file()
                               for path in ("bin/bg3.exe", "bin/bg3_dx11.exe")):
                        raise JobError("game_runtime_unavailable",
                                       "The Windows BG3 executable was not found.")
                    if prefix_path is None:
                        raise JobError("prefix_path_required",
                                       "The Proton BG3 runtime requires a prefix path.")
                selected._runtime_mode = mode
            elif runtime_mode is not None:
                raise JobError("game_runtime_unavailable",
                               "This game does not accept a runtime mode.")

            if prefix_path is not None and selected.get_prefix_path() != prefix_path:
                raise JobError("prefix_path_unsupported",
                               "This game does not use a Proton prefix.")
            vfs_supported = bool(
                (getattr(selected, "supports_vfs_deploy", False)
                 or getattr(selected, "supports_profile_vfs", False))
                and callable(getattr(selected, "set_vfs_enabled", None)))
            if deploy_mode == "vfs" and not vfs_supported:
                raise JobError("game_deploy_mode_unsupported",
                               "This game does not support VFS deployment.")
            if (deploy_mode != "vfs" and selected._deploy_mode is LinkMode.COPY
                    and not selected.deploy_mode_supports_copy):
                raise JobError("game_deploy_mode_unsupported",
                               "This game does not support copy deployment.")
            if (selected.deploy_mode_fallback is LinkMode.COPY
                    and deploy_mode != "vfs"
                    and selected._deploy_mode is not LinkMode.COPY):
                raise JobError("game_deploy_mode_unsupported",
                               "This game requires copy deployment.")
            game_real = game_path.resolve()
            staging_real = staging_path.resolve()
            if (game_real == staging_real or game_real in staging_real.parents
                    or staging_real in game_real.parents):
                raise JobError("staging_game_overlap",
                               "The staging root and game installation must be separate.")
            problem = staging_root_problem(staging_path, game.name)
            if problem is not None:
                code, detail = problem
                raise JobError("staging_root_unavailable",
                               "The staging root cannot be used for this game.",
                               {"reason": code, "detail": detail,
                                "path": str(staging_path)})
            using_vfs = (deploy_mode == "vfs" or
                         (deploy_mode is None and vfs_supported
                          and bool(getattr(selected, "vfs_enabled", False))))
            if not using_vfs and selected.get_deploy_mode() is LinkMode.HARDLINK:
                mismatched = hardlink_device_mismatches(selected, log_fn=log)
                if mismatched:
                    raise JobError("game_filesystem_mismatch",
                                   "Hard links cannot reach every deploy target.",
                                   {"targets": mismatched})

            with game_mutation_lock(selected):
                create_profile_structure(selected)
                previous_profile = getattr(game, "_active_profile_dir", None)
                settings_file = Path(game._settings_file)
                settings_before = None
                if deploy_mode is not None and vfs_supported:
                    if settings_file.is_symlink():
                        raise JobError("unsafe_game_config",
                                       "The saved game settings file is a symbolic link.")
                    settings_before = (settings_file.read_bytes()
                                       if settings_file.exists() else None)
                try:
                    selected.save_paths()
                    if deploy_mode is not None and vfs_supported:
                        selected.set_vfs_enabled(deploy_mode == "vfs")
                    warnings = selected.validate_install()
                    game.set_active_profile_dir(None)
                    if not game.load_paths():
                        raise JobError("game_configuration_failed",
                                       "The saved game path could not be loaded.")
                except Exception:
                    paths_file.unlink(missing_ok=True)
                    if deploy_mode is not None and vfs_supported:
                        if settings_before is None:
                            settings_file.unlink(missing_ok=True)
                        else:
                            settings_file.write_bytes(settings_before)
                    game.set_active_profile_dir(previous_profile)
                    game.load_paths(read_only=True)
                    raise
            log(f"Configured {game.name} at {game_path}.")
            return {
                "game_path": str(game.get_game_path()),
                "prefix_path": (str(game.get_prefix_path())
                                if game.get_prefix_path() else None),
                "staging_path": str(game.get_profile_root()),
                "save_path_override": (str(game.get_save_path_override())
                                       if game.get_save_path_override() else None),
                "appimage_path": (str(game.get_appimage_path())
                                  if game.game_id == "morrowind_openmw"
                                  and game.get_appimage_path() else None),
                "deploy_mode": ("vfs" if vfs_supported and game.vfs_enabled
                                else {
                                    LinkMode.HARDLINK: "hardlink",
                                    LinkMode.SYMLINK: "symlink",
                                    LinkMode.COPY: "copy",
                                }[game.get_deploy_mode()]),
                "runtime_mode": (game.get_runtime_mode()
                                 if game.game_id == "baldurs_gate_3" else None),
                "default_profile": str(game.get_profile_root() / "profiles" / "default"),
                "install_warnings": warnings,
            }
    except (JobError, OperationBusy):
        raise
    except Exception as exc:
        raise JobError("game_configuration_failed",
                       str(exc) or type(exc).__name__,
                       {"exception": type(exc).__name__}) from exc
