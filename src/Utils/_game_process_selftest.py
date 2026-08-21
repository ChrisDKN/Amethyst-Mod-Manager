"""Focused lifecycle tests for :mod:`Utils.game_process`.

Run with::

    PYTHONPATH=src python3 -m Utils._game_process_selftest
"""

from __future__ import annotations

import subprocess
import time

from Utils import game_process
from Utils import launch_report


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def test_marker_building() -> None:
    markers = game_process.prefix_markers("/games/prefix/pfx")
    assert "WINEPREFIX=/games/prefix/pfx" in markers
    assert "WINEPREFIX=/games/prefix" in markers
    assert "STEAM_COMPAT_DATA_PATH=/games/prefix" in markers

    session = game_process.LaunchSession("test")
    with game_process.bind(session):
        command, env = game_process.prepare_spawn(
            ["flatpak-spawn", "--host", "game"], {"BASE": "1"})
    token = f"{game_process.LAUNCH_ID_ENV}={session.token}"
    assert env[game_process.LAUNCH_ID_ENV] == session.token
    assert f"--env={token}" in command
    assert command[-1] == "game"
    print("✓ launch markers cover prefixes and Flatpak forwarding")


def test_tagged_process_stop() -> None:
    states: list[bool] = []
    logs: list[str] = []
    failures: list[str] = []
    session = game_process.LaunchSession("test process", logs.append)
    session.set_state_callback(states.append)

    started = time.monotonic()
    with game_process.bind(session), \
            launch_report.report(failures.append) as report:
        command, env = game_process.prepare_spawn(["sleep", "30"], None)
        proc = subprocess.Popen(command, env=env, start_new_session=True)
        report.mark_spawned()
        game_process.attach_process(proc)
        report.finish()

    try:
        assert _wait_for(
            lambda: proc.pid in (game_process.matching_pids(session.markers)
                                 or set()))
        assert states == [True]
        old_grace = game_process.STOP_GRACE_SECONDS
        game_process.STOP_GRACE_SECONDS = 0.05
        try:
            assert session.stop()
            proc.wait(timeout=5)
            launch_report.mark_exit(
                report, started, proc.returncode, "intentional stop")
            assert _wait_for(lambda: states == [True, False])
        finally:
            game_process.STOP_GRACE_SECONDS = old_grace
        assert proc.returncode is not None
        assert failures == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    print("✓ Stop restores idle state without a false launch-failure report")


def test_genuine_early_exit_still_reports() -> None:
    failures: list[str] = []
    report = launch_report.LaunchReport(failures.append)
    report.mark_spawned()
    launch_report.mark_exit(
        report, time.monotonic(), -9, "genuine early crash")
    assert failures == ["genuine early crash exited immediately with code -9."]
    print("✓ genuine early non-zero exits still report launch failures")


def main() -> None:
    test_marker_building()
    test_tagged_process_stop()
    test_genuine_early_exit_still_reports()
    print("All game-process self-tests passed.")


if __name__ == "__main__":
    main()
