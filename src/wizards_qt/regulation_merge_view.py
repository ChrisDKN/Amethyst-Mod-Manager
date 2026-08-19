"""Merge enabled Elden Ring ``regulation.bin`` files with native WitchyBND.

WitchyBND handles binder encryption/compression and PARAM serialization. The
GUI-neutral backend in :mod:`Utils.witchybnd` performs the field-level merge,
so this view only owns installation, source presentation, and progress.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QWidget

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import AMBER, GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame


_PG_INSTALL, _PG_RUN = 0, 1
_OUTPUT_MOD = "Merged Regulation"


class RegulationMergeView(WizardViewBase):
    """Install native WitchyBND and merge all enabled regulation sources."""

    _log_sig = Signal(str)
    _status_sig = Signal(str, str)
    _dl_status_sig = Signal(str, str)
    _dl_done_sig = Signal(bool)
    _merge_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Merge regulation.bin - {0}").format(
                             game.name))
        from Utils import witchybnd
        self._sources: list = []
        self._exe = witchybnd.find_witchy(game)

        self._log_sig.connect(self._guard(self._append_log))
        self._status_sig.connect(self._guard(
            lambda text, colour: self._set_status(
                self._run_status, text, colour)))
        self._dl_status_sig.connect(self._guard(
            lambda text, colour: self._set_status(
                self._dl_status, text, colour)))
        self._dl_done_sig.connect(self._guard(self._on_dl_done))
        self._merge_done_sig.connect(self._guard(self._on_merge_done))

        self._stack.addWidget(self._build_install_page())
        self._stack.addWidget(self._build_run_page())

        self._refresh_sources()
        self._goto_step(_PG_RUN if self._exe is not None else _PG_INSTALL)

    # ---- pages -----------------------------------------------------------------

    def _build_install_page(self) -> QWidget:
        page, layout = self._step_page(
            self.tr("Step 1: Install WitchyBND"))
        self._make_note(layout, self.tr(
            "Only one regulation.bin can be active, so mods that ship one "
            "override each other completely. This installs WitchyBND's native "
            "Linux command-line build into the game's Applications folder; "
            "no Proton prefix or Windows .NET runtime is required."))
        self._dl_status = self._make_status(layout)
        layout.addSpacing(8)
        self._install_btn = self._accent_btn(self.tr("Download and install"))
        self._install_btn.clicked.connect(self._start_install)
        layout.addWidget(self._install_btn, 0, Qt.AlignHCenter)
        layout.addStretch(1)
        return page

    def _build_run_page(self) -> QWidget:
        page, layout = self._step_page(self.tr("Step 2: Merge"))
        self._make_note(layout, self.tr(
            "Each regulation is compared with the installed game at field "
            "level, then combined in mod priority order into the '{0}' mod. "
            "Keep the contributing mods enabled so their other files remain "
            "active; enable '{0}' above them afterwards.").format(_OUTPUT_MOD))
        self._run_status = self._make_status(layout)

        palette = active_palette()
        list_label = QLabel(self.tr(
            "Mods contributing param edits (highest priority first):"))
        list_label.setStyleSheet(self._dim)
        layout.addWidget(list_label)
        self._list = QPlainTextEdit()
        self._list.setReadOnly(True)
        self._list.setMaximumHeight(96)
        self._list.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(palette, 'BG_PANEL')};"
            f" color:{_c(palette, 'TEXT_MAIN')}; border:none;}}")
        layout.addWidget(self._list)

        self._merge_btn = self._accent_btn(self.tr("Merge into one mod"))
        self._merge_btn.clicked.connect(self._do_merge)
        layout.addWidget(self._merge_btn, 0, Qt.AlignHCenter)

        log_label = QLabel(self.tr("Log:"))
        log_label.setStyleSheet(self._dim)
        layout.addWidget(log_label)
        self._log_box = QPlainTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(palette, 'BG_PANEL')};"
            f" color:{_c(palette, 'TEXT_MAIN')}; border:none;}}")
        layout.addWidget(self._log_box, 1)

        self._done_btn = self._green_btn()
        self._done_btn.clicked.connect(self._finish)
        layout.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    # ---- step flow -------------------------------------------------------------

    def _goto_step(self, index: int):
        self._stack.setCurrentIndex(index)
        if index == _PG_RUN:
            self._refresh_sources()

    def _start_install(self):
        from Utils import witchybnd
        self._install_btn.setEnabled(False)

        def worker():
            ok = False
            try:
                ok = witchybnd.install_witchy(
                    self._game,
                    log_fn=lambda message: safe_emit(
                        self._dl_status_sig, message, ""),
                )
            except Exception as exc:
                safe_emit(
                    self._dl_status_sig,
                    self.tr("Error: {0}").format(exc), RED,
                )
            safe_emit(self._dl_done_sig, ok)

        threading.Thread(
            target=worker, daemon=True, name="witchybnd-install").start()

    def _on_dl_done(self, ok: bool):
        from Utils import witchybnd
        if ok:
            self._exe = witchybnd.find_witchy(self._game)
            self._goto_step(_PG_RUN)
        else:
            self._install_btn.setEnabled(True)

    # ---- sources ---------------------------------------------------------------

    def _enabled_mod_dirs(self) -> list[tuple[str, Path]]:
        """Enabled mods in highest-first order, excluding the prior output."""
        from Utils.modlist import read_modlist
        game = self._game
        staging = game.get_effective_mod_staging_path()
        profile = (getattr(self._ctx, "profile_name", None)
                   or game.get_last_active_profile() or "default")
        profile_dir = game.get_profile_root() / "profiles" / profile
        return [
            (entry.name, staging / entry.name)
            for entry in read_modlist(profile_dir / "modlist.txt")
            if (entry.enabled and not entry.is_separator
                and entry.name.casefold() != _OUTPUT_MOD.casefold())
        ]

    def _refresh_sources(self):
        from Utils import witchybnd
        try:
            self._sources = witchybnd.find_regulation_sources(
                self._enabled_mod_dirs())
        except Exception as exc:
            self._set_status(
                self._run_status,
                self.tr("Could not read the mod list: {0}").format(exc), RED,
            )
            return

        lines: list[str] = []
        for source in self._sources:
            details: list[str] = []
            if source.regulation is not None:
                details.append("regulation.bin")
            if source.csvs:
                details.append(self.tr("{0} whole-row CSV(s)").format(
                    len(source.csvs)))
            lines.append(f"  {source.name}  -  {', '.join(details)}")
        self._list.setPlainText("\n".join(lines) or self.tr("  (none)"))

        count = len(self._sources)
        self._merge_btn.setEnabled(count > 1 and self._exe is not None)
        if count > 1:
            self._set_status(self._run_status, self.tr(
                "{0} mods ship param edits; only one regulation would survive "
                "without merging.").format(count), RED)
        elif count == 1:
            self._set_status(self._run_status, self.tr(
                "Only one mod ships param edits, so nothing conflicts."), GREEN)
        else:
            self._set_status(
                self._run_status,
                self.tr("No enabled mod ships param edits."), "")

    def _append_log(self, message: str):
        self._log_box.appendPlainText(message)
        try:
            self._log(f"Regulation merge: {message}")
        except Exception:
            pass

    # ---- merge -----------------------------------------------------------------

    def _do_merge(self):
        self._merge_btn.setEnabled(False)
        self._set_status(self._run_status, self.tr("Merging ..."), "")
        threading.Thread(
            target=self._worker, daemon=True, name="regulation-merge").start()

    def _on_merge_done(self, succeeded: bool):
        if not succeeded:
            self._merge_btn.setEnabled(
                len(self._sources) > 1 and self._exe is not None)

    def _worker(self):
        from Utils import witchybnd

        game, executable = self._game, self._exe
        log = lambda message: safe_emit(self._log_sig, message)  # noqa: E731
        succeeded = False
        try:
            if executable is None:
                raise RuntimeError(self.tr("WitchyBND is not installed."))
            game_dir = (getattr(game, "get_exe_dir", lambda: None)()
                        or game.get_game_path())
            vanilla = game_dir / witchybnd.REGULATION_NAME \
                if game_dir is not None else None
            if vanilla is None or not vanilla.is_file():
                raise RuntimeError(self.tr(
                    "Could not find the game's own regulation.bin. Restore the "
                    "game before merging so the vanilla file is in place."))

            plan = witchybnd.plan_merge(self._sources)
            if not plan.is_useful:
                raise RuntimeError(self.tr("Nothing to merge."))
            log("merging {0} mod(s), lowest priority first: {1}".format(
                len(plan.sources), ", ".join(
                    source.name for source in plan.sources)))

            destination = (game.get_effective_mod_staging_path()
                           / _OUTPUT_MOD / witchybnd.REGULATION_NAME)
            # /tmp is commonly a small RAM-backed filesystem on Linux, while
            # Witchy's expanded PARAM XML can require several GiB. Keep merge
            # scratch beside the installed tool on the profile's normal disk.
            scratch_root = executable.parent / "Merge Work"
            scratch_root.mkdir(parents=True, exist_ok=True)
            log(f"using merge scratch space at {scratch_root}")
            with tempfile.TemporaryDirectory(
                    prefix="merge-", dir=scratch_root) as td:
                report = witchybnd.merge_regulations(
                    executable, vanilla, self._sources, Path(td), destination,
                    log_fn=log,
                )

            self._ran = True
            succeeded = True
            if report.conflict_count:
                log("")
                log(f"Resolved {report.conflict_count} overlapping edit(s); "
                    "the higher-priority mod won:")
                for conflict in report.conflicts[:10]:
                    log("  {0} row {1} {2}: '{3}' -> '{4}'".format(
                        conflict.table, conflict.row_id, conflict.field,
                        conflict.previous_source, conflict.winning_source))
                if report.conflict_count > 10:
                    log(f"  ... and {report.conflict_count - 10} more.")
                safe_emit(self._status_sig, self.tr(
                    "Merged into '{0}' with {1} resolved overlap(s). Enable it "
                    "above the contributing mods, then deploy.").format(
                        _OUTPUT_MOD, report.conflict_count), AMBER)
            else:
                safe_emit(self._status_sig, self.tr(
                    "Merged and validated into '{0}'. Enable it above the "
                    "contributing mods, keep those mods enabled, then deploy."
                ).format(_OUTPUT_MOD), GREEN)
        except Exception as exc:
            log(f"error: {exc}")
            safe_emit(
                self._status_sig,
                self.tr("Merge failed: {0}").format(exc), RED,
            )
        finally:
            safe_emit(self._merge_done_sig, succeeded)
