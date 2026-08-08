"""RE Engine PAK repair wizard - Qt port of wizards/re_pak_restore.py.

RE Engine games (Resident Evil 2/3/7/8, RE4 Remake, Street Fighter 6, …) load
loose mod files by *invalidating* the matching entry inside the game's PAK
archives.  If the manager's per-profile backups are lost while mods are still
deployed, the PAKs stay invalidated and the game can fail to load.  Deploy
also writes a failsafe manifest of the original hash bytes into the game root
(``.mm_pak_restore.json``); this view restores from that manifest.

Opens as a plugins-panel-scoped tab.  All PAK logic is in the neutral
Utils.re_pak_patcher; the repair runs on a daemon thread and marshals log
lines / completion back to the UI thread via Signals.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
)

from gui_qt.theme_qt import active_palette, _c, button_qss, err_text
from gui_qt.safe_emit import safe_emit
from Utils.re_pak_patcher import (
    ROOT_MANIFEST_NAME,
    restore_from_root_manifest,
    root_manifest_summary,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame


class RePakRestoreView(QWidget):
    """Repair RE Engine PAKs from the game-root failsafe manifest."""

    # Worker thread → UI thread.
    _log_line = Signal(str)
    _done = Signal(str)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None):
        super().__init__()
        self._game = game
        self._log_fn = log_fn or (lambda _m: None)
        self._on_close_cb = on_close or (lambda: None)
        self._game_root: Path | None = game.get_game_path()
        self._running = False
        self._repair_btn: QPushButton | None = None

        self._log_line.connect(self._log)
        self._done.connect(self._finish)

        self.setObjectName("RePakRestoreView")
        self._build()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        p = active_palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Toolbar: title + Close.
        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("Repair PAK files - {0}").format(self._game.name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        close = QPushButton(self.tr("✕ Close"))
        close.setCursor(Qt.PointingHandCursor)
        close.setStyleSheet(
            button_qss("BTN_DANGER", padding="5px 12px"))
        close.clicked.connect(self._on_close)
        hb.addWidget(close)
        v.addWidget(bar)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(16, 12, 16, 12)
        bl.setSpacing(8)
        v.addWidget(body, 1)

        if not self._game_root or not self._game_root.is_dir():
            err = QLabel(self.tr("Game path is not set or invalid."))
            err.setStyleSheet(f"color:{err_text()};")
            bl.addWidget(err)
            bl.addStretch(1)
            return

        info = QLabel(
            self.tr("If the game won't load (black screen) after removing mods, the "
            "PAK archives may still have invalidated entries. This restores "
            "the original PAK data from the failsafe manifest in the game root."))
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
        bl.addWidget(info)

        root_lbl = QLabel(self.tr("Game root: {0}").format(self._game_root))
        root_lbl.setWordWrap(True)
        root_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        bl.addWidget(root_lbl)

        pak_count, entry_count = root_manifest_summary(self._game_root)
        if pak_count == 0:
            status = QLabel(
                self.tr('No restore manifest ({0}) found in the game root. There is nothing to repair - either no PAK-patching mods were deployed, or the manifest was already consumed by a clean restore.\n\nIf the game is still broken, verify the game files via Steam.').format(ROOT_MANIFEST_NAME))
            status.setWordWrap(True)
            status.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
            bl.addWidget(status)
        else:
            paks = (self.tr("{0} PAK file") if pak_count == 1
                    else self.tr("{0} PAK files")).format(pak_count)
            entries = (self.tr("{0} invalidated entry") if entry_count == 1
                       else self.tr("{0} invalidated entries")).format(entry_count)
            status = QLabel(
                self.tr("Found a restore manifest covering {0} and {1}.")
                .format(paks, entries))
            status.setWordWrap(True)
            status.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
            bl.addWidget(status)
            self._repair_btn = QPushButton(self.tr("Repair PAK files"))
            self._repair_btn.setCursor(Qt.PointingHandCursor)
            self._repair_btn.clicked.connect(self._do_repair)
            bl.addWidget(self._repair_btn, 0, Qt.AlignLeft)

        log_lbl = QLabel(self.tr("Log:"))
        log_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        bl.addWidget(log_lbl)

        self._log_text = QPlainTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(p,'BG_PANEL')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:none;"
            " font-family:monospace;}")
        bl.addWidget(self._log_text, 1)

    # ---- helpers ----------------------------------------------------------
    def _on_close(self):
        if self._running:
            return
        self._on_close_cb()

    def _log(self, msg: str):
        self._log_fn(msg)
        self._log_text.appendPlainText(msg)

    # ---- repair -----------------------------------------------------------
    def _do_repair(self):
        if self._running or not self._game_root:
            return
        self._running = True
        if self._repair_btn is not None:
            self._repair_btn.setEnabled(False)
        self._log(self.tr("Repairing PAK files from game-root manifest …"))

        game_root = self._game_root

        def run():
            try:
                restored = restore_from_root_manifest(
                    game_root, log_fn=lambda *a: safe_emit(self._log_line, *a))
                if restored:
                    msg = (self.tr("Repair complete - restored {0} entry to vanilla.")
                           if restored == 1 else
                           self.tr("Repair complete - restored {0} entries to vanilla.")
                           ).format(restored)
                else:
                    msg = self.tr("Nothing to repair - the PAK entries are already "
                           "vanilla (or no manifest was found).")
            except Exception as e:  # noqa: BLE001 - surface, don't kill the tab
                msg = self.tr("Error: {0}").format(e)
            safe_emit(self._done, msg)

        threading.Thread(target=run, daemon=True,
                         name="re-pak-restore").start()

    def _finish(self, msg: str):
        self._log(msg)
        self._running = False
        # The manifest is an append-only ledger that persists after a repair,
        # so the button stays enabled - re-running is always a safe no-op when
        # the PAKs are already vanilla.
        if self._repair_btn is not None:
            self._repair_btn.setEnabled(True)
