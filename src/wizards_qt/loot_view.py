"""External LOOT wizard - runs the official Windows LOOT build under Proton.

Downloads the latest win64 release from GitHub into
Profiles/<game>/Applications/LOOT/, deploys, then runs LOOT.exe in a tool
prefix prepared like xEdit's (Installed Path in the registry, plugins.txt
copy + My Games linked in). LOOT sorts that plugins.txt copy; when it closes
the new order is handed to the plugin panel (ctx.apply_plugin_order), which
applies it like the built-in Sort button - locks, master block and the
engine's primary plugins are kept.

LOOT itself is more than a plugin sorter: it also points out dirty plugins,
missing masters and other load order problems before the game runs.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal

from gui_qt.runtime.safe_emit import safe_emit
from Utils.bethesda.loot import (
    APP_DIR, ARCHIVE_KEYWORDS, EXE_NAME, GITHUB_API_URL,
)
from Utils.bethesda.xedit import tool_exe_path
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_INSTALL, _DEPLOY, _PROTON, _RUN = range(4)


class LootView(WizardViewBase):
    """Install, deploy and run LOOT, then apply its load order."""

    _install_status_sig = Signal(str, str)
    _install_done_sig = Signal(bool)
    _loot_done_sig = Signal(object, str)   # (LOOT's order or None, baseline)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, loot_game: str = "", **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run LOOT - {0}").format(game.name))
        self._exe = tool_exe_path(game, EXE_NAME, APP_DIR)
        self._loot_game = loot_game
        self._proton_name = ""
        self._prefix_mode = ""
        self._upgrading = False

        self._install_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._install_status, t, c)))
        self._install_done_sig.connect(self._guard(self._on_install_done))
        self._loot_done_sig.connect(self._guard(self._on_loot_done))

        # page 0: install
        page, lay = self._step_page(self.tr("Step 1: Install LOOT"))
        self._make_note(lay, self.tr(
            "Download the latest LOOT release for Windows into this game's "
            "Applications folder. LOOT runs under Proton."))
        self._install_status = self._make_status(lay)
        lay.addStretch(1)
        self._install_btn = self._accent_btn(self.tr("Install"))
        self._install_btn.clicked.connect(self._start_install)
        lay.addWidget(self._install_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)
        # page 1: deploy
        self._stack.addWidget(self._build_deploy_page(
            self.tr("Step 2: Deploy Modlist"),
            self.tr("Deploy the modlist so LOOT sees the plugins of the "
                    "active profile."),
            lambda: self._goto_step(_PROTON)))
        # page 2: proton
        self._stack.addWidget(self._build_proton_holder())
        # page 3: run
        page, lay = self._step_page(self.tr("Step 4: Run LOOT"))
        self._make_note(lay, self.tr(
            "Sort in LOOT and click Apply there, then close LOOT. The new "
            "load order is saved to the active profile, and rules you add "
            "in LOOT are shared with Sort Plugins."))
        self._run_status = self._make_status(lay)
        lay.addStretch(1)
        self._done_btn = self._green_btn(self.tr("Done"))
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self._finish)
        lay.addWidget(self._done_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)

        if self._exe is not None:
            self._goto_step(_DEPLOY)
            self._offer_tool_upgrade(_DEPLOY, self._start_upgrade)
        else:
            self._goto_step(_INSTALL)

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PROTON:
            self._enter_proton(
                self._exe, EXE_NAME, "LOOT", self._on_proton_chosen,
                title=self.tr("Step 3: Choose Proton Version"),
                missing_text=self.tr("'{0}' was not found.\n"
                                     "Please restart the wizard to reinstall "
                                     "LOOT.").format(EXE_NAME))
        elif idx == _RUN:
            self._start_run()

    # ---- install ------------------------------------------------------------------
    def _start_upgrade(self):
        self._upgrading = True
        self._goto_step(_INSTALL)

    def _start_install(self):
        self._install_btn.setEnabled(False)
        if self._upgrading:
            from Utils.bethesda.loot import remove_loot_install
            try:
                remove_loot_install(self._game,
                                    lambda m: self._log(f"LOOT Wizard: {m}"))
            except OSError as exc:
                self._set_status(self._install_status,
                                 self.tr("Could not remove the old LOOT: {0}").format(exc), RED)
                self._install_btn.setEnabled(True)
                return
            self._exe = None
        self._github_install_worker(
            GITHUB_API_URL, ARCHIVE_KEYWORDS, APP_DIR, EXE_NAME, "LOOT",
            self._install_status_sig, self._install_done_sig)

    def _on_install_done(self, ok: bool):
        if ok:
            self._upgrading = False
            self._goto_step(_DEPLOY)
        else:
            self._install_btn.setEnabled(True)

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_RUN)

    # ---- run ----------------------------------------------------------------------
    def _set_context_lock(self, held: bool):
        hook = getattr(self._ctx, "set_tool_lock", None)
        if hook is not None:
            hook("loot", "LOOT", held)

    def _start_run(self):
        exe, game = self._exe, self._game
        if exe is None:
            self._set_status(self._run_status,
                             self.tr("'{0}' was not found.").format(EXE_NAME), RED)
            return
        if not self._loot_game:
            self._set_status(self._run_status,
                             self.tr("LOOT does not know this game."), RED)
            return
        profile = getattr(self._ctx, "profile_name", None) or "default"
        try:
            deployed = game.get_last_deployed_profile() or ""
        except Exception:
            deployed = ""
        if deployed and deployed != profile:
            self._set_status(self._run_status, self.tr(
                "Profile '{0}' is deployed, not '{1}'. Deploy the active "
                "profile first.").format(deployed, profile), RED)
            return
        self._set_status(self._run_status, self.tr("Launching LOOT…"))
        self._set_context_lock(True)
        self._lock_close(True, self.tr("LOOT is preparing or running."))
        proton_name, prefix_mode, loot_game = (
            self._proton_name, self._prefix_mode, self._loot_game)
        profile_plugins = (game.get_profile_root() / "profiles" / profile
                           / "plugins.txt")

        def worker():
            from Utils.bethesda.loot import (
                loot_launch_args, loot_plugins_txt, prepare_loot_local_dir,
                pull_userlist, push_userlist, read_loot_order,
                seed_loadorder_txt, write_loot_game_settings,
            )
            from Utils.bethesda.xedit import prepare_xedit_prefix
            from Utils.executables.launch import (
                PREFIX_MODE_GAME, resolve_tool_prefix, run_tool_logged,
                shutdown_prefix_wineserver,
            )
            from Utils.wine.protontricks import (
                VCREDIST_DEP_KEY, install_vcredist, is_dep_installed,
            )
            _wlog = lambda m: self._log(f"LOOT Wizard: {m}")
            proton_script = compat_data = None
            order = None
            baseline = ""
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    if prefix_mode == PREFIX_MODE_GAME:
                        raise RuntimeError(self.tr(
                            "Could not resolve the Proton version for the "
                            "game's own prefix - launch the game once, or pick "
                            "a different prefix option."))
                    raise RuntimeError(self.tr(
                        "Could not find Proton '{0}' - check that it is "
                        "installed in Steam, Heroic or ProtonPlus.").format(
                            proton_name))
                proton_script, compat_data, env = result
                pfx = compat_data / "pfx"

                # The release ships no VC++ runtime DLLs.
                if not is_dep_installed(pfx, VCREDIST_DEP_KEY):
                    safe_emit(self._run_status_sig,
                              self.tr("Installing Visual C++ x64…"), "")
                    if not install_vcredist(proton_script, env, log_fn=_wlog,
                                            prefix_path=pfx):
                        raise RuntimeError(self.tr(
                            "Visual C++ x64 install failed. See log."))

                prepare_loot_local_dir(game, pfx)
                prepare_xedit_prefix(game, compat_data, proton_script, env,
                                     log_fn=_wlog)
                loot_txt = loot_plugins_txt(game, pfx)
                if loot_txt is None:
                    raise RuntimeError(self.tr(
                        "plugins.txt could not be copied into LOOT's prefix."))
                # Some games deploy plugins.txt read-only; LOOT must write it.
                loot_txt.chmod(0o644)
                seed_loadorder_txt(game, pfx, profile, log_fn=_wlog)
                write_loot_game_settings(game, pfx, loot_game, log_fn=_wlog)
                userlist_base = push_userlist(game, profile, loot_game,
                                              log_fn=_wlog)
                before = read_loot_order(game, pfx)
                baseline = profile_plugins.read_text(encoding="utf-8")

                args = loot_launch_args(game, pfx, loot_game)
                _wlog(f"launching {exe} via Proton with {' '.join(args)}")
                safe_emit(self._run_status_sig, self.tr(
                    "LOOT is running.\nSort, click Apply, then close LOOT."),
                    GREEN)
                safe_emit(self._run_started_sig)
                code = run_tool_logged(proton_script, exe, env, log_fn=_wlog,
                                       extra_args=args, label="LOOT",
                                       game=game, owner=self)
                if code:
                    _wlog(f"LOOT exited with code {code}.")
                pull_userlist(game, profile, loot_game, userlist_base,
                              log_fn=_wlog)
                after = read_loot_order(game, pfx)
                if after and [n.lower() for n in after] != [n.lower() for n in before]:
                    order = after
                    _wlog(f"LOOT changed the load order ({len(after)} plugins).")
                else:
                    _wlog("LOOT left the load order unchanged.")
                safe_emit(self._run_status_sig, self.tr("LOOT closed."), GREEN)
            except Exception as exc:
                safe_emit(self._run_status_sig,
                          self.tr("LOOT error: {0}").format(exc), RED)
                _wlog(f"error: {exc}")
            finally:
                try:
                    if proton_script is not None and compat_data is not None:
                        shutdown_prefix_wineserver(proton_script, compat_data,
                                                   log_fn=_wlog)
                finally:
                    safe_emit(self._loot_done_sig, order, baseline)

        threading.Thread(target=worker, daemon=True, name="loot-run").start()

    def _on_run_started(self):
        self._ran = True

    def _on_loot_done(self, order, baseline: str):
        self._set_context_lock(False)
        self._lock_close(False)
        self._done_btn.setEnabled(True)
        if not order:
            return
        apply = getattr(self._ctx, "apply_plugin_order", None)
        if apply is None:
            self._set_status(self._run_status, self.tr(
                "LOOT changed the load order, but it can't be applied "
                "here."), RED)
            return
        if apply(order, baseline):
            self._set_status(self._run_status, self.tr(
                "LOOT's load order was saved to the profile."), GREEN)
        else:
            self._set_status(self._run_status, self.tr(
                "LOOT's load order was not applied - see the notification."),
                RED)
