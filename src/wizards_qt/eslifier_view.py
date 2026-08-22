"""ESLifier wizard - Qt port of wizards/eslifier.py.

Installs ESLifier from GitHub into Applications/ESLifier/ and runs it in MO2
mode via Proton - no deploy needed: it reads the load order straight from a
prefix-free hardlinked mirror of the staging folder (see
Utils/eslifier_tools.py).  Its output lands as the "ESLifier Output" mod.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.eslifier_tools import (
    APP_DIR, EXE_NAME, GITHUB_API_URL, OUTPUT_NAME, find_eslifier_exe,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_INSTALL, _PG_PROTON, _PG_RUN = range(3)


class ESLifierView(WizardViewBase):
    """Install and run ESLifier in MO2 mode."""

    _dl_status_sig = Signal(str, str)
    _dl_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run ESLifier - {0}").format(game.name))
        self._exe = find_eslifier_exe(game)
        self._proton_name = ""
        self._prefix_mode = ""

        self._dl_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._dl_status, t, c)))
        self._dl_done_sig.connect(self._guard(self._on_dl_done))

        # page 0: install (explicit button - Tk parity)
        page, lay = self._step_page(self.tr("Step 1: Install ESLifier"))
        self._make_note(lay, (
            self.tr("ESLifier will be downloaded from GitHub and installed into this\n"
            "game's Applications folder.\n\nClick Install to begin.")))
        self._dl_status = self._make_status(lay)
        lay.addStretch(1)
        self._install_btn = self._accent_btn(self.tr("Install"))
        self._install_btn.clicked.connect(self._start_install)
        lay.addWidget(self._install_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)
        # page 1: proton
        self._stack.addWidget(self._build_proton_holder())
        # page 2: run
        page2, lay2 = self._step_page(self.tr("Step 3: Run ESLifier"))
        self._make_note(lay2, (
            self.tr("ESLifier runs in MO2 mode, reading your load order directly from\nthe mod staging folder, so no deploy is required.\n\nWhen ESLifier finishes, it writes its output as the\n'{0}' mod, which will appear in your mod list.").format(OUTPUT_NAME)))
        self._run_status = self._make_status(lay2)
        lay2.addStretch(1)
        self._done_btn = self._green_btn(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay2.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page2)

        if self._exe is not None:
            self._goto_step(_PG_PROTON)
        else:
            self._stack.setCurrentIndex(_PG_INSTALL)

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_PROTON:
            self._enter_proton(
                self._exe, EXE_NAME, "ESLifier", self._on_proton_chosen,
                title=self.tr("Step 2: Choose Proton Version"),
                missing_text=self.tr("{0} was not found.\n"
                             "Please restart the wizard and let it install "
                             "ESLifier first.").format(EXE_NAME))
        elif idx == _PG_RUN:
            self._set_status(self._run_status, self.tr("Launching ESLifier…"))
            self._start_run()

    def _start_install(self):
        self._install_btn.setEnabled(False)
        self._github_install_worker(
            GITHUB_API_URL, ["eslifier.zip"], APP_DIR, EXE_NAME, "ESLifier",
            self._dl_status_sig, self._dl_done_sig)

    def _on_dl_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_PROTON)
        else:
            self._install_btn.setEnabled(True)

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_RUN)

    # ---- run ----------------------------------------------------------------------
    def _start_run(self):
        exe, game = self._exe, self._game
        if exe is None:
            self._set_status(self._run_status,
                             self.tr("{0} was not found.").format(EXE_NAME), RED)
            return
        proton_name, prefix_mode = self._proton_name, self._prefix_mode
        profile = getattr(self._ctx, "profile_name", None) or "default"

        def worker():
            from Utils.eslifier_tools import cleanup_scan_mirror, write_settings
            from Utils.exe_launch import (
                PREFIX_MODE_GAME, resolve_tool_prefix, run_tool_logged,
                shutdown_prefix_wineserver,
            )
            _wlog = lambda m: self._log(f"ESLifier Wizard: {m}")
            scan_mirror = None
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
                pfx = compat_data / "pfx"

                try:
                    scan_mirror = write_settings(game, exe, pfx, profile,
                                                 log_fn=_wlog)
                except Exception as exc:
                    safe_emit(self._run_status_sig,
                              self.tr("Could not write settings: {0}").format(exc), RED)
                    _wlog(f"settings error: {exc}")
                    return

                _wlog(f"launching {exe} via Proton")
                safe_emit(self._run_status_sig,
                          self.tr("ESLifier is running.\nClose it when you are done, "
                          "then click Done."), GREEN)
                safe_emit(self._run_started_sig)
                run_tool_logged(proton_script, exe, env, log_fn=_wlog,
                                label="ESLifier")
                _wlog("ESLifier closed.")
                cleanup_scan_mirror(scan_mirror, log_fn=_wlog)
                scan_mirror = None
                safe_emit(self._run_status_sig,
                          self.tr("ESLifier finished. Click Done to close."), GREEN)
            except Exception as exc:
                cleanup_scan_mirror(scan_mirror, log_fn=_wlog)
                safe_emit(self._run_status_sig, self.tr("Launch error: {0}").format(exc), RED)
                self._log(f"ESLifier Wizard: launch error: {exc}")
            finally:
                # In finally: a tool that crashed is exactly when Proton
                # sidecars are most likely to be left holding the prefix.
                if proton_script is not None and compat_data is not None:
                    shutdown_prefix_wineserver(proton_script, compat_data,
                                               log_fn=_wlog)

        threading.Thread(target=worker, daemon=True, name="eslifier-run").start()

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)
