"""Adapter for the archive-aware Crimson Desert backend.

Amethyst deliberately runs CDUMM out of process.  CDUMM uses PySide6 while
Amethyst uses PyQt6, and importing both Qt bindings into one process is unsafe.
The subprocess boundary also gives us a small JSON-lines protocol that can be
validated before any game files are changed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
