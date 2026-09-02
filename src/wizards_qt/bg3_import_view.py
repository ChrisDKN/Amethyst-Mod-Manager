"""Qt view: Import a BG3 load order (.json / modsettings.lsx) → this profile.

Pick a BG3MM ``modlist.json`` or the game's ``modsettings.lsx`` → preview the
computed reorder → apply.  All the matching/planning lives in
``Utils.bg3.importer``; the view just handles the file pick, the preview text, and
calling ``ctx.refresh_modlist`` after apply.

Port of the Tk ``bg3_import_modlist_json`` plugin.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QCoreApplication, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QPlainTextEdit, QPushButton, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_PICK, _PG_PREVIEW, _PG_DONE = range(3)

_JSON_FILTERS = [
    (QCoreApplication.translate("BG3ImportView", "Load Order (*.json *.lsx)"),
     ["*.json", "*.lsx"]),
    (QCoreApplication.translate("BG3ImportView", "All files"), ["*"]),
]


class BG3ImportView(WizardViewBase):
    """Import a BG3MM order file into the active profile's modlist."""

    _preview_ready_sig = Signal(str, str)     # summary, detail
    _preview_error_sig = Signal(str)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Import BG3 Load Order - {0}").format(game.name))
        self._json_path: Path | None = None
        self._plan = None

        self._preview_ready_sig.connect(self._guard(self._on_preview_ready))
        self._preview_error_sig.connect(self._guard(
            lambda t: self._set_status(self._preview_summary, t, RED)))

        self._stack.addWidget(self._build_pick_page())
        self._stack.addWidget(self._build_preview_page())
        self._stack.addWidget(self._build_done_page())
        self._stack.setCurrentIndex(_PG_PICK)

    # ---- page 1: pick -----------------------------------------------------------
    def _build_pick_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Select a load order file"))
        self._make_note(lay,
                        self.tr("Choose a modlist.json (or an exported saved-order .json) "
                        "from BG3 Mod Manager, or a modsettings.lsx written by "
                        "the game.\nMods are matched to your installed mods by "
                        "UUID."))
        self._pick_status = self._make_status(lay)
        self._set_status(self._pick_status, self.tr("No file selected."))
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        browse = QPushButton(self.tr("Browse…"))
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._browse_json)
        rh.addWidget(browse)
        self._preview_btn = self._accent_btn(self.tr("Preview →"))
        self._preview_btn.setEnabled(False)
        self._preview_btn.clicked.connect(self._goto_preview)
        rh.addWidget(self._preview_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _browse_json(self):
        from Utils.ui.portal import pick_file
        pick_file(self.tr("Select a load order file (.json or .lsx)"),
                  lambda p: safe_emit(self._picked_sig, p), _JSON_FILTERS)

    def _on_picked(self, path):
        """Base portal-picker override - this view has no locate page, so the
        pick lands on the .json handler (the base connection stays guarded)."""
        self._on_json_picked(path)

    def _on_json_picked(self, path):
        if path and Path(path).is_file():
            self._json_path = Path(path)
            self._set_status(self._pick_status,
                             self.tr("Selected: {0}").format(Path(path).name), GREEN)
            self._preview_btn.setEnabled(True)

    # ---- page 2: preview --------------------------------------------------------
    def _build_preview_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Review changes"))
        self._preview_summary = self._make_status(lay)
        p = active_palette()
        self._preview_box = QPlainTextEdit()
        self._preview_box.setReadOnly(True)
        self._preview_box.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._preview_box.setStyleSheet(
            f"QPlainTextEdit{{background:{_c(p,'BG_PANEL')};"
            f" color:{_c(p,'TEXT_MAIN')}; border:1px solid {_c(p,'BORDER')};}}")
        lay.addWidget(self._preview_box, 1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        back = QPushButton(self.tr("← Back"))
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(lambda: self._stack.setCurrentIndex(_PG_PICK))
        rh.addWidget(back)
        rh.addStretch(1)
        self._apply_btn = self._green_btn(self.tr("Apply Order"))
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._apply)
        rh.addWidget(self._apply_btn)
        lay.addWidget(row)
        return page

    def _goto_preview(self):
        self._stack.setCurrentIndex(_PG_PREVIEW)
        self._plan = None
        self._apply_btn.setEnabled(False)
        self._preview_box.setPlainText("")
        self._set_status(self._preview_summary,
                         self.tr("Reading order and scanning installed mods…"))
        threading.Thread(target=self._compute_preview, daemon=True,
                         name="bg3-preview").start()

    def _compute_preview(self):
        from Utils.bg3.importer import compute_import_plan, format_preview
        try:
            profile = getattr(self._ctx, "profile_name", "") if self._ctx else ""
            plan = compute_import_plan(self._game, self._json_path, profile)
            self._plan = plan
            summary, detail = format_preview(plan)
            safe_emit(self._preview_ready_sig, summary, detail)
        except Exception as exc:
            self._log(f"BG3 Import: preview error: {exc}")
            safe_emit(self._preview_error_sig, self.tr("Error: {0}").format(exc))

    def _on_preview_ready(self, summary: str, detail: str):
        self._set_status(self._preview_summary, summary)
        self._preview_box.setPlainText(detail)
        self._apply_btn.setEnabled(bool(self._plan))

    # ---- apply ------------------------------------------------------------------
    def _apply(self):
        if not self._plan:
            return
        from Utils.bg3.importer import apply_plan
        try:
            path = apply_plan(self._plan)
            self._log(f"BG3 Import: wrote new load order to {path}")
            self._ran = True     # refresh_modlist on _finish
            self._stack.setCurrentIndex(_PG_DONE)
        except Exception as exc:
            self._log(f"BG3 Import: apply error: {exc}")
            self._apply_btn.setText(self.tr("Failed"))

    # ---- page 3: done -----------------------------------------------------------
    def _build_done_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Load order applied"))
        self._make_note(lay,
                        self.tr("The modlist has been reordered to match the imported order.\n"
                        "Deploy to push the new load order to the game."))
        lay.addStretch(1)
        done = self._green_btn(self.tr("Done"))
        done.clicked.connect(self._finish)
        lay.addWidget(done, 0, Qt.AlignHCenter)
        return page
