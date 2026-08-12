"""Wrye Bash wizard - Qt port of wizards/wrye_bash.py (registered by every
Bethesda game).

Auto-downloads the latest Standalone Executable release from GitHub,
extracts to Profiles/<game>/Applications/Wrye Bash/, deploys, then runs via
Proton in a tool prefix with the game's Installed Path seeded into the
registry, plugins.txt + My Games linked in, and the game folder symlinked to
C:\\wb_games\\ (WB derives its .wbtemp dir from the -o drive letter; Z:\\ is
not writable).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.xedit_tools import tool_exe_path

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_GITHUB_API = "https://api.github.com/repos/wrye-bash/wrye-bash/releases/latest"
_EXE_NAME = "Wrye Bash.exe"
_APP_DIR = "Wrye Bash"

_PG_DOWNLOAD, _PG_DEPLOY, _PG_PROTON, _PG_RUN = range(4)


class WryeBashView(WizardViewBase):
    """Download, install and run Wrye Bash."""

    _dl_status_sig = Signal(str, str)
    _dl_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run Wrye Bash - {0}").format(game.name))
        self._exe = tool_exe_path(game, _EXE_NAME, _APP_DIR)
        self._proton_name = ""
        self._prefix_mode = ""

        self._dl_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._dl_status, t, c)))
        self._dl_done_sig.connect(self._guard(self._on_dl_done))

        # page 0: auto-download
        page, lay = self._step_page(self.tr("Step 1: Download Wrye Bash"))
        self._dl_status = self._make_status(lay)
        lay.addStretch(1)
        self._stack.addWidget(page)
        # page 1: deploy
        self._stack.addWidget(self._build_deploy_page(
            self.tr("Step 2: Deploy Modlist"),
            self.tr("Deploy the modlist so Wrye Bash sees your mods and the\n"
            "Bashed Patch it creates lands in the modded Data folder.\n"
            "On restore, the new plugin is moved to Overwrite."),
            lambda: self._goto_step(_PG_PROTON)))
        # page 2: proton
        self._stack.addWidget(self._build_proton_holder())
        # page 3: run
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 4: Run Wrye Bash")))

        if self._exe is not None:
            self._goto_step(_PG_DEPLOY)
        else:
            self._goto_step(_PG_DOWNLOAD)

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_DOWNLOAD:
            self._github_install_worker(
                _GITHUB_API, ["standalone", "executable"], _APP_DIR, _EXE_NAME,
                "Wrye Bash", self._dl_status_sig, self._dl_done_sig)
        elif idx == _PG_PROTON:
            self._enter_proton(
                self._exe, _EXE_NAME, "Wrye Bash", self._on_proton_chosen,
                title=self.tr("Step 3: Choose Proton Version"),
                missing_text=self.tr("'{0}' was not found.\n"
                             "Please restart the wizard to reinstall Wrye "
                             "Bash.").format(_EXE_NAME))
        elif idx == _PG_RUN:
            self._start_run()

    def _on_dl_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_DEPLOY)

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
        self._set_status(self._run_status, self.tr("Launching Wrye Bash…"))
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.exe_launch import (
                resolve_tool_prefix, run_tool_logged, shutdown_prefix_wineserver,
            )
            from Utils.xedit_tools import prepare_xedit_prefix
            _wlog = lambda m: self._log(f"Wrye Bash Wizard: {m}")
            proton_script = compat_data = None
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    safe_emit(self._run_status_sig,
                              self.tr("Could not find Proton '{0}' - "
                              "check that it is installed in Steam.").format(
                                  proton_name), RED)
                    return
                proton_script, compat_data, env = result
                pfx = compat_data / "pfx"

                # Registry seed + plugins.txt / My Games links (WB reads all
                # three; no viewsettings/WinXP for WB).
                prepare_xedit_prefix(game, compat_data, proton_script, env,
                                     log_fn=_wlog)

                # WB derives its .wbtemp dir from the drive letter of the -o
                # path.  Z:\ (Wine's Linux root mapping) is not writable, so
                # symlink the game folder into drive_c/wb_games/ and pass a
                # C:\ path instead.
                game_path = game.get_game_path()
                game_arg = []
                if game_path:
                    real_game = game_path.resolve()
                    c_games = pfx / "drive_c" / "wb_games"
                    c_games.mkdir(parents=True, exist_ok=True)
                    link = c_games / real_game.name
                    if not link.exists() and not link.is_symlink():
                        link.symlink_to(real_game)
                    game_arg = ["-o", f"C:\\wb_games\\{real_game.name}"]

                _wlog(f"launching {exe} via Proton"
                      + (f" with -o C:\\wb_games\\{game_path.resolve().name}"
                         if game_path else ""))
                safe_emit(self._run_status_sig,
                          self.tr("Wrye Bash is running.\nClose it when you are "
                          "done, then click Done."), GREEN)
                safe_emit(self._run_started_sig)
                run_tool_logged(proton_script, exe, env, log_fn=_wlog,
                                extra_args=game_arg, label="Wrye Bash")
                _wlog("Wrye Bash closed.")
                safe_emit(self._run_status_sig,
                          self.tr("Wrye Bash finished."), GREEN)
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig,
                          self.tr("Launch error: {0}").format(exc), RED)
                self._log(f"Wrye Bash Wizard: launch error: {exc}")
            finally:
                # In finally: a tool that crashed is exactly when Proton
                # sidecars are most likely to be left holding the prefix.
                if proton_script is not None and compat_data is not None:
                    shutdown_prefix_wineserver(proton_script, compat_data,
                                               log_fn=_wlog)

        threading.Thread(target=worker, daemon=True, name="wryebash-run").start()

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)
