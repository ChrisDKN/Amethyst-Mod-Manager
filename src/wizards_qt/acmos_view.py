"""ACMOS Road Generator installer and launcher for Skyrim SE."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QCompleter, QLabel

from gui_qt.safe_emit import safe_emit
from Utils.bethesda.acmos import (
    APP_DIR, EXE_NAME, OUTPUT_DIR, contains_terrain_lod, find_acmos_exe,
    profile_mod_names,
)
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame


_NEXUS_URL = (
    "https://www.nexusmods.com/skyrimspecialedition/mods/79205?tab=files"
)
_NEXUS_FILE_ID = 715796
_ARCHIVE_KEYWORDS = ["acmos", "road", "generator"]

(_PG_DOWNLOAD, _PG_LOCATE, _PG_EXTRACT, _PG_LOD, _PG_PROTON,
 _PG_RUN) = range(6)


class ACMOSView(WizardViewBase):
    """Install ACMOS and run it against a staged terrain LOD mod."""

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run ACMOS Road Generator - {0}")
                         .format(game.name))
        self._exe = find_acmos_exe(game)
        self._proton_name = ""
        self._prefix_mode = ""
        self._proton_step = None
        self._prefer_discrete_gpu = False
        self._lod_path: Path | None = None
        self._output_path: Path | None = None

        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 1: Download ACMOS Road Generator"),
            self.tr("Click the button below to open ACMOS Road Generator on "
                    "Nexus Mods.\n\nDownload the archive manually (do NOT use "
                    "the Mod Manager download button), then click Next."),
            _NEXUS_URL,
            lambda: self._goto_step(_PG_LOCATE)))
        self._stack.addWidget(self._build_locate_page(
            self.tr("Step 2: Locate the Archive")))
        self._stack.addWidget(self._build_extract_page(
            self.tr("Step 3: Extract ACMOS Road Generator")))
        self._stack.addWidget(self._build_lod_page())
        self._stack.addWidget(self._build_proton_holder())
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 6: Run ACMOS Road Generator")))

        if self._exe is not None:
            self._goto_step(_PG_LOD)
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)
            self._nexus_auto_fetch(
                url=_NEXUS_URL, file_id=_NEXUS_FILE_ID,
                keywords=_ARCHIVE_KEYWORDS,
                label="ACMOS Road Generator",
                pages=(_PG_DOWNLOAD, _PG_LOCATE),
                on_archive=lambda _path: self._goto_step(_PG_EXTRACT))

    def _build_lod_page(self):
        page, lay = self._step_page(self.tr("Step 4: Choose Terrain LOD Mod"))
        self._make_note(lay, self.tr(
            "Choose the profile mod containing your xLODGen terrain output. "
            "Enabled and disabled mods are both listed; deployment is not "
            "required.\n\nGenerated textures are written to a separate "
            "ACMOS_Output mod. Remove an old ACMOS_Output first if you want "
            "a completely clean result."))

        label = QLabel(self.tr("Terrain LOD mod:"))
        label.setAlignment(Qt.AlignHCenter)
        lay.addWidget(label)

        self._lod_combo = QComboBox()
        self._lod_combo.setMinimumWidth(360)
        self._lod_combo.setEditable(True)
        self._lod_combo.setInsertPolicy(QComboBox.NoInsert)
        self._lod_combo.setMaxVisibleItems(10)
        self._lod_combo.lineEdit().setPlaceholderText(self.tr("Search mods…"))
        self._lod_combo.lineEdit().setClearButtonEnabled(True)
        self._lod_completer = QCompleter(
            self._lod_combo.model(), self._lod_combo)
        self._lod_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._lod_completer.setFilterMode(Qt.MatchContains)
        self._lod_completer.setCompletionMode(QCompleter.PopupCompletion)
        self._lod_completer.setMaxVisibleItems(10)
        self._lod_combo.setCompleter(self._lod_completer)
        self._lod_completer.activated[str].connect(
            self._select_lod_completion)
        self._lod_combo.currentIndexChanged.connect(self._show_selected_path)
        self._lod_combo.editTextChanged.connect(self._show_selected_path)
        lay.addWidget(self._lod_combo, 0, Qt.AlignHCenter)

        self._lod_status = self._make_status(lay)
        self._output_label = QLabel("")
        self._output_label.setAlignment(Qt.AlignHCenter)
        self._output_label.setWordWrap(True)
        self._output_label.setStyleSheet(self._dim)
        lay.addWidget(self._output_label)
        lay.addStretch(1)

        self._lod_continue_btn = self._accent_btn(self.tr("Continue"))
        self._lod_continue_btn.clicked.connect(self._accept_lod_mod)
        lay.addWidget(self._lod_continue_btn, 0, Qt.AlignHCenter)
        return page

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_LOCATE:
            self._enter_locate(
                _ARCHIVE_KEYWORDS,
                self.tr("Select the ACMOS Road Generator archive"),
                self.tr("ACMOS Road Generator archive not found in Downloads.\n"
                        "Make sure you downloaded it, then press Try Again,\n"
                        "or use Browse to select it manually."),
                lambda _path: self._goto_step(_PG_EXTRACT))
        elif idx == _PG_EXTRACT:
            self._extract_to_applications(APP_DIR, EXE_NAME,
                                          "ACMOS Road Generator")
        elif idx == _PG_LOD:
            self._populate_lod_mods()
        elif idx == _PG_PROTON:
            self._proton_step = self._enter_proton(
                self._exe, EXE_NAME, "ACMOS Road Generator",
                self._on_proton_chosen,
                allow_game_prefix=False,
                show_discrete_gpu=True,
                title=self.tr("Step 5: Choose Proton Version"),
                missing_text=self.tr(
                    "{0} was not found.\nPlease restart the wizard and "
                    "install ACMOS Road Generator first.").format(EXE_NAME))
        elif idx == _PG_RUN:
            self._start_run()

    def _on_extract_done(self, ok: bool):
        if ok:
            self._exe = find_acmos_exe(self._game)
            self._goto_step(_PG_LOD)

    def _profile_name(self) -> str:
        current = getattr(self._ctx, "current_profile", None)
        if callable(current):
            try:
                return current() or "default"
            except Exception:
                pass
        return getattr(self._ctx, "profile_name", "default") or "default"

    def _populate_lod_mods(self):
        names = profile_mod_names(self._game, self._profile_name())
        staging = Path(self._game.get_effective_mod_staging_path())
        output = staging / OUTPUT_DIR

        self._lod_combo.blockSignals(True)
        self._lod_combo.clear()
        for name in names:
            self._lod_combo.addItem(name, name)
        self._lod_combo.blockSignals(False)
        self._output_label.setText(
            self.tr("Output mod: {0}").format(output))
        self._lod_continue_btn.setEnabled(bool(names))

        if not names:
            self._set_status(
                self._lod_status,
                self.tr("No mods were found in the current profile."), RED)
            return

        best_index = min(
            range(len(names)),
            key=lambda index: self._lod_score(staging / names[index],
                                              names[index]))
        self._lod_combo.setCurrentIndex(best_index)
        self._show_selected_path()

    @staticmethod
    def _lod_score(path: Path, name: str) -> tuple[int, str]:
        low = name.casefold()
        has_lod = contains_terrain_lod(path)
        if has_lod and "xlodgen" in low and "output" in low:
            rank = 0
        elif has_lod and "lod" in low:
            rank = 1
        elif has_lod:
            rank = 2
        else:
            rank = 3
        return rank, low

    def _show_selected_path(self, _value=None):
        name = self._selected_lod_name()
        if not name:
            self._set_status(self._lod_status, "")
            return
        path = Path(self._game.get_effective_mod_staging_path()) / str(name)
        self._set_status(
            self._lod_status,
            self.tr("LOD path: {0}").format(path),
            GREEN if contains_terrain_lod(path) else "")

    def _select_lod_completion(self, text: str):
        index = self._lod_combo.findText(text, Qt.MatchFixedString)
        if index >= 0:
            self._lod_combo.setCurrentIndex(index)

    def _selected_lod_name(self):
        index = self._lod_combo.findText(
            self._lod_combo.currentText(), Qt.MatchFixedString)
        return self._lod_combo.itemData(index) if index >= 0 else None

    def _accept_lod_mod(self):
        name = self._selected_lod_name()
        if not name:
            self._set_status(self._lod_status,
                             self.tr("Select a Terrain LOD mod first."), RED)
            return

        staging = Path(self._game.get_effective_mod_staging_path())
        lod_path = staging / str(name)
        if not lod_path.is_dir():
            self._set_status(
                self._lod_status,
                self.tr("The selected mod folder no longer exists."), RED)
            return
        if not contains_terrain_lod(lod_path):
            self._set_status(
                self._lod_status,
                self.tr("The selected mod does not contain a textures/terrain "
                        "folder. Choose the mod created from xLODGen output."),
                RED)
            return

        self._lod_path = lod_path
        self._output_path = staging / OUTPUT_DIR
        self._goto_step(_PG_PROTON)

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._prefer_discrete_gpu = bool(
            self._proton_step is not None
            and self._proton_step.prefer_discrete_gpu()
        )
        self._goto_step(_PG_RUN)

    def _start_run(self):
        exe = self._exe
        lod_path = self._lod_path
        output_path = self._output_path
        if exe is None or lod_path is None or output_path is None:
            self._set_status(
                self._run_status,
                self.tr("ACMOS Road Generator is not ready to run."), RED)
            return

        self._set_status(
            self._run_status,
            self.tr("Preparing ACMOS Road Generator's Wine prefix…"))
        game = self._game
        proton_name, prefix_mode = self._proton_name, self._prefix_mode
        prefer_discrete_gpu = self._prefer_discrete_gpu

        def worker():
            from Utils.bethesda.acmos import cli_path_args
            from Utils.executables.launch import (
                PREFIX_MODE_GAME, resolve_tool_prefix, run_tool_logged,
                shutdown_prefix_wineserver,
            )

            _wlog = lambda message: self._log(
                f"ACMOS Road Generator Wizard: {message}")
            proton_script = compat_data = None
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    if prefix_mode == PREFIX_MODE_GAME:
                        message = self.tr(
                            "Could not resolve the Proton version for the "
                            "game's own prefix - launch the game once, or pick "
                            "a different prefix option.")
                    else:
                        message = self.tr(
                            "Could not find Proton '{0}' - check that it is "
                            "installed in Steam, Heroic or ProtonPlus.").format(
                                proton_name)
                    safe_emit(self._run_status_sig, message, RED)
                    return

                proton_script, compat_data, env = result
                from Utils.wizards.textures import apply_discrete_gpu_environment
                gpu_selection = apply_discrete_gpu_environment(
                    env, prefer_discrete_gpu)
                _wlog(f"GPU: {gpu_selection}")
                args = cli_path_args(lod_path, output_path, compat_data)
                _wlog(f"LOD path: {lod_path}")
                _wlog(f"output path: {output_path}")
                _wlog(f"arguments: {args!r}")
                safe_emit(self._run_started_sig)
                rc = run_tool_logged(
                    proton_script, exe, env, log_fn=_wlog,
                    extra_args=args, label="ACMOS Road Generator",
                    game=game, owner=self)
                if rc != 0:
                    safe_emit(
                        self._run_status_sig,
                        self.tr("ACMOS Road Generator exited with error "
                                "(code {0}).").format(rc), RED)
                    return
                safe_emit(
                    self._run_status_sig,
                    self.tr("ACMOS Road Generator finished."), GREEN)
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(
                    self._run_status_sig,
                    self.tr("Launch error: {0}").format(exc), RED)
                _wlog(f"launch error: {exc}")
            finally:
                if proton_script is not None and compat_data is not None:
                    shutdown_prefix_wineserver(
                        proton_script, compat_data, log_fn=_wlog)

        threading.Thread(target=worker, daemon=True,
                         name="acmos-run").start()

    def _on_run_started(self):
        self._ran = True
        self._set_status(
            self._run_status,
            self.tr("ACMOS Road Generator is running.\nChoose Roads or Paths "
                    "Only, click Generate, then close it when finished."),
            GREEN)
        self._done_btn.setEnabled(True)
