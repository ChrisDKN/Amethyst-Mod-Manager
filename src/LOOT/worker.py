from __future__ import annotations

import atexit
import json
import os
import select
import shutil
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path

from Utils.environment.temp import make_tracked_tmpdir, sweep_stale_tmpdirs


_TIMEOUT = 180.0
_workers = weakref.WeakSet()
_sweep_lock = threading.Lock()
_swept = False


class LootWorker:
    """One native LOOT session in a disposable process."""

    def __init__(self, log_fn=None, cancelled=None):
        global _swept
        with _sweep_lock:
            if not _swept:
                sweep_stale_tmpdirs("amethyst-loot-worker-")
                _swept = True
        self.log = log_fn or (lambda _: None)
        self.cancelled = cancelled or (lambda: False)
        self.root = make_tracked_tmpdir("amethyst-loot-worker-")
        self.process = None
        self._buffer = b""
        self._stderr = (self.root / "stderr.log").open("w+b")
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(str(Path(p or os.getcwd()).absolute()) for p in sys.path)
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-m", "LOOT.worker", str(self.root), str(os.getpid())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                env=env,
            )
            os.set_blocking(self.process.stdin.fileno(), False)
        except BaseException:
            self.close()
            raise
        _workers.add(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()
        self._stderr.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def call(self, operation: str, *, timeout: float | None = None, **arguments):
        if self.process is None:
            raise RuntimeError("LOOT worker is no longer running.")
        deadline = time.monotonic() + (_TIMEOUT if timeout is None else timeout)
        stage = operation
        try:
            payload = json.dumps({"operation": operation, **arguments}, ensure_ascii=True).encode() + b"\n"
            pending = memoryview(payload)
            while pending:
                if self.cancelled():
                    raise RuntimeError("LOOT cancelled.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("LOOT worker timed out while receiving its input.")
                _, ready, _ = select.select([], [self.process.stdin], [], min(remaining, 0.2))
                if ready:
                    pending = pending[os.write(self.process.stdin.fileno(), pending):]
            while True:
                if self.cancelled():
                    raise RuntimeError("LOOT cancelled.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"LOOT timed out while {stage}. The worker was stopped. "
                        "A plugin or archive may be unreadable, or libloot may have stalled.")
                if b"\n" not in self._buffer:
                    ready, _, _ = select.select([self.process.stdout], [], [], min(remaining, 0.2))
                    if not ready:
                        continue
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk:
                        self._stderr.seek(max(0, os.fstat(self._stderr.fileno()).st_size - 4000))
                        detail = self._stderr.read()[-4000:].decode("utf-8", "replace").strip()
                        raise RuntimeError(f"LOOT worker exited while {stage}. {detail}".strip())
                    self._buffer += chunk
                    continue
                line, self._buffer = self._buffer.split(b"\n", 1)
                response = json.loads(line)
                if "log" in response:
                    stage = response["log"]
                    self.log(stage)
                elif "error" in response:
                    raise RuntimeError(response["error"])
                else:
                    return response["result"]
        except BaseException:
            self.close()
            raise


@atexit.register
def _cleanup():
    for worker in list(_workers):
        worker.close()


def _serve(root: Path, parent_pid: int):
    import ctypes
    import logging
    import signal

    # A killed application cannot run finally/atexit cleanup.
    if sys.platform == "linux":
        ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0)
        if os.getppid() != parent_pid:
            return

    def send(value):
        print(json.dumps(value, ensure_ascii=True), flush=True)

    errors = []

    class NativeLog(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            if record.levelno >= logging.ERROR:
                errors.append((record.name, message))
            send({"log": message})

    logging.getLogger().addHandler(NativeLog())
    logging.getLogger().setLevel(logging.WARNING)
    import loot
    from LOOT.game_view import condition_directories
    from LOOT.loot_sorter import _load_plugins, _sort_game

    game = None
    eligibility_games = {}
    for line in sys.stdin:
        errors.clear()
        try:
            request = json.loads(line)
            operation = request.pop("operation")
            if operation == "prepare":
                game = loot.Game(getattr(loot.GameType, request["game_type"]),
                                 request["game_root"], request["local"])
                db = game.database()
                if request.get("masterlist"):
                    if request.get("prelude"):
                        db.load_masterlist_with_prelude(request["masterlist"], request["prelude"])
                    else:
                        db.load_masterlist(request["masterlist"])
                if request.get("userlist"):
                    db.load_userlist(request["userlist"])
                game.set_additional_data_paths([])
                result = {
                    "active_path": str(game.active_plugins_file_path()),
                    "directories": sorted(condition_directories(
                        db, request["plugin_names"], request["data_relative"]))
                    if request.get("masterlist") else [],
                }
            elif operation == "sort":
                from dataclasses import asdict
                result = asdict(_sort_game(game, log_fn=lambda m: send({"log": m}), **request))
            elif operation == "overlap":
                _load_plugins(game, request["paths"], lambda m: send({"log": m}))
                target = game.plugin(request["target"])
                if target is None:
                    raise RuntimeError(f"LOOT could not load {request['target']}.")
                result = [name for name in request["plugin_names"]
                          if name.lower() != request["target"].lower()
                          and (other := game.plugin(name)) is not None
                          and target.do_records_overlap(other)]
            elif operation == "eligibility":
                game_type = request["game_type"]
                if game_type not in eligibility_games:
                    folder = root / game_type
                    data = folder / "Data"
                    data.mkdir(parents=True)
                    eligibility_games[game_type] = (
                        loot.Game(getattr(loot.GameType, game_type), str(folder), str(folder)), data)
                eligibility_game, data = eligibility_games[game_type]
                source = Path(request["path"])
                staged = data / source.name
                staged.unlink(missing_ok=True)
                staged.symlink_to(source)
                try:
                    eligibility_game.load_plugins([str(staged)])
                    plugin = eligibility_game.plugin(source.name)
                    result = plugin is not None and bool(plugin.is_valid_as_light_plugin())
                finally:
                    staged.unlink(missing_ok=True)
            else:
                raise ValueError(f"Unknown LOOT operation: {operation}")
            archive_errors = []
            fatal_errors = []
            for name, message in errors:
                if name.startswith("libloot.archive.") and operation in ("sort", "overlap"):
                    archive_errors.append(message)
                else:
                    fatal_errors.append(message)
            if fatal_errors:
                raise RuntimeError("LOOT could not read its input: " + "\n".join(fatal_errors[:3]))
            if archive_errors and operation == "sort":
                warning = ("LOOT could not read these archives; their asset data was excluded. "
                           "Repair them and run LOOT again:\n"
                           + "\n".join(dict.fromkeys(archive_errors)))
                result["warnings"].append(warning)
                result["general_messages"].append({"type": "warn", "text": warning})
            send({"result": result})
        except Exception as exc:
            send({"error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    _serve(Path(sys.argv[1]), int(sys.argv[2]))
