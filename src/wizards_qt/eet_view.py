from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QCompleter, QHBoxLayout, QLabel, QPushButton, QWidget,
)

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import err_text, ok_text, warn_text
from Utils.bethesda.eet import (
    APP_DIR, EXE_NAME, configure_game_data, find_eet_exe, staged_plugins,
)
from wizards_qt._view_base import WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame


_NEXUS_URL = (
    "https://www.nexusmods.com/skyrimspecialedition/mods/921"
    "?tab=files"
)
_NEXUS_FILE_ID = 574006
_ARCHIVE_KEYWORDS = ["esp-esm", "translator"]

(_PG_DOWNLOAD, _PG_LOCATE, _PG_EXTRACT, _PG_PLUGIN, _PG_DEPLOY,
 _PG_PROTON, _PG_RUN) = range(7)


class EETView(WizardViewBase):
    _plugins_ready_sig = Signal(object)
    _tool_closed_sig = Signal(bool, str)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(
            game, log_fn, on_close, ctx,
            title=self.tr("ESP-ESM Translator - {0}").format(game.name),
        )
        self._exe = find_eet_exe(game)
        self._plugin_path: Path | None = None
        self._proton_name = ""
        self._prefix_mode = ""
        self._plugins_ready_sig.connect(self._guard(self._populate_plugin_rows))
        self._tool_closed_sig.connect(self._guard(self._on_tool_closed))

        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 1: Download ESP-ESM Translator"),
            self.tr(
                "Open the ESP-ESM Translator files page and download the "
                "'Application only Date up' archive, then click Next."),
            _NEXUS_URL,
            lambda: self._goto_step(_PG_LOCATE),
        ))
        self._stack.addWidget(self._build_locate_page(
            self.tr("Step 2: Locate the Archive")))
        self._stack.addWidget(self._build_extract_page(
            self.tr("Step 3: Extract ESP-ESM Translator")))
        self._stack.addWidget(self._build_plugin_page())
        self._stack.addWidget(self._build_eet_deploy_page())
        self._stack.addWidget(self._build_proton_holder())
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 7: Run ESP-ESM Translator")))

        if self._exe is not None:
            self._goto_step(_PG_PLUGIN)
            self._offer_tool_upgrade(_PG_PLUGIN,
                                     lambda: self._goto_step(_PG_DOWNLOAD))
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)
            self._nexus_auto_fetch(
                url=_NEXUS_URL,
                file_id=_NEXUS_FILE_ID,
                keywords=_ARCHIVE_KEYWORDS,
                label="ESP-ESM Translator",
                pages=(_PG_DOWNLOAD, _PG_LOCATE),
                on_archive=lambda _path: self._goto_step(_PG_EXTRACT),
            )

    def _build_plugin_page(self):
        page, lay = self._step_page(self.tr("Step 4: Choose Plugin (Optional)"))
        self._make_note(lay, self.tr(
            "Choose a plugin from a staged mod. EET receives that file "
            "directly, so the translation and supported sidecar-file changes "
            "stay in the owning mod folder. You can also open EET without a "
            "target and choose a staged file inside the application."
        ))
        label = QLabel(self.tr("Plugin:"))
        label.setAlignment(Qt.AlignHCenter)
        lay.addWidget(label)

        self._plugin_combo = QComboBox()
        self._plugin_combo.setMinimumWidth(520)
        self._plugin_combo.setEditable(True)
        self._plugin_combo.setInsertPolicy(QComboBox.NoInsert)
        self._plugin_combo.setMaxVisibleItems(14)
        self._plugin_combo.lineEdit().setPlaceholderText(
            self.tr("Search staged plugins…"))
        self._plugin_combo.lineEdit().setClearButtonEnabled(True)
        self._plugin_completer = QCompleter(
            self._plugin_combo.model(), self._plugin_combo)
        self._plugin_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._plugin_completer.setFilterMode(Qt.MatchContains)
        self._plugin_completer.setCompletionMode(QCompleter.PopupCompletion)
        self._plugin_completer.setMaxVisibleItems(14)
        self._plugin_combo.setCompleter(self._plugin_completer)
        self._plugin_completer.activated[QModelIndex].connect(
            self._select_plugin_completion)
        self._plugin_combo.currentIndexChanged.connect(
            self._show_selected_plugin)
        self._plugin_combo.editTextChanged.connect(self._show_selected_plugin)
        lay.addWidget(self._plugin_combo, 0, Qt.AlignHCenter)

        self._plugin_status = self._make_status(lay)
        lay.addStretch(1)
        self._plugin_continue_btn = self._accent_btn(self.tr("Continue"))
        self._plugin_continue_btn.setEnabled(False)
        self._plugin_continue_btn.clicked.connect(self._accept_plugin)
        lay.addWidget(self._plugin_continue_btn, 0, Qt.AlignHCenter)
        return page

    def _build_eet_deploy_page(self):
        page, lay = self._step_page(self.tr("Step 5: Prepare Game Data"))
        self._make_note(lay, self.tr(
            "Deploy to let EET read the active profile's masters and archives "
            "through the game's Data folder. Skip if the required files are "
            "already available there. The selected plugin is still edited "
            "directly in its staged mod."
        ))
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)
        self._deploy_skip_btn = QPushButton(self.tr("Skip"))
        self._deploy_skip_btn.setCursor(Qt.PointingHandCursor)
        self._deploy_skip_btn.clicked.connect(self._skip_deploy)
        self._deploy_btn = self._accent_btn(self.tr("Deploy"))
        self._deploy_btn.clicked.connect(self._start_eet_deploy)
        row = QWidget()
        buttons = QHBoxLayout(row)
        buttons.setContentsMargins(0, 8, 0, 0)
        buttons.setSpacing(8)
        buttons.addStretch(1)
        buttons.addWidget(self._deploy_skip_btn)
        buttons.addWidget(self._deploy_btn)
        buttons.addStretch(1)
        lay.addWidget(row)
        return page

    def _goto_step(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        if index == _PG_LOCATE:
            self._enter_locate(
                _ARCHIVE_KEYWORDS,
                self.tr("Select the ESP-ESM Translator archive"),
                self.tr(
                    "ESP-ESM Translator was not found in your download "
                    "locations. Press Try Again, or use Browse to select the "
                    "archive manually."),
                lambda _path: self._goto_step(_PG_EXTRACT),
            )
        elif index == _PG_EXTRACT:
            self._extract_to_applications(
                APP_DIR, EXE_NAME, "ESP-ESM Translator")
        elif index == _PG_PLUGIN:
            self._scan_plugins()
        elif index == _PG_PROTON:
            from Utils.executables.launch import PREFIX_MODE_GAME
            self._enter_proton(
                self._exe, EXE_NAME, "ESP-ESM Translator",
                self._on_proton_chosen,
                default_prefix_mode=PREFIX_MODE_GAME,
                title=self.tr("Step 6: Choose Proton Version"),
                missing_text=self.tr(
                    "{0} was not found. Reopen the wizard and install "
                    "ESP-ESM Translator first.").format(EXE_NAME),
            )
        elif index == _PG_RUN:
            self._start_run()

    def _on_extract_done(self, ok: bool) -> None:
        if ok:
            self._exe = find_eet_exe(self._game)
            self._goto_step(_PG_PLUGIN)

    def _scan_plugins(self) -> None:
        self._plugin_combo.clear()
        self._plugin_combo.setEnabled(False)
        self._plugin_continue_btn.setEnabled(False)
        self._set_status(
            self._plugin_status, self.tr("Scanning staged mods for plugins…"))

        def worker():
            safe_emit(self._plugins_ready_sig, staged_plugins(self._game))

        threading.Thread(target=worker, daemon=True,
                         name="eet-plugin-scan").start()

    def _populate_plugin_rows(self, plugins) -> None:
        self._plugin_combo.blockSignals(True)
        self._plugin_combo.clear()
        self._plugin_combo.addItem(
            self.tr("Open ESP-ESM Translator without a target"), "")
        staging = Path(self._game.get_effective_mod_staging_path())
        for mod_name, path in plugins:
            try:
                relative = path.relative_to(staging / mod_name)
            except ValueError:
                relative = Path(path.name)
            self._plugin_combo.addItem(path.name, str(path))
            self._plugin_combo.setItemData(
                self._plugin_combo.count() - 1,
                self.tr("{0} / {1}").format(mod_name, relative),
                Qt.ToolTipRole,
            )
        self._plugin_combo.blockSignals(False)
        self._plugin_combo.setEnabled(True)
        self._plugin_continue_btn.setEnabled(True)
        if not plugins:
            self._set_status(
                self._plugin_status,
                self.tr(
                    "No staged plugins were found. EET will open without a "
                    "target; choose a file from inside the application."),
                warn_text(),
            )
            return
        self._plugin_combo.setCurrentIndex(1)
        self._show_selected_plugin()

    def _select_plugin_completion(self, index: QModelIndex) -> None:
        combo_index = self._plugin_combo.findData(index.data(Qt.UserRole))
        if combo_index >= 0:
            self._plugin_combo.setCurrentIndex(combo_index)

    def _selected_plugin_path(self) -> Path | None:
        index = self._plugin_combo.currentIndex()
        if index < 0:
            index = self._plugin_combo.findText(
                self._plugin_combo.currentText(), Qt.MatchFixedString)
        value = self._plugin_combo.itemData(index) if index >= 0 else None
        return Path(value) if value else None

    def _show_selected_plugin(self, _value=None) -> None:
        path = self._selected_plugin_path()
        if self._plugin_combo.currentIndex() == 0:
            text = self.tr(
                "EET will open without a target plugin. Its game Data path "
                "will still be configured. Open files from a staged mod, not "
                "from the deployed Data folder.")
        else:
            text = self.tr("Plugin path: {0}").format(path) if path else ""
        self._set_status(
            self._plugin_status,
            text,
        )

    def _accept_plugin(self) -> None:
        path = self._selected_plugin_path()
        staging = Path(self._game.get_effective_mod_staging_path())
        if self._plugin_combo.currentIndex() == 0:
            self._plugin_path = None
            self._goto_step(_PG_DEPLOY)
            return
        try:
            path.relative_to(staging) if path is not None else None
            valid = (path is not None and path.is_file()
                     and path.suffix.casefold() in {".esp", ".esm", ".esl"})
        except (OSError, ValueError):
            valid = False
        if not valid:
            self._set_status(
                self._plugin_status,
                self.tr("The selected staged plugin is no longer available."),
                err_text(),
            )
            return
        self._plugin_path = path
        self._goto_step(_PG_DEPLOY)

    def _skip_deploy(self) -> None:
        self._goto_step(_PG_PROTON)

    def _start_eet_deploy(self) -> None:
        self._deploy_btn.setEnabled(False)
        self._deploy_skip_btn.setEnabled(False)

        def on_ok():
            self._goto_step(_PG_PROTON)

        def on_fail():
            self._deploy_btn.setEnabled(True)
            self._deploy_skip_btn.setEnabled(True)

        if not self._run_ctx_deploy(self._deploy_status, on_ok, on_fail):
            on_fail()

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str) -> None:
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_RUN)

    def _set_context_lock(self, held: bool) -> None:
        hook = getattr(self._ctx, "set_tool_lock", None)
        if hook is not None:
            hook("eet", self.tr("ESP-ESM Translator"), held)

    def _start_run(self) -> None:
        exe = self._exe
        plugin = self._plugin_path
        data_dir = self._game.get_mod_data_path()
        if exe is None or not exe.is_file():
            self._set_status(
                self._run_status,
                self.tr("{0} was not found.").format(EXE_NAME), err_text())
            self._done_btn.setEnabled(True)
            return
        if plugin is not None and not plugin.is_file():
            self._set_status(
                self._run_status,
                self.tr("The selected staged plugin is unavailable."), err_text())
            self._done_btn.setEnabled(True)
            return
        if data_dir is None or not Path(data_dir).is_dir():
            self._set_status(
                self._run_status,
                self.tr("The game's Data folder is not configured or available."),
                err_text(),
            )
            self._done_btn.setEnabled(True)
            return

        self._set_status(
            self._run_status, self.tr("Preparing ESP-ESM Translator…"))
        self._set_context_lock(True)
        self._lock_close(True, self.tr(
            "ESP-ESM Translator is preparing or running — close it to continue."))
        self._ran = True
        game = self._game
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.bethesda.registry import maybe_register_for_game
            from Utils.executables.launch import (
                PREFIX_MODE_GAME, link_mygames, link_plugins_txt,
                resolve_tool_prefix, run_tool_logged, run_tool_winetricks_style,
                shutdown_prefix_wineserver,
            )
            from Utils.wine.paths import to_wine_path

            log = lambda message: self._log(
                f"ESP-ESM Translator Wizard: {message}")
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
                prefix = compat_data / "pfx"
                if not (prefix / "drive_c").is_dir():
                    prefix = compat_data

                if prefix_mode != PREFIX_MODE_GAME:
                    try:
                        maybe_register_for_game(
                            prefix_dir=compat_data,
                            proton_script=proton_script,
                            env=env,
                            game=game,
                            log_fn=log,
                        )
                    except Exception as exc:
                        log(f"registry write skipped: {exc}")
                    link_plugins_txt(game, prefix, log)
                    link_mygames(game, prefix, log)

                wine_data = to_wine_path(Path(data_dir), prefix)
                wine_plugin = to_wine_path(plugin, prefix) if plugin else None
                settings = configure_game_data(exe, game, wine_data)
                log(f"configured game Data path {wine_data} in {settings}")
                if plugin is not None:
                    log(f"opening staged plugin {plugin}")

                env.setdefault("PROTON_USE_WINED3D", "1")
                env.setdefault("WINE_D3D_CONFIG", "renderer=gdi")
                safe_emit(
                    self._run_status_sig,
                    self.tr(
                        "ESP-ESM Translator is running. Close it when done."),
                    ok_text(),
                )
                launched = True
                if env.get("AMM_WINETRICKS_STYLE") == "1":
                    return_code = run_tool_winetricks_style(
                        proton_script, exe, compat_data, log_fn=log,
                        extra_args=[wine_plugin] if wine_plugin else None,
                        extra_env={
                            "PROTON_USE_WINED3D": env["PROTON_USE_WINED3D"],
                            "WINE_D3D_CONFIG": env["WINE_D3D_CONFIG"],
                        },
                        cwd=exe.parent, label="ESP-ESM Translator",
                        game=game, owner=self,
                    )
                else:
                    return_code = run_tool_logged(
                        proton_script, exe, env, log_fn=log,
                        extra_args=[wine_plugin] if wine_plugin else None,
                        cwd=exe.parent,
                        label="ESP-ESM Translator", game=game, owner=self,
                    )
                if return_code != 0:
                    detail = self.tr("EET exited with code {0}.").format(
                        return_code)
            except Exception as exc:
                detail = str(exc)
                log(f"launch error: {exc}")
            finally:
                if proton_script is not None and compat_data is not None:
                    shutdown_prefix_wineserver(
                        proton_script, compat_data, log_fn=log)
                safe_emit(self._tool_closed_sig, launched, detail)

        threading.Thread(target=worker, daemon=True, name="eet-run").start()

    def _on_tool_closed(self, launched: bool, detail: str) -> None:
        self._set_context_lock(False)
        if not launched:
            self._lock_close(False)
            self._set_status(
                self._run_status,
                self.tr("Could not launch ESP-ESM Translator: {0}").format(
                    detail),
                err_text(),
            )
            self._done_btn.setEnabled(True)
            return
        self._finish_run(detail)

    def _finish_run(self, detail: str) -> None:
        self._lock_close(False)
        if detail:
            if self._plugin_path is None:
                suffix = " " + self.tr(
                    "Any changes remain at the location chosen in EET.")
            else:
                suffix = " " + self.tr(
                    "Changes remain in the selected staged mod.")
            color = warn_text()
        else:
            suffix = ""
            color = ok_text()
        if detail:
            text = detail + suffix
        else:
            if self._plugin_path is None:
                text = self.tr(
                    "ESP-ESM Translator finished. Any file selected inside "
                    "EET was changed in place.")
            else:
                text = self.tr(
                    "ESP-ESM Translator finished. Changes were saved directly "
                    "to the selected staged mod.")
            text += " " + self.tr("Deploy when you are ready to apply changes.")
        self._set_status(self._run_status, text, color)
        self._done_btn.setEnabled(True)
