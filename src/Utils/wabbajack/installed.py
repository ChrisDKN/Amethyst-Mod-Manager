from __future__ import annotations

import fcntl
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from Utils.deployment.locking import game_mutation_lock
from Utils.profiles.state import read_profile_settings
from .diagnostics import emit, emit_exception
from .paths import WabbajackError
from .store import installation_info, installations


@dataclass(frozen=True)
class InstalledList:
    game_name: str
    directory: Path
    info: dict
    profiles: tuple[str, ...]
    locked_profiles: tuple[str, ...]
    groups: tuple[str, ...]

    @property
    def title(self) -> str:
        return str(self.info.get("name") or self.directory.name)

    @property
    def status(self) -> str:
        return str(self.info.get("status") or "interrupted")


@dataclass(frozen=True)
class RemovalResult:
    directory: Path
    profiles: tuple[str, ...]
    groups: tuple[str, ...]
    restored_profile: str = ""


def _same_path(first, second) -> bool:
    try:
        return Path(first).resolve() == Path(second).resolve()
    except (OSError, RuntimeError, ValueError, TypeError):
        return False


def _direct_profiles(directory: Path, profile_root: Path,
                     installation_id: str = "") -> list[Path]:
    profiles_root = profile_root / "profiles"
    if not profiles_root.is_dir():
        return []
    result = []
    mods_root = directory / "root" / "mods"
    for profile in profiles_root.iterdir():
        if not profile.is_dir():
            continue
        settings = read_profile_settings(profile, None)
        if settings.get("is_group"):
            continue
        saved = settings.get("wabbajack_directory")
        saved_id = str(settings.get("wabbajack_install_id") or "")
        state_match = bool(saved and _same_path(saved, directory)
                           and (not installation_id or not saved_id
                                or saved_id == installation_id))
        mods = profile / "mods"
        link_match = mods.is_symlink() and _same_path(mods, mods_root)
        if state_match or link_match:
            result.append(profile)
    return sorted(result, key=lambda path: path.name.casefold())


def _referencing_groups(game, profile_names) -> list[str]:
    from Utils.profiles.groups import get_members, list_groups
    wanted = set(profile_names)
    profiles_root = Path(game.get_profile_root()) / "profiles"
    return [name for name in list_groups(game)
            if wanted.intersection(get_members(profiles_root / name))]


def _locked_profiles(profiles) -> list[str]:
    result = []
    for profile in profiles:
        settings = read_profile_settings(profile, None)
        if (profile.name == "default" or settings.get("original_default")
                or settings.get("profile_locked")):
            result.append(profile.name)
    return result


def _snapshot(game_name, game, info) -> InstalledList:
    directory = Path(info["directory"])
    root = Path(game.get_profile_root())
    profiles = _direct_profiles(directory, root, str(info.get("id") or ""))
    names = tuple(profile.name for profile in profiles)
    return InstalledList(
        game_name=game_name,
        directory=directory,
        info=info,
        profiles=names,
        locked_profiles=tuple(_locked_profiles(profiles)),
        groups=tuple(_referencing_groups(game, names)),
    )


def installed_lists(games=None, log=None) -> list[InstalledList]:
    if games is None:
        from .games import configured_games
        games = configured_games()
    roots = {}
    for game_name, game in sorted(games.items(), key=lambda row: row[0].casefold()):
        if not game.is_configured():
            continue
        root = Path(game.get_profile_root())
        try:
            key = root.resolve()
        except OSError:
            key = root.absolute()
        roots.setdefault(key, []).append((game_name, game, root))
    result = []
    for candidates in roots.values():
        rows = installations(candidates[0][2], log)
        for info in rows:
            package_game = str(info.get("game") or "")
            from .games import matches_game
            chosen = next(((name, game) for name, game, _root in candidates
                           if package_game and matches_game(game, package_game)),
                          candidates[0][:2])
            result.append(_snapshot(chosen[0], chosen[1], info))
    result.sort(key=lambda item: (
        item.status == "complete", item.game_name.casefold(),
        item.title.casefold(), str(item.directory)))
    emit(log, "installed_lists.scan.completed", games=len(roots),
         installations=len(result))
    return result


