from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

from Utils.games.instances import instance_id


_current = ContextVar("launch_logs", default=None)
ENV_KEYS = ("UMU_LOG", "PROTON_LOG", "PROTON_LOG_DIR")


def log_directory(game, executable=None) -> Path:
    profile = getattr(game, "_active_profile_dir", None)
    identity = (str(instance_id(game) or game.name), str(profile or ""),
                str(Path(executable).resolve()) if executable else "__game__")
    key = hashlib.sha256(repr(identity).encode()).hexdigest()[:24]
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "AmethystModManager" / "launch-logs" / key


def latest_run(directory: Path) -> Path | None:
    try:
        name = (directory / "latest").read_text(encoding="utf-8").strip()
        if name.startswith("run-") and Path(name).name == name:
            return directory / name
    except OSError:
        pass
    return None


def latest_log(directory: Path, kind: str) -> Path | None:
    run = latest_run(directory)
    if run is None:
        return None
    if kind == "umu":
        return run / "umu.log"
    try:
        return max((run / "proton").glob("*.log"),
                   key=lambda path: path.stat().st_mtime_ns, default=None)
    except OSError:
        return None


def active() -> bool:
    return _current.get() is not None


def apply_environment(env) -> None:
    run = _current.get()
    if run is not None and env is not None:
        env.update(UMU_LOG="debug", PROTON_LOG="1",
                   PROTON_LOG_DIR=str(run / "proton"))
        mounts = [p for p in env.get("STEAM_COMPAT_MOUNTS", "").split(":") if p]
        if str(run) not in mounts:
            env["STEAM_COMPAT_MOUNTS"] = ":".join([*mounts, str(run)])


def open_output():
    run = _current.get()
    return (run / "umu.log").open("a+b", buffering=0) if run else None


@contextmanager
def capture(game, executable=None, *, label: str, log_fn):
    directory = log_directory(game, executable)
    run = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        previous = latest_run(directory)
        run = Path(tempfile.mkdtemp(prefix="run-", dir=directory))
        (run / "proton").mkdir()
        (run / "umu.log").write_text(f"{label}\n", encoding="utf-8")
        pointer = directory / (run.name + ".latest")
        pointer.write_text(run.name, encoding="utf-8")
        pointer.replace(directory / "latest")
        for old in directory.glob("run-*"):
            if old.is_dir() and old not in (run, previous):
                shutil.rmtree(old, ignore_errors=True)
    except OSError as exc:
        log_fn(f"Could not prepare launch logs: {exc}")
        run = None

    def log(message):
        if run is not None:
            try:
                with (run / "umu.log").open("a", encoding="utf-8") as output:
                    output.write(str(message) + "\n")
            except OSError:
                pass
        log_fn(message)

    token = _current.set(run)
    try:
        yield log
    finally:
        _current.reset(token)
