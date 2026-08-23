"""Install the me3 mod loader for FROMSOFTWARE games.

FROMSOFTWARE titles cannot load loose files - assets live in encrypted DVDBND
archives - so mods are served at runtime by me3, an external loader that injects
a DLL and launches the game itself.  Amethyst never bundles it (same policy as
umu-run): it is a host-side tool with its own release cadence that has to see
the host's Steam and Proton.

The wizard is one page: report what is installed, offer to fetch the newest
release, then verify.  Installing means three files, not one - the Linux CLI
plus the Windows injection payload it needs to hook the game (see
``Games.FromSoftware.me3_runtime``), which is why this does not just drop a
binary on PATH.

Inside our own Flatpak the install cannot be done for the user at all: a copy
written into the sandbox is invisible to the host Steam that has to run it.  In
that case the page shows the upstream one-liner to run on the host instead.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_HELP_URL = "https://me3.help/"


def _runtime():
    """Import the me3 runtime helpers lazily (keeps app startup light)."""
    from Games.FromSoftware import me3_runtime
    return me3_runtime


class Me3InstallView(WizardViewBase):
    """Detect, install and verify the me3 loader."""

    _log_sig = Signal(str)
    _status_sig = Signal(str, str)
    _busy_sig = Signal(bool)
    _progress_sig = Signal(int)
    _refresh_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Install me3 - {0}").format(game.name))
        self._busy = False

        self._log_sig.connect(self._guard(self._append_log))
        self._status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._status, t, c)))
        self._busy_sig.connect(self._guard(self._set_busy))
        self._progress_sig.connect(self._guard(self._on_progress))
        self._refresh_sig.connect(self._guard(self._refresh_state))

        self._stack.addWidget(self._build_page())
        self._refresh_state()

    # ---- page ------------------------------------------------------------------

    def _build_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("me3 mod loader"))
        rt = _runtime()

        self._make_note(lay, self.tr(
            "{0} mods are loaded by me3 at runtime rather than copied into the "
            "game folder, so me3 must be installed for the Play button to "
            "start a modded game.").format(self._game.name))

        self._status = self._make_status(lay)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setVisible(False)
        lay.addWidget(self._bar)

        row = QWidget()
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 4, 0, 4)
        rh.setSpacing(8)

        self._in_flatpak = rt._in_flatpak()
        if self._in_flatpak:
            # Nothing we install from in here would be visible to the host.
            self._install_btn = None
            self._make_note(lay, self.tr(
                "Amethyst is running as a Flatpak, so me3 has to be installed "
                "on the host system where Steam runs. Open a terminal on the "
                "host and run:"))
            cmd = QLabel(
                "curl --proto '=https' --tlsv1.2 -sSfL "  # i18n: skip — shell command, copied verbatim
                "https://github.com/garyttierney/me3/releases/latest/download/"
                "installer.sh | sh")
            cmd.setWordWrap(True)
            cmd.setTextInteractionFlags(Qt.TextSelectableByMouse)
            p = active_palette()
            cmd.setStyleSheet(
                f"background:{_c(p,'BG_PANEL')}; color:{_c(p,'TEXT_MAIN')};"
                " padding:8px; border-radius:4px; font-family:monospace;")
            lay.addWidget(cmd)
        else:
            self._install_btn = self._accent_btn(
                self.tr("Download and install me3"))
            self._install_btn.clicked.connect(self._do_install)
            rh.addWidget(self._install_btn)

        self._recheck_btn = self._accent_btn(self.tr("Re-check"))
        self._recheck_btn.clicked.connect(self._refresh_state)
        rh.addWidget(self._recheck_btn)

        self._help_btn = self._accent_btn(self.tr("Open me3 website"))
        self._help_btn.clicked.connect(lambda: self._open_url(_HELP_URL))
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
        """Report whether me3 and its Windows payload are present."""
        rt = _runtime()
        found = rt.find_me3()
        # Probes host locations too - inside the sandbox XDG_DATA_HOME points at
        # our private data dir, not the ~/.local/share me3 installs into.
        payload_ok = rt.find_windows_payload() is not None

        if found is None and self._in_flatpak:
            self._set_status(self._status, self.tr(
                "me3 was not found. It must be installed on the host system, "
                "not inside the Flatpak sandbox."), RED)
            return
        if found is None:
            self._set_status(self._status,
                             self.tr("me3 is not installed."), RED)
            return

        version = rt.me3_version() or self.tr("unknown version")
        if not payload_ok:
            # The CLI alone runs but cannot inject into the game.
            self._set_status(self._status, self.tr(
                "me3 {0} found at {1}, but its Windows files are missing. "
                "Re-install to repair it.").format(version, found), RED)
        else:
            self._set_status(self._status, self.tr(
                "me3 {0} is installed at {1}.").format(version, found), GREEN)

    def _on_progress(self, pct: int):
        self._bar.setVisible(0 < pct < 100)
        self._bar.setValue(pct)

    def _set_busy(self, busy: bool):
        self._busy = busy
        if self._install_btn is not None:
            self._install_btn.setEnabled(not busy)
        self._recheck_btn.setEnabled(not busy)

    def _append_log(self, msg: str):
        self._log_box.appendPlainText(msg)
        try:
            self._log(f"me3 Wizard: {msg}")
        except Exception:
            pass

    # ---- install ---------------------------------------------------------------

    def _do_install(self):
        if self._busy:
            return
        self._set_busy(True)
        self._append_log(self.tr("Fetching the latest me3 release…"))
        self._set_status(self._status, self.tr("Installing…"), "")

        def worker():
            rt = _runtime()
            try:
                ok = rt.install_me3(log_fn=lambda m: safe_emit(self._log_sig, m))
            except Exception as exc:
                # install_me3 already logs its own failures; this is the
                # unexpected-error net so the wizard never hangs on "Installing".
                safe_emit(self._log_sig, self.tr("Error: {0}").format(exc))
                ok = False
            if ok:
                self._ran = True
                safe_emit(self._log_sig, self.tr("Install finished."))
            else:
                safe_emit(self._log_sig, self.tr("Install did not complete."))
            safe_emit(self._progress_sig, 0)
            safe_emit(self._busy_sig, False)
            safe_emit(self._refresh_sig)

        threading.Thread(target=worker, daemon=True,
                         name="me3-install").start()