@contextmanager
def _installation_lock(directory: Path):
    lock_path = directory / "install.lock"
    if lock_path.is_symlink():
        raise WabbajackError("The installation lock is a symbolic link")
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WabbajackError(
                "This modlist already has an active operation") from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _restore_deployment(game, profile_name: str, log, progress) -> None:
    from Utils.deployment import restore_root_folder_for_game
    from Utils.deployment.pipeline import (
        check_paths_mounted, finalize_filegraph_recovery)

    error = check_paths_mounted(game)
    if error:
        raise WabbajackError(f"Restore aborted: {error}")
    previous = getattr(game, "_active_profile_dir", None)
    profile = Path(game.get_profile_root()) / "profiles" / profile_name
    try:
        game.set_active_profile_dir(profile)
        game.load_paths()
        game_root = game.get_game_path()
        if hasattr(game, "restore"):
            game.restore(
                log_fn=log,
                progress_fn=lambda done, total, phase=None:
                progress("Restoring deployed game", done, total, phase or ""),
            )
        root_files = game.get_effective_root_folder_path()
        if root_files.is_dir() and game_root:
            restore_root_folder_for_game(
                game, root_folder_dir=root_files, game_root=game_root,
                log_fn=log)
        finalize_filegraph_recovery(game, profile, log_fn=log)
        game.clear_deploy_active()
    finally:
        game.set_active_profile_dir(previous)
        game.load_paths()


def remove_installed_list(game, directory: Path, *, log=None,
                          progress=None) -> RemovalResult:
    log = log or (lambda _message: None)
    progress = progress or (lambda _phase, _current, _total, _detail="": None)
    profile_root = Path(game.get_profile_root())
    managed_root = profile_root.resolve() / ".wabbajack"
    directory = Path(directory)
    if (directory.is_symlink() or not directory.is_dir()
            or directory.resolve().parent != managed_root):
        raise WabbajackError(
            "Installation is outside managed Wabbajack storage")

    emit(log, "installed_list.remove.started", directory=directory,
         game=getattr(game, "name", ""))
    step = "Acquiring operation locks"
    try:
        with game_mutation_lock(game), _installation_lock(directory):
            step = "Reading installation state"
            info = installation_info(directory, log)
            if not info:
                raise WabbajackError("The managed installation state is unreadable")
            if info.get("profile_root") and not _same_path(
                    info["profile_root"], profile_root):
                raise WabbajackError(
                    "The installation belongs to a different game profile directory")
            profiles = _direct_profiles(
                directory, profile_root, str(info.get("id") or ""))
            for profile in profiles:
                if (profile.is_symlink()
                        or profile.resolve().parent
                        != (profile_root / "profiles").resolve()):
                    raise WabbajackError(
                        f"Linked profile is outside managed storage: {profile.name}")
            locked = _locked_profiles(profiles)
            if locked:
                raise WabbajackError(
                    "Unlock these profiles before removing the list: "
                    + ", ".join(locked))
            names = tuple(profile.name for profile in profiles)
            groups = tuple(_referencing_groups(game, names))
            deployed = ""
            if game.get_deploy_active():
                candidate = game.get_last_deployed_profile()
                if candidate in names or candidate in groups:
                    deployed = candidate
                    step = f"Restoring deployed profile {candidate}"
                    progress("Restoring deployed game", 0, 0, candidate)
                    _restore_deployment(game, candidate, log, progress)

            if names:
                step = "Updating Profile Groups"
                progress("Updating Profile Groups", 0, len(groups), "")
                from Utils.profiles.groups import remove_profiles_everywhere
                remove_profiles_everywhere(game, names, log_fn=log)
            for index, profile in enumerate(profiles):
                step = f"Removing profile {profile.name}"
                progress("Removing profiles", index, len(profiles), profile.name)
                shutil.rmtree(profile)
            step = "Removing managed installation files"
            progress("Removing installed files", 0, 1, str(directory))
            shutil.rmtree(directory)
            progress("Removing installed files", 1, 1, str(directory))
            result = RemovalResult(directory, names, groups, deployed)
            emit(log, "installed_list.remove.completed", directory=directory,
                 profiles=names, groups=groups, restored_profile=deployed)
            return result
    except Exception as exc:
        emit_exception(log, "installed_list.remove.failed", exc,
                       directory=directory, step=step)
        raise WabbajackError(f"{step} failed: {exc}") from exc
