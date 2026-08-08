"""VRAMr / BENDr / ParallaxR wizards - Qt port of wizards/vramr.py and
wizards/bendr_parallaxr.py.

All three share one shape: manual Nexus download → locate → extract to
Applications/<tool>/ (verified by its tools/ dir) → deploy → run the neutral
wrapper (wrappers/vramr.py / bendr.py / parallaxr.py) against the deployed
Data folder, writing output as a staging mod.  VRAMr adds a preset radio
group; the others run straight away.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QT_TRANSLATE_NOOP, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QLabel, QRadioButton, QVBoxLayout,
    QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.texture_tools import (
    VRAMR_PRESETS, texture_tool_installed, vramr_installed,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# tool id → (title, nexus_url, app_dir, archive keyword, output dir, run desc,
#            has_presets)
_TOOLS = {
    "vramr": (
        "VRAMr",
        "https://www.nexusmods.com/skyrimspecialedition/mods/90557?tab=files",
        "VRAMr", "vramr", "VRAMr",
        QT_TRANSLATE_NOOP("TextureToolView",
                          "Select an optimisation preset, then click Run."),
        True),
    "bendr": (
        "BENDr",
        "https://www.nexusmods.com/skyrimspecialedition/mods/121578?tab=files",
        "BENDr", "bendr", "BENDr",
        QT_TRANSLATE_NOOP("TextureToolView",
                          "Processes normal maps: BSA extract → filter → "
                          "parallax prep → bend normals → BC7 compress"),
        False),
    "parallaxr": (
        "ParallaxR",
        "https://www.nexusmods.com/skyrimspecialedition/mods/124711?tab=files",
        "ParallaxR", "parallaxr", "ParallaxR",
        QT_TRANSLATE_NOOP("TextureToolView",
                          "Processes parallax textures: BSA extract → filter "
                          "pairs → height maps → output QC"),
        False),
}

_PG_DOWNLOAD, _PG_LOCATE, _PG_EXTRACT, _PG_DEPLOY, _PG_RUN = range(5)


class TextureToolView(WizardViewBase):
    """Download, install, deploy and run VRAMr / BENDr / ParallaxR."""

    _run_reenable_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, tool: str = "vramr", **_extra):
        (self._name, self._nexus_url, self._app_dir, self._archive_kw,
         self._output_dir, self._run_desc, self._has_presets) = _TOOLS[tool]
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run {0} - {1}").format(self._name, game.name))
        self._preset = "optimum"
        self._run_reenable_sig.connect(self._guard(
            lambda: self._run_btn.setEnabled(True)))

        self._installed = (vramr_installed(game) if tool == "vramr"
                           else texture_tool_installed(game, self._app_dir))

        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 1: Download {0}").format(self._name),
            self.tr("Click the button below to open the {0} page on Nexus "
            "Mods, then download the archive.\n\nOnce downloaded, click Next.")
            .format(self._name),
            self._nexus_url,
            lambda: self._goto_step(_PG_LOCATE)))
        self._stack.addWidget(self._build_locate_page(
            self.tr("Step 2: Locate the Archive")))
        self._stack.addWidget(self._build_extract_page(
            self.tr("Step 3: Extract {0}").format(self._name)))
        self._stack.addWidget(self._build_deploy_page(
            self.tr("Step 4: Deploy Modlist"),
            self.tr("{0} reads your mods from the deployed Data folder.\n\n"
            "Deploy your modlist first, then click Run.").format(self._name),
            lambda: self._goto_step(_PG_RUN)))
        self._stack.addWidget(self._build_run_page())

        if self._installed:
            self._goto_step(_PG_DEPLOY)
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)

    def _build_run_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 5: Run {0}").format(self._name))
        self._make_note(lay, self.tr(self._run_desc))

        if self._has_presets:
            p = active_palette()
            box = QWidget()
            box.setStyleSheet(
                f"background:{_c(p,'BG_PANEL')}; border-radius:6px;")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(12, 8, 12, 8)
            self._preset_group = QButtonGroup(self)
            for key, label, desc in VRAMR_PRESETS:
                row = QWidget()
                rh = QHBoxLayout(row); rh.setContentsMargins(0, 2, 0, 2)
                rb = QRadioButton(label)
                rb.setProperty("preset_key", key)
                if key == "optimum":
                    rb.setChecked(True)
                self._preset_group.addButton(rb)
                rh.addWidget(rb)
                d = QLabel(desc)
                d.setStyleSheet(self._dim)
                rh.addWidget(d)
                rh.addStretch(1)
                bl.addWidget(row)
            lay.addWidget(box)

        staging = self._game.get_effective_mod_staging_path()
        out_lbl = QLabel(self.tr("Output: {0}").format(staging / self._output_dir))
        out_lbl.setWordWrap(True)
        out_lbl.setStyleSheet(self._dim)
        lay.addWidget(out_lbl)

        self._run_status = self._make_status(lay)
        lay.addStretch(1)
        self._run_btn = self._accent_btn(self.tr("▶  Run {0}").format(self._name))
        self._run_btn.clicked.connect(self._start_run)
        lay.addWidget(self._run_btn, 0, Qt.AlignHCenter)
        self._done_btn = self._green_btn(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_LOCATE:
            self._enter_locate(
                [self._archive_kw],
                self.tr("Select the {0} archive").format(self._name),
                self.tr("{0} archive not found in Downloads.\n"
                "Make sure you downloaded it, then press Try Again,\n"
                "or use Browse to select it manually.").format(self._name),
                lambda _p: self._goto_step(_PG_EXTRACT))
        elif idx == _PG_EXTRACT:
            self._extract_to_applications(
                self._app_dir, "", self._name, marker="tools")

    def _on_extract_done(self, ok: bool):
        if ok:
            self._installed = True
            self._goto_step(_PG_DEPLOY)

    # ---- run ----------------------------------------------------------------------
    def _selected_preset(self) -> str:
        if not self._has_presets:
            return ""
        btn = self._preset_group.checkedButton()
        return btn.property("preset_key") if btn is not None else "optimum"

    def _start_run(self):
        game = self._game
        if not self._installed:
            self._set_status(self._run_status,
                             self.tr("{0} not found. Please restart the wizard.").format(self._name), RED)
            return
        game_data_dir = game.get_mod_data_path()
        if game_data_dir is None or not game_data_dir.is_dir():
            self._set_status(self._run_status,
                             self.tr("Game Data folder not found. Deploy first."), RED)
            return

        self._run_btn.setEnabled(False)
        preset = self._selected_preset()
        from Utils.texture_tools import applications_dir
        bat_dir = applications_dir(game, self._app_dir)
        staging = game.get_effective_mod_staging_path()
        output_dir = staging / self._output_dir
        name, tool_id = self._name, self._app_dir.lower()

        if preset:
            run_msg = self.tr("Running {0} ({1})… This may take a while.").format(
                name, preset)
        else:
            run_msg = self.tr("Running {0}… This may take a while.").format(name)
        self._set_status(self._run_status, run_msg)
        self._log(f"{name} Wizard: starting pipeline…")

        def worker():
            _wlog = lambda m: self._log(str(m))
            try:
                if tool_id == "vramr":
                    from wrappers.vramr import run_vramr
                    run_vramr(bat_dir=bat_dir, game_data_dir=game_data_dir,
                              output_dir=output_dir, preset=preset,
                              log_fn=_wlog)
                elif tool_id == "bendr":
                    from wrappers.bendr import run_bendr
                    run_bendr(bat_dir=bat_dir, game_data_dir=game_data_dir,
                              output_dir=output_dir, log_fn=_wlog)
                else:
                    from wrappers.parallaxr import run_parallaxr
                    run_parallaxr(bat_dir=bat_dir, game_data_dir=game_data_dir,
                                  output_dir=output_dir, log_fn=_wlog)
                safe_emit(self._run_status_sig,
                          self.tr("{0} complete! Output is ready as a mod.")
                          .format(name), GREEN)
                # marks _ran + enables Done (no auto-close - let the user read
                # the result and click Done).
                safe_emit(self._run_started_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig,
                          self.tr("Error: {0}").format(exc), RED)
                self._log(f"{name} Wizard: error: {exc}")
                safe_emit(self._run_reenable_sig)

        threading.Thread(target=worker, daemon=True,
                         name=f"{tool_id}-run").start()

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)
