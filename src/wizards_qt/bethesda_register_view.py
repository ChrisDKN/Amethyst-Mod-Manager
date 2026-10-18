"""Qt view for the shared Bethesda registry workflow."""

from __future__ import annotations

from Utils.diagnostics.privacy import redact_paths

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPlainTextEdit

from gui_qt.runtime.safe_emit import safe_emit
from gui_qt.appearance.theme import active_palette, _c
from wizards_qt._view_base import WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame


class RegisterGamePathView(WizardViewBase):
    """One-button view: write the game path into the prefix registry."""

    _log_sig = Signal(str)
    _finish_sig = Signal(bool)   # ok -> re-enable button with a result label

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Register Game Path - {0}").format(game.name))

        self._log_sig.connect(self._guard(self._append_box))
        self._finish_sig.connect(self._guard(self._on_finished))

        registry_name = getattr(game, "synthesis_registry_name", "") or "?"
        game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
        prefix_path = (game.get_prefix_path()
                       if hasattr(game, "get_prefix_path") else None)

        page, lay = self._step_page("")
        p = active_palette()
        not_cfg = self.tr("not configured")
        info = QLabel(
            self.tr("Registry key:  HKLM\\Software\\Bethesda Softworks\\{0}\n"
            "Game path:     {1}\n"
            "Proton prefix: {2}").format(
                registry_name, game_path or not_cfg, prefix_path or not_cfg))
        info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        lay.addWidget(info)

        self._log_box = QPlainTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(p,'BG_PANEL')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:1px solid {_c(p,'BORDER')};}}")
        lay.addWidget(self._log_box, 1)

        self._run_btn = self._accent_btn(self.tr("Write Registry Keys"))
        self._run_btn.clicked.connect(self._on_run)
        lay.addWidget(self._run_btn, 0, Qt.AlignHCenter)

        self._stack.addWidget(page)

        if (game_path is None or prefix_path is None
                or not Path(prefix_path).is_dir()):
            self._run_btn.setEnabled(False)
            if game_path is None:
                self._append_box(self.tr("Game path is not configured - set it first."))
            else:
                self._append_box(self.tr("Proton prefix not found - launch the game "
                                 "once via Steam first."))

    # ---- logging ----------------------------------------------------------------
    def _append_box(self, msg: str):
        self._log_box.appendPlainText(redact_paths(msg))
        self._log(msg)

    def _log_line(self, msg: str):
        """Log callback usable from the worker thread (marshals to UI)."""
        safe_emit(self._log_sig, msg)

    # ---- run --------------------------------------------------------------------
    def _on_run(self):
        self._run_btn.setEnabled(False)
        self._run_btn.setText(self.tr("Writing …"))
        self._auto_fetch_cancel.clear()
        threading.Thread(target=self._do_register, daemon=True,
                         name="register-game-path").start()

    def _on_finished(self, ok: bool):
        self._run_btn.setEnabled(True)
        self._run_btn.setText(self.tr("Done - Write Again") if ok else self.tr("Retry"))

    def _do_register(self):
        from amethyst_backend.game_registry import inspect_runner, write_runner

        game = self._game
        ok = False
        try:
            profile = getattr(game, "_active_profile_dir", None)
            if profile is None:
                profile = game.get_profile_root() / "profiles" / (getattr(self._ctx, "profile_name", None) or "default")
            report = inspect_runner(game, profile, self._auto_fetch_cancel)(self._log_line, lambda *_a: None)
            if report["prefix_path"]:
                self._log_line(self.tr("Prefix: {0}").format(report["prefix_path"]))
            if report["runner_path"]:
                self._log_line(self.tr("Proton: {0}").format(Path(report["runner_path"]).parent.name))
            result = write_runner(game, profile, report["revision"], True,
                                  self._auto_fetch_cancel)(self._log_line, lambda *_a: None)
            ok = result["ok"]
        except Exception as exc:
            self._log_line(self.tr("Registry write raised: {0}").format(exc))
        if ok:
            self._log_line(self.tr("Registry keys written and verified."))
        else:
            self._log_line(self.tr("Registry write finished with errors - see log above."))
        safe_emit(self._finish_sig, ok)
