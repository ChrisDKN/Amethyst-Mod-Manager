from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QCompleter, QLabel

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import err_text, ok_text, warn_text
from Utils.bethesda.cao import (
    APP_DIR, EXE_NAME, configure_profile, find_cao_exe, profile_for_game,
    staged_mod_paths,
)
from wizards_qt._view_base import WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame


_NEXUS_URL = (
    "https://www.nexusmods.com/skyrimspecialedition/mods/23316"
    "?tab=files&file_id=400106"
)
_NEXUS_FILE_ID = 400106
_ARCHIVE_KEYWORDS = ["cathedral", "assets", "optimizer"]

(_PG_DOWNLOAD, _PG_LOCATE, _PG_EXTRACT, _PG_MOD, _PG_PROTON,
 _PG_RUN) = range(6)


class CAOView(WizardViewBase):
    _tool_closed_sig = Signal(bool, str)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(
            game, log_fn, on_close, ctx,
            title=self.tr("Assets Optimizer (CAO) - {0}").format(game.name),
        )
        self._exe = find_cao_exe(game)
        self._proton_name = ""
        self._prefix_mode = ""
        self._mod_path: Path | None = None
        self._tool_closed_sig.connect(self._guard(self._on_tool_closed))

        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 1: Download Cathedral Assets Optimizer"),
            self.tr(
                "Click the button below to open Cathedral Assets Optimizer "
                "on Nexus Mods.\n\nDownload the 64-bit archive, then click Next."),
            _NEXUS_URL,
            lambda: self._goto_step(_PG_LOCATE),
        ))
        self._stack.addWidget(self._build_locate_page(
            self.tr("Step 2: Locate the Archive")))
        self._stack.addWidget(self._build_extract_page(
            self.tr("Step 3: Extract Cathedral Assets Optimizer")))
        self._stack.addWidget(self._build_mod_page())
        self._stack.addWidget(self._build_proton_holder())
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 6: Run Assets Optimizer (CAO)")))

        if self._exe is not None:
            self._goto_step(_PG_MOD)
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)
            self._nexus_auto_fetch(
                url=_NEXUS_URL,
                file_id=_NEXUS_FILE_ID,
                keywords=_ARCHIVE_KEYWORDS,
                label="Cathedral Assets Optimizer",
                pages=(_PG_DOWNLOAD, _PG_LOCATE),
                on_archive=lambda _path: self._goto_step(_PG_EXTRACT),
            )

    def _build_mod_page(self):
        page, lay = self._step_page(self.tr("Step 4: Choose Mod"))
        self._make_note(lay, self.tr(
            "Choose the staged mod that CAO should optimize. Enabled and "
            "disabled mod folders are both listed; deployment is not required."
        ))

        label = QLabel(self.tr("Mod:"))
        label.setAlignment(Qt.AlignHCenter)
        lay.addWidget(label)

        self._mod_combo = QComboBox()
        self._mod_combo.setMinimumWidth(420)
        self._mod_combo.setEditable(True)
        self._mod_combo.setInsertPolicy(QComboBox.NoInsert)
        self._mod_combo.setMaxVisibleItems(12)
        self._mod_combo.lineEdit().setPlaceholderText(self.tr("Search mods…"))
        self._mod_combo.lineEdit().setClearButtonEnabled(True)
        self._mod_completer = QCompleter(
            self._mod_combo.model(), self._mod_combo)
        self._mod_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._mod_completer.setFilterMode(Qt.MatchContains)
        self._mod_completer.setCompletionMode(QCompleter.PopupCompletion)
        self._mod_completer.setMaxVisibleItems(12)
        self._mod_combo.setCompleter(self._mod_completer)
        self._mod_completer.activated[str].connect(
            self._select_mod_completion)
        self._mod_combo.currentIndexChanged.connect(self._show_selected_mod)
        self._mod_combo.editTextChanged.connect(self._show_selected_mod)
        lay.addWidget(self._mod_combo, 0, Qt.AlignHCenter)

        self._mod_status = self._make_status(lay)
        lay.addStretch(1)
        self._mod_continue_btn = self._accent_btn(self.tr("Continue"))
        self._mod_continue_btn.clicked.connect(self._accept_mod)
        lay.addWidget(self._mod_continue_btn, 0, Qt.AlignHCenter)
        return page

    def _goto_step(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        if index == _PG_LOCATE:
            self._enter_locate(
                _ARCHIVE_KEYWORDS,
                self.tr("Select the Cathedral Assets Optimizer archive"),
                self.tr(
                    "Cathedral Assets Optimizer archive not found in your "
                    "download locations.\nPress Try Again, or use Browse to "
                    "select it manually."),
                lambda _path: self._goto_step(_PG_EXTRACT),
            )
        elif index == _PG_EXTRACT:
            self._extract_to_applications(
                APP_DIR, EXE_NAME, "Cathedral Assets Optimizer")
        elif index == _PG_MOD:
            self._populate_mods()
        elif index == _PG_PROTON:
            self._enter_proton(
                self._exe, EXE_NAME, "Assets Optimizer (CAO)",
                self._on_proton_chosen,
                title=self.tr("Step 5: Choose Proton Version"),
                missing_text=self.tr(
                    "{0} was not found. Reopen the wizard and install Cathedral "
                    "Assets Optimizer first.").format(EXE_NAME),
            )
        elif index == _PG_RUN:
            self._start_run()

    def _on_extract_done(self, ok: bool) -> None:
        if ok:
            self._exe = find_cao_exe(self._game)
            self._goto_step(_PG_MOD)

    def _populate_mods(self) -> None:
        paths = staged_mod_paths(self._game)
        self._mod_combo.blockSignals(True)
        self._mod_combo.clear()
        for path in paths:
            self._mod_combo.addItem(path.name, str(path))
        self._mod_combo.blockSignals(False)
        self._mod_continue_btn.setEnabled(bool(paths))
        if not paths:
            self._set_status(
                self._mod_status,
                self.tr("No mod folders were found in the staging folder."),
                err_text(),
            )
            return
        self._mod_combo.setCurrentIndex(0)
        self._show_selected_mod()

    def _select_mod_completion(self, text: str) -> None:
        index = self._mod_combo.findText(text, Qt.MatchFixedString)
        if index >= 0:
            self._mod_combo.setCurrentIndex(index)

    def _selected_mod_path(self) -> Path | None:
        index = self._mod_combo.findText(
            self._mod_combo.currentText(), Qt.MatchFixedString)
        value = self._mod_combo.itemData(index) if index >= 0 else None
        return Path(value) if value else None

    def _show_selected_mod(self, _value=None) -> None:
        path = self._selected_mod_path()
        self._set_status(
            self._mod_status,
            self.tr("Mod path: {0}").format(path) if path else "",
        )

    def _accept_mod(self) -> None:
        path = self._selected_mod_path()
        if path is None:
            self._set_status(
                self._mod_status, self.tr("Select a mod first."), err_text())
            return
        staging = Path(self._game.get_effective_mod_staging_path())
        try:
            valid = path.parent.resolve() == staging.resolve() and path.is_dir()
        except OSError:
            valid = False
        if not valid:
            self._set_status(
                self._mod_status,
                self.tr("The selected mod folder no longer exists."),
                err_text(),
            )
            return
        self._mod_path = path
        self._goto_step(_PG_PROTON)

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str) -> None:
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_RUN)

    def _set_context_lock(self, held: bool) -> None:
        hook = getattr(self._ctx, "set_tool_lock", None)
        if hook is not None:
            hook("cao", self.tr("Assets Optimizer (CAO)"), held)

    def _start_run(self) -> None:
        exe = self._exe
        mod_path = self._mod_path
        if exe is None or not exe.is_file():
            self._set_status(
                self._run_status,
                self.tr("{0} was not found.").format(EXE_NAME),
                err_text(),
            )
            return
        if mod_path is None or not mod_path.is_dir():
            self._set_status(
                self._run_status,
                self.tr("The selected mod folder is unavailable."),
                err_text(),
            )
            return

        self._set_status(self._run_status,
                         self.tr("Preparing Assets Optimizer…"))
        self._set_context_lock(True)
        self._lock_close(True, self.tr(
            "Assets Optimizer is preparing or running - close it to continue."))
        self._ran = True
        game = self._game
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.executables.launch import (
                PREFIX_MODE_GAME, resolve_tool_prefix, run_tool_logged,
                shutdown_prefix_wineserver,
            )
            from Utils.wine.paths import to_wine_path

            log = lambda message: self._log(
                f"Assets Optimizer (CAO) Wizard: {message}")
            proton_script = compat_data = None
            launched = False
            detail = ""
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=log)
                if result is None:
                    if prefix_mode == PREFIX_MODE_GAME:
                        raise RuntimeError(self.tr(
                            "Could not resolve Proton for the game's own prefix."))
                    raise RuntimeError(self.tr(
                        "Could not find Proton '{0}'.").format(proton_name))
                proton_script, compat_data, env = result
                user_path = to_wine_path(
                    mod_path, compat_data / "pfx").replace("\\", "/")
                profile = profile_for_game(game)
                settings = configure_profile(exe, profile, user_path)
                log(f"selected CAO profile {profile}; userPath={user_path} "
                    f"in {settings}")

                safe_emit(self._run_status_sig,
                          self.tr("Assets Optimizer is running. Close it when done."),
                          ok_text())
                launched = True
                return_code = run_tool_logged(
                    proton_script, exe, env, log_fn=log,
                    label="Assets Optimizer (CAO)", game=game, owner=self,
                )
                if return_code != 0:
                    detail = self.tr("CAO exited with code {0}.").format(return_code)
            except Exception as exc:
                detail = str(exc)
                log(f"launch error: {exc}")
            finally:
                if proton_script is not None and compat_data is not None:
                    shutdown_prefix_wineserver(
                        proton_script, compat_data, log_fn=log)
                safe_emit(self._tool_closed_sig, launched, detail)

        threading.Thread(target=worker, daemon=True, name="cao-run").start()

    def _on_tool_closed(self, launched: bool, detail: str) -> None:
        self._set_context_lock(False)
        self._lock_close(False)
        if not launched:
            self._set_status(
                self._run_status,
                self.tr("Could not launch Assets Optimizer: {0}").format(detail),
                err_text(),
            )
            self._done_btn.setEnabled(True)
            return

        if detail:
            text = self.tr(
                "{0} Any changes remain in the selected mod folder.").format(
                    detail)
            color = warn_text()
        else:
            text = self.tr(
                "Assets Optimizer finished. Changes were saved directly to "
                "the selected mod folder.")
            color = ok_text()
        self._set_status(self._run_status, text, color)
        self._done_btn.setEnabled(True)
