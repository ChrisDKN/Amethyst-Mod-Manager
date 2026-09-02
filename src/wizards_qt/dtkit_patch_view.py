"""Darktide dtkit-patch wizard - Qt port of wizards/dtkit_patch.py.

The Darktide Mod Loader (DML) mod ships the current Windows ``dtkit-patch.exe``
in its ``tools/`` folder. Once the modlist is deployed, that exe (and the
``bundle/`` folder) land in the game directory via Darktide's custom routing
rules. We run the shipped exe under the game's Proton prefix - mirroring DML's
``toggle_darktide_mods.bat`` - so the patcher version stays in lock-step with
the user's DML install (required after every game update).

Two steps (plugins-panel-scoped tab):
  1. Deploy the modlist (through the app's deploy machinery via
     QtWizardContext.run_deploy) so tools/dtkit-patch.exe + bundle/ are present.
     Skipped automatically when the exe is already deployed.
  2. Run ``dtkit-patch --toggle bundle`` under Proton, showing live output.
     ``--toggle`` flips the patched/unpatched state; re-run after each update.

All blocking work runs on a daemon thread; Signals marshal status/output back
to the UI thread (guarded by ``_closing`` via WizardViewBase).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QHBoxLayout, QPlainTextEdit

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c, ok_text, err_text
from wizards_qt._view_base import WizardViewBase
from Utils.wizards.dtkit import (
    find_deployed_dtkit_exe,
    run_dtkit_patch_proton,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame


class DtkitPatchView(WizardViewBase):
    """Deploy the modlist, then toggle the Darktide bundle patch."""

    # Worker → UI thread. _toggle_line appends one patcher output line;
    # _toggle_done(ok) re-enables the buttons + reports the final result.
    _toggle_line_sig = Signal(str)
    _toggle_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Patch Game (dtkit-patch) - {0}").format(game.name))
        self._toggling = False

        self._toggle_line_sig.connect(self._guard(self._append_output))
        self._toggle_done_sig.connect(self._guard(self._on_toggle_done))

        self.setObjectName("DtkitPatchView")

        # Step 0 = deploy (skipped below if the exe is already present),
        # step 1 = run/toggle.
        self._stack.addWidget(self._build_deploy_page(
            self.tr("Step 1: Deploy mods"),
            self.tr("dtkit-patch.exe ships with the Darktide Mod Loader and runs "
                    "under Proton, so it always matches your installed version.\n\n"
                    "Your mods are deployed first so the patcher and the bundle "
                    "database are present in the game folder."),
            self._goto_run))
        self._stack.addWidget(self._build_toggle_page())

        # If DML is already deployed, skip straight to the run step.
        if find_deployed_dtkit_exe(self._game.get_game_path()) is not None:
            self._stack.setCurrentIndex(1)
        else:
            self._stack.setCurrentIndex(0)

    # ---- step 2: toggle ---------------------------------------------------
    def _build_toggle_page(self) -> QWidget:
        p = active_palette()
        page, lay = self._step_page(self.tr("Step 2: Toggle bundle patch"))

        game_path = self._game.get_game_path()
        exe = find_deployed_dtkit_exe(game_path)
        self._make_note(lay, self.tr(
            "Patcher:\n{0}\n\nGame folder (cwd):\n{1}\n\n"
            "Toggle flips the patch on or off (same as the Mod Loader's "
            "toggle_darktide_mods.bat). Patch to enable mods; toggle again to "
            "disable. Re-run after every game update.").format(exe, game_path))

        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(p,'BG_PANEL')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:none; font-family:monospace;}}")
        lay.addWidget(self._output, 1)

        self._toggle_status = self._make_status(lay)

        self._done_btn = self._green_btn(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        self._toggle_btn = self._accent_btn(self.tr("Toggle Patch"))
        self._toggle_btn.clicked.connect(self._start_toggle)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        rh.addWidget(self._toggle_btn)
        rh.addWidget(self._done_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _goto_run(self):
        """on_next from the deploy page: verify the exe landed, then advance."""
        if find_deployed_dtkit_exe(self._game.get_game_path()) is None:
            self._set_status(
                self._deploy_status,
                self.tr("Deploy finished, but tools/dtkit-patch.exe was not found "
                        "in the game folder.\nMake sure the Darktide Mod Loader "
                        "mod is enabled."),
                err_text())
            return
        self._stack.setCurrentIndex(1)

    # ---- run --------------------------------------------------------------
    def _append_output(self, text: str):
        self._output.appendPlainText(text)

    def _start_toggle(self):
        if self._toggling:
            return
        self._toggling = True
        self._toggle_btn.setEnabled(False)
        self._set_status(self._toggle_status,
                         self.tr("Running dtkit-patch - toggle…"))
        game = self._game

        def worker():
            try:
                ok = run_dtkit_patch_proton(
                    game,
                    flag="--toggle",
                    log_fn=self._log,
                    line_fn=lambda line: safe_emit(self._toggle_line_sig, line))
            except Exception as exc:  # noqa: BLE001 - surface, don't kill the tab
                self._log(f"dtkit-patch wizard: run error: {exc}")
                safe_emit(self._toggle_line_sig, self.tr("Error: {0}").format(exc))
                ok = False
            safe_emit(self._toggle_done_sig, ok)

        threading.Thread(target=worker, daemon=True,
                         name="dtkit-patch-toggle").start()

    def _on_toggle_done(self, ok: bool):
        self._toggling = False
        self._toggle_btn.setEnabled(True)
        self._done_btn.setEnabled(True)
        if ok:
            self._ran = True
            self._set_status(
                self._toggle_status,
                self.tr("Done. The bundle patch state was toggled.\nLaunch the "
                        "game to verify mods load (toggle again to disable)."),
                ok_text())
        else:
            self._set_status(
                self._toggle_status,
                self.tr("dtkit-patch did not complete successfully.\nCheck the "
                        "output above and the log."),
                err_text())
