"""Qt view: Install SMAPI (mod loader) for Stardew Valley.

Downloads the latest SMAPI installer zip from GitHub, then installs it with no
user input — the payload is unpacked natively and the launcher swap applied by
``Utils.smapi_installer`` (see that module for why we don't run SMAPI's own
interactive .NET installer in a terminal).

Like the script-extender wizard, the destination is selectable:
game folder / Root_Folder (staging) / a managed root-flagged mod.
Port of the Tk ``sdv_smapi`` plugin.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QRadioButton, QVBoxLayout, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_DOWNLOAD, _PG_INSTALL = range(2)


class SmapiView(WizardViewBase):
    """Download + install SMAPI."""

    _dl_status_sig = Signal(str, str)
    _dl_progress_sig = Signal(int)          # 0-100, or -1 for indeterminate
    _dl_next_sig = Signal()
    _install_status_sig = Signal(str, str)
    _install_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Install SMAPI — {0}").format(game.name))

        self._installed_mode = ""

        self._dl_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._dl_status, t, c)))
        self._dl_progress_sig.connect(self._guard(self._on_dl_progress))
        self._dl_next_sig.connect(self._guard(
            lambda: self._dl_next_btn.setEnabled(True)))
        self._install_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._run_status, t, c)))
        self._install_done_sig.connect(self._guard(self._on_install_done))

        self._stack.addWidget(self._build_download_page())
        self._stack.addWidget(self._build_install_page())
        self._stack.setCurrentIndex(_PG_DOWNLOAD)

        threading.Thread(target=self._do_fetch_and_download, daemon=True,
                         name="smapi-download").start()

    # ---- destination picker ------------------------------------------------------
    def _build_mode_box(self) -> QWidget:
        """Install-destination radios (same three choices as the script
        extender wizard)."""
        p = active_palette()
        box = QFrame()
        box.setStyleSheet(
            f"QFrame{{background:{_c(p,'BG_PANEL')}; border-radius:6px;}}")
        bv = QVBoxLayout(box)
        bv.setContentsMargins(12, 10, 12, 10)
        bv.setSpacing(4)
        head = QLabel(self.tr("Install destination"))
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        bv.addWidget(head)
        self._mode_group = QButtonGroup(self)
        for val, label in (
                ("game", self.tr("Game folder (restores to vanilla first)")),
                ("root", self.tr("Root_Folder (staging)")),
                ("mod", self.tr("As a managed mod (root-flagged)"))):
            rb = QRadioButton(label)
            rb.setProperty("mode", val)
            if val == "game":
                rb.setChecked(True)
            self._mode_group.addButton(rb)
            bv.addWidget(rb)
        return box

    def _selected_mode(self) -> str:
        btn = self._mode_group.checkedButton()
        return btn.property("mode") if btn is not None else "game"

    # ---- page 1: download -------------------------------------------------------
    def _build_download_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Download SMAPI"))
        self._dl_status = self._make_status(lay)
        self._set_status(self._dl_status,
                         self.tr("Checking for the latest SMAPI release…"))
        self._dl_bar = QProgressBar()
        self._dl_bar.setRange(0, 0)     # indeterminate until first progress
        lay.addWidget(self._dl_bar)
        lay.addWidget(self._build_mode_box())
        self._make_note(lay,
                        self.tr("SMAPI is installed automatically — no terminal "
                        "window and no prompts to answer."))
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        browse = QPushButton(self.tr("Browse…"))
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._browse_smapi)
        rh.addWidget(browse)
        self._dl_next_btn = self._accent_btn(self.tr("Next →"))
        self._dl_next_btn.setEnabled(False)
        self._dl_next_btn.clicked.connect(lambda: self._goto_install())
        rh.addWidget(self._dl_next_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _on_dl_progress(self, pct: int):
        if pct < 0:
            self._dl_bar.setRange(0, 0)
            return
        if self._dl_bar.maximum() == 0:
            self._dl_bar.setRange(0, 100)
        self._dl_bar.setValue(pct)

    def _do_fetch_and_download(self):
        from Utils.smapi_installer import (
            download_smapi, fetch_latest_smapi_asset,
        )
        from Utils.wizard_archives import get_downloads_dir
        try:
            safe_emit(self._dl_status_sig,
                      self.tr("Fetching latest SMAPI release from GitHub…"), "")
            tag, url = fetch_latest_smapi_asset()
            filename = url.split("/")[-1]
            dest = get_downloads_dir() / filename
            safe_emit(self._dl_status_sig,
                      self.tr("Downloading SMAPI {0}…").format(tag), "")
            self._log(f"SMAPI Wizard: downloading {url} → {dest}")

            def hook(block_num, block_size, total_size):
                if total_size > 0:
                    pct = min(100, block_num * block_size * 100 / total_size)
                    safe_emit(self._dl_progress_sig, int(pct))

            download_smapi(url, dest, reporthook=hook)
            safe_emit(self._dl_progress_sig, 100)
            self._archive_path = dest
            self._log(f"SMAPI Wizard: downloaded {filename}")
            safe_emit(self._dl_status_sig,
                      self.tr("Downloaded SMAPI {0}: {1}\n"
                      "Choose the install destination, then click Next.")
                      .format(tag, filename), GREEN)
            safe_emit(self._dl_next_sig)
        except Exception as exc:
            self._log(f"SMAPI Wizard: download error: {exc}")
            safe_emit(self._dl_progress_sig, -1)
            safe_emit(self._dl_status_sig,
                      self.tr("Download failed: {0}\n\n"
                      "Use Browse to select a manually downloaded archive.")
                      .format(exc), RED)
            safe_emit(self._dl_next_sig)

    def _browse_smapi(self):
        from Utils.portal_filechooser import pick_file
        pick_file(self.tr("Select the SMAPI archive"),
                  lambda p: safe_emit(self._picked_sig, p))

    def _on_picked(self, path):
        """Base portal-picker override — this view has no locate page, so the
        pick lands on the SMAPI handler (the base connection stays guarded)."""
        self._on_smapi_picked(path)

    def _on_smapi_picked(self, path):
        if path and Path(path).is_file():
            self._archive_path = Path(path)
            self._set_status(self._dl_status,
                             self.tr("Selected: {0}\n"
                             "Choose the install destination, then click Next.")
                             .format(Path(path).name), GREEN)
            self._dl_next_btn.setEnabled(True)

    # ---- page 2: install --------------------------------------------------------
    def _build_install_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Install SMAPI"))
        self._run_status = self._make_status(lay)
        self._install_bar = QProgressBar()
        self._install_bar.setRange(0, 0)
        self._install_bar.setTextVisible(False)
        lay.addWidget(self._install_bar)
        lay.addStretch(1)
        self._done_btn = self._green_btn(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    def _goto_install(self):
        if self._archive_path is None or not Path(self._archive_path).is_file():
            return
        self._installed_mode = self._selected_mode()
        self._stack.setCurrentIndex(_PG_INSTALL)
        self._install_bar.setVisible(True)
        if self._installed_mode == "game":
            # A game-folder install swaps the REAL launcher, so revert to
            # vanilla through the app's restore machinery (deploy mutex +
            # progress popup) and abort when it can't run — never patch a
            # still-deployed root (the swap would post-date the snapshot and
            # be swept into overwrite/ on the next restore).
            self._run_ctx_restore(self._run_status,
                                  on_ok=self._start_install,
                                  on_fail=self._on_restore_failed)
            return
        self._start_install()

    def _start_install(self):
        self._set_status(self._run_status, self.tr("Installing SMAPI…"))
        threading.Thread(target=self._do_install, daemon=True,
                         name="smapi-install").start()

    def _on_restore_failed(self):
        self._install_bar.setVisible(False)
        self._done_btn.setEnabled(True)

    def _do_install(self):
        from Utils.smapi_installer import install_smapi
        mode = self._installed_mode
        try:
            safe_emit(self._install_status_sig,
                      self.tr("Unpacking and installing SMAPI…"), "")
            dest_label, file_count, _mod = install_smapi(
                self._game, self._archive_path, mode,
                restore_first=False, log_fn=self._log)
            extra = ("" if mode == "game" else
                     self.tr("\n\nDeploy your mods to activate it."))
            safe_emit(self._install_status_sig,
                      self.tr("SMAPI installed successfully!\n"
                      "{0} file(s) installed into the {1}.{2}\n\n"
                      "Click Done to close.")
                      .format(file_count, dest_label, extra), GREEN)
            safe_emit(self._install_done_sig, True)
        except Exception as exc:
            safe_emit(self._install_status_sig,
                      self.tr("Error: {0}").format(exc), RED)
            self._log(f"SMAPI Wizard error: {exc}")
            safe_emit(self._install_done_sig, False)

    def _on_install_done(self, ok: bool):
        self._install_bar.setVisible(False)
        self._done_btn.setEnabled(True)
        # A managed-mod install changed modlist.txt — the base _finish()
        # reloads it on the GUI thread when _ran is set.
        if ok and self._installed_mode == "mod":
            self._ran = True
