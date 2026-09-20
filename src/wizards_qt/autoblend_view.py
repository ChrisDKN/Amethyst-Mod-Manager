"""Install and run AutoBlend for Skyrim SE or LE."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal

from gui_qt.safe_emit import safe_emit
from Utils.bethesda.autoblend import APP_DIR, EXE_NAME, GITHUB_API_URL, OUTPUT_NAME
from Utils.bethesda.xedit import tool_exe_path
from wizards_qt._view_base import GREEN, RED, WizardViewBase

_INSTALL, _PROTON, _RUN = range(3)


def _native_run_finished(startup_log: Path, start_offset: int,
                         result_log: Path, previous_mtime_ns: int) -> bool:
    try:
        if result_log.stat().st_mtime_ns <= previous_mtime_ns:
            return False
        with startup_log.open("rb") as stream:
            stream.seek(start_offset)
            exited = b"main: exiting with code 0" in stream.read()
        return exited and b"=== Result ===" in result_log.read_bytes()
    except OSError:
        return False


class AutoBlendView(WizardViewBase):
    _install_status_sig = Signal(str, str)
    _install_done_sig = Signal(bool)
    _process_done_sig = Signal()

    def __init__(self, game, log_fn=None, on_close=None, ctx=None, **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run AutoBlend - {0}").format(game.name))
        self._exe = tool_exe_path(game, EXE_NAME, APP_DIR)
        self._proton_name = ""
        self._prefix_mode = ""

        self._install_status_sig.connect(self._guard(
            lambda text, color: self._set_status(self._install_status, text, color)))
        self._install_done_sig.connect(self._guard(self._on_install_done))
        self._process_done_sig.connect(self._guard(self._on_process_done))

        page, lay = self._step_page(self.tr("Step 1: Install AutoBlend"))
        self._make_note(lay, self.tr(
            "Download the latest AutoBlend release into this game's Applications folder."))
        self._install_status = self._make_status(lay)
        lay.addStretch(1)
        self._install_btn = self._accent_btn(self.tr("Install"))
        self._install_btn.clicked.connect(self._start_install)
        lay.addWidget(self._install_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)

        self._stack.addWidget(self._build_proton_holder())

        page, lay = self._step_page(self.tr("Step 3: Run AutoBlend"))
        self._make_note(lay, self.tr(
            "AutoBlend will read the active profile's enabled mods in MO2 mode. "
            "It works with deployed and VFS profiles. Choose Start in "
            "AutoBlend's window to generate the '{0}' mod. After it finishes, "
            "enable that mod and AutoBlend.esp. Disable the output mod before "
            "rerunning AutoBlend.").format(OUTPUT_NAME))
        self._run_status = self._make_status(lay)
        lay.addStretch(1)
        self._run_btn = self._accent_btn(self.tr("Launch AutoBlend"))
        self._run_btn.clicked.connect(self._start_run)
        lay.addWidget(self._run_btn, 0, Qt.AlignHCenter)
        self._done_btn = self._green_btn()
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)

        self._goto_step(_PROTON if self._exe else _INSTALL)
        if self._exe is not None:
            self._offer_tool_upgrade(_PROTON,
                                     lambda: self._goto_step(_INSTALL))

    def _goto_step(self, step: int):
        self._stack.setCurrentIndex(step)
        if step == _PROTON:
            self._enter_proton(
                self._exe, EXE_NAME, "AutoBlend", self._on_proton_chosen,
                title=self.tr("Step 2: Choose Proton Version"))
        elif step == _RUN:
            self._start_run()

    def _start_install(self):
        self._install_btn.setEnabled(False)
        self._github_install_worker(
            GITHUB_API_URL, ["autoblend", "win-x64"], APP_DIR, EXE_NAME,
            "AutoBlend", self._install_status_sig, self._install_done_sig)

    def _on_install_done(self, ok: bool):
        if ok:
            self._goto_step(_PROTON)
        else:
            self._install_btn.setEnabled(True)

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_RUN)

    def _set_context_lock(self, held: bool):
        hook = getattr(self._ctx, "set_tool_lock", None)
        if hook is not None:
            hook("autoblend", "AutoBlend", held)

    def _start_run(self):
        exe = self._exe
        if exe is None:
            self._set_status(self._run_status, self.tr("AutoBlend.exe was not found."), RED)
            return
        self._run_btn.setEnabled(False)
        self._set_context_lock(True)
        self._lock_close(True, self.tr("AutoBlend is running."))
        self._ran = True
        game = self._game
        profile = getattr(self._ctx, "profile_name", None) or "default"
        proton_name, prefix_mode = self._proton_name, self._prefix_mode
        from themes import get_ctk_appearance
        from Utils.ui.config import get_appearance_mode
        ui_theme = get_ctk_appearance(get_appearance_mode())

        def worker():
            from Utils.bethesda.autoblend import prepare_autoblend
            from Utils.executables.launch import (
                resolve_tool_prefix, run_tool_logged, shutdown_prefix_wineserver,
            )
            log = lambda msg: self._log(f"AutoBlend Wizard: {msg}")
            proton_script = compat_data = None
            watch_stop = threading.Event()
            native_done = threading.Event()
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=log)
                if result is None:
                    raise RuntimeError(self.tr("Could not resolve the selected Proton prefix."))
                proton_script, compat_data, env = result
                output = prepare_autoblend(game, exe, compat_data / "pfx", profile,
                                           log_fn=log, ui_theme=ui_theme)
                result_log = output / "AutoBlend-log.txt"
                previous_mtime_ns = (result_log.stat().st_mtime_ns
                                     if result_log.is_file() else -1)
                startup_log = (compat_data / "pfx" / "drive_c" / "users" / "steamuser"
                               / "AppData" / "Roaming" / "AutoBlend" / "native-startup.log")
                start_offset = startup_log.stat().st_size if startup_log.is_file() else 0

                def watch_native_exit():
                    while not watch_stop.wait(1):
                        if not _native_run_finished(startup_log, start_offset,
                                                    result_log, previous_mtime_ns):
                            continue
                        native_done.set()
                        if watch_stop.wait(10):
                            return
                        from Utils.executables.launch import live_tool_labels, reap_live_tools
                        if live_tool_labels(owner=self):
                            log("AutoBlend finished but its process stayed alive; ending it.")
                            reap_live_tools(owner=self, log_fn=log, kill_wineserver=False)
                        return

                threading.Thread(target=watch_native_exit, daemon=True,
                                 name="autoblend-exit-watch").start()
                env.pop("wx_msw_dark_mode", None)
                if ui_theme == "dark":
                    env["WX_MSW_DARK_MODE"] = "2"
                else:
                    env.pop("WX_MSW_DARK_MODE", None)
                safe_emit(self._run_status_sig,
                          self.tr("AutoBlend is running. Generate output in its window."), GREEN)
                code = run_tool_logged(proton_script, exe, env, log_fn=log,
                                       label="AutoBlend", game=game, owner=self)
                if code and not native_done.is_set():
                    raise RuntimeError(self.tr("AutoBlend exited with code {0}.").format(code))
                if code:
                    log(f"AutoBlend native run completed before process cleanup (code {code}).")
                safe_emit(self._run_status_sig,
                          self.tr("AutoBlend closed. Click Done to refresh the mod list."), GREEN)
            except Exception as exc:
                safe_emit(self._run_status_sig,
                          self.tr("AutoBlend error: {0}").format(exc), RED)
                log(f"error: {exc}")
            finally:
                watch_stop.set()
                if (proton_script is not None and compat_data is not None
                        and prefix_mode == "isolated"):
                    shutdown_prefix_wineserver(proton_script, compat_data, log_fn=log)
                safe_emit(self._process_done_sig)

        threading.Thread(target=worker, daemon=True, name="autoblend-run").start()

    def _on_process_done(self):
        self._set_context_lock(False)
        self._lock_close(False)
        self._run_btn.setEnabled(True)
        self._done_btn.setEnabled(self._ran)
