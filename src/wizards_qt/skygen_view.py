"""SkyGen BOS/SkyPatcher patch generator - Qt port of wizards/skygen.py.

A modlist-panel-scoped tab, three pages:
  1. Scan - worker scans the active load order (progress bar + Cancel).
  2. Generate - BOS/SkyPatcher mode radios + a scrollable checkbox list of
     eligible plugins (Select/Deselect all) + Generate.
  3. Done - summary + Open output folder.
The scan/generate logic lives in Utils/bethesda/skygen.py.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QRadioButton, QScrollArea, QVBoxLayout, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, close_button, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase
import Utils.bethesda.skygen as core

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_SCAN, _PG_GENERATE, _PG_DONE = range(3)


class SkyGenView(WizardViewBase):
    """Generate BOS or SkyPatcher patch INIs from the active load order."""

    _scan_status_sig = Signal(str, str)
    _scan_progress_sig = Signal(float)
    _scan_done_sig = Signal(object)       # (dna_map, lo_map, summary) or None
    _gen_status_sig = Signal(str, str)
    _gen_done_sig = Signal(str, object, int)   # (mode, out_dir, written)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("SkyGen - Patch Generator - {0}").format(game.name))
        self._dna_map: dict = {}
        self._lo_map: dict = {}
        self._cancel = False
        self._plugin_checks: dict = {}   # plugin_name -> QCheckBox
        self._out_dir: "Path | None" = None

        self._scan_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._scan_status, t, c)))
        self._scan_progress_sig.connect(self._guard(
            lambda f: self._scan_bar.setValue(int(f * 100))))
        self._scan_done_sig.connect(self._guard(self._on_scan_done))
        self._gen_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._gen_status, t, c)))
        self._gen_done_sig.connect(self._guard(self._on_gen_done))

        self._stack.addWidget(self._build_scan_page())
        self._stack.addWidget(self._build_generate_page())
        self._stack.addWidget(self._build_done_page())
        self._stack.setCurrentIndex(_PG_SCAN)

    # ---- page 1: scan ----------------------------------------------------------
    def _build_scan_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Scan Active Plugins"))
        self._make_note(lay, (
            self.tr("SkyGen scans your active load order to find plugins that add "
            "objects/records eligible for Base Object Swapper or SkyPatcher "
            "patches, and flags those already patched.")))
        self._scan_status = self._make_status(lay)
        self._scan_bar = QProgressBar()
        self._scan_bar.setRange(0, 100)
        self._scan_bar.setTextVisible(False)
        lay.addWidget(self._scan_bar)
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        self._scan_cancel_btn = QPushButton(self.tr("Cancel"))
        self._scan_cancel_btn.setCursor(Qt.PointingHandCursor)
        self._scan_cancel_btn.setEnabled(False)
        self._scan_cancel_btn.clicked.connect(self._request_cancel)
        rh.addWidget(self._scan_cancel_btn)
        self._scan_btn = self._accent_btn(self.tr("Scan →"))
        self._scan_btn.clicked.connect(self._start_scan)
        rh.addWidget(self._scan_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _request_cancel(self):
        self._cancel = True

    def _start_scan(self):
        self._cancel = False
        self._scan_btn.setEnabled(False)
        self._scan_cancel_btn.setEnabled(True)
        self._set_status(self._scan_status, self.tr("Scanning…"))
        game = self._game

        def worker():
            _wlog = lambda m: self._log(f"SkyGen: {m}")
            try:
                result = core.scan_load_order(
                    game,
                    progress_fn=lambda f, n, i, t: (
                        safe_emit(self._scan_progress_sig, f),
                        safe_emit(self._scan_status_sig, f"({i+1}/{t}) {n}", "")),
                    log_fn=_wlog,
                    cancel_fn=lambda: self._cancel)
                safe_emit(self._scan_done_sig, result)
            except Exception as exc:
                import traceback
                _wlog(f"scan error: {exc}\n{traceback.format_exc()}")
                safe_emit(self._scan_status_sig,
                          self.tr("Error: {0}").format(exc), RED)
                safe_emit(self._scan_done_sig, None)

        threading.Thread(target=worker, daemon=True, name="skygen-scan").start()

    def _on_scan_done(self, result):
        self._scan_btn.setEnabled(True)
        self._scan_cancel_btn.setEnabled(False)
        if result is None:
            if not self._cancel:
                self._set_status(
                    self._scan_status,
                    self.tr("No active plugins found.\nMake sure a profile is loaded "
                    "and has an active load order."), RED)
            return
        self._dna_map, self._lo_map, summary = result
        self._set_status(self._scan_status, summary, GREEN)
        self._populate_generate()
        self._stack.setCurrentIndex(_PG_GENERATE)

    # ---- page 2: generate ---------------------------------------------------------
    def _build_generate_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Generate Patches"))
        p = active_palette()

        mode_row = QWidget()
        mh = QHBoxLayout(mode_row); mh.setContentsMargins(0, 0, 0, 0)
        mlbl = QLabel(self.tr("Mode:"))
        mlbl.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
        mh.addWidget(mlbl)
        self._mode_group = QButtonGroup(self)
        self._rb_bos = QRadioButton(self.tr("Base Object Swapper"))
        self._rb_bos.setChecked(True)
        self._rb_sp = QRadioButton(self.tr("SkyPatcher"))
        self._mode_group.addButton(self._rb_bos)
        self._mode_group.addButton(self._rb_sp)
        self._rb_bos.toggled.connect(lambda _c: self._populate_generate())
        mh.addWidget(self._rb_bos); mh.addWidget(self._rb_sp)
        mh.addStretch(1)
        lay.addWidget(mode_row)

        btn_row = QWidget()
        bh = QHBoxLayout(btn_row); bh.setContentsMargins(0, 0, 0, 0); bh.setSpacing(8)
        sel_all = QPushButton(self.tr("Select All"))
        sel_all.setCursor(Qt.PointingHandCursor)
        sel_all.clicked.connect(lambda: self._set_all_checks(True))
        des_all = QPushButton(self.tr("Deselect All"))
        des_all.setCursor(Qt.PointingHandCursor)
        des_all.clicked.connect(lambda: self._set_all_checks(False))
        bh.addWidget(sel_all); bh.addWidget(des_all)
        self._plugin_count = QLabel("")
        self._plugin_count.setStyleSheet(self._dim)
        bh.addWidget(self._plugin_count)
        bh.addStretch(1)
        lay.addWidget(btn_row)

        self._plugin_scroll = QScrollArea()
        self._plugin_scroll.setWidgetResizable(True)
        self._plugin_scroll.setFrameShape(QScrollArea.NoFrame)
        lay.addWidget(self._plugin_scroll, 1)

        self._gen_status = self._make_status(lay)

        arow = QWidget()
        ah = QHBoxLayout(arow); ah.setContentsMargins(0, 8, 0, 0); ah.setSpacing(8)
        ah.addStretch(1)
        back = QPushButton(self.tr("← Back"))
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(lambda: self._stack.setCurrentIndex(_PG_SCAN))
        ah.addWidget(back)
        self._gen_btn = self._accent_btn(self.tr("Generate →"))
        self._gen_btn.clicked.connect(self._start_generate)
        ah.addWidget(self._gen_btn)
        ah.addStretch(1)
        lay.addWidget(arow)
        return page

    def _mode(self) -> str:
        return "BOS" if self._rb_bos.isChecked() else "SkyPatcher"

    def _populate_generate(self):
        if not hasattr(self, "_plugin_scroll"):
            return
        p = active_palette()
        mode = self._mode()
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(4, 4, 4, 4)
        iv.setSpacing(1)
        self._plugin_checks = {}
        for name, dna in self._dna_map.items():
            eligible = ((dna.bos_eligible if mode == "BOS" else dna.sp_eligible)
                        and not dna.is_framework)
            if not eligible:
                continue
            sigs = dna.signatures & (core.BOS_SIGS if mode == "BOS"
                                     else core.SP_SIGS)
            chk = QCheckBox(f"{name}  ({', '.join(sorted(sigs))})")
            chk.setChecked(True)
            chk.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
            self._plugin_checks[name] = chk
            iv.addWidget(chk)
        iv.addStretch(1)
        self._plugin_scroll.setWidget(inner)
        self._plugin_count.setText(self.tr("{0} eligible plugin(s)").format(len(self._plugin_checks)))

    def _set_all_checks(self, on: bool):
        for chk in self._plugin_checks.values():
            chk.setChecked(on)

    def _start_generate(self):
        self._cancel = False
        self._gen_btn.setEnabled(False)
        self._set_status(self._gen_status, self.tr("Generating…"))
        game = self._game
        mode = self._mode()
        dna_map, lo_map = self._dna_map, self._lo_map
        selected = {n for n, chk in self._plugin_checks.items() if chk.isChecked()}

        def worker():
            _wlog = lambda m: self._log(f"SkyGen: {m}")
            try:
                mod_name, out_dir, written = core.generate_patches(
                    game, mode, dna_map, lo_map,
                    is_selected=lambda n: n in selected,
                    cancel_fn=lambda: self._cancel,
                    log_fn=_wlog)
                safe_emit(self._gen_done_sig, mode, out_dir, written)
            except Exception as exc:
                import traceback
                _wlog(f"generate error: {exc}\n{traceback.format_exc()}")
                safe_emit(self._gen_status_sig,
                          self.tr("Error: {0}").format(exc), RED)

        threading.Thread(target=worker, daemon=True, name="skygen-gen").start()

    def _on_gen_done(self, mode: str, out_dir, written: int):
        self._gen_btn.setEnabled(True)
        self._out_dir = out_dir
        self._ran = True   # new mod added - refresh on close
        self._done_summary.setText(
            self.tr('Generated {0} {1} patch INI(s).\n\nOutput mod: {2}\n{3}').format(written, mode, out_dir.name, out_dir))
        self._stack.setCurrentIndex(_PG_DONE)
        if getattr(self._ctx, "refresh_modlist", None):
            self._ctx.refresh_modlist()

    # ---- page 3: done -------------------------------------------------------------
    def _build_done_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Patch generation complete"))
        self._done_summary = self._make_status(lay)
        lay.addStretch(1)
        open_btn = QPushButton(self.tr("Open output folder"))
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.clicked.connect(self._open_output)
        lay.addWidget(open_btn, 0, Qt.AlignHCenter)
        close = close_button(self.tr("Close"))
        close.clicked.connect(self._finish)
        lay.addWidget(close, 0, Qt.AlignHCenter)
        return page

    def _open_output(self):
        if self._out_dir is not None:
            from Utils.environment.xdg import xdg_open
            xdg_open(self._out_dir,
                     log_fn=lambda m: self._log(f"SkyGen: {m}"))
