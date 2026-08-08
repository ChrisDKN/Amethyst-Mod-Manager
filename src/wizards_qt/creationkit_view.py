"""Creation Kit (+ CKPE) wizard - Qt port of wizards/creationkit.py.

Detects CreationKit.exe in the game root (installed via Steam), optionally
installs/updates Creation Kit Platform Extended from GitHub as a
root-flagged managed mod, deploys, then runs CK via Proton from the game
root.  The isolated prefix is relocated to wine_prefixes/creationkit_<Proton>/
(a prefix next to the exe would land inside the game install); pre-launch:
registry seed, d3dcompiler_47 + VC++ deps, plugins.txt/My Games links, the
winhttp=native,builtin override + cold wineserver restart so CKPE's loader
runs on the first launch.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.creationkit_tools import (
    EXE_NAME, ckpe_mod_installed, creationkit_exe_path,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_PG_DETECT, _PG_CKPE, _PG_DEPLOY, _PG_PROTON, _PG_RUN = range(5)


def _ck_isolated_prefix_dir(proton_name: str):
    """Isolated CK prefix lives in the app config's wine_prefixes/ folder -
    CreationKit.exe is in the game root, so the default (next to the exe)
    would create a ~1 GB Wine prefix inside the game install."""
    from Utils.config_paths import get_wine_prefixes_dir
    return get_wine_prefixes_dir() / f"creationkit_{proton_name}"


class CreationKitView(WizardViewBase):
    """Set up and run the Creation Kit (+ CKPE) for Skyrim SE."""

    _ckpe_status_sig = Signal(str, str)
    _ckpe_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run Creation Kit - {0}").format(game.name))
        self._exe = creationkit_exe_path(game)
        self._proton_name = ""
        self._prefix_mode = ""

        self._ckpe_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._ckpe_status, t, c)))
        self._ckpe_done_sig.connect(self._guard(self._on_ckpe_done))

        self._stack.addWidget(self._build_detect_page())
        self._stack.addWidget(self._build_ckpe_page())
        # Deploy auto-starts (Tk parity) with a Skip escape hatch.
        page, lay = self._step_page(self.tr("Step 3: Deploy Modlist"))
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)
        skip = QPushButton(self.tr("Skip"))
        skip.setCursor(Qt.PointingHandCursor)
        skip.clicked.connect(lambda: self._goto_step(_PG_PROTON))
        lay.addWidget(skip, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)
        self._stack.addWidget(self._build_proton_holder())
        self._stack.addWidget(self._build_ck_run_page())

        self._goto_step(_PG_DETECT)

    # ---- pages ---------------------------------------------------------------
    def _build_detect_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 1: Locate Creation Kit"))
        if self._exe is None:
            note = self._make_note(lay, (
                self.tr('{0} was not found in the game folder.\n\nThe Creation Kit is installed through Steam:\nSkyrim Special Edition → ⚙ → Manage → Creation Kit.\n\nInstall it, then reopen this wizard.').format(EXE_NAME)))
            note.setStyleSheet(f"color:{RED};")
            lay.addStretch(1)
            return page
        found = self._make_note(lay, self.tr("Found {0} in the game folder.").format(EXE_NAME))
        found.setStyleSheet(f"color:{GREEN};")
        lay.addStretch(1)
        nxt = self._accent_btn(self.tr("Next →"))
        nxt.clicked.connect(lambda: self._goto_step(_PG_CKPE))
        lay.addWidget(nxt, 0, Qt.AlignHCenter)
        return page

    def _build_ckpe_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 2: Creation Kit Platform Extended"))
        already = ckpe_mod_installed(self._game)
        self._make_note(lay, (
            self.tr("Creation Kit Platform Extended (CKPE) patches the Creation Kit "
            "so it runs correctly. It is downloaded from GitHub and installed "
            "as a mod with the root flag enabled, so it deploys into the game "
            "folder next to CreationKit.exe.\n\n")
            + (self.tr("CKPE already appears to be installed. You can update it or skip.")
               if already else
               self.tr("Click Install to download and add the latest CKPE (SSE build)."))))
        self._ckpe_status = self._make_status(lay)
        lay.addStretch(1)

        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        skip = QPushButton(self.tr("Skip"))
        skip.setCursor(Qt.PointingHandCursor)
        skip.clicked.connect(lambda: self._goto_step(_PG_DEPLOY))
        rh.addWidget(skip)
        self._ckpe_btn = self._accent_btn(
            self.tr("Update CKPE") if already else self.tr("Install CKPE"))
        self._ckpe_btn.clicked.connect(self._start_ckpe_install)
        rh.addWidget(self._ckpe_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    def _build_ck_run_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 4: Run Creation Kit"))
        self._run_status = self._make_status(lay)
        self._make_note(lay, (
            self.tr("Note: on a brand-new prefix the first launch may open the plain "
            "Creation Kit without Creation Kit Platform Extended (CKPE). If "
            "you need CKPE, close the Creation Kit and run the wizard again - "
            "CKPE loads on the second launch once the prefix is initialised."
            "\n\nThe Creation Kit can also occasionally crash on startup "
            "under Proton (a known Wine timing issue). If it closes "
            "immediately, just relaunch.")))
        lay.addStretch(1)
        self._done_btn = self._green_btn(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        return page

    # ---- routing ---------------------------------------------------------------
    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_DEPLOY:
            self._run_ctx_deploy(self._deploy_status,
                                 lambda: self._goto_step(_PG_PROTON))
        elif idx == _PG_PROTON:
            self._enter_proton(
                self._exe, EXE_NAME, "Creation Kit", self._on_proton_chosen,
                isolated_prefix_dir_fn=_ck_isolated_prefix_dir,
                title=self.tr("Step 3: Choose Proton Version"),
                missing_text=self.tr("{0} was not found in the game folder.\n"
                             "Install the Creation Kit from Steam, then "
                             "reopen this wizard.").format(EXE_NAME))
        elif idx == _PG_RUN:
            self._start_run()

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_RUN)

    # ---- CKPE install ---------------------------------------------------------------
    def _start_ckpe_install(self):
        self._ckpe_btn.setEnabled(False)
        self._set_status(self._ckpe_status, self.tr("Contacting GitHub…"))
        game = self._game

        def worker():
            from Utils.creationkit_tools import install_ckpe_mod
            _wlog = lambda m: self._log(f"Creation Kit Wizard: {m}")
            try:
                tag = install_ckpe_mod(
                    game,
                    status_fn=lambda t: safe_emit(self._ckpe_status_sig, t, ""),
                    log_fn=_wlog)
                safe_emit(self._ckpe_status_sig,
                          self.tr("CKPE {0} installed as a mod (root flag enabled).").format(tag),
                          GREEN)
                safe_emit(self._ckpe_done_sig, True)
            except Exception as exc:
                safe_emit(self._ckpe_status_sig,
                          self.tr("CKPE install error: {0}").format(exc), RED)
                _wlog(f"CKPE install error: {exc}")
                safe_emit(self._ckpe_done_sig, False)

        threading.Thread(target=worker, daemon=True, name="ckpe-install").start()

    def _on_ckpe_done(self, ok: bool):
        if ok:
            self._ran = True   # new mod in the modlist - refresh on close
            self._goto_step(_PG_DEPLOY)
        else:
            self._ckpe_btn.setEnabled(True)

    # ---- run ----------------------------------------------------------------------
    def _start_run(self):
        exe, game = self._exe, self._game
        if exe is None:
            self._set_status(self._run_status,
                             self.tr("{0} was not found.").format(EXE_NAME), RED)
            return
        self._set_status(self._run_status, self.tr("Launching Creation Kit…"))
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.bethesda_registry import maybe_register_for_game
            from Utils.deploy import apply_wine_dll_overrides
            from Utils.exe_launch import (
                link_mygames, link_plugins_txt, resolve_tool_prefix,
                run_tool_logged, shutdown_prefix_wineserver,
            )
            from Utils.protontricks import (
                D3D_DEP_KEY, VCREDIST_DEP_KEY, install_d3dcompiler_47,
                install_vcredist, is_dep_installed,
            )
            _wlog = lambda m: self._log(f"Creation Kit Wizard: {m}")
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog,
                    isolated_prefix_dir=_ck_isolated_prefix_dir(proton_name))
                if result is None:
                    safe_emit(self._run_status_sig,
                              self.tr("Could not find Proton '{0}' - "
                              "check that it is installed in Steam.").format(proton_name), RED)
                    return
                proton_script, compat_data, env = result

                game_path = game.get_game_path()
                if game_path is None:
                    safe_emit(self._run_status_sig,
                              self.tr("Game path not configured."), RED)
                    return
                pfx = compat_data / "pfx"

                # CK reads the game's Installed Path from the registry - a
                # fresh tool prefix never has it (idempotent, marker-guarded).
                maybe_register_for_game(
                    prefix_dir=compat_data, proton_script=proton_script,
                    env=env, game=game, log_fn=_wlog)

                # Common runtime deps (idempotent via amethyst_deps.json).
                # steam_id is omitted for d3dcompiler so the protontricks
                # fallback can't target the game prefix instead of this one.
                if not is_dep_installed(pfx, D3D_DEP_KEY):
                    safe_emit(self._run_status_sig,
                              self.tr("Installing d3dcompiler_47…"), "")
                    install_d3dcompiler_47("", log_fn=_wlog, prefix_path=pfx)
                if not is_dep_installed(pfx, VCREDIST_DEP_KEY):
                    safe_emit(self._run_status_sig,
                              self.tr("Installing VC++ Redistributable (first run "
                              "only)…"), "")
                    install_vcredist(proton_script, env, log_fn=_wlog,
                                     prefix_path=pfx)

                link_plugins_txt(game, pfx, _wlog)
                link_mygames(game, pfx, _wlog)

                # CKPE ships a winhttp.dll loader; the prefix must prefer the
                # native DLL.  Write the override, then shut the wineserver
                # down so the CK launch starts cold and reads it from disk
                # (a live server only reads the registry on cold start -
                # otherwise CKPE only loads from the second launch on).
                apply_wine_dll_overrides(
                    compat_data, {"winhttp": "native,builtin"}, log_fn=_wlog)
                shutdown_prefix_wineserver(proton_script, compat_data,
                                           log_fn=_wlog)

                # CKPE crashes on startup if CKPEPlugins/ is missing from the
                # game root; the CKPE mod ships one - create it only as a
                # fallback for manual CKPE installs.
                ckpe_plugins = game_path / "CKPEPlugins"
                if ckpe_plugins.is_dir():
                    _wlog("CKPEPlugins/ present in game root (from CKPE mod).")
                else:
                    try:
                        ckpe_plugins.mkdir(exist_ok=True)
                        _wlog("CKPEPlugins/ missing - created it in the game "
                              "root (manual CKPE install fallback).")
                    except OSError as exc:
                        _wlog(f"could not create CKPEPlugins/: {exc}")

                _wlog(f"launching {exe} via Proton from {game_path}")
                safe_emit(self._run_status_sig,
                          self.tr("Creation Kit is running.\nClose it when you are "
                          "done, then click Done."), GREEN)
                safe_emit(self._run_started_sig)
                run_tool_logged(proton_script, exe, env, log_fn=_wlog,
                                cwd=game_path, label="Creation Kit")
                shutdown_prefix_wineserver(proton_script, compat_data,
                                           log_fn=_wlog)
                _wlog("Creation Kit closed.")
                safe_emit(self._run_status_sig, self.tr("Creation Kit finished."), GREEN)
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig, self.tr("Launch error: {0}").format(exc), RED)
                self._log(f"Creation Kit Wizard: launch error: {exc}")

        threading.Thread(target=worker, daemon=True, name="ck-run").start()

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)
