"""Plugin Audit wizard - Qt port of wizards/plugin_audit.py.

A modlist-panel-scoped tab, three pages:
  1. Scan - worker audits the active load order (progress bar).
  2. Results - three grouped sections (safe-to-disable / blocked by new
     records / blocked by dependents) with a checkbox + columns; actions:
     Re-Scan, Select All Safe, Disable Selected, Clean Orphaned INIs.
  3. Cleanup result - summary + Re-Scan.
The scan/disable/cleanup logic lives in Utils/plugin_audit_core.py.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase
import Utils.plugin_audit_core as core

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_SCAN, _PG_RESULTS, _PG_CLEANUP = range(3)


class PluginAuditView(WizardViewBase):
    """Audit the load order for plugins that can be safely disabled."""

    _scan_status_sig = Signal(str, str)
    _scan_progress_sig = Signal(float)
    _scan_done_sig = Signal(object)       # entries dict or None
    _cleanup_done_sig = Signal(int, int)  # (found, deleted)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Plugin Audit - {0}").format(game.name))
        self._entries: dict = {}
        self._checks: dict = {}   # plugin_name -> QCheckBox (safe entries only)

        self._scan_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._scan_status, t, c)))
        self._scan_progress_sig.connect(self._guard(
            lambda f: self._scan_bar.setValue(int(f * 100))))
        self._scan_done_sig.connect(self._guard(self._on_scan_done))
        self._cleanup_done_sig.connect(self._guard(self._on_cleanup_done))

        self._stack.addWidget(self._build_scan_page())
        self._stack.addWidget(self._build_results_page())
        self._stack.addWidget(self._build_cleanup_page())
        self._stack.setCurrentIndex(_PG_SCAN)

    # ---- page 1: scan ----------------------------------------------------------
    def _build_scan_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Scan Load Order"))
        self._make_note(lay, (
            self.tr("Audits your active load order to find patched plugins that can be "
            "safely disabled (their patches still apply at runtime), and flags "
            "those blocked by new records or by other plugins depending on "
            "them.")))
        self._scan_status = self._make_status(lay)
        self._scan_bar = QProgressBar()
        self._scan_bar.setRange(0, 100)
        self._scan_bar.setTextVisible(False)
        lay.addWidget(self._scan_bar)
        lay.addStretch(1)
        self._scan_btn = self._accent_btn(self.tr("Start Scan"))
        self._scan_btn.clicked.connect(self._start_scan)
        lay.addWidget(self._scan_btn, 0, Qt.AlignHCenter)
        return page

    def _start_scan(self):
        self._scan_btn.setEnabled(False)
        self._set_status(self._scan_status, self.tr("Scanning…"))
        game = self._game

        def worker():
            _wlog = lambda m: self._log(f"Plugin Audit: {m}")
            try:
                entries = core.scan_load_order(
                    game,
                    progress_fn=lambda f: safe_emit(self._scan_progress_sig, f),
                    log_fn=_wlog)
                safe_emit(self._scan_done_sig, entries)
            except Exception as exc:
                _wlog(f"scan error: {exc}")
                safe_emit(self._scan_status_sig,
                          self.tr("Error: {0}").format(exc), RED)
                safe_emit(self._scan_done_sig, None)

        threading.Thread(target=worker, daemon=True, name="audit-scan").start()

    def _on_scan_done(self, entries):
        self._scan_btn.setEnabled(True)
        if entries is None:
            self._set_status(self._scan_status, self.tr("No active plugins found."), RED)
            return
        self._entries = entries
        self._populate_results()
        self._stack.setCurrentIndex(_PG_RESULTS)

    # ---- page 2: results ----------------------------------------------------------
    def _build_results_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Audit Results"))
        self._results_summary = self._make_status(lay)
        self._results_scroll = QScrollArea()
        self._results_scroll.setWidgetResizable(True)
        self._results_scroll.setFrameShape(QScrollArea.NoFrame)
        lay.addWidget(self._results_scroll, 1)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rescan = QPushButton(self.tr("← Re-Scan"))
        rescan.setCursor(Qt.PointingHandCursor)
        rescan.clicked.connect(lambda: self._stack.setCurrentIndex(_PG_SCAN))
        rh.addWidget(rescan)
        self._sel_safe_btn = QPushButton(self.tr("Select All Safe"))
        self._sel_safe_btn.setCursor(Qt.PointingHandCursor)
        self._sel_safe_btn.clicked.connect(
            lambda: [c.setChecked(True) for c in self._checks.values()])
        rh.addWidget(self._sel_safe_btn)
        rh.addStretch(1)
        self._clean_btn = QPushButton(self.tr("Clean Orphaned INIs"))
        self._clean_btn.setCursor(Qt.PointingHandCursor)
        self._clean_btn.clicked.connect(self._start_cleanup)
        rh.addWidget(self._clean_btn)
        self._disable_btn = self._accent_btn(self.tr("Disable Selected"))
        self._disable_btn.clicked.connect(self._disable_selected)
        rh.addWidget(self._disable_btn)
        lay.addWidget(row)
        return page

    def _populate_results(self):
        p = active_palette()
        entries = self._entries
        safe = sorted((e for e in entries.values() if e.can_disable),
                      key=lambda e: -e.priority)
        blocked_new = [e for e in entries.values()
                       if not e.can_disable and e.has_new_records]
        blocked_dep = [e for e in entries.values()
                       if not e.can_disable and not e.has_new_records
                       and e.is_patched]

        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(4, 4, 4, 4)
        iv.setSpacing(1)
        self._checks = {}

        def _section(title, colour):
            lbl = QLabel(title)
            lbl.setStyleSheet(f"color:{colour}; font-weight:700;")
            iv.addWidget(lbl)

        def _row(entry, selectable):
            row = QWidget()
            rh = QHBoxLayout(row); rh.setContentsMargins(0, 0, 0, 0); rh.setSpacing(6)
            if selectable:
                chk = QCheckBox()
                self._checks[entry.plugin_name] = chk
                rh.addWidget(chk)
            else:
                spacer = QLabel("")
                spacer.setFixedWidth(20)
                rh.addWidget(spacer)
            name = QLabel(entry.plugin_name)
            name.setFixedWidth(220)
            name.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')};")
            rh.addWidget(name)
            label, colour = entry.primary_patch_label
            patch = QLabel(label)
            patch.setFixedWidth(150)
            patch.setStyleSheet(f"color:{colour};")
            rh.addWidget(patch)
            pri = QLabel(str(entry.priority) if entry.priority >= 0 else "-")
            pri.setFixedWidth(60)
            pri.setStyleSheet(self._dim)
            rh.addWidget(pri)
            status = QLabel(self.tr("Safe to disable") if selectable
                            else entry.unsafe_reason or self.tr("Blocked"))
            status.setWordWrap(True)
            status.setStyleSheet(self._dim)
            rh.addWidget(status, 1)
            iv.addWidget(row)

        if safe:
            _section(self.tr("Safe to disable ({0})").format(len(safe)), GREEN)
            for e in safe:
                _row(e, selectable=True)
        if blocked_new:
            _section(self.tr("Blocked - adds new records ({0})").format(
                     len(blocked_new)),
                     "#e0a83c")
            for e in blocked_new:
                _row(e, selectable=False)
        if blocked_dep:
            _section(self.tr("Blocked - required by other plugins "
                     "({0})").format(len(blocked_dep)), RED)
            for e in blocked_dep:
                _row(e, selectable=False)
        iv.addStretch(1)
        self._results_scroll.setWidget(inner)

        self._results_summary.setText(
            self.tr("Audit complete - {0} plugins, {1} safe to disable.")
            .format(len(entries), len(safe)))
        self._sel_safe_btn.setEnabled(bool(safe))
        self._disable_btn.setEnabled(bool(safe))
        self._clean_btn.setEnabled(bool(blocked_new or blocked_dep))

    def _disable_selected(self):
        selected = [n for n, chk in self._checks.items() if chk.isChecked()]
        if not selected:
            return
        # Confirm via the shared borderless overlay.
        from gui_qt.confirm_overlay import ConfirmOverlay
        preview = "\n".join(f"  • {n}" for n in selected[:5])
        if len(selected) > 5:
            preview += "\n  • …"
        ConfirmOverlay.show_over(
            self, self.tr("Disable Selected Plugins"),
            (self.tr("Disable {0} plugin(s)?\n\n{1}\n\n"
             "The patches for these plugins will still apply at runtime.")
             .format(len(selected), preview)),
            lambda ok: self._do_disable(selected) if ok else None,
            confirm_label=self.tr("Disable"))

    def _do_disable(self, selected):
        disabled, msg = core.disable_plugins(self._game, selected)
        self._log(f"Plugin Audit: {msg}")
        self._ran = True
        # plugins.txt changed - re-sync + reload panels, then close.
        self._finish()

    # ---- cleanup ------------------------------------------------------------------
    def _start_cleanup(self):
        targets = core.orphaned_ini_targets(self._entries)
        if not targets:
            self._results_summary.setText(self.tr("No orphaned INIs to clean."))
            return
        from gui_qt.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(
            self, self.tr("Clean Orphaned INIs"),
            (self.tr("Delete SkyGen-generated INI files for {0} plugin(s) "
             "that cannot be disabled?\n\nThis removes INIs in the SkyGen BOS "
             "and SkyGen SkyPatcher output mods. INIs that ship with original "
             "mods are not affected.").format(len(targets))),
            lambda ok: self._run_cleanup(targets) if ok else None,
            confirm_label=self.tr("Clean"))

    def _run_cleanup(self, targets):
        self._clean_btn.setEnabled(False)
        game = self._game

        def worker():
            _wlog = lambda m: self._log(f"Plugin Audit: {m}")
            found, deleted = core.cleanup_orphaned_inis(game, targets,
                                                        log_fn=_wlog)
            safe_emit(self._cleanup_done_sig, found, deleted)

        threading.Thread(target=worker, daemon=True, name="audit-clean").start()

    def _on_cleanup_done(self, found: int, deleted: int):
        self._ran = True
        self._cleanup_summary.setText(
            self.tr("Cleanup complete - deleted {0} of {1} INI(s) found.\n\n"
            "Re-scan to verify.").format(deleted, found)
            if found else self.tr("No SkyGen INIs found to clean."))
        self._stack.setCurrentIndex(_PG_CLEANUP)
        if getattr(self._ctx, "refresh_modlist", None):
            self._ctx.refresh_modlist()

    # ---- page 3: cleanup result ---------------------------------------------------
    def _build_cleanup_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Cleanup Complete"))
        self._cleanup_summary = self._make_status(lay)
        lay.addStretch(1)
        rescan = self._accent_btn(self.tr("Re-Scan to Verify"))
        rescan.clicked.connect(lambda: self._stack.setCurrentIndex(_PG_SCAN))
        lay.addWidget(rescan, 0, Qt.AlignHCenter)
        close = self._green_btn(self.tr("Close"))
        close.setEnabled(True)
        close.clicked.connect(self._finish)
        lay.addWidget(close, 0, Qt.AlignHCenter)
        return page
