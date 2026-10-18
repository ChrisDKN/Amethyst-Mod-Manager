from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .jobs import JobError


def _same(left: Path | None, right: Path | None) -> bool:
    if left is None or right is None:
        return left is right
    return left.resolve(strict=False) == right.resolve(strict=False)


def _deploy_method(game: Any) -> str:
    from Utils.deployment import LinkMode

    if (getattr(game, "supports_vfs_deploy", False)
            or getattr(game, "supports_profile_vfs", False)):
        if getattr(game, "vfs_enabled", False):
            return "vfs"
    modes = {LinkMode.HARDLINK: "hardlink", LinkMode.SYMLINK: "symlink",
             LinkMode.COPY: "copy"}
    return modes[game.get_deploy_mode()]


def _configuration_revision(
    game: Any, profile_dir: Path, old_root: Path, files: list[Path],
    current: dict[str, Any], target: dict[str, Any],
) -> str:
    basis = {"profile": str(profile_dir), "current": current, "target": target}
    digest = hashlib.sha256(json.dumps(basis, sort_keys=True).encode())
    paths_file = Path(game._paths_file)
    state_files = [paths_file, Path(game._settings_file)]
    state_files.extend(sorted((old_root / "profiles").glob("*/profile_state.json")))
    state_files.extend(sorted((old_root / "profiles").glob("*/profile_settings.json")))
    for path in state_files:
        digest.update(str(path).encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    for path in files:
        rel = path.relative_to(old_root)
        digest.update(str(rel).encode())
        try:
            stat = path.lstat()
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
        except OSError:
            digest.update(b"<missing>")
        destination = Path(target["staging_path"]) / rel
        try:
            stat = destination.lstat()
            digest.update(f"dest:{stat.st_size}:{stat.st_mtime_ns}".encode())
        except OSError:
            digest.update(b"dest:<missing>")
    return digest.hexdigest()


def _build_plan(
    game: Any, profile_dir: Path, changes: dict[str, Any],
    log: Callable[[str], None],
) -> tuple[dict[str, Any], Any, list[Path]]:
    from Utils.deployment import LinkMode
    from Utils.hardlink_check import hardlink_device_mismatches
    from Utils.mods.staging import collect_staging_files, staging_root_problem
    from Utils.wine.prefix import normalize_prefix_path

    if not game.is_configured():
        raise JobError("game_not_configured", "Configure this game before changing its paths.")
    if profile_dir.is_symlink() or not profile_dir.is_dir():
        raise JobError("profile_not_found", "The selected profile is unavailable.")
    selected = copy.copy(game)
    selected.set_active_profile_dir(profile_dir)
    if not selected.load_paths(read_only=True):
        raise JobError("game_paths_unavailable", "The saved game paths could not be loaded.")

    old_root = Path(game.get_profile_root())
    if (profile_dir.parent.resolve(strict=False)
            != (old_root / "profiles").resolve(strict=False)):
        raise JobError("profile_not_found",
                       "The selected profile is no longer in this game's staging root.")
    if Path(game._paths_file).is_symlink():
        raise JobError("unsafe_game_config", "The saved game paths file is a symbolic link.")
    if Path(game._settings_file).is_symlink():
        raise JobError("unsafe_game_config", "The saved game settings file is a symbolic link.")
    old_game = selected.get_game_path()
    old_prefix = selected.get_prefix_path()
    old_saves = selected.get_save_path_override()
    appimage_supported = (
        game.game_id == "morrowind_openmw"
        and callable(getattr(selected, "get_appimage_path", None))
        and callable(getattr(selected, "set_appimage_path", None)))
    old_appimage = selected.get_appimage_path() if appimage_supported else None
    old_runtime = (selected.get_runtime_mode()
                   if game.game_id == "baldurs_gate_3" else None)
    old_method = _deploy_method(selected)
    was_deployed = selected.get_deploy_active()
    game_path = changes.get("game_path", old_game)
    prefix_path = changes.get("prefix_path", old_prefix)
    staging_path = changes.get("staging_path", selected._staging_path)
    saves_path = changes.get("save_path_override", old_saves)
    appimage_path = changes.get("appimage_path", old_appimage)
    if "appimage_path" in changes and not appimage_supported:
        raise JobError("appimage_path_unsupported",
                       "This game does not use an AppImage path.")
    if prefix_path is not None and "prefix_path" in changes:
        prefix_path = normalize_prefix_path(prefix_path)
    selected._game_path = game_path
    selected._prefix_path = prefix_path
    if "prefix_path" in changes:
        selected._prefix_path_cleared = prefix_path is None
    selected._staging_path = staging_path
    selected._save_path_override = saves_path
    if "appimage_path" in changes:
        selected._openmw_appimage_path = appimage_path
    modes = {"hardlink": LinkMode.HARDLINK, "symlink": LinkMode.SYMLINK,
             "copy": LinkMode.COPY}
    if "deploy_mode" in changes:
        if changes["deploy_mode"] == "vfs":
            if not ((getattr(selected, "supports_vfs_deploy", False)
                     or getattr(selected, "supports_profile_vfs", False))
                    and callable(getattr(selected, "set_vfs_enabled", None))):
                raise JobError("game_deploy_mode_unsupported",
                               "This game does not support VFS deployment.")
        else:
            selected._deploy_mode = modes[changes["deploy_mode"]]

    if game.game_id == "baldurs_gate_3":
        runtime = changes.get("runtime_mode")
        if runtime is None:
            runtime = ("proton" if "prefix_path" in changes
                       and prefix_path is not None else
                       selected._infer_runtime_mode(game_path)
                       if "game_path" in changes or "prefix_path" in changes
                       else old_runtime)
        selected._runtime_mode = runtime
        if runtime == "native":
            if "prefix_path" in changes and prefix_path is not None:
                raise JobError("game_runtime_prefix_conflict",
                               "A native BG3 runtime cannot use a Proton prefix.")
            selected._prefix_path = None
            selected._prefix_path_cleared = True
        elif prefix_path is None:
            raise JobError("prefix_path_required",
                           "The Proton BG3 runtime requires a prefix path.")
        if runtime == "native" and not (game_path / "bin/bg3").is_file():
            raise JobError("game_runtime_unavailable",
                           "The native BG3 executable was not found.")
        if runtime == "proton" and not any(
                (game_path / rel).is_file()
                for rel in ("bin/bg3.exe", "bin/bg3_dx11.exe")):
            raise JobError("game_runtime_unavailable",
                           "The Windows BG3 executable was not found.")
    elif "runtime_mode" in changes:
        raise JobError("game_runtime_unavailable",
                       "This game does not accept a runtime mode.")

    new_root = Path(selected.get_profile_root())
    for root in (old_root, new_root):
        if any((root / name).is_symlink() for name in (
                "mods", "profiles", "overwrite", "Root_Folder", "Applications")):
            raise JobError("unsafe_staging_path",
                           "A managed staging directory is a symbolic link.")
        profiles_root = root / "profiles"
        if profiles_root.is_symlink() or (profiles_root.is_dir() and any(
                entry.is_symlink() and entry.is_dir()
                for entry in profiles_root.iterdir())):
            raise JobError("unsafe_profile_path",
                           "A profile directory is a symbolic link.")
    if (not game_path or not game_path.is_dir() or game_path.is_symlink()):
        raise JobError("game_path_unavailable",
                       "The game installation path is unavailable.")
    if selected._prefix_path is not None:
        if not selected._prefix_path.is_dir() or selected._prefix_path.is_symlink():
            raise JobError("prefix_path_unavailable", "The prefix path is unavailable.")
        if selected.get_prefix_path() != selected._prefix_path:
            raise JobError("prefix_path_unsupported",
                           "This game does not use a Proton prefix.")
    if ("save_path_override" in changes and saves_path is not None
            and saves_path.exists() and not saves_path.is_dir()):
        raise JobError("save_path_unavailable",
                       "The manual saves path is not a directory.")
    if ("appimage_path" in changes and appimage_path is not None
            and not appimage_path.is_file()):
        raise JobError("appimage_path_unavailable",
                       "The AppImage path is not a file.")
    if new_root.is_symlink() or old_root.is_symlink():
        raise JobError("unsafe_staging_path", "A staging root is a symbolic link.")
    for other in (game_path, selected._prefix_path):
        if other is None:
            continue
        real = other.resolve(strict=False)
        staged = new_root.resolve(strict=False)
        if real == staged or real in staged.parents or staged in real.parents:
            raise JobError("staging_path_overlap",
                           "The staging root must be separate from game and prefix paths.")
    problem = staging_root_problem(new_root, game.name, current_root=old_root)
    if problem is not None:
        code, detail = problem
        raise JobError("staging_root_unavailable",
                       "The staging root cannot be used for this game.",
                       {"reason": code, "detail": detail})
    if not _same(old_root, new_root):
        old_real = old_root.resolve(strict=False)
        new_real = new_root.resolve(strict=False)
        if old_real in new_real.parents or new_real in old_real.parents:
            raise JobError("staging_path_overlap",
                           "The old and new staging roots must be separate.")
    target_method = changes.get("deploy_mode", old_method)
    if target_method == "copy" and not selected.deploy_mode_supports_copy:
        raise JobError("game_deploy_mode_unsupported",
                       "This game does not support copy deployment.")
    if (selected.deploy_mode_fallback is LinkMode.COPY
            and target_method in {"hardlink", "symlink"}):
        raise JobError("game_deploy_mode_unsupported",
                       "This game requires copy deployment.")
    if target_method == "hardlink":
        mismatch = hardlink_device_mismatches(selected, log_fn=log)
        if mismatch:
            raise JobError("game_filesystem_mismatch",
                           "Hard links cannot reach every deploy target.",
                           {"targets": mismatch})

    target = {
        "game_path": str(game_path),
        "prefix_path": (str(selected._prefix_path)
                        if selected._prefix_path is not None else None),
        "staging_path": str(new_root),
        "save_path_override": (str(saves_path) if saves_path else None),
        "appimage_path": (str(appimage_path) if appimage_path else None),
        "deploy_mode": target_method,
        "runtime_mode": (selected.get_runtime_mode()
                         if game.game_id == "baldurs_gate_3" else None),
    }
    current = {
        "game_path": str(old_game) if old_game else None,
        "prefix_path": str(old_prefix) if old_prefix else None,
        "staging_path": str(old_root),
        "save_path_override": (str(old_saves) if old_saves else None),
        "appimage_path": (str(old_appimage) if old_appimage else None),
        "deploy_mode": old_method,
        "runtime_mode": old_runtime,
    }
    changed = {key: target[key] != current[key] for key in target}
    if was_deployed and any(changed.values()):
        raise JobError("game_deployed",
                       "Restore the game before changing its saved paths or deploy mode.")

    files: list[Path] = []
    total_bytes = collisions = 0
    if changed["staging_path"]:
        raw_files, _ = collect_staging_files(old_root)
        files = [path for path in raw_files
                 if path.name != ".amethyst-operation.lock"]
        for path in files:
            try:
                if not path.is_symlink():
                    total_bytes += path.lstat().st_size
                destination = new_root / path.relative_to(old_root)
                rel = destination.relative_to(new_root)
                if any((new_root / Path(*rel.parts[:index])).is_symlink()
                       for index in range(1, len(rel.parts))):
                    raise JobError("unsafe_staging_path",
                                   "The destination contains a symbolic-link directory.")
                if destination.exists() or destination.is_symlink():
                    collisions += 1
            except OSError:
                continue
    plan = {
        "profile": profile_dir.name,
        "current": current,
        "target": target,
        "changed": changed,
        "migration": {"needed": bool(files), "file_count": len(files),
                      "bytes": total_bytes, "existing_destinations": collisions},
        "install_warnings": selected.validate_install(),
    }
    plan["revision"] = _configuration_revision(
        game, profile_dir, old_root, files, current, target)
    return plan, selected, files


def plan_reconfigure_game(
    game: Any, profile_dir: Path, changes: dict[str, Any],
    log: Callable[[str], None] = lambda message: None,
) -> dict[str, Any]:
    plan, _, _ = _build_plan(game, profile_dir, changes, log)
    return plan


def reconfigure_game(
    game: Any, profile_dir: Path, changes: dict[str, Any], revision: str,
    migrate_staging: bool | None, owner: str,
    log: Callable[[str], None], progress: Callable[..., None],
) -> dict[str, Any]:
    from Utils.deployment.locking import game_mutation_lock
    from Utils.mods.staging import collect_staging_files, migrate_staging_files
    from Utils.operations import acquire_game_operation
    from Utils.profiles.structure import create_profile_structure

    if Path(game.get_profile_root()).is_symlink():
        raise JobError("unsafe_staging_path", "The current staging root is a symbolic link.")
    with acquire_game_operation(game, "headless.game_reconfigure", owner=owner):
        with game_mutation_lock(game):
            plan, selected, files = _build_plan(game, profile_dir, changes, log)
            if plan["revision"] != revision:
                raise JobError("game_configuration_changed",
                               "The game configuration changed; request a new plan.",
                               {"current_revision": plan["revision"]})
            if not any(plan["changed"].values()):
                return {"game": game.name, "profile": profile_dir.name,
                        "paths": plan["target"], "warnings": plan["install_warnings"],
                        "migration": {"moved": 0, "skipped": 0, "failed": 0,
                                      "left_in_old_root": False}}
            migration = plan["migration"]
            if migration["needed"] and migrate_staging is None:
                raise JobError("staging_migration_choice_required",
                               "Choose whether to move the existing staging files.",
                               {"migration": migration})
            old_root = Path(plan["current"]["staging_path"])
            new_root = Path(plan["target"]["staging_path"])
            requested_profile = new_root / "profiles" / profile_dir.name
            if (plan["changed"]["staging_path"] and migrate_staging is False
                    and profile_dir.name != "default"
                    and not requested_profile.is_dir()):
                raise JobError("staging_profile_missing",
                               "The selected profile is absent from the new staging root; "
                               "move the staging files to preserve it.",
                               {"profile": profile_dir.name,
                                "target": str(requested_profile)})
            paths_file = Path(game._paths_file)
            snapshot_paths = [paths_file, Path(game._settings_file)]
            for directory in sorted((old_root / "profiles").glob("*")):
                if directory.is_dir() and not directory.is_symlink():
                    snapshot_paths.extend((directory / "profile_state.json",
                                           directory / "profile_settings.json"))
            snapshot_paths.extend((profile_dir / "profile_state.json",
                                   profile_dir / "profile_settings.json"))
            snapshots = {path: path.read_bytes() if path.exists() else None
                         for path in snapshot_paths}
            previous_active = getattr(game, "_active_profile_dir", None)
            vfs_change = (plan["changed"]["deploy_mode"] and
                          (plan["current"]["deploy_mode"] == "vfs" or
                           plan["target"]["deploy_mode"] == "vfs"))
            try:
                progress(0, 3, "Saving game configuration")
                selected._staging_path = getattr(game, "_staging_path", None)
                selected.save_paths()
                if vfs_change:
                    selected.set_vfs_enabled(plan["target"]["deploy_mode"] == "vfs")
                selected._staging_path = changes.get("staging_path",
                                                getattr(game, "_staging_path", None))
                selected.save_paths()
            except Exception as exc:
                for path, data in snapshots.items():
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                if isinstance(exc, JobError):
                    raise
                raise JobError("game_reconfiguration_failed",
                               "The game configuration could not be saved.",
                               {"reason": str(exc)}) from exc

            moved = skipped = failed = 0
            should_migrate = (migrate_staging if migrate_staging is not None
                              else True)
            if plan["changed"]["staging_path"] and should_migrate:
                fresh_files, _ = collect_staging_files(old_root)
                files = [path for path in fresh_files
                         if path.name != ".amethyst-operation.lock"]
                progress(1, 3, "Moving staging files")
                moved, skipped, failed = migrate_staging_files(
                    old_root, new_root, files,
                    progress_cb=lambda done, total, detail: progress(
                        done, total, "Moving staging files"),
                    log_fn=log)
            try:
                create_profile_structure(selected)
                new_active = requested_profile
                if (not new_active.is_dir() and migrate_staging
                        and profile_dir.name != "default"):
                    new_active.mkdir(parents=True)
                    (new_active / "modlist.txt").touch()
                    (new_active / "plugins.txt").touch()
                if not new_active.is_dir():
                    new_active = new_root / "profiles" / "default"
                selected.set_active_profile_dir(new_active)
                selected.save_paths()
                if vfs_change and plan["changed"]["staging_path"]:
                    selected.set_vfs_enabled(plan["target"]["deploy_mode"] == "vfs")
                game.set_active_profile_dir(new_active)
                if not game.load_paths():
                    raise RuntimeError("The saved game path could not be loaded")
            except Exception as exc:
                game.set_active_profile_dir(previous_active)
                game.load_paths(read_only=True)
                raise JobError("game_reconfiguration_incomplete",
                               "The new paths were saved but the game could not be reloaded.",
                               {"reason": str(exc), "target": plan["target"]}) from exc
            if failed:
                raise JobError("staging_migration_incomplete",
                               "Some staging files could not be moved.",
                               {"moved": moved, "skipped": skipped,
                                "failed": failed, "target": plan["target"]})
            try:
                warnings = list(game.validate_install())
            except Exception as exc:
                warning = f"Install validation failed: {exc}"
                warnings = [warning]
                log(warning)
            if moved:
                try:
                    from Utils.filegraph.service import FileGraphService
                    library = FileGraphService.open_library(
                        game, new_active, log_fn=log)
                    library.refresh(new_active)
                except Exception as exc:
                    warning = f"Mod catalog refresh failed: {exc}"
                    warnings.append(warning)
                    log(warning)
            progress(3, 3, "Game reconfigured")
            return {
                "game": game.name, "profile": new_active.name,
                "paths": plan["target"], "warnings": warnings,
                "migration": {"moved": moved, "skipped": skipped,
                              "failed": failed, "left_in_old_root":
                              bool(migration["needed"] and not migrate_staging)},
            }
