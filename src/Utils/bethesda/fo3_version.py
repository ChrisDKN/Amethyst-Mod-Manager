"""Detect the Fallout 3 Anniversary Edition patch, which FOSE cannot load.

Bethesda's 2024 "Anniversary" patch bumped Fallout3.exe to 1.7.0.4. FOSE (and
every mod that needs it) only ever targeted 1.7.0.3, so a modlist deployed on
top of 1.7.0.4 silently does nothing. The Fallout Anniversary Patcher wizard
downgrades the exe back to 1.7.0.3 - this module is what tells the deploy path
to point users at it.
"""

from __future__ import annotations

from pathlib import Path

# The Anniversary Edition build. The pre-Anniversary exe FOSE targets reports
# 1.7.0.3, so an exact match is what we key on rather than a >= comparison:
# an unknown future build should not be labelled "run the downgrader".
ANNIVERSARY_VERSION = "1.7.0.4"

# The version the patcher produces - used only to recognise an already-fixed
# install in logs.
DOWNGRADED_VERSION = "1.7.0.3"

# The game exe, NOT the launcher. Fallout 3's ``exe_name`` is
# Fallout3Launcher.exe (versioned 1.3.1.0 regardless of the game patch), so the
# version lives on Fallout3.exe alone.
GAME_EXE_NAME = "Fallout3.exe"

# Handler game_ids this check applies to (base game + GOTY).
_FO3_GAME_IDS = {"Fallout3", "Fallout3GOTY"}


def is_fallout_3(game) -> bool:
    """True when *game* is the Fallout 3 handler or its GOTY subclass."""
    try:
        return str(getattr(game, "game_id", "")) in _FO3_GAME_IDS
    except Exception:
        return False


def game_exe_path(game) -> "Path | None":
    """Path to Fallout3.exe in the configured game folder, or None."""
    try:
        root = game.get_game_path()
    except Exception:
        return None
    if root is None:
        return None
    exe = Path(root) / GAME_EXE_NAME
    return exe if exe.is_file() else None


def needs_downgrade(game) -> bool:
    """True when *game* is Fallout 3 and its exe is the Anniversary build.

    Never raises and never guesses: an unreadable or missing exe, or a version
    string we can't parse, returns False so deploy is not blocked by a check
    that failed to run.
    """
    if not is_fallout_3(game):
        return False
    exe = game_exe_path(game)
    if exe is None:
        return False
    try:
        from Utils.executables.icon import extract_exe_version
        return extract_exe_version(exe) == ANNIVERSARY_VERSION
    except Exception:
        return False
