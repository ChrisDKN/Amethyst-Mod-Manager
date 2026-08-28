#!/usr/bin/env python3
"""Entry point for the Qt (PySide6) UI.

Run from src/ so the gui_qt / Utils / Games packages import cleanly:

    ../.venv/bin/python3 run_qt.py
"""

import os
import sys
import time

_PYTHON_ENTRY_STARTED = time.perf_counter()
_STARTUP_STARTED = _PYTHON_ENTRY_STARTED
_LAUNCHER_TIMING_PRESENT = False
try:
    _launch_wall_started = float(
        os.environ.pop("_AMM_LAUNCH_WALL_STARTED", ""))
    _launcher_elapsed = time.time() - _launch_wall_started
    # Ignore a stale/injected marker or an implausible wall-clock jump.  The
    # launcher normally hands this across for well under a minute; the wider
    # bound still covers a first dependency installation.
    if 0.0 <= _launcher_elapsed <= 3600.0:
        _STARTUP_STARTED -= _launcher_elapsed
        _LAUNCHER_TIMING_PRESENT = True
except (TypeError, ValueError):
    pass

_BOOTSTRAP_IMPORT_STARTED = _PYTHON_ENTRY_STARTED
import app_bootstrap
_BOOTSTRAP_SETUP_STARTED = time.perf_counter()

_BOOTSTRAP_TIMINGS = app_bootstrap.setup_environment()
_BOOTSTRAP_FINISHED = time.perf_counter()

from Utils import perftrace

_startup_timing = (perftrace.StartupTimeline(_STARTUP_STARTED)
                   if perftrace.is_enabled() else None)
if _startup_timing is not None:
    if _LAUNCHER_TIMING_PRESENT:
        _startup_timing.record(
            "Run source launcher and start Python",
            phase_started=_STARTUP_STARTED,
            phase_finished=_PYTHON_ENTRY_STARTED, category="launcher")
    _startup_timing.record(
        "Import startup bootstrap", phase_started=_BOOTSTRAP_IMPORT_STARTED,
        phase_finished=_BOOTSTRAP_SETUP_STARTED, category="imports")
    for _label, _started, _finished, _category in _BOOTSTRAP_TIMINGS:
        _startup_timing.record(
            _label, phase_started=_started, phase_finished=_finished,
            category=_category)
    _startup_timing.record(
        "Load startup timing support", phase_started=_BOOTSTRAP_FINISHED,
        category="diagnostics")
perftrace.set_startup_timeline(_startup_timing)

_GUI_IMPORT_STARTED = time.perf_counter()
from gui_qt.app import run
if _startup_timing is not None:
    _startup_timing.record(
        "Import complete Qt application (aggregate)",
        phase_started=_GUI_IMPORT_STARTED, category="aggregate")

if __name__ == "__main__":
    sys.exit(run(startup_timing=_startup_timing))
