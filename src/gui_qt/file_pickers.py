"""QFileDialog pickers - the last-resort step of the portal chooser waterfall.

``Utils.ui.portal`` tries the XDG portal, then zenity, then kdialog,
then whatever pickers the GUI registered here.  Until now nothing was ever
registered, so on a desktop with no ``org.freedesktop.portal.FileChooser``
backend (wlroots compositors like Sway ship only ScreenCast/Screenshot) the
waterfall bottomed out at whichever zenity was on PATH - inside the AppImage
that is the cut-down static zenity-rs, which can't navigate directories.

These pickers depend on nothing but Qt, which is already bundled, so they work
on a bare tiling WM with no portal, no zenity and no file manager.
``DontUseNativeDialog`` is mandatory: the native path would route straight back
into the portal that just failed.
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtWidgets import QFileDialog

# Our filter labels already carry a "(*.zip, *.7z)" tail for zenity/kdialog.
# Qt reads the *trailing* parenthesised group as the globs, so strip ours
# before appending a real one or the dialog shows a doubled suffix.
_TRAILING_PARENS = re.compile(r"\s*\([^()]*\)\s*$")

# Remembered across calls so a user without a portal doesn't restart at $HOME
# on every pick. Module-level: the pickers are only ever used one at a time.
_last_dir: str = ""


def _qt_filter(label: str, patterns) -> str:
    """Build one Qt name-filter string from a (label, globs) pair."""
    # The save waterfall pre-joins its globs into a single string (a leftover
    # of the Tk contract); accept both shapes.
    if isinstance(patterns, str):
        patterns = patterns.split()
    # Qt splits the glob group on whitespace/';' only - commas would become
    # part of the glob itself.
    globs = " ".join(patterns) or "*"
    return f"{_TRAILING_PARENS.sub('', label).strip()} ({globs})"


def _name_filters(filters) -> list[str]:
    """Convert the backend's [(label, [glob, ...]), ...] to Qt name filters."""
    return [_qt_filter(label, pats) for label, pats in (filters or [])] or ["All files (*)"]


def _build(parent, title: str, mode, *, filters=None, save_name: str = ""):
    """Create a non-native QFileDialog seeded at the last-used directory."""
    dlg = QFileDialog(parent, title)
    dlg.setOption(QFileDialog.DontUseNativeDialog, True)
    dlg.setFileMode(mode)
    if _last_dir:
        dlg.setDirectory(_last_dir)
    if mode is QFileDialog.Directory:
        dlg.setOption(QFileDialog.ShowDirsOnly, True)
    else:
        dlg.setNameFilters(_name_filters(filters))
    if save_name:
        dlg.setAcceptMode(QFileDialog.AcceptSave)
        dlg.selectFile(save_name)
    return dlg


def _run(dlg) -> list[Path]:
    """Exec *dlg* modally, remember its directory, return the selection."""
    global _last_dir
    if not dlg.exec():
        return []
    chosen = [Path(p) for p in dlg.selectedFiles() if p]
    if chosen:
        first = chosen[0]
        _last_dir = str(first if first.is_dir() else first.parent)
    return chosen


def _pick_folder(parent, title: str) -> "Path | None":
    chosen = _run(_build(parent, title, QFileDialog.Directory))
    return chosen[0] if chosen else None


def _pick_file(parent, title: str, filters=None) -> "Path | None":
    chosen = _run(_build(parent, title, QFileDialog.ExistingFile, filters=filters))
    return chosen[0] if chosen else None


def _pick_files(parent, title: str, filters=None) -> list[Path]:
    return _run(_build(parent, title, QFileDialog.ExistingFiles, filters=filters))


def _pick_save(parent, title: str, current_name: str = "", filters=None) -> "Path | None":
    dlg = _build(parent, title, QFileDialog.AnyFile, filters=filters,
                 save_name=current_name or "untitled")
    chosen = _run(dlg)
    return chosen[0] if chosen else None


def build_pickers(parent) -> dict:
    """Return the folder/file/files/save dict glue.register_all expects."""
    return {
        "folder": lambda title: _pick_folder(parent, title),
        "file": lambda title, filters=None: _pick_file(parent, title, filters),
        "files": lambda title, filters=None: _pick_files(parent, title, filters),
        "save": lambda title, current_name="", filters=None: _pick_save(
            parent, title, current_name, filters),
    }
