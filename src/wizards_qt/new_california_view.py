from __future__ import annotations

import threading

from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtWidgets import QLabel, QLineEdit, QPlainTextEdit

from gui_qt.runtime.safe_emit import safe_emit
from Utils.bethesda.new_california import (
    EXE_NAME, FILE_ID, KEYWORDS, MOD_NAME, NEXUS_URL, Installation,
    applications_dir, cleanup_installer, extract_installer, find_installer,
    installer_payload_present, validate_profile_name,
)
from wizards_qt._view_base import AMBER, GREEN, RED, WizardViewBase

_DOWNLOAD, _LOCATE, _EXTRACT, _SETTINGS, _PROTON, _RUN = range(6)


class NewCaliforniaView(WizardViewBase):
    _work_done = Signal(object, object)
    _log_line = Signal(str)

    def __init__(self, game, log_fn=None, on_close=None, ctx=None, **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Install New California - {0}").format(game.name))
        self._exe = find_installer(game)
        self._expected_paths = (game.get_game_path(), game.get_prefix_path())
        self._installation = None
        self._failure = ""
        self._cleanup_warning = ""
        self._close_cleanup_attempted = False
        self._deployment_attempted = False
        self._restore_attempted = False
        self._lock_key = f"new-california-{id(self)}"
        self._work_done.connect(self._guard(self._on_work_done))
        QCoreApplication.instance().aboutToQuit.connect(self.tab_closing)
        self._log_line.connect(self._guard(lambda line: self._output.appendPlainText(line)))
        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 1: Download New California"),
            self.tr("Download the New California 2.3.1 full installer ZIP. Premium accounts download automatically; other accounts can download in the browser and the archive will be detected."),
            NEXUS_URL, lambda: self._goto_step(_LOCATE)))
        self._stack.addWidget(self._build_locate_page())
        self._stack.addWidget(self._build_extract_page(self.tr("Step 2: Extract the installer")))
        self._stack.addWidget(self._build_settings())
        self._stack.addWidget(self._build_proton_holder())
        self._stack.addWidget(self._build_run_page(self.tr("Installing New California")))
        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setMaximumBlockCount(2000)
        self._stack.widget(_RUN).layout().insertWidget(1, self._output, 1)
        if self._exe:
            self._goto_step(_SETTINGS)
        else:
            self._stack.setCurrentIndex(_DOWNLOAD)
            self._nexus_auto_fetch(
                url=NEXUS_URL, file_id=FILE_ID, keywords=KEYWORDS,
                label=MOD_NAME, pages=(_DOWNLOAD, _LOCATE),
                on_archive=lambda _path: self._goto_step(_EXTRACT))

    def _build_settings(self):
        page, lay = self._step_page(self.tr("Step 3: Create a New California profile"))
        self._make_note(lay, self.tr(
            "The wizard restores the current deployment, creates an empty profile with its own mods, and runs the installer. Its files become a managed New California mod and its edited INIs stay in the new profile. Your original INIs and game files are restored afterwards.\n\n"
            "In the installer, confirm the New Vegas folder shown below. Complete the installer and close it without launching the game. The wizard finishes with the game restored; deploy the new profile when ready.\n\n"
            "Extracted installer files are removed after installation or when you close this wizard. The downloaded ZIP, Wine prefixes and recovery backups are kept."))
        self._game_path = QLabel(str(self._game.get_game_path()))
        self._game_path.setWordWrap(True)
        self._game_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._game_path)
        lay.addWidget(QLabel(self.tr("New profile name:")))
        self._profile_name = QLineEdit(MOD_NAME)
        lay.addWidget(self._profile_name)
        self._settings_status = self._make_status(lay)
        lay.addStretch(1)
        button = self._accent_btn(self.tr("Continue"))
        button.clicked.connect(self._choose_prefix)
        lay.addWidget(button, 0, Qt.AlignHCenter)
        return page

    def _choose_prefix(self):
        try:
            validate_profile_name(self._game, self._profile_name.text())
            if not all(getattr(self._ctx, name, None) for name in ("select_profile", "run_deploy", "run_restore")):
                raise RuntimeError(self.tr("Profile selection and deployment are unavailable here."))
            self._target_name = self._profile_name.text()
        except Exception as exc:
            self._set_status(self._settings_status, str(exc), RED)
            return
        self._goto_step(_PROTON)

    def _goto_step(self, page):
        self._stack.setCurrentIndex(page)
        if page == _LOCATE:
            self._enter_locate(KEYWORDS, self.tr("Select the New California installer ZIP"),
                               self.tr("Installer archive not found. Download it, then try again or browse."),
                               lambda _path: self._goto_step(_EXTRACT))
        elif page == _EXTRACT:
            self._lock_close(True)
            self._set_status(self._extract_status, self.tr("Extracting installer into Applications…"))
            self._work(self._extract, lambda: self._extracted(), self._extract_failed)
        elif page == _PROTON:
            from Utils.executables.launch import PREFIX_MODE_GAME
            self._enter_proton(self._exe, EXE_NAME, MOD_NAME, self._start,
                               default_prefix_mode=PREFIX_MODE_GAME)

    def _extract(self):
        self._exe = extract_installer(self._game, self._archive_path, self._write_log)

    def _extracted(self):
        self._lock_close(False)
        self._goto_step(_SETTINGS)

    def _extract_failed(self, error):
        self._lock_close(False)
        self._set_status(self._extract_status, str(error), RED)

    def _write_log(self, message):
        self._log(f"New California: {message}")
        safe_emit(self._log_line, str(message))

    def _tool_lock(self, held):
        callback = getattr(self._ctx, "set_tool_lock", None)
        if callback:
            callback(self._lock_key, MOD_NAME, held)

    def _work(self, function, success, failure):
        self._success, self._on_failure = success, failure
        self._tool_lock(True)

        def worker():
            from Utils.deployment.locking import coordinated_game_mutation
            try:
                with coordinated_game_mutation(self._game, "wizard.new_california"):
                    function()
            except Exception as exc:
                self._write_log(str(exc))
                safe_emit(self._work_done, False, exc)
            else:
                safe_emit(self._work_done, True, None)

        threading.Thread(target=worker, daemon=True, name="new-california").start()

    def _on_work_done(self, ok, error):
        self._tool_lock(False)
        try:
            if ok:
                self._success()
            else:
                self._on_failure(error)
        except Exception as exc:
            self._abort(exc)

    def _start(self, proton_name, prefix_mode):
        if self.tab_close_blocked():
            return
        self._proton_name, self._prefix_mode = proton_name, prefix_mode
        self._goto_step(_RUN)
        self._lock_close(True)
        self._set_status(self._run_status, self.tr("Saving current INIs…"))
        try:
            self._source_profile = self._ctx.current_profile() if self._ctx.current_profile else self._ctx.profile_name
            if not self._ctx.select_profile(self._source_profile):
                raise RuntimeError(self.tr("Return to Fallout New Vegas before starting this wizard."))
            if (self._game.get_game_path(), self._game.get_prefix_path()) != self._expected_paths:
                raise RuntimeError(self.tr("The game or prefix paths changed. Reopen the wizard to review them."))
        except Exception as exc:
            self._show_failure(exc)
            return
        self._work(self._snapshot, self._restore_original, self._abort)

    def _snapshot(self):
        self._installation = Installation(self._game, self._source_profile, self._target_name)
        self._installation.snapshot_inis()

    def _restore_original(self):
        self._run_ctx_restore(self._run_status, self._create_profile,
                              lambda: self._abort(self.tr("Could not restore the original deployment.")))

    def _create_profile(self):
        self._set_status(self._run_status, self.tr("Creating the new profile…"))

        def create():
            self._installation.create_profile(self._write_log)
            self._installation.backup_game_files()
            self._installation.protect_game_inis()

        self._work(create, self._select_and_deploy, self._abort)

    def _select_and_deploy(self):
        if not self._ctx.select_profile(self._installation.name):
            self._abort(self.tr("Could not select the new profile."))
            return
        self._ran = True
        self._deployment_attempted = True
        self._run_ctx_deploy(self._run_status, self._run_installer,
                             lambda: self._installer_failed(self.tr("Could not deploy the empty profile.")))

    def _run_installer(self):
        self._set_status(self._run_status, self.tr("Complete the New California installer, then close it without launching the game."))
        self._work(self._install, self._restore_installed,
                   self._installer_failed)

    def _install(self):
        from Utils.bethesda.registry import maybe_register_for_game
        from Utils.executables.launch import resolve_tool_prefix, run_tool_logged

        session = self._installation
        session.check_profile()
        result = resolve_tool_prefix(self._exe, self._game, self._proton_name,
                                     self._prefix_mode, log_fn=self._write_log)
        if result is None:
            raise RuntimeError("Could not prepare the installer prefix.")
        proton, compat_data, env = result
        try:
            session.prepare_inis(compat_data)
            if not maybe_register_for_game(compat_data, proton, env, self._game, self._write_log):
                raise RuntimeError("Could not register New Vegas in the installer prefix.")
            session.detach_game_files()
            code = run_tool_logged(proton, self._exe, env, log_fn=self._write_log,
                                   cwd=self._exe.parent, label=MOD_NAME,
                                   game=self._game, owner=self)
            if code != 0:
                raise RuntimeError(f"Installer exited with code {code}.")
            session.capture_inis()
        finally:
            try:
                session.capture_game_changes()
            finally:
                session.restore_inis()

    def _installer_failed(self, error):
        self._failure = str(error)
        self._restore_installed()

    def _restore_installed(self):
        self._restore_attempted = True
        self._run_ctx_restore(self._run_status, self._after_restore,
                              lambda: self._abort(self.tr("Restore failed. Keep the recovery files and run Restore before continuing.")))

    def _after_restore(self):
        def finish():
            self._installation.restore_game_inis()
            self._installation.restore_game_files()
            self._installation.restore_source_inis()
            if not self._failure:
                self._installation.register_mod(self._archive_path, self._write_log)
                try:
                    cleanup_installer(self._game, self._write_log)
                except Exception as exc:
                    self._cleanup_warning = str(exc)
                    self._write_log(f"Installer cleanup failed: {exc}")

        self._work(finish, self._complete, self._abort)

    def _complete(self):
        self._game.load_paths()
        if self._failure:
            self._show_failure(self._failure)
            return
        self._lock_close(False)
        self._done_btn.setEnabled(True)
        self._set_status(self._run_status, self.tr("New California is installed. The game is restored. Deploy this profile when you are ready to play."), GREEN)
        if self._cleanup_warning:
            self._set_status(self._run_status, self.tr(
                "New California is installed, but extracted installer files could not be removed. Remove them manually from {0}.\n{1}").format(
                    applications_dir(self._game) / "installer", self._cleanup_warning), AMBER)
        if self._ctx.refresh_modlist:
            self._ctx.refresh_modlist()
        if self._ctx.refresh_plugins:
            self._ctx.refresh_plugins()

    def _abort(self, error):
        self._failure = str(error)
        if self._deployment_attempted and not self._restore_attempted:
            self._restore_installed()
            return
        if self._installation is None:
            self._show_failure(self._failure)
            return

        self._work(self._installation.cleanup_inis, lambda: self._show_failure(self._failure),
                   lambda cleanup_error: self._show_failure(f"{self._failure}\n{cleanup_error}"))

    def _show_failure(self, message):
        self._tool_lock(False)
        self._lock_close(False)
        self._done_btn.setEnabled(True)
        recovery = f"\nRecovery files: {self._installation.recovery}" if self._installation else ""
        self._set_status(self._run_status, str(message) + recovery, RED)
        self._write_log(str(message) + recovery)

    def _finish(self):
        if self._closing or self.tab_close_blocked():
            return
        if self._close_cleanup_attempted or not installer_payload_present(self._game):
            super()._finish()
            return
        self._close_cleanup_attempted = True
        self._auto_fetch_cancel.set()
        self._lock_close(True)

        def finished():
            self._lock_close(False)
            super(NewCaliforniaView, self)._finish()

        def failed(error):
            self._lock_close(False)
            self._stack.setCurrentIndex(_RUN)
            self._done_btn.setEnabled(True)
            self._set_status(self._run_status, self.tr(
                "Extracted installer files could not be removed. Remove them manually from {0}.\n{1}").format(
                    applications_dir(self._game) / "installer", str(error)), AMBER)

        self._work(lambda: cleanup_installer(self._game, self._write_log), finished, failed)

    def _on_auto_dl_archive(self, path):
        if not self._close_cleanup_attempted:
            super()._on_auto_dl_archive(path)

    def tab_closing(self):
        if self._closing:
            return
        self._auto_fetch_cancel.set()
        if self.tab_close_blocked():
            self._write_log(f"Installer is still in use; extracted files retained at {applications_dir(self._game) / 'installer'}")
            return
        try:
            cleanup_installer(self._game, self._write_log)
        except Exception as exc:
            message = self.tr("Extracted installer files could not be removed from {0}: {1}").format(
                applications_dir(self._game) / "installer", str(exc))
            self._write_log(message)
            notify = getattr(self._ctx, "notify", None)
            if notify:
                notify(message, "warning")
        self._closing = True
