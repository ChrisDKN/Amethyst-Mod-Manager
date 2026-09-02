"""Qt view: Register Game Path in Wine Registry (Bethesda games).

Writes ``HKLM\\Software\\Bethesda Softworks\\<Game>\\Installed Path`` (and the
Wow6432Node mirror) into the game's Steam compatdata prefix so tools run inside
that prefix (xEdit, LOOT, Wrye Bash, Synthesis…) can locate the game.  A thin
GUI over ``Utils.bethesda.registry.register_bethesda_game_path``; the Proton env
is resolved by ``Utils.wine.proton.resolve_proton_env``.

Port of the Tk ``bethesda_register_game_path`` plugin.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPlainTextEdit

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
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
        self._log_box.appendPlainText(msg)
        self._log(msg)

    def _log_line(self, msg: str):
        """Log callback usable from the worker thread (marshals to UI)."""
        safe_emit(self._log_sig, msg)

    # ---- run --------------------------------------------------------------------
    def _on_run(self):
        self._run_btn.setEnabled(False)
        self._run_btn.setText(self.tr("Writing …"))
        threading.Thread(target=self._do_register, daemon=True,
                         name="register-game-path").start()

    def _on_finished(self, ok: bool):
        self._run_btn.setEnabled(True)
        self._run_btn.setText(self.tr("Done - Write Again") if ok else self.tr("Retry"))

    def _do_register(self):
        game = self._game
        registry_name = getattr(game, "synthesis_registry_name", None)
        if not registry_name:
            self._log_line(self.tr("This game has no Bethesda registry name; nothing to do."))
            safe_emit(self._finish_sig, False)
            return

        game_path = game.get_game_path()
        prefix_path = game.get_prefix_path()
        if game_path is None or prefix_path is None or not prefix_path.is_dir():
            self._log_line(self.tr("Game path or Proton prefix not available."))
            safe_emit(self._finish_sig, False)
            return

        from Utils.wine.proton import resolve_proton_env
        proton_script, env = resolve_proton_env(game, self._log_line)
        if proton_script is None or env is None:
            safe_emit(self._finish_sig, False)
            return

        env["WINEDEBUG"] = "-all"
        compat_data = Path(env["STEAM_COMPAT_DATA_PATH"])

        self._log_line(self.tr("Prefix: {0}").format(compat_data))
        self._log_line(self.tr("Proton: {0}").format(proton_script.parent.name))

        # Drop the idempotency marker so a manual run always re-writes the keys.
        from Utils.bethesda.registry import _marker_path, register_bethesda_game_path
        try:
            _marker_path(compat_data, registry_name).unlink(missing_ok=True)
        except OSError:
            pass

        try:
            ok = register_bethesda_game_path(
                prefix_dir=compat_data,
                proton_script=proton_script,
                env=env,
                game_path=Path(game_path),
                registry_game_name=registry_name,
                log_fn=self._log_line,
            )
        except Exception as exc:
            self._log_line(self.tr("Registry write raised: {0}").format(exc))
            ok = False

        if ok:
            self._log_line(self.tr("Registry keys written (64-bit + Wow6432Node views)."))
        else:
            self._log_line(self.tr("Registry write finished with errors - see log above."))
        safe_emit(self._finish_sig, ok)
