"""BSA Pack Candidates wizard - which mods are worth packing into an archive.

A modlist-panel-scoped tab, two pages:
  1. Scan - worker assesses every enabled mod (progress bar).
  2. Results - four grouped sections (safe / with care / repack / nothing to
     pack), ranked by how many files each mod could pack. Each row links to the
     mod in the Mod Files tab, where the existing Pack BSA button does the work.

Report-only by design: nothing here writes an archive. The assessment lives in
Utils/bsa_pack_candidates.py.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea, QVBoxLayout,
    QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import AMBER, GREEN, RED, WizardViewBase
import Utils.bsa_pack_candidates as core

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_SCAN, _PG_RESULTS = range(2)


class BsaPackCandidatesView(WizardViewBase):
    """Rank mods by how much they would gain from being packed into a BSA/BA2."""

    _scan_status_sig = Signal(str, str)
    _scan_progress_sig = Signal(float)
    _scan_done_sig = Signal(object)   # list[Candidate] or None

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("BSA Pack Candidates - {0}")
                         .format(game.name))
        self._candidates: list = []

        self._scan_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._scan_status, t, c)))
        self._scan_progress_sig.connect(self._guard(
            lambda f: self._scan_bar.setValue(int(f * 100))))
        self._scan_done_sig.connect(self._guard(self._on_scan_done))

        self._stack.addWidget(self._build_scan_page())
        self._stack.addWidget(self._build_results_page())
        self._stack.setCurrentIndex(_PG_SCAN)

    # ---- page 1: scan ----------------------------------------------------------
    def _build_scan_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Find Pack Candidates"))
        self._make_note(lay, self.tr(
            "Ranks your enabled mods by how many files they could pack into a "
            "BSA/BA2, and flags the ones that would break if packed. A file "
            "inside an archive loses to any loose file from any mod, so a mod "
            "that currently wins a conflict stops winning once it is packed."))
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
        self._scan_bar.setValue(0)
        self._set_status(self._scan_status, self.tr("Scanning…"))
        game = self._game
        profile = self._profile_name()

        def worker():
            def _wlog(m):
                self._log(f"BSA Pack Candidates: {m}")
            try:
                staging, pdir, idx = core.resolve_paths(game, profile)
                cands = core.analyse(
                    game, staging, pdir, idx,
                    progress_fn=lambda f: safe_emit(self._scan_progress_sig, f),
                    log_fn=_wlog)
                safe_emit(self._scan_done_sig, cands)
            except Exception as exc:
                _wlog(f"scan error: {exc}")
                safe_emit(self._scan_status_sig,
                          self.tr("Error: {0}").format(exc), RED)
                safe_emit(self._scan_done_sig, None)

        threading.Thread(target=worker, daemon=True,
                         name="bsa-pack-candidates").start()

    def _profile_name(self) -> str:
        # current_profile() is live; profile_name is frozen at open.
        fn = getattr(self._ctx, "current_profile", None)
        if callable(fn):
            try:
                return fn() or "default"
            except Exception:
                pass
        return getattr(self._ctx, "profile_name", "default") or "default"

    def _on_scan_done(self, cands):
        self._scan_btn.setEnabled(True)
        if cands is None:
            return
        if not cands:
            self._set_status(self._scan_status, self.tr(
                "Nothing to assess - this game has no BSA/BA2 format we can "
                "write, or the profile has no mods indexed yet."), AMBER)
            return
        self._candidates = cands
        self._populate_results()
        self._stack.setCurrentIndex(_PG_RESULTS)

    # ---- page 2: results ----------------------------------------------------------
    def _build_results_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Pack Candidates"))
        self._results_summary = self._make_status(lay)
        self._results_scroll = QScrollArea()
        self._results_scroll.setWidgetResizable(True)
        self._results_scroll.setFrameShape(QScrollArea.NoFrame)
        lay.addWidget(self._results_scroll, 1)

        row = QWidget()
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 8, 0, 0)
        rh.setSpacing(8)
        rescan = QPushButton(self.tr("← Re-Scan"))
        rescan.setCursor(Qt.PointingHandCursor)
        rescan.clicked.connect(lambda: self._stack.setCurrentIndex(_PG_SCAN))
        rh.addWidget(rescan)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _populate_results(self):
        from Utils.cache_tools import format_size

        p = active_palette()
        groups = core.group_by_bucket(self._candidates)
        safe = groups.get(core.BUCKET_SAFE, [])
        care = groups.get(core.BUCKET_CARE, [])
        repack = groups.get(core.BUCKET_REPACK, [])
        toobig = groups.get(core.BUCKET_TOOBIG, [])
        skip = groups.get(core.BUCKET_SKIP, [])

        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(4, 4, 8, 4)
        iv.setSpacing(0)
        # Alternating fills + a hairline under each row: the notes wrap to three
        # lines on a Deck-width panel, so without a boundary two mods read as
        # one block.
        row_bg = (_c(p, "BG_ROW"), _c(p, "BG_ROW_ALT"))
        line = _c(p, "BORDER_FAINT")
        self._row_index = 0

        def _section(title, colour, first: bool):
            lbl = QLabel(title)
            lbl.setStyleSheet(
                f"color:{colour}; font-weight:700; "
                f"padding:{'4' if first else '14'}px 4px 4px 4px;")
            iv.addWidget(lbl)
            # Restart banding per section so the first row is always the base
            # tone - a section that happened to start on the alt tone looked
            # like it belonged to the section above.
            self._row_index = 0

        def _row(cand, note: str, note_colour: str = ""):
            row = QWidget()
            # A plain QWidget ignores stylesheet backgrounds without this.
            row.setAttribute(Qt.WA_StyledBackground, True)
            row.setStyleSheet(
                f"background:{row_bg[self._row_index % 2]};"
                f"border-bottom:1px solid {line};")
            self._row_index += 1
            rh = QHBoxLayout(row)
            rh.setContentsMargins(6, 7, 6, 7)
            rh.setSpacing(8)
            name = QLabel(cand.mod_name)
            name.setFixedWidth(230)
            name.setWordWrap(True)
            name.setToolTip(cand.mod_name)
            name.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            name.setStyleSheet(
                f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; border:none;")
            rh.addWidget(name)
            count = QLabel(self.tr("{0} files").format(cand.packable_count))
            count.setFixedWidth(80)
            count.setAlignment(Qt.AlignTop | Qt.AlignRight)
            count.setStyleSheet(self._dim + "border:none;")
            rh.addWidget(count)
            size = QLabel(format_size(cand.packable_bytes))
            size.setFixedWidth(70)
            size.setAlignment(Qt.AlignTop | Qt.AlignRight)
            size.setStyleSheet(self._dim + "border:none;")
            rh.addWidget(size)
            hint = QLabel(note)
            hint.setWordWrap(True)
            hint.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            hint.setStyleSheet((f"color:{note_colour};" if note_colour
                                else self._dim) + "border:none;")
            rh.addWidget(hint, 1)
            open_btn = QPushButton(self.tr("Open ›"))
            open_btn.setCursor(Qt.PointingHandCursor)
            open_btn.setStyleSheet("border:none;")
            # Bind the name per row - a late-bound closure would hand every
            # button the last mod in the list.
            open_btn.clicked.connect(
                lambda _checked=False, n=cand.mod_name: self._open_mod(n))
            rh.addWidget(open_btn, 0, Qt.AlignTop)
            iv.addWidget(row)

        def _extra(cand) -> str:
            bits = []
            if cand.needs_split:
                bits.append(self.tr(
                    "Over the size limit as one archive - tick \"Separate "
                    "textures archive\" when packing."))
            if cand.needs_stub:
                bits.append(self.tr(
                    "A stub plugin will be created so the archive loads."))
            return (" " + " ".join(bits)) if bits else ""

        first = True
        if safe:
            _section(self.tr("Safe to pack ({0})").format(len(safe)), GREEN, first)
            first = False
            for c in safe:
                _row(c, self.tr("No conflicts to lose.") + _extra(c))
        if care:
            _section(self.tr("Packable with care ({0})").format(len(care)),
                     AMBER, first)
            first = False
            for c in care:
                _row(c, self.tr(
                    "Wins {0} contested file(s) - tick \"Skip winning files\" "
                    "when packing so they stay loose.").format(c.winning_count)
                    + _extra(c), AMBER)
        if repack:
            _section(self.tr("Already has an archive - loose files remain ({0})")
                     .format(len(repack)), _c(p, "TEXT_MAIN"), first)
            first = False
            for c in repack:
                _row(c, self.tr("{0} file(s) already archived.")
                     .format(c.archived_count) + _extra(c))
        if toobig:
            _section(self.tr("Too large for one archive ({0})")
                     .format(len(toobig)), RED, first)
            first = False
            for c in toobig:
                if c.oversize_files:
                    note = self.tr(
                        "{0} file(s) exceed the per-file size field - packing "
                        "would fail.").format(c.oversize_files)
                else:
                    note = self.tr(
                        "Over the archive size limit even with textures split "
                        "off.")
                _row(c, note, RED)
        if skip:
            _section(self.tr("Nothing to pack ({0})").format(len(skip)),
                     _c(p, "TEXT_DIM"), first)
            note = QLabel(self.tr(
                "These mods ship no files the engine would load from inside an "
                "archive - plugins, script-extender DLLs, config files and "
                "anything at the mod root always stay loose."))
            note.setWordWrap(True)
            note.setStyleSheet(self._dim + "padding:2px 4px;")
            iv.addWidget(note)

        iv.addStretch(1)
        self._results_scroll.setWidget(inner)
        self._results_summary.setText(self.tr(
            "{0} mod(s) assessed - {1} safe to pack, {2} need care, "
            "{3} already archived, {4} too large.").format(
                len(self._candidates), len(safe), len(care), len(repack),
                len(toobig)))

    def _open_mod(self, mod_name: str):
        """Jump to this mod in the Mod Files tab (where Pack BSA lives)."""
        fn = getattr(self._ctx, "show_mod_files", None)
        if callable(fn):
            fn(mod_name)
