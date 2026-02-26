"""
Path and file-picker utilities for the GUI.
Used by TopBar and dialogs (e.g. ExeConfigDialog). No dependency on other gui modules.
"""

import subprocess
from pathlib import Path


def _to_wine_path(linux_path: "Path | str") -> str:
    r"""Convert a Linux absolute path to a Proton/Wine Z:\ path."""
    return "Z:" + str(linux_path).replace("/", "\\")


def _pick_file_zenity(title: str) -> str:
    """Open a native GTK file picker via zenity. Returns the chosen path or ''."""
    try:
        result = subprocess.run(
            [
                "zenity", "--file-selection",
                f"--title={title}",
                "--file-filter=Mod Archives (*.zip, *.7z, *.tar.gz, *.tar) | *.zip *.7z *.tar.gz *.tar",
                "--file-filter=All files | *",
            ],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    return ""
