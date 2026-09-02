"""Track PID-tagged temporary directories and sweep crash leftovers."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_CREATED: list[Path] = []


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    return True


def make_tracked_tmpdir(prefix: str) -> Path:
    """mkdtemp with the owner pid in the name; removed at exit or by a later sweep."""
    d = Path(tempfile.mkdtemp(prefix=f"{prefix}{os.getpid()}_"))
    _CREATED.append(d)
    return d


def sweep_stale_tmpdirs(prefix: str) -> None:
    """Remove *prefix*-matching temp dirs whose owning process is gone.

    /tmp is RAM-backed tmpfs on SteamOS, so dirs leaked by killed/crashed
    instances (atexit never ran) cost memory until someone removes them.
    Dirs without a parseable pid (pre-pid-tag format) are treated as stale.
    """
    for d in Path(tempfile.gettempdir()).glob(prefix + "*"):
        if d in _CREATED:
            continue
        pid_s = d.name[len(prefix):].split("_", 1)[0]
        if pid_s.isdigit() and _pid_alive(int(pid_s)):
            continue
        shutil.rmtree(d, ignore_errors=True)


def _cleanup() -> None:
    for d in _CREATED:
        shutil.rmtree(d, ignore_errors=True)
    _CREATED.clear()


atexit.register(_cleanup)
