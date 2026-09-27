from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from gui_qt.collapsible_section import CollapsibleSection
from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import AMBER, GREEN, RED, WizardViewBase
import Utils.bsa.candidates as core

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_SCAN, _PG_RESULTS = range(2)


class BsaPackCandidatesView(WizardViewBase):
    _scan_progress_sig = Signal(float)
    _scan_done_sig = Signal(object)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("BSA Pack Candidates - {0}").format(game.name))
        self._candidates = []
        self._selected = set()
        self._checks = {}
        self._selection_anchor = None
        self._sections = {}
        self._section_expanded = {}
        self._outcomes = {}
        self._scan_profile = None
        self._scan_staging = None
        self._scan_running = False
        self._cancel = None
        self._scan_progress_sig.connect(self._guard(
            lambda f: self._scan_bar.setValue(int(f * 100))))
        self._scan_done_sig.connect(self._guard(self._on_scan_done))
        self._stack.addWidget(self._build_scan_page())
        self._stack.addWidget(self._build_results_page())
        self._stack.setCurrentIndex(_PG_SCAN)

    def _build_scan_page(self):
        page, lay = self._step_page(self.tr("Find Pack Candidates"))
        self._make_note(lay, self.tr(
            "Assess enabled mods for batch packing, and find archives packed by Amethyst, "
            "including in disabled mods. Archived files lose to loose files from any mod; "
            "keep winning conflict files loose to preserve their priority."))
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

    def _profile_name(self):
        fn = getattr(self._ctx, "current_profile", None)
        return (fn() if callable(fn) else getattr(self._ctx, "profile_name", "default")) or "default"

    def _start_scan(self):
        if self._scan_running or self._tool_running:
            return
        self._scan_running = True
        self._scan_btn.setEnabled(False)
        self._stack.setCurrentIndex(_PG_SCAN)
        self._scan_bar.setValue(0)
        self._set_status(self._scan_status, self.tr("Scanning…"))
        game, profile = self._game, self._profile_name()
        try:
            staging, pdir, idx = core.resolve_paths(game, profile)
        except Exception as exc:
            self._on_scan_done({"profile": profile, "error": str(exc)})
            return

        def worker():
            try:
                cands = core.analyse(
                    game, staging, pdir, idx,
                    progress_fn=lambda f: safe_emit(self._scan_progress_sig, f),
                    log_fn=lambda msg: self._log(f"BSA Pack Candidates: {msg}"))
                result = {"profile": profile, "staging": staging, "candidates": cands}
            except Exception as exc:
                self._log(f"BSA Pack Candidates: scan error: {exc}")
                result = {"profile": profile, "error": str(exc)}
            safe_emit(self._scan_done_sig, result)

        threading.Thread(target=worker, daemon=True, name="bsa-pack-candidates").start()

    def _on_scan_done(self, result):
        self._scan_running = False
        self._scan_btn.setEnabled(True)
        if result["profile"] != self._profile_name():
            self._scan_profile = None
            self._set_status(self._scan_status, self.tr("The active profile changed. Start a new scan."), AMBER)
            return
        if "error" in result:
            self._set_status(self._scan_status, self.tr("Error: {0}").format(result["error"]), RED)
            return
        self._scan_profile, self._scan_staging = result["profile"], result["staging"]
        self._candidates = result["candidates"]
        self._selected.intersection_update(c.mod_name for c in self._candidates if c.can_pack or c.can_unpack)
        self._populate_results()
        self._stack.setCurrentIndex(_PG_RESULTS)

    def _build_results_page(self):
        page, lay = self._step_page(self.tr("Pack Candidates"))
        self._results_summary = self._make_status(lay)
        selection = QHBoxLayout()
        self._selection_buttons = []
        for text, mode in ((self.tr("Select Safe"), "safe"),
                           (self.tr("Select All Actionable"), "all"),
                           (self.tr("Clear Selection"), "clear")):
            button = QPushButton(text)
            button.clicked.connect(lambda _checked=False, mode=mode: self._select(mode))
            selection.addWidget(button)
            self._selection_buttons.append(button)
        selection.addStretch(1)
        lay.addLayout(selection)
        self._results_scroll = QScrollArea()
        self._results_scroll.setWidgetResizable(True)
        self._results_scroll.setFrameShape(QScrollArea.NoFrame)
        lay.addWidget(self._results_scroll, 1)
        self._outcome_scroll = QScrollArea()
        self._outcome_scroll.setWidgetResizable(True)
        self._outcome_scroll.setMaximumHeight(150)
        self._outcome_scroll.hide()
        outcome_body = QWidget()
        self._outcome_layout = QVBoxLayout(outcome_body)
        self._outcome_labels = {}
        self._outcome_scroll.setWidget(outcome_body)
        lay.addWidget(self._outcome_scroll)
        self._batch_status = self._make_status(lay)
        self._batch_bar = QProgressBar()
        self._batch_bar.setRange(0, 100)
        self._batch_bar.hide()
        lay.addWidget(self._batch_bar)
        actions = QHBoxLayout()
        self._rescan_btn = QPushButton(self.tr("← Re-Scan"))
        self._rescan_btn.clicked.connect(self._start_scan)
        actions.addWidget(self._rescan_btn)
        actions.addStretch(1)
        self._pack_btn = self._accent_btn("")
        self._pack_btn.clicked.connect(lambda: self._start_batch("pack"))
        actions.addWidget(self._pack_btn)
        self._unpack_btn = QPushButton("")
        self._unpack_btn.clicked.connect(lambda: self._start_batch("unpack"))
        actions.addWidget(self._unpack_btn)
        self._cancel_btn = QPushButton(self.tr("Cancel Batch"))
        self._cancel_btn.clicked.connect(self._cancel_batch)
        self._cancel_btn.hide()
        actions.addWidget(self._cancel_btn)
        lay.addLayout(actions)
        return page

    def _note(self, cand):
        bits = []
        if cand.packed_archives:
            bits.append(self.tr("Packed by Amethyst: {0}").format(", ".join(cand.packed_archives)))
        if cand.missing_archives:
            bits.append(self.tr("Missing or unavailable recorded archives: {0}").format(", ".join(cand.missing_archives)))
        if cand.packing_error:
            bits.append(self.tr("Packing metadata could not be read: {0}").format(cand.packing_error))
        if not cand.enabled:
            bits.append(self.tr("Disabled mod; available for unpacking only."))
        elif cand.bucket == core.BUCKET_TOOBIG:
            bits.append(self.tr("Exceeds the archive or per-file size limit."))
        elif cand.packable_count == 0:
            bits.append(self.tr("No enabled packable loose files remain."))
        elif cand.winning_count:
            bits.append(self.tr("Wins {0} contested file(s); keep winning conflict files loose.").format(cand.winning_count))
        else:
            bits.append(self.tr("No conflicts to lose."))
        if cand.has_archive and not cand.packed_archives:
            bits.append(self.tr("Already contains archives; matching archives will be merged."))
        if cand.needs_split:
            bits.append(self.tr("Textures will be split automatically to fit the archive size limit."))
        if cand.needs_stub:
            bits.append(self.tr("A stub plugin will be created so the archive loads."))
        return " ".join(bits)

    def _outcome_text(self, outcome):
        status = outcome["status"]
        if status == "success":
            return self.tr("Completed: {0} file(s).").format(outcome["files"])
        labels = {"failed": self.tr("Failed"), "cancelled": self.tr("Cancelled"), "skipped": self.tr("Skipped")}
        return self.tr("{0}: {1}").format(labels[status], outcome.get("error", ""))

    def _populate_results(self):
        from Utils.downloads.cache import format_size
        p = active_palette()
        tracked = [c for c in self._candidates if c.packed_archives]
        groups = core.group_by_bucket([c for c in self._candidates if not c.packed_archives])
        sections = [("tracked", self.tr("Packed by Amethyst ({0})").format(len(tracked)), GREEN, tracked)]
        for bucket, title, colour in (
            (core.BUCKET_SAFE, self.tr("Safe to pack ({0})"), GREEN),
            (core.BUCKET_CARE, self.tr("Packable with care ({0})"), AMBER),
            (core.BUCKET_REPACK, self.tr("Already has archives ({0})"), _c(p, "TEXT_MAIN")),
            (core.BUCKET_TOOBIG, self.tr("Too large ({0})"), RED),
        ):
            sections.append((bucket, title.format(len(groups[bucket])), colour, groups[bucket]))
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(4, 4, 8, 4)
        lay.setSpacing(0)
        self._checks = {}
        self._selection_anchor = None
        self._section_expanded.update(
            (key, section.is_expanded()) for key, section in self._sections.items())
        self._sections = {}
        for key, title, colour, cands in sections:
            if not cands:
                continue
            section = CollapsibleSection(title)
            section.setStyleSheet(
                f"QToolButton#SectionToggle {{ color:{colour}; font-weight:700; "
                "padding:6px 0; border:none; background:transparent; }")
            section.set_expanded(self._section_expanded.get(key, True))
            self._sections[key] = section
            lay.addWidget(section)
            rows = QVBoxLayout(section.body)
            rows.setContentsMargins(0, 0, 0, 0)
            rows.setSpacing(0)
            for index, cand in enumerate(cands):
                row = QWidget()
                row.setAttribute(Qt.WA_StyledBackground, True)
                bg = _c(p, "BG_ROW_ALT" if index % 2 else "BG_ROW")
                row.setStyleSheet(f"background:{bg}; border-bottom:1px solid {_c(p, 'BORDER_FAINT')};")
                rh = QHBoxLayout(row)
                rh.setContentsMargins(6, 7, 6, 7)
                check = QCheckBox()
                check.setAccessibleName(cand.mod_name)
                check.setToolTip(self.tr("Shift-click to select or deselect a range."))
                check.setChecked(cand.mod_name in self._selected)
                check.setEnabled(cand.can_pack or cand.can_unpack)
                check.clicked.connect(lambda checked, name=cand.mod_name: self._toggle(
                    name, checked, bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)))
                self._checks[cand.mod_name] = check
                rh.addWidget(check, 0, Qt.AlignTop)
                name = QLabel(cand.mod_name)
                name.setMinimumWidth(120)
                name.setMaximumWidth(230)
                name.setWordWrap(True)
                name.setToolTip(cand.mod_name)
                name.setStyleSheet(f"color:{_c(p, 'TEXT_MAIN')}; font-weight:600; border:none;")
                rh.addWidget(name, 1, Qt.AlignTop)
                totals = QLabel(self.tr("{0} files\n{1}").format(cand.packable_count, format_size(cand.packable_bytes)))
                totals.setStyleSheet(self._dim + "border:none;")
                rh.addWidget(totals, 0, Qt.AlignTop)
                hint = QLabel(self._note(cand))
                hint.setWordWrap(True)
                hint.setStyleSheet(f"color:{RED if cand.missing_archives or cand.packing_error else colour}; border:none;")
                rh.addWidget(hint, 2, Qt.AlignTop)
                if cand.packed_archives:
                    unpack = QPushButton(self.tr("Unpack"))
                    unpack.setEnabled(cand.can_unpack)
                    unpack.clicked.connect(lambda _checked=False, name=cand.mod_name: self._start_batch("unpack", [name]))
                    rh.addWidget(unpack, 0, Qt.AlignTop)
                open_btn = QPushButton(self.tr("Open ›"))
                open_btn.clicked.connect(lambda _checked=False, name=cand.mod_name: self._open_mod(name))
                rh.addWidget(open_btn, 0, Qt.AlignTop)
                rows.addWidget(row)
        if not self._sections:
            lay.addWidget(QLabel(self.tr("No packing candidates or Amethyst packing records to show.")))
        lay.addStretch(1)
        old = self._results_scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self._results_scroll.setWidget(inner)
        self._results_summary.setText(self.tr("{0} mod(s) assessed; {1} packed by Amethyst.").format(len(self._candidates), len(tracked)))
        self._update_actions()

    def _toggle(self, name, checked, shift=False):
        visible = [name for name, check in self._checks.items()
                   if check.isEnabled() and check.isVisibleTo(self._results_scroll.widget())]
        names = [name]
        if shift and self._selection_anchor in visible and name in visible:
            first, last = sorted((visible.index(self._selection_anchor), visible.index(name)))
            names = visible[first:last + 1]
        else:
            self._selection_anchor = name
        for selected_name in names:
            self._checks[selected_name].setChecked(checked)
            if checked:
                self._selected.add(selected_name)
            else:
                self._selected.discard(selected_name)
        self._update_actions()

    def _select(self, mode):
        self._selection_anchor = None
        if mode == "clear":
            selected = set()
        elif mode == "safe":
            selected = {c.mod_name for c in self._candidates if c.can_pack and c.bucket == core.BUCKET_SAFE}
        else:
            selected = {c.mod_name for c in self._candidates if c.can_pack or c.can_unpack}
        self._selected = selected
        for name, check in self._checks.items():
            check.setChecked(name in selected)
        self._update_actions()

    def _eligible(self, action, names):
        return [c for c in self._candidates if c.mod_name in names
                and (c.can_pack if action == "pack" else c.can_unpack)]

    def _update_actions(self):
        pack = len(self._eligible("pack", self._selected))
        unpack = len(self._eligible("unpack", self._selected))
        self._pack_btn.setText(self.tr("Pack Selected ({0})").format(pack))
        self._unpack_btn.setText(self.tr("Unpack Selected ({0})").format(unpack))
        available = callable(getattr(self._ctx, "run_archive_batch", None)) and not self._tool_running
        self._pack_btn.setEnabled(available and pack > 0)
        self._unpack_btn.setEnabled(available and unpack > 0)

    def _start_batch(self, action, names=None):
        if self._tool_running or self._scan_running:
            return
        if self._scan_profile != self._profile_name():
            self._set_status(self._batch_status, self.tr("The active profile changed. Re-scan before packing or unpacking."), AMBER)
            return
        cands = self._eligible(action, self._selected if names is None else names)
        if not cands:
            return
        selected = [c.mod_name for c in cands]
        if action == "pack":
            from gui_qt.bsa_pack_overlay import BsaPackOverlay
            from Utils.bsa.pack import archive_kind_for_game
            BsaPackOverlay.show_over(
                self, archive_name="", existing=any(c.has_archive for c in cands),
                kind=archive_kind_for_game(self._game), batch_count=len(cands),
                required_splits=sum(c.needs_split for c in cands),
                on_done=lambda opts: self._submit_batch(action, selected, opts) if opts is not None else None)
        else:
            from gui_qt.confirm_overlay import ConfirmOverlay
            ConfirmOverlay.show_over(
                self, self.tr("Unpack selected mods"), self.tr(
                    "Extract only archives recorded as packed by Amethyst. Existing loose files are preserved. "
                    "After success, the recorded archives and unneeded generated stub plugins are removed."),
                lambda confirmed: self._submit_batch(action, selected, {}) if confirmed else None,
                confirm_label=self.tr("Unpack"), list_items=selected)

    def _submit_batch(self, action, names, opts):
        if self._closing or self._tool_running:
            return
        fn = getattr(self._ctx, "run_archive_batch", None)
        if not callable(fn):
            return
        cancel = fn({"action": action, "mod_names": names, "options": opts,
                     "profile_name": self._scan_profile, "staging": str(self._scan_staging)},
                    self._guard(self._on_batch_progress), self._guard(self._on_batch_done))
        if cancel is None:
            return
        self._cancel = cancel
        self._outcomes = {}
        for label in self._outcome_labels.values():
            self._outcome_layout.removeWidget(label)
            label.deleteLater()
        self._outcome_labels = {}
        self._outcome_scroll.hide()
        self._lock_close(True, self.tr("Cancel the batch and wait for it to finish before closing."))
        self._results_scroll.setEnabled(False)
        self._rescan_btn.setEnabled(False)
        for button in self._selection_buttons:
            button.setEnabled(False)
        self._batch_bar.setValue(0)
        self._batch_bar.show()
        self._cancel_btn.setEnabled(True)
        self._cancel_btn.show()
        self._set_status(self._batch_status, self.tr("Starting archive batch…"))
        self._update_actions()

    def _on_batch_progress(self, info):
        completed = info["index"] if info.get("outcome") else info["index"] - 1
        self._batch_bar.setValue(int(100 * completed / info["total"]))
        self._set_status(self._batch_status, self.tr("{0} / {1}: {2} — {3} / {4} files").format(
            info["index"], info["total"], info["mod_name"], info["done"], info["file_total"]))
        if info.get("outcome"):
            outcome = info["outcome"]
            self._outcomes[outcome["mod_name"]] = outcome
            self._show_outcome(outcome)

    def _show_outcome(self, outcome):
        name = outcome["mod_name"]
        label = self._outcome_labels.get(name)
        if label is None:
            label = QLabel()
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            self._outcome_layout.addWidget(label)
            self._outcome_labels[name] = label
        label.setText(self.tr("{0} — {1}").format(name, self._outcome_text(outcome)))
        colour = RED if outcome["status"] == "failed" else GREEN if outcome["status"] == "success" else AMBER
        label.setStyleSheet(f"color:{colour}; padding:4px;")
        self._outcome_scroll.show()

    def _cancel_batch(self):
        if self._cancel is not None:
            self._cancel.set()
            self._cancel_btn.setEnabled(False)
            self._set_status(self._batch_status, self.tr("Cancelling; waiting for the current operation to stop safely…"), AMBER)

    def _on_batch_done(self, outcomes):
        self._cancel = None
        self._outcomes = {item["mod_name"]: item for item in outcomes}
        for outcome in outcomes:
            self._show_outcome(outcome)
        self._lock_close(False)
        self._results_scroll.setEnabled(True)
        self._rescan_btn.setEnabled(True)
        for button in self._selection_buttons:
            button.setEnabled(True)
        self._cancel_btn.hide()
        self._batch_bar.setValue(100)
        ok = sum(item["status"] == "success" for item in outcomes)
        failed = sum(item["status"] == "failed" for item in outcomes)
        self._set_status(self._batch_status, self.tr(
            "{0} succeeded, {1} failed, {2} cancelled or skipped.").format(ok, failed, len(outcomes) - ok - failed),
            RED if failed else GREEN)
        self._selected.clear()
        self._start_scan()

    def _open_mod(self, mod_name):
        fn = getattr(self._ctx, "show_mod_files", None)
        if callable(fn):
            fn(mod_name)
