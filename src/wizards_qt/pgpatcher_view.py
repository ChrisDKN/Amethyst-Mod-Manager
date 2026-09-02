"""PGPatcher wizard - Qt port of wizards/pgpatcher.py.

Auto-downloads PGPatcher from GitHub → Proton step → installs
d3dcompiler_47 + .NET 8 into the tool prefix → applies the settings.json
bootstrap → delete-old-output prompt + deploy (with the optional per-mod
conflict-resolution MO2-parity dummy instance) → runs PGPatcher.exe via
Proton (registry seed + plugins.txt link first; `--ignore-mo2vfscheck` in
MO2 mode).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPushButton, QWidget

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.bethesda.xedit import tool_exe_path

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_GITHUB_API = "https://api.github.com/repos/hakasapl/PGPatcher/releases/latest"
_PATCHER_EXE = "PGPatcher.exe"
_PATCHER_DIR = "PGPatcher"
# .NET 8 install runs through Utils.wine.proton.install_dotnet_runtime.
_AMBER = "#e0a83c"

_PG_DOWNLOAD, _PG_PROTON, _PG_DEPS, _PG_CONFIG, _PG_DEPLOY, _PG_RUN = range(6)


class PGPatcherView(WizardViewBase):
    """Set up and run PGPatcher for Skyrim SE."""

    _dl_status_sig = Signal(str, str)
    _dl_done_sig = Signal(bool)
    _d3d_status_sig = Signal(str, str)
    _net8_status_sig = Signal(str, str)
    _deps_done_sig = Signal(bool)
    _config_status_sig = Signal(str, str)
    _config_done_sig = Signal(bool)
    _deploy_status_sig = Signal(str, str)
    _goto_run_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run PGPatcher - {0}").format(game.name))
        self._exe = tool_exe_path(game, _PATCHER_EXE, _PATCHER_DIR)
        self._proton_name = ""
        self._prefix_mode = ""
        self._prefix_env = None     # (proton_script, compat_data, env)
        # Per-mod conflict resolution via a dummy MO2 instance (opt-in).
        self._mo2_dummy_dir: "Path | None" = None
        self._mo2_game_type: int = 0
        self._use_mo2_parity = False

        for sig, slot in (
            (self._dl_status_sig, lambda t, c: self._set_status(self._dl_status, t, c)),
            (self._dl_done_sig, self._on_dl_done),
            (self._d3d_status_sig, lambda t, c: self._set_status(self._d3d_status, t, c)),
            (self._net8_status_sig, lambda t, c: self._set_status(self._net8_status, t, c)),
            (self._deps_done_sig, self._on_deps_done),
            (self._config_status_sig, lambda t, c: self._set_status(self._config_status, t, c)),
            (self._config_done_sig, self._on_config_done),
            (self._deploy_status_sig, lambda t, c: self._set_status(self._deploy_status, t, c)),
            (self._goto_run_sig, lambda: self._goto_step(_PG_RUN)),
        ):
            sig.connect(self._guard(slot))

        # page 0: auto-download
        page, lay = self._step_page(self.tr("Step 1: Download PGPatcher"))
        self._dl_status = self._make_status(lay)
        lay.addStretch(1)
        self._stack.addWidget(page)
        # page 1: proton
        self._stack.addWidget(self._build_proton_holder())
        # page 2: deps
        page, lay = self._step_page(self.tr("Step 3: Install Dependencies"))
        self._d3d_status = self._make_status(lay)
        self._net8_status = self._make_status(lay)
        lay.addStretch(1)
        self._stack.addWidget(page)
        # page 3: config
        page, lay = self._step_page(self.tr("Step 4: Apply PGPatcher Config"))
        self._config_status = self._make_status(lay)
        lay.addStretch(1)
        self._stack.addWidget(page)
        # page 4: deploy + MO2 parity checkbox
        self._stack.addWidget(self._build_pg_deploy_page())
        # page 5: run
        self._stack.addWidget(self._build_run_page(self.tr("Step 6: Run PGPatcher")))

        if self._exe is not None:
            self._goto_step(_PG_PROTON)
        else:
            self._goto_step(_PG_DOWNLOAD)

    def _build_pg_deploy_page(self) -> QWidget:
        page, lay = self._step_page(self.tr("Step 5: Deploy Modlist"))
        self._make_note(lay, (
            self.tr("Before deploying, please delete any output from a previous\n"
            "PGPatcher run (the 'PGPatcher_output' mod in your mod list / "
            "staging folder).\n\nOnce you have done this, click Deploy.")))
        self._parity_chk = QCheckBox(self.tr("Per-mod conflict resolution (MO2 parity)"))
        lay.addWidget(self._parity_chk, 0, Qt.AlignHCenter)
        self._make_note(lay, (
            self.tr("Builds a synthetic MO2 instance so PGPatcher attributes\n"
            "conflicts per-mod, matching a real MO2 setup. Experimental.")))
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)
        row = QWidget()
        rh = QHBoxLayout(row); rh.setContentsMargins(0, 8, 0, 0); rh.setSpacing(8)
        rh.addStretch(1)
        self._deploy_skip_btn = QPushButton(self.tr("Skip"))
        self._deploy_skip_btn.setCursor(Qt.PointingHandCursor)
        self._deploy_skip_btn.clicked.connect(self._skip_deploy)
        rh.addWidget(self._deploy_skip_btn)
        self._deploy_btn = self._accent_btn(self.tr("Deploy"))
        self._deploy_btn.clicked.connect(self._start_pg_deploy)
        rh.addWidget(self._deploy_btn)
        rh.addStretch(1)
        lay.addWidget(row)
        return page

    # ---- routing ----------------------------------------------------------------
    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_DOWNLOAD:
            self._github_install_worker(
                _GITHUB_API, ["pgpatcher"], _PATCHER_DIR, _PATCHER_EXE,
                "PGPatcher", self._dl_status_sig, self._dl_done_sig)
        elif idx == _PG_PROTON:
            self._enter_proton(
                self._exe, _PATCHER_EXE, "PGPatcher", self._on_proton_chosen,
                title=self.tr("Step 2: Choose Proton Version"),
                missing_text=self.tr("{0} was not found.\n"
                             "Please restart the wizard and download "
                             "PGPatcher first.").format(_PATCHER_EXE))
        elif idx == _PG_DEPS:
            self._set_status(self._d3d_status, self.tr("Checking d3dcompiler_47…"))
            self._start_deps()
        elif idx == _PG_CONFIG:
            self._set_status(self._config_status, self.tr("Applying config…"))
            self._start_config()
        elif idx == _PG_RUN:
            self._set_status(self._run_status, self.tr("Launching PGPatcher…"))
            self._start_run()

    def _on_dl_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_PROTON)

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_DEPS)

    # ---- deps (d3dcompiler_47 + .NET 8) -------------------------------------------
    def _start_deps(self):
        exe, game = self._exe, self._game
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.executables.launch import PREFIX_MODE_GAME, resolve_tool_prefix
            from Utils.wine.protontricks import (
                D3D_DEP_KEY, dotnet_dep_key, install_d3dcompiler_47,
                is_dep_installed,
            )
            _wlog = lambda m: self._log(f"PGPatcher Wizard: {m}")
            try:
                safe_emit(self._d3d_status_sig,
                          self.tr("Preparing PGPatcher's Wine prefix…"), "")
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    if prefix_mode == PREFIX_MODE_GAME:
                        safe_emit(self._d3d_status_sig,
                            self.tr("Could not resolve the Proton version for "
                            "the game's own prefix - launch the game once, or "
                            "pick a different prefix option."), RED)
                    else:
                        safe_emit(self._d3d_status_sig,
                            self.tr("Could not find Proton '{0}' - check that "
                            "it is installed in Steam, Heroic or ProtonPlus, "
                            "then reopen this wizard.").format(proton_name), RED)
                    safe_emit(self._deps_done_sig, False)
                    return
                self._prefix_env = result
                proton_script, compat_data, env = result
                prefix_path = compat_data / "pfx"

                # --- d3dcompiler_47 ---
                if is_dep_installed(prefix_path, D3D_DEP_KEY):
                    safe_emit(self._d3d_status_sig,
                              self.tr("d3dcompiler_47 already installed - skipping."),
                              GREEN)
                else:
                    safe_emit(self._d3d_status_sig,
                              self.tr("Installing d3dcompiler_47… (may take a minute)"),
                              "")
                    # steam_id deliberately omitted: the protontricks fallback
                    # installs by app id into the game prefix, not this one.
                    ok = install_d3dcompiler_47("", log_fn=_wlog,
                                                prefix_path=prefix_path)
                    safe_emit(self._d3d_status_sig,
                              self.tr("d3dcompiler_47 installed.") if ok else
                              self.tr("d3dcompiler_47 install failed - continuing "
                              "anyway."),
                              GREEN if ok else _AMBER)

                # --- .NET 8 ---
                safe_emit(self._net8_status_sig, self.tr("Checking .NET 8…"), "")
                net8_key = dotnet_dep_key("8")
                if is_dep_installed(prefix_path, net8_key):
                    safe_emit(self._net8_status_sig,
                              self.tr(".NET 8 already installed - skipping."), GREEN)
                    safe_emit(self._deps_done_sig, True)
                    return

                from Utils.wine.proton import install_dotnet_runtime
                ok = install_dotnet_runtime(
                    "8", proton_script, env, prefix_path,
                    log_fn=_wlog,
                    status_fn=lambda m: safe_emit(self._net8_status_sig, m, ""))
                if not ok:
                    raise RuntimeError(self.tr(".NET 8 install failed (see log)."))
                safe_emit(self._net8_status_sig, self.tr(".NET 8 ready."), GREEN)
                safe_emit(self._deps_done_sig, True)
            except Exception as exc:
                safe_emit(self._net8_status_sig,
                          self.tr("Error: {0}").format(exc), RED)
                self._log(f"PGPatcher Wizard: .NET 8 install error: {exc}")
                safe_emit(self._deps_done_sig, False)

        threading.Thread(target=worker, daemon=True, name="pgpatcher-deps").start()

    def _on_deps_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_CONFIG)

    # ---- config ----------------------------------------------------------------
    def _start_config(self):
        exe, game = self._exe, self._game

        def worker():
            _wlog = lambda m: self._log(f"PGPatcher Wizard: {m}")
            try:
                if exe is None:
                    raise RuntimeError(self.tr(
                        "{0} not found - please restart the wizard.").format(
                            _PATCHER_EXE))
                from Utils.executables.arguments import _bootstrap_pgpatcher_settings
                _bootstrap_pgpatcher_settings(
                    exe,
                    game.get_game_path(),
                    game.get_effective_mod_staging_path(),
                    log_fn=_wlog,
                    update=True,
                )
                safe_emit(self._config_status_sig, self.tr("Config applied."), GREEN)
                safe_emit(self._config_done_sig, True)
            except Exception as exc:
                safe_emit(self._config_status_sig,
                          self.tr("Config error: {0}").format(exc), RED)
                self._log(f"PGPatcher Wizard: config error: {exc}")
                safe_emit(self._config_done_sig, False)

        threading.Thread(target=worker, daemon=True, name="pgpatcher-cfg").start()

    def _on_config_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_DEPLOY)

    # ---- deploy + MO2 dummy --------------------------------------------------------
    def _skip_deploy(self):
        # Honour the parity checkbox even when the user skips deploying.
        self._use_mo2_parity = self._parity_chk.isChecked()
        if self._use_mo2_parity:
            self._set_status(self._deploy_status, self.tr("Building MO2 instance…"))
            threading.Thread(target=self._build_dummy_then_run, daemon=True,
                             name="pgpatcher-mo2").start()
        else:
            self._goto_step(_PG_RUN)

    def _start_pg_deploy(self):
        self._use_mo2_parity = self._parity_chk.isChecked()
        self._deploy_btn.setEnabled(False)
        self._deploy_skip_btn.setEnabled(False)

        def _after_deploy():
            if self._use_mo2_parity:
                self._set_status(self._deploy_status, self.tr("Building MO2 instance…"))
                threading.Thread(target=self._build_dummy_then_run, daemon=True,
                                 name="pgpatcher-mo2").start()
            else:
                self._goto_step(_PG_RUN)

        def _re_enable():
            self._deploy_btn.setEnabled(True)
            self._deploy_skip_btn.setEnabled(True)

        if not self._run_ctx_deploy(self._deploy_status, _after_deploy,
                                    _re_enable):
            _re_enable()

    def _build_dummy_then_run(self):
        try:
            self._maybe_build_mo2_dummy()
        except Exception as exc:
            safe_emit(self._deploy_status_sig,
                      self.tr("MO2 instance error: {0}").format(exc), RED)
            self._log(f"PGPatcher Wizard: MO2 dummy error: {exc}")
            return
        safe_emit(self._goto_run_sig)

    def _maybe_build_mo2_dummy(self):
        """Build the dummy MO2 instance for the active profile and remember
        its path so the settings re-apply + launch switch PGPatcher into MO2
        mode.  Runs on a worker thread."""
        if not self._use_mo2_parity:
            self._mo2_dummy_dir = None
            return
        from Utils.bethesda.xedit import applications_dir
        from Utils.mo2.dummy import build_mo2_dummy_instance

        profile = getattr(self._ctx, "profile_name", None) or "default"
        apps_dir = applications_dir(self._game, _PATCHER_DIR)
        game_pfx = self._game.get_prefix_path()
        # The dummy's Z: paths resolve through the *tool* prefix; GOG
        # detection inspects the *game* prefix's AppData folders.
        tool_pfx = None
        if self._prefix_env is not None:
            tool_pfx = self._prefix_env[1] / "pfx"
        self._mo2_dummy_dir, self._mo2_game_type = build_mo2_dummy_instance(
            self._game, apps_dir, profile,
            prefix=tool_pfx or game_pfx,
            game_prefix=game_pfx,
            log_fn=lambda msg: self._log(f"PGPatcher Wizard: {msg}"),
        )

    # ---- run ----------------------------------------------------------------------
    def _start_run(self):
        exe, game = self._exe, self._game
        if exe is None:
            self._set_status(self._run_status,
                             self.tr("{0} was not found.").format(_PATCHER_EXE), RED)
            return
        proton_name, prefix_mode = self._proton_name, self._prefix_mode
        prefix_env = self._prefix_env
        mo2_dummy_dir, mo2_game_type = self._mo2_dummy_dir, self._mo2_game_type

        def worker():
            from Utils.bethesda.registry import maybe_register_for_game
            from Utils.executables.launch import (
                PREFIX_MODE_GAME, link_plugins_txt, resolve_tool_prefix,
                run_tool_logged, shutdown_prefix_wineserver,
            )
            _wlog = lambda m: self._log(f"PGPatcher Wizard: {m}")
            proton_script = compat_data = None
            try:
                result = prefix_env or resolve_tool_prefix(
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

                # Seed the game's Installed Path into the prefix registry -
                # Skyrim only writes it to the game prefix on first launch
                # (idempotent, marker-guarded).
                maybe_register_for_game(
                    prefix_dir=compat_data, proton_script=proton_script,
                    env=env, game=game, log_fn=_wlog)
                link_plugins_txt(game, compat_data / "pfx", _wlog)

                # Re-apply settings.json now that we know whether MO2 parity
                # is on and where the dummy instance lives - the config step
                # ran before the dummy was built, so this is the
                # authoritative write of modmanager.type.
                try:
                    from Utils.executables.arguments import _bootstrap_pgpatcher_settings
                    _bootstrap_pgpatcher_settings(
                        exe,
                        game.get_game_path(),
                        game.get_effective_mod_staging_path(),
                        log_fn=_wlog,
                        update=True,
                        pfx=compat_data / "pfx",
                        mo2_instance_dir=mo2_dummy_dir,
                        game_type=mo2_game_type if mo2_dummy_dir is not None else None,
                    )
                except Exception as exc:
                    _wlog(f"settings re-apply error: {exc}")

                # MO2 mode requires either real USVFS or this bypass on Linux.
                extra_args = (["--ignore-mo2vfscheck"]
                              if mo2_dummy_dir is not None else None)

                _wlog(f"launching {exe} via Proton")
                safe_emit(self._run_status_sig,
                          self.tr("PGPatcher is running.\nWait for it to finish, then "
                          "click Done."), GREEN)
                safe_emit(self._run_started_sig)
                run_tool_logged(proton_script, exe, env, log_fn=_wlog,
                                extra_args=extra_args, label="PGPatcher",
                                game=game, owner=self)
                _wlog("PGPatcher closed.")
                safe_emit(self._run_status_sig, self.tr("PGPatcher finished."), GREEN)
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig,
                          self.tr("Launch error: {0}").format(exc), RED)
                self._log(f"PGPatcher Wizard: launch error: {exc}")
            finally:
                # In finally: a tool that crashed is exactly when Proton
                # sidecars are most likely to be left holding the prefix.
                if proton_script is not None and compat_data is not None:
                    shutdown_prefix_wineserver(proton_script, compat_data,
                                               log_fn=_wlog)

        threading.Thread(target=worker, daemon=True, name="pgpatcher-run").start()

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)
