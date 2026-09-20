"""Profile paths and SnowFixer launcher settings."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from Utils.bethesda.mo2_patcher import prepare_mo2_patcher


APP_DIR = "SnowFixer"
EXE_NAME = "SnowFixer.exe"
OUTPUT_NAME = "SnowFixer_output"
GITHUB_API_URL = "https://api.github.com/repos/Cl3mus33/SnowFixer/releases/latest"


def prepare_snowfixer(game, exe: Path, pfx: Path, profile: str,
                      log_fn: Callable[[str], None] = lambda _msg: None,
                      ui_theme: str | None = None) -> Path:
    settings_file = (pfx / "drive_c" / "users" / "steamuser" / "AppData"
                     / "Roaming" / "SnowFixer" / "settings.json")
    return prepare_mo2_patcher(
        game, exe, pfx, profile,
        tool_name="SnowFixer", output_name=OUTPUT_NAME, settings_file=settings_file,
        log_fn=log_fn, ui_theme=ui_theme,
    )
