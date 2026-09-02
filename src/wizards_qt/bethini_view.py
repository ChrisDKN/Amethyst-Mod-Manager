"""BethINI Pie wizard - Qt port of wizards/bethini.py.

Manual Nexus download → locate in ~/Downloads → extract to
Profiles/<game>/Applications/BethINI Pie/ → Proton step → run.  On an
isolated/shared prefix the Bethesda registry key is seeded and My Games +
plugins.txt linked in so BethINI edits the same INIs the game uses (no-op
when reusing the game's own prefix).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.bethesda.xedit import tool_exe_path

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_NEXUS_URL = "https://www.nexusmods.com/site/mods/631?tab=files"
_EXE_NAME = "Bethini.exe"
_APP_DIR = "BethINI Pie"

_PG_DOWNLOAD, _PG_LOCATE, _PG_EXTRACT, _PG_PROTON, _PG_RUN = range(5)


class BethiniView(WizardViewBase):
    """Install and run BethINI Pie."""

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run BethINI Pie - {0}").format(game.name))
        self._exe = tool_exe_path(game, _EXE_NAME, _APP_DIR)
        self._proton_name = ""
        self._prefix_mode = ""

        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 1: Download BethINI Pie"),
            self.tr("Click the button below to open the BethINI Pie page on Nexus "
            "Mods.\n\nDownload the archive manually (do NOT use the Mod "
            "Manager download button), then click Next."),
            _NEXUS_URL,
            lambda: self._goto_step(_PG_LOCATE)))
        self._stack.addWidget(self._build_locate_page(
            self.tr("Step 2: Locate the Archive")))
        self._stack.addWidget(self._build_extract_page(
            self.tr("Step 3: Extract BethINI Pie")))
        self._stack.addWidget(self._build_proton_holder())
        self._stack.addWidget(self._build_run_page(self.tr("Step 5: Run BethINI Pie")))

        if self._exe is not None:
            self._goto_step(_PG_PROTON)
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_LOCATE:
            self._enter_locate(
                ["bethini"], self.tr("Select the BethINI Pie archive"),
                self.tr("BethINI archive not found in Downloads.\n"
                "Make sure you downloaded it, then press Try Again,\n"
                "or use Browse to select it manually."),
                lambda _p: self._goto_step(_PG_EXTRACT))
        elif idx == _PG_EXTRACT:
            self._extract_to_applications(_APP_DIR, _EXE_NAME, "BethINI")
        elif idx == _PG_PROTON:
            self._enter_proton(
                self._exe, _EXE_NAME, "BethINI Pie", self._on_proton_chosen,
                title=self.tr("Step 4: Choose Proton Version"),
                missing_text=self.tr("'{0}' was not found.\n"
                             "Please restart the wizard and install BethINI "
                             "Pie first.").format(_EXE_NAME))
        elif idx == _PG_RUN:
            self._start_run()

    def _on_extract_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_PROTON)

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_RUN)

    # ---- run ----------------------------------------------------------------------
    def _start_run(self):
        exe, game = self._exe, self._game
        if exe is None:
            self._set_status(self._run_status,
                             self.tr("'{0}' was not found.").format(_EXE_NAME), RED)
            return
        self._set_status(self._run_status,
                         self.tr("Preparing BethINI Pie's Wine prefix…"))
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.executables.launch import (
                PREFIX_MODE_GAME, link_mygames, link_plugins_txt,
                resolve_tool_prefix, run_tool_logged, shutdown_prefix_wineserver,
            )
            _wlog = lambda m: self._log(f"BethINI Wizard: {m}")
            proton_script = compat_data = None
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    if prefix_mode == PREFIX_MODE_GAME:
                        safe_emit(self._run_status_sig,
                            self.tr("Could not resolve the Proton version for the "
                            "game's own prefix - launch the game once, or pick a "
                            "different prefix option."), RED)
                    else:
                        safe_emit(self._run_status_sig,
                            self.tr("Could not find Proton '{0}' - check that it "
                            "is installed in Steam, Heroic or ProtonPlus.").format(
                                proton_name), RED)
                    return
                proton_script, compat_data, env = result

                # On an isolated/shared prefix, seed the Bethesda registry
                # key and link My Games + plugins.txt so BethINI can locate
                # the game and edit the same INIs the game uses.  No-op when
                # running in the game's own prefix.
                if prefix_mode != PREFIX_MODE_GAME:
                    try:
                        from Utils.bethesda.registry import maybe_register_for_game
                        maybe_register_for_game(
                            prefix_dir=compat_data,
                            proton_script=proton_script,
                            env=env,
                            game=game,
                            log_fn=_wlog,
                        )
                    except Exception as exc:
                        _wlog(f"registry write skipped: {exc}")
                    link_mygames(game, compat_data / "pfx", _wlog)
                    link_plugins_txt(game, compat_data / "pfx", _wlog)

                _wlog(f"launching {exe} via Proton")
                safe_emit(self._run_status_sig,
                          self.tr("BethINI Pie is running.\nConfigure your INI "
                          "settings, then close it and click Done."), GREEN)
                safe_emit(self._run_started_sig)
                run_tool_logged(proton_script, exe, env, log_fn=_wlog,
                                label="BethINI Pie", owner=self)
                _wlog("BethINI Pie closed.")
                safe_emit(self._run_status_sig, self.tr("BethINI Pie finished."), GREEN)
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig, self.tr("Launch error: {0}").format(exc), RED)
                self._log(f"BethINI Wizard: launch error: {exc}")
            finally:
                # In finally: a tool that crashed is exactly when Proton
                # sidecars are most likely to be left holding the prefix.
                if proton_script is not None and compat_data is not None:
                    shutdown_prefix_wineserver(proton_script, compat_data,
                                               log_fn=_wlog)

        threading.Thread(target=worker, daemon=True, name="bethini-run").start()

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)
