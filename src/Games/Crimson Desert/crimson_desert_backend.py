"""Adapter for the archive-aware Crimson Desert backend.

Amethyst deliberately runs CDUMM out of process.  CDUMM uses PySide6 while
Amethyst uses PyQt6, and importing both Qt bindings into one process is unsafe.
The subprocess boundary also gives us a small JSON-lines protocol that can be
validated before any game files are changed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path


class CrimsonBackendError(RuntimeError):
    """Raised when the Crimson Desert backend is missing or reports failure."""


@dataclass(frozen=True)
class BackendCommand:
    executable: str
    arguments: tuple[str, ...]
    cwd: Path | None = None

    def argv(self, *extra: str) -> list[str]:
        return [self.executable, *self.arguments, *extra]


def discover_backend() -> BackendCommand | None:
    """Find a source checkout or standalone CDUMM executable.

    ``AMETHYST_CDUMM_COMMAND`` is the stable integration point.  For local
    development, ``AMETHYST_CDUMM_ROOT`` may point at a CDUMM source checkout.
    No implicit download or installation happens here.
    """
    configured = os.environ.get("AMETHYST_CDUMM_COMMAND", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return BackendCommand(str(candidate), ())

    root_value = os.environ.get("AMETHYST_CDUMM_ROOT", "").strip()
    if root_value:
        root = Path(root_value).expanduser().resolve()
        main_module = root / "src" / "cdumm" / "main.py"
        if main_module.is_file():
            venv_python = root / ".venv" / "bin" / "python"
            interpreter = venv_python if venv_python.is_file() else Path(sys.executable)
            return BackendCommand(
                str(interpreter),
                ("-m", "cdumm.main"),
                cwd=root,
            )
    return None


def run_worker(
    command: BackendCommand,
    worker_args: Iterable[str],
    *,
    log_fn: Callable[[str], None] | None = None,
    timeout: float = 120.0,
) -> dict:
    """Run one CDUMM worker command and return its final ``done`` message."""
    log = log_fn or (lambda _message: None)
    env = os.environ.copy()
    if command.cwd is not None:
        source_dir = str(command.cwd / "src")
        current = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = source_dir if not current else f"{source_dir}{os.pathsep}{current}"

    try:
        completed = subprocess.run(
            command.argv("--worker", *worker_args),
            cwd=str(command.cwd) if command.cwd else None,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CrimsonBackendError(f"Could not run Crimson backend: {e}") from e

    result: dict | None = None
    errors: list[str] = []
    for raw_line in completed.stdout.splitlines():
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError:
            log(raw_line)
            continue
        message_type = message.get("type")
        if message_type == "done":
            result = message
        elif message_type == "error":
            errors.append(str(message.get("msg", "Unknown backend error")))
        else:
            log(str(message.get("msg") or message))

    if completed.returncode != 0 or errors or result is None:
        detail = "; ".join(errors)
        if not detail:
            detail = completed.stderr.strip() or "backend returned no result"
        raise CrimsonBackendError(detail)
    return result


def self_check(command: BackendCommand, *, timeout: float = 120.0) -> dict:
    """Validate backend imports without reading or modifying game files."""
    return run_worker(command, ("self_check",), timeout=timeout)


def probe_game(command: BackendCommand, game_dir: Path, *, timeout: float = 120.0) -> dict:
    """Parse the live archive indexes without writing game or backend state."""
    if command.cwd is None:
        raise CrimsonBackendError(
            "The standalone backend does not expose the read-only probe yet."
        )
    probe = Path(__file__).with_name("crimson_desert_probe.py")
    env = os.environ.copy()
    source_dir = str(command.cwd / "src")
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = source_dir if not current else f"{source_dir}{os.pathsep}{current}"
    try:
        completed = subprocess.run(
            [command.executable, str(probe), str(game_dir)],
            cwd=str(command.cwd),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CrimsonBackendError(f"Could not probe Crimson Desert: {e}") from e
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as e:
        detail = completed.stderr.strip() or "probe returned no JSON result"
        raise CrimsonBackendError(detail) from e
    if completed.returncode != 0 or not result.get("ok"):
        raise CrimsonBackendError("; ".join(result.get("errors", [])))
    return result


def storage_paths(game_dir: Path) -> tuple[Path, Path, Path]:
    """Return CDUMM's database, delta and vanilla-snapshot paths."""
    root = game_dir / "CDMods"
    return root / "cdumm.db", root / "deltas", root / "vanilla"


