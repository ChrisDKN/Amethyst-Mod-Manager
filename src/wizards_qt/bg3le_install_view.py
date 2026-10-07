"""Install bg3le, the Script Extender for Baldur's Gate 3's native Linux build.

BG3SE is a Windows DLL the native build never loads; mods that need a Script
Extender run there through bg3le, which installs outside the game folder and
hooks in through a Steam launch option (see Games/Baldur's Gate 3/
bg3le_runtime.py).  Like the me3 wizard this is one page: report what is
installed and whether the game will load it, offer to fetch the newest release
and run its own installer, then re-check.

bg3le's installer edits Steam's localconfig.vdf, which Steam rewrites from
memory when it exits, so it refuses while Steam is running; its message says
so and is shown in the log.  Inside our Flatpak the install cannot reach the
host's Steam at all, so the page points at the release instead.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QWidget

from Utils.diagnostics.privacy import redact_paths
from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame


def _runtime():
    """bg3le_runtime, loaded by path: its folder's name is not importable."""
    cached = sys.modules.get("bg3le_runtime_bg3")
    if cached is not None:
        return cached
    path = (Path(__file__).resolve().parent.parent / "Games"
            / "Baldur's Gate 3" / "bg3le_runtime.py")
    spec = importlib.util.spec_from_file_location("bg3le_runtime_bg3", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules["bg3le_runtime_bg3"] = module
    spec.loader.exec_module(module)
    return module


class Bg3leInstallView(WizardViewBase):
    """Detect, install and verify bg3le."""

    _log_sig = Signal(str)
    _busy_sig = Signal(bool)
    _refresh_sig = Signal()
    _update_sig = Signal(str)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Install bg3le - {0}").format(game.name))
        self._busy = False
        self._installed_text = ""

        self._log_sig.connect(self._guard(self._append_log))
        self._busy_sig.connect(self._guard(self._set_busy))
        self._refresh_sig.connect(self._guard(self._refresh_state))
        self._update_sig.connect(self._guard(self._on_update_checked))

        self._stack.addWidget(self._build_page())
        self._refresh_state()

    # ---- page ------------------------------------------------------------------

    def _build_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("bg3le Script Extender"))
        rt = _runtime()

        self._make_note(lay, self.tr(
            "The native Linux build of Baldur's Gate 3 cannot load BG3SE, so "
            "mods that need a Script Extender run through bg3le instead. It "
            "installs into ~/.local/share/bg3le and adds itself to the game's "
            "Steam Launch Options. Steam has to be closed while it installs."))

        self._status = self._make_status(lay)

        row = QWidget()
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 4, 0, 4)
        rh.setSpacing(8)

        self._in_flatpak = rt._in_flatpak()
        if self._in_flatpak:
            self._install_btn = None
            self._make_note(lay, self.tr(
                "Amethyst is running as a Flatpak, so bg3le has to be installed "
                "on the host system where Steam runs: download the latest "
                "release, unzip it and run ./install.py."))
        else:
            self._install_btn = self._accent_btn(
                self.tr("Download and install bg3le"))
            self._install_btn.clicked.connect(self._do_install)
            rh.addWidget(self._install_btn)

        self._recheck_btn = self._accent_btn(self.tr("Re-check"))
        self._recheck_btn.clicked.connect(self._refresh_state)
        rh.addWidget(self._recheck_btn)

        self._help_btn = self._accent_btn(self.tr("Open bg3le page"))
        self._help_btn.clicked.connect(lambda: self._open_url(rt.PROJECT_URL))
        rh.addWidget(self._help_btn)
        rh.addStretch(1)
        lay.addWidget(row)

        p = active_palette()
        log_lbl = QLabel(self.tr("Log:"))
        log_lbl.setStyleSheet(self._dim)
        lay.addWidget(log_lbl)
        self._log_box = QPlainTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(p,'BG_PANEL')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:none;}}")
        lay.addWidget(self._log_box, 1)

        done_row = QWidget()
        dh = QHBoxLayout(done_row)
        dh.setContentsMargins(0, 0, 0, 0)
        dh.addStretch(1)
        self._done_btn = self._green_btn()
        self._done_btn.clicked.connect(self._finish)
        dh.addWidget(self._done_btn)
        lay.addWidget(done_row)
        return page

    # ---- state -----------------------------------------------------------------

    def _refresh_state(self):
        """Report whether bg3le is installed and whether launches load it."""
        rt = _runtime()
        root = rt.install_dir()
        if root is None:
            self._set_status(self._status,
                             self.tr("bg3le is not installed."), RED)
            if self._install_btn is not None:
                self._install_btn.setText(self.tr("Download and install bg3le"))
            return
        if self._install_btn is not None:
            self._install_btn.setText(self.tr("Update bg3le"))
        version = rt.installed_version() or self.tr("version unknown")
        problem = rt.launch_problem(self._game)
        if problem:
            self._set_status(self._status, problem, RED)
        else:
            self._installed_text = self.tr(
                "bg3le {0} is installed at {1} and the game loads it.").format(
                version, root)
            self._set_status(self._status, self._installed_text, GREEN)
            # The release feed is a network call: off the GUI thread.
            threading.Thread(
                target=lambda: safe_emit(self._update_sig, rt.update_available()),
                daemon=True, name="bg3le-update-check").start()

    def _on_update_checked(self, newest: str):
        if not newest or self._busy:
            return
        self._set_status(self._status, self.tr(
            "{0} bg3le {1} is available.").format(self._installed_text, newest), GREEN)
        if self._install_btn is not None:
            self._install_btn.setText(self.tr("Update to {0}").format(newest))

    def _set_busy(self, busy: bool):
        self._busy = busy
        if self._install_btn is not None:
            self._install_btn.setEnabled(not busy)
        self._recheck_btn.setEnabled(not busy)

    def _append_log(self, msg: str):
        self._log_box.appendPlainText(redact_paths(msg))
        try:
            self._log(f"bg3le Wizard: {msg}")
        except Exception:
            pass

    # ---- install ---------------------------------------------------------------

    def _do_install(self):
        if self._busy:
            return
        self._set_busy(True)
        self._append_log(self.tr("Fetching the latest bg3le release…"))
        self._set_status(self._status, self.tr("Installing…"), "")

        def worker():
            rt = _runtime()
            try:
                ok = rt.install_bg3le(log_fn=lambda m: safe_emit(self._log_sig, m))
            except Exception as exc:
                safe_emit(self._log_sig, self.tr("Error: {0}").format(exc))
                ok = False
            if ok:
                self._ran = True
                safe_emit(self._log_sig, self.tr("Install finished."))
            else:
                safe_emit(self._log_sig, self.tr("Install did not complete."))
            safe_emit(self._busy_sig, False)
            safe_emit(self._refresh_sig)

        threading.Thread(target=worker, daemon=True,
                         name="bg3le-install").start()
