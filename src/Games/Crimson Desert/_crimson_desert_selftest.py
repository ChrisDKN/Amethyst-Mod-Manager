"""Focused tests for the fail-closed Crimson Desert backend adapter."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock


def _load_backend():
    path = Path(__file__).with_name("crimson_desert_backend.py")
    spec = importlib.util.spec_from_file_location("crimson_backend_selftest", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load backend module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run() -> None:
    backend = _load_backend()
    with mock.patch.dict(os.environ, {}, clear=True):
        assert backend.discover_backend() is None

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "src" / "cdumm" / "main.py"
        main.parent.mkdir(parents=True)
        main.write_text("", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"AMETHYST_CDUMM_ROOT": str(root)},
            clear=True,
        ):
            command = backend.discover_backend()
        assert command is not None
        assert command.arguments == ("-m", "cdumm.main")
        assert command.cwd == root

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "worker.py"
        script.write_text(
            "import json, sys\n"
            "print(json.dumps({'type': 'error', 'msg': 'blocked'}))\n"
            "sys.exit(1)\n",
            encoding="utf-8",
        )
        command = backend.BackendCommand(sys.executable, (str(script),))
        try:
            backend.run_worker(command, ("self_check",))
        except backend.CrimsonBackendError as e:
            assert "blocked" in str(e)
        else:
            raise AssertionError("backend errors must fail closed")

    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "game"
        db_path = game_dir / "CDMods" / "cdumm.db"
        db_path.parent.mkdir(parents=True)
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "CREATE TABLE mods ("
                "id INTEGER PRIMARY KEY, enabled INTEGER, priority INTEGER)"
            )
            connection.executemany(
                "INSERT INTO mods (id, enabled, priority) VALUES (?, ?, ?)",
                [(1, 1, 7), (2, 0, 8), (3, 1, 99)],
            )
        command = backend.BackendCommand("unused", ())
        backend.configure_mods(command, game_dir, [1, 2], [2, 1])
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute(
                "SELECT id, enabled, priority FROM mods ORDER BY id"
            ).fetchall()
        assert rows == [(1, 1, 2), (2, 1, 1), (3, 1, 99)]

        try:
            backend.configure_mods(command, game_dir, [1, 404], [1])
        except backend.CrimsonBackendError as e:
            assert "no longer exist" in str(e)
        else:
            raise AssertionError("stale CDUMM mappings must fail closed")


if __name__ == "__main__":
    run()
    print("Crimson Desert backend self-test passed.")
