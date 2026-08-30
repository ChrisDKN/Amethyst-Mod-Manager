"""Shared startup hardening for cli.py and run_qt.py (AppImage/flatpak aware).

Importing this module before any Utils/Games/gui_qt import works because
Python puts the launched script's directory at sys.path[0].
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def setup_environment() -> list[tuple[str, float, float, str]]:
    """Prepare the runtime and return early-startup timing intervals.

    This module must run before importing Utils, so it cannot use the shared
    startup timer directly.  The entry point replays these standard-library
    timestamps into that timer immediately afterwards.
    """
    timings: list[tuple[str, float, float, str]] = []
    diagnostics: list[str] = []
    phase_started = time.perf_counter()
    # Drop dead /tmp/.mount_* entries from sys.path. Older AppImage builds
    # exported PYTHONPATH globally; a shell launched from the GUI inherits a
    # path pointing at a mount that vanishes the moment the AppImage exits.
    old_path = list(sys.path)
    sys.path[:] = [p for p in sys.path if not (
        p.startswith("/tmp/.mount_") and not Path(p).is_dir())]
    removed_paths = len(old_path) - len(sys.path)
    if removed_paths:
        diagnostics.append(
            f"Startup paths: removed {removed_paths} stale AppImage path(s).")

    src = Path(__file__).resolve().parent
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    # When running inside the current AppImage, add the bundled _vendor dir
    # so we can find Pillow / other deps. The launcher used to do this via
    # PYTHONPATH, which leaked into child shells.
    if os.environ.get("APPDIR"):
        vendor = Path(os.environ["APPDIR"]) / "share" / "amethyst-mod-manager" / "_vendor"
        if vendor.is_dir() and str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
            diagnostics.append(f"Startup paths: using bundled vendor directory: {vendor}")

    # Drop a stale MOD_MANAGER_GAMES pointing at /tmp/.mount_* (same leak path).
    # "Stale" is NOT just "gone": a previous AppImage's mount can still be alive
    # when we start (self-update relaunch, second instance), and inheriting it
    # made every handler load from a mount that vanished mid-discovery (GH#340).
    # Any /tmp/.mount_* path that isn't inside OUR $APPDIR is someone else's.
    mmg = os.environ.get("MOD_MANAGER_GAMES", "")
    if mmg.startswith("/tmp/.mount_"):
        appdir = os.environ.get("APPDIR", "")
        try:
            ours = bool(appdir) and Path(mmg).resolve().is_relative_to(
                Path(appdir).resolve())
        except Exception:
            ours = False
        if not ours or not Path(mmg).is_dir():
            os.environ.pop("MOD_MANAGER_GAMES", None)
            diagnostics.append(
                "Startup paths: removed stale MOD_MANAGER_GAMES from another AppImage.")
    # Set MOD_MANAGER_GAMES so game_loader can find the Games/ directory.
    if not os.environ.get("MOD_MANAGER_GAMES"):
        games_dir = src / "Games"
        if games_dir.is_dir():
            os.environ["MOD_MANAGER_GAMES"] = str(games_dir)
    diagnostics.append(
        "Startup paths: game handlers directory: "
        + (os.environ.get("MOD_MANAGER_GAMES") or "not configured"))
    timings.append(("Prepare Python/runtime paths", phase_started,
                    time.perf_counter(), "bootstrap"))

    # Apply the user's own env vars (Settings ▸ Advanced) before anything reads
    # one - that includes Qt, which latches QT_QPA_PLATFORM / QT_XCB_GL_INTEGRATION
    # when the QApplication is built. Runs after the sys.path setup above (it
    # imports Utils) and after MOD_MANAGER_GAMES, which it isn't allowed to set.
    phase_started = time.perf_counter()
    try:
        from Utils.app_env import apply_saved_env
        apply_saved_env(log_fn=diagnostics.append)
    except Exception as exc:
        diagnostics.append(
            f"Startup environment: loader failed: {type(exc).__name__}: {exc}")
    timings.append(("Load saved environment settings", phase_started,
                    time.perf_counter(), "configuration"))

    # Capture stderr to a file as early as possible - BEFORE any GUI/Qt import -
    # so a crash during startup leaves a trace on disk even when launched from a
    # desktop icon / AppImage with no terminal. This is the in-Python equivalent
    # of run_qt.sh's `2> >(tee …)`, which the AppImage/flatpak builds never run.
    # Native crashes (segfaults) write to fd 2 too, so this + faulthandler cover
    # them. Best-effort; must never block startup.
    phase_started = time.perf_counter()
    try:
        from Utils.stderr_capture import install_stderr_file, install_faulthandler
        stderr_ok = install_stderr_file()
        fault_ok = install_faulthandler()
        diagnostics.append(
            f"Startup diagnostics: stderr capture={'ready' if stderr_ok else 'unavailable'}, "
            f"faulthandler={'ready' if fault_ok else 'unavailable'}.")
    except Exception as exc:
        diagnostics.append(
            f"Startup diagnostics: setup failed: {type(exc).__name__}: {exc}")
    timings.append(("Initialize crash/stderr capture", phase_started,
                    time.perf_counter(), "diagnostics"))

    try:
        from Utils.app_log import app_log
        for message in diagnostics:
            app_log(message)
    except Exception:
        pass
    for message in diagnostics:
        try:
            print(f"[startup] {message}", file=sys.stderr)
        except Exception:
            pass

    # Filegraph is part of Amethyst's runtime, not an optional acceleration.
    # Validate it before profile state can be opened or mutated so a damaged
    # package fails with the loader's actionable reinstall/version message.
    phase_started = time.perf_counter()
    from Utils.filegraph_native import require_native
    require_native()
    timings.append(("Validate native filegraph extension", phase_started,
                    time.perf_counter(), "filegraph"))
    return timings
