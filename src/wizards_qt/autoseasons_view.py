"""Install and run AutoSeasons for Skyrim Special Edition."""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal

from gui_qt.safe_emit import safe_emit
from Utils.bethesda.autoseasons import (
    EXE_NAME, GITHUB_API_URL, OUTPUT_NAME, find_autoseasons_exe,
)
from Utils.mods.modlist import read_modlist
from wizards_qt._view_base import GREEN, RED, WizardViewBase


_INSTALL, _DEPLOY, _PROTON, _RUN = range(4)


class AutoSeasonsView(WizardViewBase):
    _install_status_sig = Signal(str, str)
    _install_done_sig = Signal(bool)
    _process_done_sig = Signal()

    def __init__(self, game, log_fn=None, on_close=None, ctx=None, **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run AutoSeasons - {0}").format(game.name))
        self._exe = find_autoseasons_exe(game)
        self._proton_name = ""
        self._prefix_mode = ""
        self._install_status_sig.connect(self._guard(
            lambda text, color: self._set_status(self._install_status, text, color)))
        self._install_done_sig.connect(self._guard(self._on_install_done))
        self._process_done_sig.connect(self._guard(self._on_process_done))

        page, lay = self._step_page(self.tr("Step 1: Install AutoSeasons"))
        self._make_note(lay, self.tr(
            "Install AutoSeasons as a regular mod in this profile. The published "
            "release includes its .NET runtime and shader files."))
        self._install_status = self._make_status(lay)
        lay.addStretch(1)
        self._install_btn = self._accent_btn(self.tr("Download and Install"))
        self._install_btn.clicked.connect(self._start_install)
        lay.addWidget(self._install_btn, 0, Qt.AlignHCenter)
        self._check_btn = self._accent_btn(self.tr("Check Installed Mods"))
        self._check_btn.clicked.connect(self._check_installed)
        lay.addWidget(self._check_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)

        page, lay = self._step_page(self.tr("Step 2: Deploy Modlist"))
        self._make_note(lay, self.tr(
            "Deploy the active profile so AutoSeasons sees its merged Data folder. "
            "This also prepares VFS profiles. Disable '{0}' before rerunning; "
            "AutoSeasons must not scan its previous output.").format(OUTPUT_NAME))
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)
        self._deploy_btn = self._accent_btn(self.tr("Deploy"))
        self._deploy_btn.clicked.connect(self._start_deploy)
        lay.addWidget(self._deploy_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)

        self._stack.addWidget(self._build_proton_holder())

        page, lay = self._step_page(self.tr("Step 4: Run AutoSeasons"))
        self._make_note(lay, self.tr(
            "AutoSeasons will use the active profile's plugin order and merged "
            "Data folder. Choose Start Patching in its window. The plugin and "
            "Seasons INIs will be written to '{0}'. After it finishes, enable "
            "that mod and AutoSeasons.esp. Run PGPatcher afterward if your "
            "modlist uses PBR or Complex Material.").format(OUTPUT_NAME))
        self._run_status = self._make_status(lay)
        lay.addStretch(1)
        self._run_btn = self._accent_btn(self.tr("Launch AutoSeasons"))
        self._run_btn.clicked.connect(self._start_run)
        lay.addWidget(self._run_btn, 0, Qt.AlignHCenter)
        self._done_btn = self._green_btn()
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)

        self._goto_step(_DEPLOY if self._exe else _INSTALL)

    def _goto_step(self, step: int):
        self._stack.setCurrentIndex(step)
        if step == _PROTON:
            self._enter_proton(
                self._exe, EXE_NAME, "AutoSeasons", self._on_proton_chosen,
                title=self.tr("Step 3: Choose Proton Version"))

    def _check_installed(self):
        self._exe = find_autoseasons_exe(self._game)
        if self._exe:
            self._goto_step(_DEPLOY)
        else:
            self._set_status(self._install_status,
                             self.tr("AutoSeasons.exe was not found in installed mods."), RED)

    def _start_install(self):
        self._install_btn.setEnabled(False)
        game = self._game
        profile = getattr(self._ctx, "profile_name", None) or "default"

        def worker():
            from Utils.bethesda.autoseasons import install_autoseasons
            from Utils.ca_bundle import download_file
            from Utils.wizards.archives import fetch_latest_github_asset

            log = lambda msg: self._log(f"AutoSeasons Wizard: {msg}")
            try:
                safe_emit(self._install_status_sig,
                          self.tr("Fetching latest release from GitHub…"), "")
                tag, url = fetch_latest_github_asset(GITHUB_API_URL, ["autoseasons"])
                with tempfile.TemporaryDirectory(prefix="amm-autoseasons-") as temp:
                    archive = Path(temp) / Path(url).name
                    safe_emit(self._install_status_sig,
                              self.tr("Downloading {0}…").format(tag), "")
                    download_file(url, archive)
                    safe_emit(self._install_status_sig,
                              self.tr("Installing AutoSeasons as a mod…"), "")
                    self._exe = install_autoseasons(game, archive, profile, log_fn=log)
                safe_emit(self._install_status_sig,
                          self.tr("AutoSeasons installed as a mod."), GREEN)
                safe_emit(self._install_done_sig, True)
            except Exception as exc:
                log(f"install error: {exc}")
                safe_emit(self._install_status_sig,
                          self.tr("Install error: {0}").format(exc), RED)
                safe_emit(self._install_done_sig, False)

        threading.Thread(target=worker, daemon=True, name="autoseasons-install").start()

    def _on_install_done(self, ok: bool):
        if ok:
            self._ran = True
            self._goto_step(_DEPLOY)
        else:
            self._install_btn.setEnabled(True)

    def _start_deploy(self):
        profile = getattr(self._ctx, "profile_name", None) or "default"
        modlist = self._game.get_profile_root() / "profiles" / profile / "modlist.txt"
        if any(entry.name.casefold() == OUTPUT_NAME.casefold() and entry.enabled
               for entry in read_modlist(modlist)):
            self._set_status(self._deploy_status,
                             self.tr("Disable {0} before deploying.").format(OUTPUT_NAME), RED)
            return
        self._deploy_btn.setEnabled(False)
        self._lock_close(True, self.tr("Deploying the active profile."))

        def done():
            self._lock_close(False)
            self._goto_step(_PROTON)

        def failed():
            self._lock_close(False)
            self._deploy_btn.setEnabled(True)

        if not self._run_ctx_deploy(self._deploy_status, done, failed):
            failed()

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_RUN)

    def _set_context_lock(self, held: bool):
        hook = getattr(self._ctx, "set_tool_lock", None)
        if hook is not None:
            hook("autoseasons", "AutoSeasons", held)

    def _start_run(self):
        exe = self._exe
        if exe is None or not exe.is_file():
            self._set_status(self._run_status, self.tr("AutoSeasons.exe was not found."), RED)
            return
        self._run_btn.setEnabled(False)
        self._set_context_lock(True)
        self._lock_close(True, self.tr("AutoSeasons is running."))
        game = self._game
        profile = getattr(self._ctx, "profile_name", None) or "default"
        proton_name, prefix_mode = self._proton_name, self._prefix_mode
        from themes import get_ctk_appearance
        from Utils.ui.config import get_appearance_mode
        ui_theme = get_ctk_appearance(get_appearance_mode())

        def worker():
            from Utils.bethesda.autoseasons import prepare_autoseasons
            from Utils.executables.launch import (
                resolve_tool_prefix, run_tool_logged, shutdown_prefix_wineserver,
            )

            log = lambda msg: self._log(f"AutoSeasons Wizard: {msg}")
            proton_script = compat_data = None
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=log)
                if result is None:
                    raise RuntimeError(self.tr("Could not resolve the selected Proton prefix."))
                proton_script, compat_data, env = result
                prepare_autoseasons(game, exe, compat_data / "pfx", profile,
                                    log_fn=log, ui_theme=ui_theme)
                self._ran = True
                env.pop("wx_msw_dark_mode", None)
                if ui_theme == "dark":
                    env["WX_MSW_DARK_MODE"] = "2"
                else:
                    env.pop("WX_MSW_DARK_MODE", None)
                safe_emit(self._run_status_sig,
                          self.tr("AutoSeasons is running. Generate output in its window."), GREEN)
                code = run_tool_logged(proton_script, exe, env, log_fn=log,
                                       label="AutoSeasons", game=game, owner=self)
                if code:
                    raise RuntimeError(self.tr("AutoSeasons exited with code {0}.").format(code))
                safe_emit(self._run_status_sig,
                          self.tr("AutoSeasons closed. Click Done to refresh the mod list."), GREEN)
            except Exception as exc:
                safe_emit(self._run_status_sig,
                          self.tr("AutoSeasons error: {0}").format(exc), RED)
                log(f"error: {exc}")
            finally:
                if (proton_script is not None and compat_data is not None
                        and prefix_mode == "isolated"):
                    shutdown_prefix_wineserver(proton_script, compat_data, log_fn=log)
                safe_emit(self._process_done_sig)

        threading.Thread(target=worker, daemon=True, name="autoseasons-run").start()

    def _on_process_done(self):
        self._set_context_lock(False)
        self._lock_close(False)
        self._run_btn.setEnabled(True)
        self._done_btn.setEnabled(True)
