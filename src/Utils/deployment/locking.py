from __future__ import annotations

import errno
import fcntl
import os
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


class DeploymentBusy(RuntimeError):
    pass


@contextmanager
def game_mutation_lock(game):
    root = Path(game.get_profile_root())
    from Utils.environment.sandbox import document_portal_path_hint
    problem = document_portal_path_hint(root)
    if problem:
        raise RuntimeError(problem)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".amethyst-operation.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentBusy("An installation or deployment is already modifying this game's profiles") from exc
        except OSError as exc:
            message = f"Could not lock this game's profiles: {exc.strerror}"
            if exc.errno in (errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP):
                message += (
                    ". The staging filesystem does not support required file "
                    "locking. Select a staging folder with file-locking support "
                    "and direct filesystem access."
                )
            raise OSError(exc.errno, message, str(lock_path)) from exc
        yield
    finally:
        os.close(fd)


def guard_deployment(function):
    @wraps(function)
    def guarded(game, *args, **kwargs):
        from Utils.deployment.pipeline import check_paths_mounted
        error = check_paths_mounted(game)
        if error:
            kwargs["log_fn"](f"Deploy aborted: {error}")
            return False
        try:
            with game_mutation_lock(game):
                return function(game, *args, **kwargs)
        except DeploymentBusy as exc:
            kwargs["log_fn"](f"Deploy aborted: {exc}")
            return False
    return guarded
