"""Mewgenics GPAK wizard - Qt port of wizards/mewgenics_gpak.py.

Two actions against the game root: unpack resources.gpak → Unpacked/, or
repack Unpacked/ → resources.gpak.  Work runs on daemon threads with output
into a read-only log box; the ``gpak`` library does the packing.
"""

from __future__ import annotations

import shutil
import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QWidget

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_RESOURCES_GPAK = "resources.gpak"
_UNPACKED_DIR = "Unpacked"


class GpakView(WizardViewBase):
    """Unpack or repack resources.gpak in the game root."""

    _log_sig = Signal(str)
    _running_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("GPAK tools - {0}").format(game.name))
        self._game_root = game.get_game_path()
        self._running = False

        self._log_sig.connect(self._guard(self._append_log))
        self._running_sig.connect(self._guard(self._set_running))

        self._stack.addWidget(self._build_page())

    def _build_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("GPAK unpack / repack"))

        if self._game_root is None or not self._game_root.is_dir():
            err = QLabel(self.tr("Game path is not set or invalid."))
            err.setAlignment(Qt.AlignHCenter)
            err.setStyleSheet(f"color:{RED};")
            lay.addWidget(err)
            lay.addStretch(1)
            return page

        root_note = QLabel(self.tr("Game root: {0}").format(self._game_root))
        root_note.setWordWrap(True)
        root_note.setStyleSheet(self._dim)
        lay.addWidget(root_note)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 4, 0, 4); rh.setSpacing(8)
        self._unpack_btn = self._accent_btn(self.tr("Unpack resources.gpak"))
        self._unpack_btn.clicked.connect(self._do_unpack)
        rh.addWidget(self._unpack_btn)
        self._repack_btn = self._accent_btn(self.tr("Repack Unpacked folder"))
        self._repack_btn.clicked.connect(self._do_repack)
        rh.addWidget(self._repack_btn)
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
        return page

    def _append_log(self, msg: str):
        self._log_box.appendPlainText(msg)
        self._log_fn_safe(msg)

    def _log_fn_safe(self, msg: str):
        try:
            self._log(f"GPAK: {msg}")
        except Exception:
            pass

    def _set_running(self, running: bool):
        self._running = running
        self._unpack_btn.setEnabled(not running)
        self._repack_btn.setEnabled(not running)

    # ---- actions ----------------------------------------------------------------
    def _do_unpack(self):
        if self._running or not self._game_root:
            return
        resources = self._game_root / _RESOURCES_GPAK
        unpack_dir = self._game_root / _UNPACKED_DIR
        if not resources.is_file():
            self._append_log(
                self.tr("'{0}' not found in game root.").format(_RESOURCES_GPAK))
            return
        self._set_running(True)
        self._append_log(self.tr("Unpacking resources.gpak…"))

        def worker():
            try:
                from Games.Mewgenics.gpak import extract_gpak
                if unpack_dir.exists():
                    safe_emit(self._log_sig,
                              self.tr("Removing previous Unpacked folder…"))
                    shutil.rmtree(unpack_dir)
                extract_gpak(resources, unpack_dir, try_zlib=True)
                safe_emit(self._log_sig, self.tr("Unpack complete."))
            except Exception as exc:
                safe_emit(self._log_sig, self.tr("Error: {0}").format(exc))
            finally:
                safe_emit(self._running_sig, False)

        threading.Thread(target=worker, daemon=True, name="gpak-unpack").start()

    def _do_repack(self):
        if self._running or not self._game_root:
            return
        unpack_dir = self._game_root / _UNPACKED_DIR
        resources = self._game_root / _RESOURCES_GPAK
        if not unpack_dir.is_dir():
            self._append_log(
                self.tr("'{0}' folder not found. Unpack first.").format(_UNPACKED_DIR))
            return
        self._set_running(True)
        self._append_log(self.tr("Repacking to resources.gpak…"))

        def worker():
            try:
                from Games.Mewgenics.gpak import pack_gpak
                pack_gpak(unpack_dir, resources, compress=False)
                safe_emit(self._log_sig, self.tr("Repack complete."))
            except Exception as exc:
                safe_emit(self._log_sig, self.tr("Error: {0}").format(exc))
            finally:
                safe_emit(self._running_sig, False)

        threading.Thread(target=worker, daemon=True, name="gpak-repack").start()
