"""Qt view: Run Synthesis (Mutagen Synthesis patcher) for Bethesda games.

Download the latest Synthesis release → pick a Proton version → bootstrap its
own Wine/.NET prefix → launch Synthesis.exe.  All non-GUI logic lives in
``Utils.synthesis_setup``.  Port of the Tk ``bethesda_synthesis`` plugin.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QPlainTextEdit, QProgressBar, QRadioButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_DOWNLOAD, _PG_PROTON, _PG_SETUP = range(3)
_AMBER = "#e0a06c"


class SynthesisView(WizardViewBase):
    """Install + run Mutagen Synthesis in its own prefix."""

    _dl_status_sig = Signal(str, str)
    _dl_progress_sig = Signal(int)
    _dl_done_sig = Signal()                # download OK → go to proton step
    _setup_log_sig = Signal(str)
    _setup_status_sig = Signal(str, str)
    _setup_done_sig = Signal()             # enable Launch button
    _launch_done_sig = Signal()            # re-enable Launch button

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run Synthesis - {0}").format(game.name))
        self._selected_proton: Path | None = None
        self._proton_candidates: list[Path] = []
        self._plugins_links: list[Path] = []
        self._mygames_link: Path | None = None

        self._dl_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._dl_status, t, c)))
        self._dl_progress_sig.connect(self._guard(self._on_dl_progress))
        self._dl_done_sig.connect(self._guard(lambda: self._goto_proton()))
        self._setup_log_sig.connect(self._guard(self._append_setup_log))
        self._setup_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._setup_status, t, c)))
        self._setup_done_sig.connect(self._guard(
            lambda: self._launch_btn.setEnabled(True)))
        self._launch_done_sig.connect(self._guard(self._on_launch_done))

        self._stack.addWidget(self._build_download_page())
        self._stack.addWidget(self._build_proton_page())
        self._stack.addWidget(self._build_setup_page())
        self._stack.setCurrentIndex(_PG_DOWNLOAD)

        threading.Thread(target=self._do_download, daemon=True,
                         name="synthesis-download").start()

    # ---- page 1: download -------------------------------------------------------
    def _build_download_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Download Synthesis"))
        self._dl_status = self._make_status(lay)
        self._set_status(self._dl_status, self.tr("Fetching latest release from GitHub …"))
        self._dl_bar = QProgressBar()
        self._dl_bar.setRange(0, 0)
        lay.addWidget(self._dl_bar)
        lay.addStretch(1)
        return page

    def _on_dl_progress(self, pct: int):
        if pct < 0:
            self._dl_bar.setRange(0, 0)
            return
        if self._dl_bar.maximum() == 0:
            self._dl_bar.setRange(0, 100)
        self._dl_bar.setValue(pct)

    def _do_download(self):
        from Utils.synthesis_setup import download_and_extract_synthesis
        try:
            def hook(block_num, block_size, total_size):
                if total_size > 0:
                    pct = min(100, block_num * block_size * 100 / total_size)
                    safe_emit(self._dl_progress_sig, int(pct))
            safe_emit(self._dl_status_sig,
                      self.tr("Fetching latest release from GitHub …"), "")
            tag = download_and_extract_synthesis(
                self._game, reporthook=hook, log_fn=self._log)
            safe_emit(self._dl_progress_sig, 100)
            safe_emit(self._dl_status_sig,
                      self.tr("Installed Synthesis {0}.").format(tag), GREEN)
            safe_emit(self._dl_done_sig)
        except Exception as exc:
            self._log(f"Synthesis: download error: {exc}")
            safe_emit(self._dl_progress_sig, -1)
            safe_emit(self._dl_status_sig,
                      self.tr("Download failed: {0}").format(exc), RED)

    # ---- page 2: proton ---------------------------------------------------------
    def _build_proton_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Select Proton Version"))
        self._make_note(lay,
                        self.tr("Synthesis will run in its own Wine prefix next to "
                        "Synthesis.exe.\nPick a Proton version to create that "
                        "prefix with."))
        self._proton_status = self._make_status(lay)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._proton_holder = QWidget()
        self._proton_layout = QVBoxLayout(self._proton_holder)
        self._proton_layout.setContentsMargins(0, 0, 0, 0)
        self._proton_layout.setSpacing(4)
        scroll.setWidget(self._proton_holder)
        lay.addWidget(scroll, 1)
        self._proton_group = QButtonGroup(self)
        self._proton_continue = self._accent_btn(self.tr("Continue →"))
        self._proton_continue.setEnabled(False)
        self._proton_continue.clicked.connect(self._on_proton_chosen)
        lay.addWidget(self._proton_continue, 0, Qt.AlignHCenter)
        return page

    def _goto_proton(self):
        self._stack.setCurrentIndex(_PG_PROTON)
        from Utils.synthesis_setup import list_proton, load_saved_proton
        # clear any previous radios
        while self._proton_layout.count():
            item = self._proton_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._proton_candidates = list_proton()
        if not self._proton_candidates:
            self._set_status(self._proton_status,
                             self.tr("No Proton installations found. Install Proton "
                             "(e.g. GE-Proton) via Steam and try again."), RED)
            return
        self._set_status(self._proton_status, "")
        saved = load_saved_proton(self._game)
        preselect = 0
        for i, script in enumerate(self._proton_candidates):
            if script.parent.name == saved:
                preselect = i
                break
        for i, script in enumerate(self._proton_candidates):
            rb = QRadioButton(script.parent.name)
            self._proton_group.addButton(rb, i)
            self._proton_layout.addWidget(rb)
            if i == preselect:
                rb.setChecked(True)
        self._proton_layout.addStretch(1)
        self._proton_continue.setEnabled(True)

    def _on_proton_chosen(self):
        idx = self._proton_group.checkedId()
        if idx < 0 or idx >= len(self._proton_candidates):
            return
        from Utils.synthesis_setup import save_proton
        self._selected_proton = self._proton_candidates[idx]
        save_proton(self._game, self._selected_proton.parent.name)
        self._goto_setup()

    # ---- page 3: setup + launch -------------------------------------------------
    def _build_setup_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 3: Prepare Prefix"))
        self._setup_status = self._make_status(lay)
        self._set_status(self._setup_status, self.tr("Preparing …"))
        p = active_palette()
        self._setup_log = QPlainTextEdit()
        self._setup_log.setReadOnly(True)
        self._setup_log.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(p,'BG_PANEL')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:1px solid {_c(p,'BORDER')};}}")
        lay.addWidget(self._setup_log, 1)
        self._launch_btn = self._accent_btn(self.tr("Launch Synthesis"))
        self._launch_btn.setEnabled(False)
        self._launch_btn.clicked.connect(self._on_launch)
        lay.addWidget(self._launch_btn, 0, Qt.AlignHCenter)
        return page

    def _append_setup_log(self, msg: str):
        self._setup_log.appendPlainText(msg)

    def _setup_log_line(self, msg: str):
        safe_emit(self._setup_log_sig, msg)
        self._log(msg)

    def _goto_setup(self):
        self._stack.setCurrentIndex(_PG_SETUP)
        threading.Thread(target=self._do_setup, daemon=True,
                         name="synthesis-setup").start()

    def _do_setup(self):
        from Utils.synthesis_setup import (
            setup_synthesis_prefix, synthesis_dir, synthesis_prefix_parent,
        )
        game_path = self._game.get_game_path()
        if game_path is None:
            self._setup_log_line(self.tr("Game path is not configured; aborting."))
            safe_emit(self._setup_status_sig,
                      self.tr("Game path not configured."), RED)
            return
        if self._selected_proton is None:
            self._setup_log_line(self.tr("No Proton selected; aborting."))
            return

        sdir = synthesis_dir(self._game)
        self._setup_log_line(self.tr("Synthesis dir: {0}").format(sdir))
        self._setup_log_line(
            self.tr("Proton: {0}").format(self._selected_proton.parent.name))
        self._setup_log_line(self.tr("Game path: {0}").format(game_path))
        self._setup_log_line("")

        try:
            ok = setup_synthesis_prefix(
                synthesis_dir_path=sdir,
                proton_script=self._selected_proton,
                game_path=Path(game_path),
                log_fn=self._setup_log_line,
                prefix_parent=synthesis_prefix_parent(self._game),
                registry_game_name=getattr(
                    self._game, "synthesis_registry_name",
                    "Skyrim Special Edition"),
            )
        except Exception as exc:
            self._setup_log_line(self.tr("Prefix setup raised: {0}").format(exc))
            ok = False

        if ok:
            safe_emit(self._setup_status_sig,
                      self.tr("Prefix ready. Click Launch Synthesis."), GREEN)
        else:
            safe_emit(self._setup_status_sig,
                      self.tr("Setup completed with errors - launch may still work."),
                      _AMBER)
        safe_emit(self._setup_done_sig)

    # ---- launch -----------------------------------------------------------------
    def _current_profile(self) -> str:
        prof = getattr(self._ctx, "profile_name", "") if self._ctx else ""
        return prof or self._game.get_last_active_profile()

    def _on_launch(self):
        if self._selected_proton is None:
            return
        self._launch_btn.setEnabled(False)
        self._launch_btn.setText(self.tr("Running …"))
        self._ran = True
        threading.Thread(target=self._do_launch, daemon=True,
                         name="synthesis-launch").start()

    def _do_launch(self):
        from Utils.synthesis_setup import (
            launch_synthesis, remove_symlinks, symlink_mygames, symlink_plugins,
        )
        profile = self._current_profile()
        self._plugins_links = symlink_plugins(
            self._game, profile, self._setup_log_line)
        try:
            self._mygames_link = symlink_mygames(self._game, self._setup_log_line)
            launch_synthesis(self._game, self._selected_proton, profile,
                             self._setup_log_line)
        finally:
            remove_symlinks(self._plugins_links, self._setup_log_line)
            if self._mygames_link is not None:
                remove_symlinks([self._mygames_link], self._setup_log_line)
                self._mygames_link = None
            safe_emit(self._launch_done_sig)

    def _on_launch_done(self):
        self._launch_btn.setEnabled(True)
        self._launch_btn.setText(self.tr("Launch Synthesis"))