def ensure_snapshot(
    command: BackendCommand,
    game_dir: Path,
    *,
    log_fn: Callable[[str], None] | None = None,
    timeout: float = 600.0,
) -> dict:
    """Create CDUMM's hash baseline once, before the first real apply."""
    db_path, _deltas_dir, _vanilla_dir = storage_paths(game_dir)
    if db_path.is_file():
        try:
            with sqlite3.connect(db_path) as connection:
                row = connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()
            if row and int(row[0]) > 0:
                return {"type": "done", "count": int(row[0]), "existing": True}
        except sqlite3.Error:
            pass
    return run_worker(
        command, ("snapshot", str(game_dir), str(db_path)),
        log_fn=log_fn, timeout=timeout,
    )


def import_mod(
    command: BackendCommand,
    game_dir: Path,
    source: Path,
    *,
    existing_mod_id: int | None = None,
    log_fn: Callable[[str], None] | None = None,
    timeout: float = 300.0,
) -> dict:
    """Import one archive-aware source into CDUMM without applying it."""
    db_path, deltas_dir, _vanilla_dir = storage_paths(game_dir)
    args = ["import", str(source), str(game_dir), str(db_path), str(deltas_dir)]
    if existing_mod_id is not None:
        args.append(str(existing_mod_id))
    return run_worker(
        command,
        tuple(args),
        log_fn=log_fn,
        timeout=timeout,
    )


def _run_cli(
    command: BackendCommand,
    args: Iterable[str],
    timeout: float = 120.0,
) -> str:
    env = os.environ.copy()
    if command.cwd is not None:
        source_dir = str(command.cwd / "src")
        current = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = source_dir if not current else f"{source_dir}{os.pathsep}{current}"
    completed = subprocess.run(
        command.argv(*args), cwd=str(command.cwd) if command.cwd else None,
        env=env, text=True, capture_output=True,
        timeout=timeout, check=False,
    )
    if completed.returncode != 0:
        raise CrimsonBackendError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout


def configure_mods(
    command: BackendCommand,
    game_dir: Path,
    managed_ids: Iterable[int],
    ordered_enabled_ids: Iterable[int],
) -> None:
    """Atomically mirror Amethyst enable state and order into CDUMM.

    Amethyst supplies enabled mods from low to high priority. Unmanaged CDUMM
    rows are deliberately left untouched.
    """
    del command  # The database path is CDUMM's stable on-disk integration API.
    db_path, _deltas_dir, _vanilla_dir = storage_paths(game_dir)
    managed = [int(mod_id) for mod_id in managed_ids]
    ordered = [int(mod_id) for mod_id in ordered_enabled_ids]
    if len(ordered) != len(set(ordered)):
        raise CrimsonBackendError("Duplicate CDUMM IDs in the Amethyst profile")
    if not set(ordered).issubset(managed):
        raise CrimsonBackendError("Enabled CDUMM IDs are missing from the managed set")
    try:
        with sqlite3.connect(db_path, timeout=10.0) as connection:
            if managed:
                placeholders = ",".join("?" for _ in managed)
                rows = connection.execute(
                    f"SELECT id FROM mods WHERE id IN ({placeholders})", managed
                ).fetchall()
                found = {int(row[0]) for row in rows}
                missing = sorted(set(managed) - found)
                if missing:
                    raise CrimsonBackendError(
                        "Mapped CDUMM mods no longer exist: "
                        + ", ".join(str(mod_id) for mod_id in missing)
                    )
                connection.execute(
                    f"UPDATE mods SET enabled = 0 WHERE id IN ({placeholders})",
                    managed,
                )
            for priority, mod_id in enumerate(ordered, start=1):
                connection.execute(
                    "UPDATE mods SET enabled = 1, priority = ? WHERE id = ?",
                    (priority, mod_id),
                )
            connection.commit()
    except sqlite3.Error as e:
        raise CrimsonBackendError(f"Could not update CDUMM profile state: {e}") from e


def list_mods(command: BackendCommand, game_dir: Path) -> list[dict]:
    output = _run_cli(
        command,
        ("list-mods", "--game-dir", str(game_dir), "--status", "--json"),
    )
    try:
        value = json.loads(output)
    except json.JSONDecodeError as e:
        raise CrimsonBackendError("CDUMM returned an invalid mod list") from e
    return value if isinstance(value, list) else []


def apply(
    command: BackendCommand,
    game_dir: Path,
    *,
    log_fn=None,
    timeout: float = 300.0,
) -> dict:
    db_path, _deltas_dir, vanilla_dir = storage_paths(game_dir)
    return run_worker(
        command, ("apply", str(game_dir), str(vanilla_dir), str(db_path), "0"),
        log_fn=log_fn, timeout=timeout,
    )


def revert(
    command: BackendCommand,
    game_dir: Path,
    *,
    log_fn=None,
    timeout: float = 300.0,
) -> dict:
    db_path, _deltas_dir, vanilla_dir = storage_paths(game_dir)
    return run_worker(
        command, ("revert", str(game_dir), str(vanilla_dir), str(db_path)),
        log_fn=log_fn, timeout=timeout,
    )
