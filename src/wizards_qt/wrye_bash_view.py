"""Qt view for shared Wrye Bash acquisition, installation and execution."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, Signal

from gui_qt.runtime.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.bethesda.xedit import tool_exe_path
from Utils.bethesda.wrye_bash import APP_DIR as _APP_DIR, EXE_NAME as _EXE_NAME

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_DOWNLOAD, _PG_DEPLOY, _PG_PROTON, _PG_RUN = range(4)


class WryeBashView(WizardViewBase):
    """Download, install and run Wrye Bash."""

    _dl_status_sig = Signal(str, str)
    _dl_done_sig = Signal(bool)
    _bash_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run Wrye Bash - {0}").format(game.name))
        self._exe = tool_exe_path(game, _EXE_NAME, _APP_DIR)
        self._proton_name = ""
        self._prefix_mode = ""
        self._download_control = None

        self._dl_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._dl_status, t, c)))
        self._dl_done_sig.connect(self._guard(self._on_dl_done))
        self._bash_done_sig.connect(self._guard(self._on_bash_done))

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
            "A new Bashed Patch is saved to Overwrite after Wrye Bash closes."),
            lambda: self._goto_step(_PG_PROTON)))
        # page 2: proton
        self._stack.addWidget(self._build_proton_holder())
        # page 3: run
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 4: Run Wrye Bash")))

        if self._exe is not None:
            self._goto_step(_PG_DEPLOY)
            self._offer_tool_upgrade(_PG_DEPLOY,
                                     lambda: self._goto_step(_PG_DOWNLOAD))
        else:
            self._goto_step(_PG_DOWNLOAD)

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_DOWNLOAD:
            self._download()
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

    def _selection(self):
        from amethyst_backend.tools import catalogue

        game = self._game
        profile = getattr(game, "_active_profile_dir", None)
        if profile is None:
            profile = game.get_profile_root() / "profiles" / (getattr(self._ctx, "profile_name", None) or "default")
        listing = catalogue(game, profile)
        tool_id = getattr(self._ctx, "wizard_tool_id", None)
        row = next(row for row in listing["tools"] if "wrye_bash" in row and (not tool_id or row["id"] == tool_id))
        return profile, row["id"], listing["revision"], row["wrye_bash"]

    def _download(self):
        from Utils.downloads.control import DownloadControl

        self._download_control = control = DownloadControl()

        def worker():
            from amethyst_backend.application_install import inspect_runner, install_runner
            from amethyst_backend.tool_acquisition import acquire_runner

            try:
                profile, tool_id, revision, choice = self._selection()
                safe_emit(self._dl_status_sig, QCoreApplication.translate(
                    "WizardViewBase", "Fetching latest release from GitHub…"), "")

                def progress(done, total, _stage):
                    text = QCoreApplication.translate("WizardViewBase", "Downloading {0}…").format(_APP_DIR)
                    if total:
                        text += f" {min(100, done * 100 // total)}%"
                    safe_emit(self._dl_status_sig, text, "")

                acquired = acquire_runner(None, self._game, tool_id, 0, choice, control)(self._log, progress)
                archive = acquired["archive"]["path"]
                safe_emit(self._dl_status_sig, QCoreApplication.translate("WizardViewBase", "Extracting…"), "")
                plan = inspect_runner(self._game, profile, tool_id, revision, choice, archive,
                                      self._auto_fetch_cancel, family="wrye_bash")(self._log, lambda *_a: None)
                installed = install_runner(self._game, profile, tool_id, revision, choice, archive,
                                           plan["revision"], False, self._auto_fetch_cancel,
                                           family="wrye_bash")(self._log, lambda *_a: None)
                from pathlib import Path
                self._exe = Path(installed["destination"]["exe_path"])
                safe_emit(self._dl_status_sig, QCoreApplication.translate(
                    "WizardViewBase", "Downloaded and extracted {0}.").format(_APP_DIR), GREEN)
                safe_emit(self._dl_done_sig, True)
            except Exception as exc:
                self._log(f"Wrye Bash Wizard: download error: {exc}")
                safe_emit(self._dl_status_sig, QCoreApplication.translate("WizardViewBase", "Error: {0}").format(exc), RED)
                safe_emit(self._dl_done_sig, False)

        threading.Thread(target=worker, daemon=True, name="wryebash-download").start()

    def _finish(self):
        if self._tool_running:
            return
        if self._download_control is not None:
            self._download_control.cancel()
        super()._finish()

    def _start_run(self):
        if self._exe is None:
            self._set_status(self._run_status, self.tr("'{0}' was not found.").format(_EXE_NAME), RED)
            return
        self._set_status(self._run_status, self.tr("Launching Wrye Bash…"))
        self._lock_close(True, self.tr("Wrye Bash is preparing or running."))
        request = {"proton_name": self._proton_name, "prefix_mode": self._prefix_mode, "deploy": False}

        def worker():
            from amethyst_backend.wrye_bash_run import plan_runner, run_runner

            completed = False
            log = lambda message: self._log(f"Wrye Bash Wizard: {message}")
            try:
                profile, tool_id, revision, choice = self._selection()
                plan = plan_runner(self._game, profile, tool_id, revision, choice, request,
                                   self._auto_fetch_cancel)(log, lambda *_a: None)

                def started():
                    safe_emit(self._run_status_sig, self.tr("Wrye Bash is running.\nClose it when you are "
                              "done, then click Done."), GREEN)
                    safe_emit(self._run_started_sig)

                run_runner(self._game, profile, tool_id, revision, choice, request, plan["revision"],
                           self._auto_fetch_cancel, on_started=started)(log, lambda *_a: None)
                safe_emit(self._run_status_sig, self.tr("Wrye Bash finished."), GREEN)
                completed = True
            except Exception as exc:
                safe_emit(self._run_status_sig, self.tr("Launch error: {0}").format(exc), RED)
                log(f"launch error: {exc}")
            finally:
                safe_emit(self._bash_done_sig, completed)

        threading.Thread(target=worker, daemon=True, name="wryebash-run").start()

    def _on_run_started(self):
        self._ran = True

    def _on_bash_done(self, completed: bool):
        self._lock_close(False)
        self._done_btn.setEnabled(True)
        if completed:
            self._finish()
