from pathlib import Path
import stat

from Utils.launchers.steam import all_steamapps_dirs, find_steam_libraries
from Utils.operations import acquire_resource_operation


DISABLED_SUFFIX = ' (Disabled)'


def content_roots(app_id, game=None):
    app_id = _numeric_id(app_id)
    libraries = list(all_steamapps_dirs())
    libraries.extend(path.parent for path in find_steam_libraries())
    game_path = game.get_game_path() if game is not None else None
    if game_path:
        libraries.extend(path for path in Path(game_path).parents if path.name.lower() == 'steamapps')
    return tuple(dict.fromkeys(path.resolve() / 'workshop' / 'content' / app_id for path in libraries))


def _numeric_id(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError('Invalid Steam Workshop ID.')
    return value


def _directory(path):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(mode):
        raise ValueError(f'Workshop content must be a directory, not a file or symbolic link: {path}')
    return True


def locations(roots, item_id):
    item_id = _numeric_id(item_id)
    found = []
    for root in roots:
        active = root / item_id
        disabled = root / (item_id + DISABLED_SUFFIX)
        present, parked = _directory(active), _directory(disabled)
        if present or parked:
            found.append((active, disabled, present, parked))
    return found


def content_state(roots, item_id):
    found = locations(roots, item_id)
    if any(parked for _active, _disabled, _present, parked in found):
        state = False
    elif any(present and next(active.iterdir(), None) is not None
             for active, _disabled, present, _parked in found):
        state = True
    else:
        return None, 'Waiting for Steam to download this subscription.'
    paths = '\n'.join(str(disabled if parked else active) for active, disabled, _present, parked in found)
    warning = ''
    if state is False and any(present and next(active.iterdir(), None) is not None
                             for active, _disabled, present, _parked in found):
        warning = '\nSteam has restored content in an original folder. It may load in-game; '
        warning += 'move that content aside before enabling to restore the disabled copy.'
    return state, paths + warning


def _move(active, disabled, enabled):
    from Utils.atomic_write import rename_new

    present, parked = _directory(active), _directory(disabled)
    if enabled:
        if not parked:
            return False
        if present:
            active.rmdir()
        try:
            rename_new(disabled, active)
        except OSError:
            if not active.exists():
                active.mkdir()
            raise
    else:
        if parked:
            if present and next(active.iterdir(), None) is not None:
                raise ValueError(f'Steam restored content at {active}; the disabled copy is preserved at {disabled}.')
            return False
        if not present or next(active.iterdir(), None) is None:
            return False
        rename_new(active, disabled)
        try:
            active.mkdir()
        except OSError as exc:
            try:
                rename_new(disabled, active)
            except OSError:
                raise OSError(f'Could not leave an empty Steam folder at {active}. '
                              f'Your content is preserved at {disabled}: {exc}') from exc
            raise
    return True


def set_enabled(roots, app_id, item_id, enabled):
    _numeric_id(app_id)
    _numeric_id(item_id)
    if type(enabled) is not bool:
        raise ValueError('Enabled must be a boolean.')
    with acquire_resource_operation(f'steam-workshop:{app_id}:{item_id}', 'workshop_toggle', blocking=False):
        found = locations(roots, item_id)
        if not found:
            raise ValueError('Steam has not downloaded this subscription yet.')
        for active, disabled, present, parked in found:
            if parked and present and next(active.iterdir(), None) is not None:
                raise ValueError(f'Steam restored content at {active}. Move it aside before restoring '
                                 f'the disabled copy at {disabled}; neither copy has been overwritten.')
        changed = []
        try:
            for active, disabled, _present, _parked in found:
                if _move(active, disabled, enabled):
                    changed.append((active, disabled))
        except Exception as exc:
            failures = []
            for active, disabled in reversed(changed):
                try:
                    _move(active, disabled, not enabled)
                except Exception as rollback:
                    failures.append(str(rollback))
            if failures:
                raise OSError(f'{exc}\nCould not restore every library: ' + '\n'.join(failures)) from exc
            raise
