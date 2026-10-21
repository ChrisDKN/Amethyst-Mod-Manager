import json
from pathlib import Path

from Games.Bethesda.bethesda_ini import _read_ini_key, _set_ini_key
from Utils.atomic_write import write_atomic_text


_LINK_NAME = "AmethystSaves"
_STATE_NAME = ".amethyst-profile-saves.json"
_OVERRIDES = {"sLocalSavePath": _LINK_NAME + "\\", "bUseMyGamesDirectory": "1"}


def _save_overrides(game, directory: Path) -> dict[str, str]:
    overrides = dict(_OVERRIDES)
    if game._PROFILE_SAVES_PARENT_RELATIVE:
        overrides["sLocalSavePath"] = f"..\\{directory.name}\\{_LINK_NAME}\\"
    return overrides


def _read_save_state(state_path: Path) -> dict:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("Expected a profile save state object.")
    target = state.get("target")
    entries = state.get("inis")
    overrides = state.get("overrides", _OVERRIDES)
    if (not isinstance(target, str) or not target or not Path(target).is_absolute()
            or not isinstance(entries, list) or not isinstance(overrides, dict)
            or set(overrides) != set(_OVERRIDES)
            or any(not isinstance(value, str) for value in overrides.values())):
        raise ValueError("Invalid profile save state fields.")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid profile save INI entry.")
        path = entry.get("path")
        previous = entry.get("previous")
        if (not isinstance(path, str) or not path or not Path(path).is_absolute()
                or not isinstance(entry.get("existed"), bool)
                or not isinstance(previous, dict)
                or any(key not in previous or (previous[key] is not None
                       and not isinstance(previous[key], str)) for key in overrides)):
            raise ValueError("Invalid profile save INI settings.")
    return state


def _managed_save_link(game, link: Path) -> bool:
    if not link.is_symlink():
        return False
    try:
        relative = link.resolve().relative_to(
            (game.get_profile_root() / "profiles").resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return len(relative.parts) == 2 and relative.name == game._SAVES_FOLDER_NAME


def _recover_save_state(game, directory: Path, state_path: Path, log_fn) -> None:
    has_state = state_path.is_file()
    status = "Invalid" if has_state else "Missing"
    log_fn(f"  WARN: {status} profile save state: {state_path}. "
           "Previous INI values are unavailable; removing Amethyst save-path "
           "overrides and retaining bUseMyGamesDirectory.")
    save_path = _save_overrides(game, directory)["sLocalSavePath"]
    for name in game._profile_saves_ini_files(directory):
        ini = game._resolve_ini_path(directory, name)
        if (ini.is_file() and _read_ini_key(
                ini, "General", "sLocalSavePath", case_insensitive=True) == save_path):
            _set_ini_key(ini, "General", "sLocalSavePath", None,
                         case_insensitive=True)
    link = directory / _LINK_NAME
    if _managed_save_link(game, link):
        link.unlink()
        log_fn(f"  Removed profile saves link: {link}")
    if not has_state:
        return
    backup = state_path.with_name(state_path.name + ".corrupt")
    number = 1
    while backup.exists() or backup.is_symlink():
        backup = state_path.with_name(f"{state_path.name}.corrupt.{number}")
        number += 1
    state_path.rename(backup)
    log_fn(f"  Preserved invalid profile save state: {backup}")


def _restore_legacy_link(game, directory: Path, log_fn) -> None:
    link = directory / game._SAVES_FOLDER_NAME
    if _managed_save_link(game, link):
        link.unlink()
        log_fn(f"  Removed legacy profile saves link: {link}")
    elif link.exists() or link.is_symlink():
        return
    backup = directory / (game._SAVES_FOLDER_NAME + game._SAVES_BACKUP_SUFFIX)
    if backup.exists() or backup.is_symlink():
        backup.rename(link)
        log_fn(f"  Restored original saves folder: {link}")


def restore_profile_saves(game, log_fn) -> None:
    # Multiple prefix folders can share the same profile INIs.
    for directory in reversed(game._saves_link_targets()):
        state_path = directory / _STATE_NAME
        if state_path.is_file():
            try:
                state = _read_save_state(state_path)
            except ValueError:
                _recover_save_state(game, directory, state_path, log_fn)
                _restore_legacy_link(game, directory, log_fn)
                continue
            for entry in state["inis"]:
                ini = Path(entry["path"])
                if not ini.is_file():
                    continue
                for key, value in state.get("overrides", _OVERRIDES).items():
                    if _read_ini_key(ini, "General", key, case_insensitive=True) == value:
                        _set_ini_key(ini, "General", key, entry["previous"][key],
                                     case_insensitive=True)
                if not entry["existed"] and not ini.read_text(encoding="utf-8").strip():
                    ini.unlink()
            link = directory / _LINK_NAME
            if link.is_symlink() and link.resolve() == Path(state["target"]):
                link.unlink()
                log_fn(f"  Removed profile saves link: {link}")
            state_path.unlink()
            log_fn(f"  Restored profile save INI settings in {directory}")
        elif (not state_path.exists() and not state_path.is_symlink()
              and _managed_save_link(game, directory / _LINK_NAME)):
            _recover_save_state(game, directory, state_path, log_fn)
        _restore_legacy_link(game, directory, log_fn)


def deploy_profile_saves(game, profile: str, log_fn) -> None:
    restore_profile_saves(game, log_fn)
    if not game.profile_saves:
        return
    directories = game._saves_link_targets()
    if not directories:
        raise RuntimeError("Prefix path is required for profile-specific saves.")
    target = game._profile_saves_dir(profile).resolve()
    target.mkdir(parents=True, exist_ok=True)
    for directory in directories:
        link = directory / _LINK_NAME
        if link.is_dir() and not link.is_symlink():
            try:
                link.rmdir()
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot redirect profile saves: {link} already exists.") from exc
            log_fn(f"  Removed empty profile saves directory: {link}")
        if link.exists() or link.is_symlink():
            raise RuntimeError(f"Cannot redirect profile saves: {link} already exists.")
    try:
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            overrides = _save_overrides(game, directory)
            entries = {}
            for name in game._profile_saves_ini_files(directory):
                ini = game._resolve_ini_path(directory, name).resolve()
                entries[str(ini)] = {
                    "path": str(ini),
                    "existed": ini.exists(),
                    "previous": {
                        key: _read_ini_key(ini, "General", key, case_insensitive=True)
                        for key in _OVERRIDES
                    },
                }
            state = {"target": str(target), "inis": list(entries.values()),
                     "overrides": overrides}
            write_atomic_text(directory / _STATE_NAME, json.dumps(state, indent=2))
            (directory / _LINK_NAME).symlink_to(target)
            for entry in entries.values():
                for key, value in overrides.items():
                    _set_ini_key(Path(entry["path"]), "General", key, value,
                                 case_insensitive=True)
            log_fn(f"  Profile saves: {directory / _LINK_NAME} → {target}")
    except Exception:
        restore_profile_saves(game, log_fn)
        raise
