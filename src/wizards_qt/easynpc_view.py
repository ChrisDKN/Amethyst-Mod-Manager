from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal

from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import err_text, ok_text
from Utils.wizards.easynpc import (
    APP_DIR, EXE_NAME, NEXUS_MOD_ID, NEXUS_URL, find_archive, find_exe,
    latest_main_file, launch_args,
)
from wizards_qt._view_base import WizardViewBase


(_PG_DOWNLOAD, _PG_LOCATE, _PG_EXTRACT, _PG_DEPLOY,
 _PG_PROTON, _PG_DEPS, _PG_RUN) = range(7)


class EasyNpcView(WizardViewBase):
    _deps_status_sig = Signal(str, str)
    _deps_done_sig = Signal(bool)
    _tool_closed_sig = Signal(bool, str)

    def __init__(self, game, log_fn=None, on_close=None, ctx=None, **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("EasyNPC Next - {0}").format(game.name))
        self._exe = find_exe(game)
        self._prefix_env = None
        self._deployed_profile = None
        self._deps_running = False
        self._deps_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._deps_status, t, c)))
        self._deps_done_sig.connect(self._guard(self._on_deps_done))
        self._tool_closed_sig.connect(self._guard(self._on_tool_closed))

        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 1: Download EasyNPC Next"),
            self.tr("Premium accounts download the newest Main file automatically. "
                    "With a free account, open the Nexus files page and download "
                    "the EasyNPC Next Main file. This wizard watches your download "
                    "locations for the archive."),
            NEXUS_URL, lambda: self._goto_step(_PG_LOCATE),
        ))
        self._stack.addWidget(self._build_locate_page(
            self.tr("Step 2: Locate EasyNPC Next")))
        self._stack.addWidget(self._build_extract_page(
            self.tr("Step 3: Install EasyNPC Next")))
        self._stack.addWidget(self._build_deploy_page())
        self._stack.addWidget(self._build_proton_holder())
        self._stack.addWidget(self._build_deps_page())
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 7: Run EasyNPC Next")))

        if self._exe is None:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)
            self._start_auto_fetch()
        else:
            self._stack.setCurrentIndex(_PG_DEPLOY)

    def _start_auto_fetch(self):
        if self._auto_fetch_started:
            return
        self._auto_fetch_started = True
        self._dl_next_btn.setEnabled(False)
        api_fn = getattr(self._ctx, "nexus_api", None)
        try:
            api = api_fn() if api_fn is not None else None
        except Exception:
            api = None
        self._auto_fetch_pages = (_PG_DOWNLOAD, _PG_LOCATE)
        self._auto_fetch_on_archive = lambda _path: self._goto_step(_PG_EXTRACT)
        armed_at = time.time() - 5

        def worker():
            from Utils.downloads.mpi import start_auto_fetch
            file_id = 0
            premium = False
            if api is not None:
                try:
                    premium = bool(api.validate().is_premium)
                    if premium:
                        latest = latest_main_file(api)
                        file_id = latest.file_id
                        self._log(
                            f"EasyNPC Next Wizard: newest Main file {file_id} "
                            f"(version {latest.version})")
                except Exception as exc:
                    premium = False
                    self._log(f"EasyNPC Next Wizard: Nexus lookup failed: {exc}")
            if self._auto_fetch_cancel.is_set():
                return
            start_auto_fetch(
                api=api if premium else None,
                game_domain="skyrimspecialedition",
                mod_id=NEXUS_MOD_ID, file_id=file_id,
                find_archive_fn=lambda: find_archive(since=armed_at),
                on_archive=lambda p: safe_emit(self._auto_dl_archive_sig, p),
                cancel=self._auto_fetch_cancel, label="EasyNPC Next",
                on_download_started=lambda: self._on_fetch_started(),
                on_progress=lambda done, total: self._on_fetch_progress(done, total),
                on_waiting=lambda: self._on_fetch_waiting(),
                log_fn=lambda m: self._log(f"EasyNPC Next Wizard: {m}"),
            )

        threading.Thread(target=worker, daemon=True,
                         name="easynpc-main-file").start()

    def _on_fetch_started(self):
        safe_emit(self._auto_dl_gate_sig, False)
        safe_emit(self._auto_dl_status_sig,
                  self.tr("Premium account: downloading the newest Main file…"), "")

    def _on_fetch_progress(self, done: int, total: int):
        if total:
            safe_emit(self._auto_dl_status_sig,
                      self.tr("Downloading EasyNPC Next… {0}%").format(
                          min(100, int(done * 100 / total))), "")

    def _on_fetch_waiting(self):
        safe_emit(self._auto_dl_gate_sig, True)
        safe_emit(self._auto_dl_status_sig,
                  self.tr("Waiting for the manual download to finish…"), "")

    def _locate_rescan(self):
        found = find_archive()
        if found is None:
            self._archive_path = None
            self._set_status(self._locate_status, self.tr(
                "No EasyNPC Next archive found. Download the Main file, "
                "press Try Again, or browse to it."), err_text())
            return
        self._archive_found(found, self.tr("Found: {0}").format(found.name))

    def _build_deploy_page(self):
        page, lay = self._step_page(self.tr("Step 4: Deploy Profile"))
        self._make_note(lay, self.tr(
            "Deploy the active profile so EasyNPC can read its game Data. "
            "The wizard also gives EasyNPC the current staged mods and plugin order. "
            "After building a merge, enable its output mod in Amethyst and "
            "deploy when ready."))
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)
        self._deploy_btn = self._accent_btn(self.tr("Deploy and Continue"))
        self._deploy_btn.clicked.connect(self._start_deploy)
        lay.addWidget(self._deploy_btn, 0, Qt.AlignHCenter)
        update_btn = self._accent_btn(self.tr("Get Latest Main File"))
        update_btn.clicked.connect(self._update_tool)
        lay.addWidget(update_btn, 0, Qt.AlignHCenter)
        return page

    def _update_tool(self):
        self._stack.setCurrentIndex(_PG_DOWNLOAD)
        self._start_auto_fetch()

    def _build_deps_page(self):
        page, lay = self._step_page(self.tr("Step 6: Install Dependencies"))
        self._make_note(lay, self.tr(
            "EasyNPC Next targets .NET 8 Desktop. The wizard checks the x64 "
            "Visual C++ runtime and installs missing prerequisites into "
            "the selected Wine prefix."))
        self._deps_status = self._make_status(lay)
        lay.addStretch(1)
        self._deps_retry_btn = self._accent_btn(self.tr("Try Again"))
        self._deps_retry_btn.setEnabled(False)
        self._deps_retry_btn.clicked.connect(self._start_deps)
        lay.addWidget(self._deps_retry_btn, 0, Qt.AlignHCenter)
        return page

    def _goto_step(self, index: int):
        self._stack.setCurrentIndex(index)
        if index == _PG_LOCATE:
            self._enter_locate(
                ["easynpc"], self.tr("Select the EasyNPC Next Main archive"),
                "", lambda _path: self._goto_step(_PG_EXTRACT))
        elif index == _PG_EXTRACT:
            self._extract_to_applications(APP_DIR, EXE_NAME, "EasyNPC Next")
        elif index == _PG_PROTON:
            self._enter_proton(
                self._exe, EXE_NAME, "EasyNPC Next", self._on_proton_chosen,
                title=self.tr("Step 5: Choose Proton Version"))
        elif index == _PG_DEPS:
            self._start_deps()
        elif index == _PG_RUN:
            self._start_run()

    def _on_extract_done(self, ok: bool):
        if ok:
            self._exe = find_exe(self._game)
            self._goto_step(_PG_DEPLOY)

    def _start_deploy(self):
        self._deploy_btn.setEnabled(False)
        completed = [False]

        def done():
            completed[0] = True
            profile_fn = getattr(self._ctx, "current_profile", None)
            self._deployed_profile = (
                profile_fn() if profile_fn is not None else None
            ) or getattr(self._ctx, "profile_name", None) or "default"
            self._goto_step(_PG_PROTON)

        def failed():
            completed[0] = True
            self._deploy_btn.setEnabled(True)

        if not self._run_ctx_deploy(self._deploy_status, done, failed) and not completed[0]:
            failed()

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_DEPS)

    def _start_deps(self):
        if self._deps_running:
            return
        self._deps_running = True
        self._deps_retry_btn.setEnabled(False)
        exe, game = self._exe, self._game
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.executables.launch import resolve_tool_prefix
            from Utils.wizards.easynpc import set_wine_compat_version
            from Utils.wine.proton import install_dotnet_runtime
            from Utils.wine.protontricks import (
                VCREDIST_DEP_KEY, dotnet_dep_key, install_vcredist,
                is_dep_installed,
            )

            log = lambda m: self._log(f"EasyNPC Next Wizard: {m}")
            try:
                safe_emit(self._deps_status_sig,
                          self.tr("Preparing Wine prefix…"), "")
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=log)
                if result is None:
                    raise RuntimeError(self.tr("Could not prepare the selected Proton prefix."))
                self._prefix_env = result
                proton_script, compat_data, env = result
                prefix = Path(compat_data) / "pfx"
                if not prefix.is_dir():
                    prefix = Path(compat_data)

                if not is_dep_installed(prefix, VCREDIST_DEP_KEY):
                    safe_emit(self._deps_status_sig,
                              self.tr("Installing Visual C++ x64…"), "")
                    if not install_vcredist(
                            proton_script, env, log_fn=log, prefix_path=prefix):
                        raise RuntimeError(self.tr("Visual C++ x64 install failed. See log."))
                if not is_dep_installed(prefix, dotnet_dep_key("8")):
                    safe_emit(self._deps_status_sig,
                              self.tr("Installing .NET 8 Desktop Runtime…"), "")
                    if not install_dotnet_runtime(
                            "8", proton_script, env, prefix, log_fn=log,
                            status_fn=lambda m: safe_emit(
                                self._deps_status_sig, m, "")):
                        raise RuntimeError(self.tr(".NET 8 install failed. See log."))
                set_wine_compat_version(proton_script, env, log_fn=log)
                safe_emit(self._deps_status_sig,
                          self.tr("Dependencies ready."), ok_text())
                safe_emit(self._deps_done_sig, True)
            except Exception as exc:
                log(f"dependency setup failed: {exc}")
                safe_emit(self._deps_status_sig, str(exc), err_text())
                safe_emit(self._deps_done_sig, False)

        threading.Thread(target=worker, daemon=True,
                         name="easynpc-deps").start()

    def _on_deps_done(self, ok: bool):
        self._deps_running = False
        if ok:
            self._goto_step(_PG_RUN)
        else:
            self._deps_retry_btn.setEnabled(True)

    def _set_context_lock(self, held: bool):
        hook = getattr(self._ctx, "set_tool_lock", None)
        if hook is not None:
            hook("easynpc_next", self.tr("EasyNPC Next"), held)

    def _start_run(self):
        exe = self._exe
        if exe is None or not exe.is_file() or self._prefix_env is None:
            self._set_status(self._run_status,
                             self.tr("EasyNPC Next is not ready to launch."),
                             err_text())
            self._done_btn.setEnabled(True)
            return
        profile_fn = getattr(self._ctx, "current_profile", None)
        profile = (profile_fn() if profile_fn is not None else None)
        profile = profile or getattr(self._ctx, "profile_name", None) or "default"
        if profile != self._deployed_profile:
            self._set_status(self._run_status, self.tr(
                "The active profile changed after deployment. Reopen the wizard "
                "and deploy the current profile."), err_text())
            self._done_btn.setEnabled(True)
            return
        self._set_status(self._run_status, self.tr("Preparing EasyNPC Next…"))
        self._set_context_lock(True)
        self._lock_close(True, self.tr("EasyNPC Next is running."))
        proton_script, compat_data, env = self._prefix_env
        game = self._game

        def worker():
            from Utils.bethesda.registry import maybe_register_for_game
            from Utils.executables.launch import (
                link_mygames, link_plugins_txt, run_tool_logged,
                shutdown_prefix_wineserver,
            )

            log = lambda m: self._log(f"EasyNPC Next Wizard: {m}")
            launched = False
            detail = ""
            try:
                run_env = dict(env)
                prefix = Path(compat_data) / "pfx"
                if not prefix.is_dir():
                    prefix = Path(compat_data)
                try:
                    maybe_register_for_game(
                        prefix_dir=compat_data, proton_script=proton_script,
                        env=run_env, game=game, log_fn=log)
                except Exception as exc:
                    log(f"registry setup skipped: {exc}")
                link_plugins_txt(game, prefix, log)
                link_mygames(game, prefix, log)
                args = launch_args(game, exe, prefix, profile, log_fn=log)
                run_env.pop("DOTNET_ROOT", None)
                run_env.pop("DOTNET_BUNDLE_EXTRACT_BASE_DIR", None)
                safe_emit(self._run_status_sig,
                          self.tr("EasyNPC Next is running. Close it when done."),
                          ok_text())
                launched = True
                code = run_tool_logged(
                    proton_script, exe, run_env, log_fn=log, extra_args=args,
                    cwd=exe.parent, label="EasyNPC Next", game=game, owner=self)
                if code:
                    detail = self.tr("EasyNPC Next exited with code {0}.").format(code)
            except Exception as exc:
                detail = str(exc)
                log(f"launch failed: {exc}")
            finally:
                try:
                    shutdown_prefix_wineserver(proton_script, compat_data, log_fn=log)
                finally:
                    safe_emit(self._tool_closed_sig, launched, detail)

        threading.Thread(target=worker, daemon=True,
                         name="easynpc-run").start()

    def _on_tool_closed(self, launched: bool, detail: str):
        if not launched:
            self._lock_close(False)
            self._set_context_lock(False)
            self._set_status(self._run_status,
                             self.tr("Could not launch EasyNPC Next: {0}").format(detail),
                             err_text())
            self._done_btn.setEnabled(True)
            return

        self._set_status(self._run_status,
                         self.tr("EasyNPC Next closed. Refreshing mod list…"))
        try:
            refresh = getattr(self._ctx, "refresh_modlist", None)
            if refresh is None:
                raise RuntimeError(self.tr("Mod list refresh is unavailable."))
            refresh()
            if detail:
                self._set_status(self._run_status, self.tr(
                    "Mod list refreshed. {0}").format(detail), err_text())
            else:
                self._set_status(self._run_status, self.tr(
                    "Mod list refreshed. Enable the output mod and deploy when ready."),
                    ok_text())
        except Exception as exc:
            self._log(f"EasyNPC Next Wizard: mod list refresh failed: {exc}")
            self._set_status(self._run_status,
                             self.tr("Mod list refresh failed: {0}").format(exc),
                             err_text())
        finally:
            self._lock_close(False)
            self._set_context_lock(False)
            self._done_btn.setEnabled(True)
