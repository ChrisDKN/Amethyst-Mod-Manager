"""Pandora Behaviour Engine+ wizard - Qt port of wizards/pandora.py.

Pandora ships as a regular mod, so this wizard is only offered when
"Pandora Behaviour Engine+.exe" is found under the mod staging folder
(gated in the game files via Utils.pandora_tools.find_pandora_exe).

Steps (plugins-panel-scoped tab):
  1. Deploy the modlist (through the app's deploy machinery via
     QtWizardContext.run_deploy, so the deploy mutex + progress popup apply).
     The user is reminded to delete any previous 'Pandora_output' mod first.
  2. Choose Proton version + prefix placement (shared ProtonStepWidget).
  3. Silently install the .NET 10 desktop runtime into that prefix
     (skipped when already marked installed).
  4. Run Pandora via Proton with --tesv:<game_path>; Done enables once it
     has launched.

All blocking work (prefix init, .NET install, Pandora run) happens on daemon
threads with Signals marshalling status back to the UI thread.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
)

from gui_qt.theme_qt import (
    active_palette, _c, button_qss, close_button, ok_text, err_text,
)
from gui_qt.safe_emit import safe_emit
from Utils.pandora_tools import EXE_NAME, find_pandora_exe

if TYPE_CHECKING:
    from Games.base_game import BaseGame



class PandoraView(QWidget):
    """Deploy mods and run Pandora Behaviour Engine+."""

    # (text, color) status updates from workers → UI thread.
    _deploy_status_sig = Signal(str, str)
    _deps_status_sig = Signal(str, str)
    _run_status_sig = Signal(str, str)
    _goto_step_sig = Signal(int)          # advance the stack from a worker
    _run_started_sig = Signal()           # Pandora launched → enable Done

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None):
        super().__init__()
        self._game = game
        self._log = log_fn or (lambda _m: None)
        self._on_close_cb = on_close or (lambda: None)
        self._ctx = ctx
        self._exe = find_pandora_exe(game)
        self._proton_name = ""
        self._prefix_mode = ""
        self._prefix_env = None       # (proton_script, compat_data, env)
        self._busy = False            # a worker step is running
        self._ran = False             # Pandora was launched at least once
        self._closing = False         # teardown started - ignore late signals

        # Guard every worker→UI signal: a daemon worker (deps install, or the
        # run worker blocking on proc.communicate()) can emit AFTER the user
        # closed the tab and the widget was deleteLater()'d. Dropping late
        # signals avoids touching a half-deleted widget.
        self._deploy_status_sig.connect(
            lambda t, c: None if self._closing
            else self._set_status(self._deploy_status, t, c))
        self._deps_status_sig.connect(
            lambda t, c: None if self._closing
            else self._set_status(self._deps_status, t, c))
        self._run_status_sig.connect(
            lambda t, c: None if self._closing
            else self._set_status(self._run_status, t, c))
        self._goto_step_sig.connect(
            lambda i: None if self._closing else self._goto_step(i))
        self._run_started_sig.connect(
            lambda: None if self._closing else self._on_run_started())

        self.setObjectName("PandoraView")
        self._build()

    # ---- layout -------------------------------------------------------------
    def _build(self):
        p = active_palette()
        self._dim = f"color:{_c(p,'TEXT_DIM')};"
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget(); bar.setObjectName("HeaderBar")
        hb = QHBoxLayout(bar); hb.setContentsMargins(12, 8, 8, 8); hb.setSpacing(8)
        title = QLabel(self.tr("Run Pandora - {0}").format(self._game.name))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600;")
        hb.addWidget(title)
        hb.addStretch(1)
        close = close_button(self.tr("✕ Close"), pal=p)
        close.clicked.connect(self._on_close)
        hb.addWidget(close)
        v.addWidget(bar)

        self._stack = QStackedWidget()
        v.addWidget(self._stack, 1)

        self._stack.addWidget(self._build_step_deploy())   # 0
        self._stack.addWidget(self._build_step_proton())   # 1
        self._stack.addWidget(self._build_step_deps())     # 2
        self._stack.addWidget(self._build_step_run())      # 3
        self._stack.setCurrentIndex(0)

    def _step_page(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        p = active_palette()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)
        head = QLabel(title)
        head.setAlignment(Qt.AlignHCenter)
        head.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:700;")
        lay.addWidget(head)
        return page, lay

    def _make_status(self, lay: QVBoxLayout) -> QLabel:
        lbl = QLabel("")
        lbl.setAlignment(Qt.AlignHCenter)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(self._dim)
        lay.addWidget(lbl)
        return lbl

    def _set_status(self, lbl: QLabel, text: str, color: str):
        lbl.setStyleSheet(f"color:{color};" if color else self._dim)
        lbl.setText(text)

    # ---- step 1: deploy -------------------------------------------------------
    def _build_step_deploy(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Deploy Modlist"))
        note = QLabel(
            self.tr("Before deploying, please delete any output from a previous\n"
            "Pandora run (the 'Pandora_output' mod in your mod list).\n\n"
            "Once you have done this, click Deploy."))
        note.setAlignment(Qt.AlignHCenter)
        note.setWordWrap(True)
        note.setStyleSheet(self._dim)
        lay.addWidget(note)
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        self._skip_btn = QPushButton(self.tr("Skip"))
        self._skip_btn.setCursor(Qt.PointingHandCursor)
        self._skip_btn.clicked.connect(lambda: self._goto_step(1))
        rh.addWidget(self._skip_btn)
        self._deploy_btn = QPushButton(self.tr("Deploy"))
        self._deploy_btn.setCursor(Qt.PointingHandCursor)
        self._deploy_btn.setStyleSheet(
            button_qss("BTN_INFO"))
        self._deploy_btn.clicked.connect(self._start_deploy)
        rh.addWidget(self._deploy_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _start_deploy(self):
        run_deploy = getattr(self._ctx, "run_deploy", None)
        if run_deploy is None:
            safe_emit(self._deploy_status_sig,
                      self.tr("Deploy is unavailable here."), err_text())
            return
        self._busy = True
        self._deploy_btn.setEnabled(False)
        self._skip_btn.setEnabled(False)
        safe_emit(self._deploy_status_sig, self.tr("Deploying…"), "")

        def _done(ok: bool):
            # Fired on the UI thread by the app's deploy completion handler.
            self._busy = False
            if ok:
                self._set_status(self._deploy_status, self.tr("Deploy complete."), ok_text())
                self._goto_step(1)
            else:
                self._set_status(self._deploy_status,
                                 self.tr("Deploy failed - see log."), err_text())
                self._deploy_btn.setEnabled(True)
                self._skip_btn.setEnabled(True)

        if not run_deploy(_done):
            self._busy = False
            self._set_status(self._deploy_status,
                             self.tr("Could not start deploy - see log."), err_text())
            self._deploy_btn.setEnabled(True)
            self._skip_btn.setEnabled(True)

    # ---- step 2: proton -------------------------------------------------------
    def _build_step_proton(self) -> QWidget:
        if self._exe is None:
            # Shouldn't happen (the tool is gated on the exe existing), but
            # keep the Tk fallback: an error page instead of the picker.
            page, lay = self._step_page(self.tr("Step 2: Choose Proton Version"))
            err = QLabel(self.tr("'{0}' was not found in your mod staging folder.\n\nInstall Pandora Behaviour Engine+ as a mod, then reopen this wizard.").format(EXE_NAME))
            err.setAlignment(Qt.AlignHCenter)
            err.setWordWrap(True)
            err.setStyleSheet(f"color:{err_text()};")
            lay.addWidget(err)
            lay.addStretch(1)
            return page
        from wizards_qt.proton_step import ProtonStepWidget
        return ProtonStepWidget(
            self._game, self._exe, EXE_NAME, "Pandora",
            on_continue=self._on_proton_chosen,
            log_fn=self._log,
            title=self.tr("Step 2: Choose Proton Version"),
            wizard_id=getattr(self._ctx, "wizard_tool_id", ""),
            wizard_label=getattr(self._ctx, "wizard_tool_label", ""),
            wizard_label_args=getattr(
                self._ctx, "wizard_tool_label_args", ()),
        )

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(2)
        self._start_deps()

    # ---- step 3: .NET 10 --------------------------------------------------------
    def _build_step_deps(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 3: Install Dependencies"))
        self._deps_status = self._make_status(lay)
        self._deps_status.setText(self.tr("Checking .NET 10…"))
        lay.addStretch(1)
        return page

    def _start_deps(self):
        if self._busy:
            return
        self._busy = True
        exe, game = self._exe, self._game
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.exe_launch import PREFIX_MODE_GAME, resolve_tool_prefix
            from Utils.pandora_tools import install_net10, net10_installed
            try:
                safe_emit(self._deps_status_sig,
                    self.tr("Preparing Pandora's Wine prefix…"), "")
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode,
                    log_fn=lambda m: self._log(f"Pandora Wizard: {m}"))
                if result is None:
                    if prefix_mode == PREFIX_MODE_GAME:
                        safe_emit(self._deps_status_sig,
                            self.tr("Could not resolve the Proton version for "
                            "the game's own prefix - launch the game once, or "
                            "pick a different prefix option."), err_text())
                    else:
                        safe_emit(self._deps_status_sig,
                            self.tr("Could not find Proton '{0}' - check that "
                            "it is installed in Steam, Heroic or ProtonPlus, "
                            "then reopen this wizard.").format(proton_name),
                            err_text())
                    return
                self._prefix_env = result
                proton_script, compat_data, env = result
                if net10_installed(compat_data):
                    safe_emit(self._deps_status_sig,
                        self.tr(".NET 10 already installed - skipping."), ok_text())
                else:
                    install_net10(
                        proton_script, compat_data, env,
                        log_fn=lambda m: self._log(f"Pandora Wizard: {m}"),
                        status_fn=lambda t: safe_emit(self._deps_status_sig, t, ""))
                    safe_emit(self._deps_status_sig,
                        self.tr(".NET 10 installed successfully."), ok_text())
                safe_emit(self._goto_step_sig, 3)
            except Exception as exc:
                safe_emit(self._deps_status_sig,
                          self.tr("Error: {0}").format(exc), err_text())
                self._log(f"Pandora Wizard: .NET 10 install error: {exc}")
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True,
                         name="pandora-deps").start()

    # ---- step 4: run ------------------------------------------------------------
    def _build_step_run(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 4: Run Pandora"))
        self._run_status = self._make_status(lay)
        self._run_status.setText(self.tr("Launching Pandora…"))
        lay.addStretch(1)
        self._done_btn = QPushButton(self.tr("Done"))
        self._done_btn.setCursor(Qt.PointingHandCursor)
        self._done_btn.setEnabled(False)
        self._done_btn.setStyleSheet(
            button_qss("BTN_SUCCESS"))
        self._done_btn.clicked.connect(self._on_done)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    def _start_run(self):
        if self._busy:
            return
        self._busy = True
        exe, game = self._exe, self._game
        prefix_env = self._prefix_env

        def worker():
            from Utils.pandora_tools import run_pandora
            try:
                if prefix_env is None:
                    safe_emit(self._run_status_sig,
                        self.tr("Prefix was not prepared - go back and retry."),
                        err_text())
                    return
                proton_script, compat_data, env = prefix_env
                self._log(f"Pandora Wizard: launching {exe} via Proton")
                rc = run_pandora(
                    exe, game, proton_script, compat_data, env,
                    log_fn=lambda m: self._log(f"Pandora Wizard: {m}"),
                    on_started=lambda *a: safe_emit(self._run_started_sig, *a),
                    owner=self)
                if rc != 0:
                    safe_emit(self._run_status_sig,
                        self.tr("Pandora exited with error (code {0}).\nSee the "
                        "log for details. Click Done to close.").format(rc),
                        err_text())
                else:
                    safe_emit(self._run_status_sig,
                        self.tr("Pandora finished. Click Done to close."), ok_text())
            except Exception as exc:
                safe_emit(self._run_status_sig,
                          self.tr("Launch error: {0}").format(exc), err_text())
                self._log(f"Pandora Wizard: launch error: {exc}")
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True,
                         name="pandora-run").start()

    def _on_run_started(self):
        self._ran = True
        self._set_status(
            self._run_status,
            self.tr("Pandora is running.\nClose it when you are done, then click Done."),
            ok_text())
        self._done_btn.setEnabled(True)

    # ---- shared -------------------------------------------------------------
    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == 3:
            self._start_run()

    def _on_close(self):
        # Both the header ✕ and the Done button always close. Any in-flight
        # daemon worker keeps running harmlessly (Pandora is its own process;
        # deploy/.NET steps finish in the background) - the _closing guard
        # drops their late UI signals so nothing touches the deleted widget.
        self._finish()

    def _on_done(self):
        self._finish()

    def _finish(self):
        if self._closing:
            return
        self._closing = True
        # Snapshot before the widget is torn down - the refresh is a ctx
        # method (safe post-close), but read our own flags/ctx first.
        do_refresh = self._ran and getattr(self._ctx, "refresh_modlist", None)
        self._on_close_cb()
        if do_refresh:
            # Pandora may have written a new Pandora_output mod - re-sync the
            # modlist so it appears (mirrors the Tk wizard's _reload_mod_panel).
            do_refresh()
