"""
dtkit_patch_helper.py
Non-GUI helpers for running dtkit-patch under Proton.

The Darktide Mod Loader (DML) mod ships the current Windows ``dtkit-patch.exe``
in its ``tools/`` folder.  Once the modlist is deployed, that exe lands in
``<game>/tools/dtkit-patch.exe`` (via Darktide's custom routing rules).  We run
it under the game's Proton prefix, mirroring DML's ``toggle_darktide_mods.bat``:

    cd <game>
    .\\tools\\dtkit-patch --toggle .\\bundle

Running the shipped exe under Proton (instead of a separately-downloaded native
Linux build) keeps the patcher version in lock-step with the user's DML install,
which is required after every game update.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from Utils.app_log import safe_log as _safe_log

# Relative location of the patcher exe / bundle inside the deployed game folder
# (DML's layout, mirrored by Darktide's custom routing rules).
_DTKIT_REL = "tools/dtkit-patch.exe"
_BUNDLE_REL = "bundle"


# ---------------------------------------------------------------------------
# Locating the deployed exe
# ---------------------------------------------------------------------------

def find_deployed_dtkit_exe(game_path: "Path | None") -> "Path | None":
    """Return ``<game_path>/tools/dtkit-patch.exe`` if it exists, else None.

    The exe is placed there when the Darktide Mod Loader mod is deployed.
    """
    if game_path is None:
        return None
    candidate = Path(game_path) / _DTKIT_REL
    return candidate if candidate.is_file() else None


# ---------------------------------------------------------------------------
# Running under Proton
# ---------------------------------------------------------------------------

def run_dtkit_patch_proton(
    game,
    flag: str = "--toggle",
    log_fn: "Callable[[str], None] | None" = None,
    line_fn: "Callable[[str], None] | None" = None,
) -> bool:
    """Run the deployed ``dtkit-patch.exe`` against ``<game>/bundle`` via Proton.

    Mirrors DML's ``toggle_darktide_mods.bat`` (``--toggle`` flips the patched
    state).  cwd is the game folder so the relative ``bundle`` path resolves the
    same way the .bat does.

    *log_fn* receives diagnostic/log lines; *line_fn* (optional) receives each
    line of the patcher's own stdout/stderr for live display.  Returns True on
    a zero exit code, False otherwise (errors are logged, not raised).
    """
    _log = _safe_log(log_fn)
    _emit = line_fn or (lambda _l: None)

    game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
    if game_path is None or not Path(game_path).is_dir():
        _log("dtkit-patch: game path is not configured.")
        return False

    exe = find_deployed_dtkit_exe(game_path)
    if exe is None:
        _log(
            "dtkit-patch: tools/dtkit-patch.exe not found in the game folder. "
            "Deploy the Darktide Mod Loader mod first."
        )
        return False

    from Utils.protontricks import build_proton_env_for_game
    from Utils.steam_finder import proton_run_command

    proton_script, env = build_proton_env_for_game(game)
    if proton_script is None:
        _log(
            "dtkit-patch: could not resolve a Proton install / prefix for the "
            "game. Launch the game once through Steam to create its prefix."
        )
        return False

    # Flatpak without the Compat.i386 extension: wine would die with the
    # cryptic "/lib/ld-linux.so.2: could not open" - fail with the fix instead.
    from Utils.flatpak_i386 import preflight_i386_error
    i386_err = preflight_i386_error(proton_script)
    if i386_err:
        _log(f"dtkit-patch: {i386_err}")
        _emit(i386_err)
        return False

    cmd = proton_run_command(proton_script, "runinprefix", str(exe), flag,
                             _BUNDLE_REL, env=env)
    _log(f"dtkit-patch: running {exe.name} {flag} {_BUNDLE_REL} via Proton (cwd={game_path})")
    try:
        result = subprocess.run(
            cmd,
            env=env,
            cwd=str(game_path),
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        _log(f"dtkit-patch: failed to run: {exc}")
        return False

    for line in result.stdout.strip().splitlines():
        _emit(line)
        _log(f"dtkit-patch: {line}")
    for line in result.stderr.strip().splitlines():
        _emit(f"[stderr] {line}")
        _log(f"dtkit-patch [stderr]: {line}")

    if result.returncode == 0:
        _log("dtkit-patch: done.")
        return True
    _log(f"dtkit-patch: exited with code {result.returncode}.")
    return False
