"""
Path and file-picker utilities for the GUI.
Used by TopBar and dialogs (e.g. ExeConfigDialog). No dependency on other gui modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from Utils.portal_filechooser import pick_file


def _to_wine_path(linux_path: Path | str) -> str:
    r"""Convert a Linux absolute path to a Proton/Wine Z:\ path."""
    return "Z:" + str(linux_path).replace("/", "\\")


def pick_file_mod_archive(title: str, callback: Callable[[str], None]) -> None:
    """
    Open a native file picker (XDG portal or zenity) for mod archives.
    Runs async; callback(path: str) is invoked with the chosen path or ''.
    Caller should schedule callback on main thread for Tkinter, e.g.:
        pick_file_mod_archive(title, lambda p: self.after(0, lambda: self._on_picked(p)))
    """
    def _cb(p: Path | None) -> None:
        callback(str(p) if p else "")

    pick_file(title, _cb)
