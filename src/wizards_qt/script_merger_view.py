"""Witcher 3 Script Merger wizard - Qt port of wizards/script_merger_tw3.py.

Deploy → manual Nexus download → locate (sm-fae archive) → extract to
Applications/ScriptMerger/ → choose Proton version/prefix (shared wizard
step) → install .NET 8 into the chosen prefix (dep-marked in
amethyst_deps.json) → run WitcherScriptMerger.exe.  On Done the game is
restored so merged files are rescued into staging, then the modlist
refreshes.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase
from Utils.xedit_tools import tool_exe_path

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_NEXUS_URL = "https://www.nexusmods.com/witcher3/mods/8405?tab=files&file_id=59566"
_NEXUS_FILE_ID = 59566
_MERGER_EXE = "WitcherScriptMerger.exe"
_MERGER_DIR = "ScriptMerger"
# .NET 8 install runs through Utils.proton_tools.install_dotnet_runtime.

(_PG_DEPLOY, _PG_DOWNLOAD, _PG_LOCATE, _PG_EXTRACT, _PG_PROTON, _PG_NET8,
 _PG_RUN) = range(7)


class ScriptMergerView(WizardViewBase):
    """Deploy mods and run WitcherScriptMerger."""

    _net8_status_sig = Signal(str, str)
    _net8_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Run Script Merger - {0}").format(game.name))
        self._proton_name = ""
        self._prefix_mode = ""
        self._prefix_env = None
        self._collateral_keys: set = set()

        self._net8_status_sig.connect(self._guard(
            lambda t, c: self._set_status(self._net8_status, t, c)))
        self._net8_done_sig.connect(self._guard(self._on_net8_done))

        # page 0: deploy (auto-start + Skip - Tk parity)
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QPushButton
        page, lay = self._step_page(self.tr("Step 1: Deploy Modlist"))
        self._deploy_status = self._make_status(lay)
        lay.addStretch(1)
        skip = QPushButton(self.tr("Skip"))
        skip.setCursor(Qt.PointingHandCursor)
        skip.clicked.connect(self._advance_from_deploy)
        lay.addWidget(skip, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)
        # page 1: manual download
        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 2: Download Script Merger"),
            self.tr("Click the button below to open the Script Merger download page\n"
            "on Nexus Mods, then download the archive.\n\n"
            "Once downloaded, click Next."),
            _NEXUS_URL,
            lambda: self._goto_step(_PG_LOCATE)))
        # page 2: locate
        self._stack.addWidget(self._build_locate_page(
            self.tr("Step 3: Locate the Archive")))
        # page 3: extract
        self._stack.addWidget(self._build_extract_page(
            self.tr("Step 4: Extract Script Merger")))
        # page 4: proton
        self._stack.addWidget(self._build_proton_holder())
        # page 5: .NET 8
        page, lay = self._step_page(self.tr("Step 6: Install .NET 8"))
        self._net8_status = self._make_status(lay)
        lay.addStretch(1)
        self._stack.addWidget(page)
        # page 6: run
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 7: Run Script Merger")))

        self._goto_step(_PG_DEPLOY)

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_DEPLOY:
            self._run_ctx_deploy(self._deploy_status, self._advance_from_deploy)
        elif idx == _PG_LOCATE:
            self._enter_locate(
                ["sm-fae"], self.tr("Select the Script Merger archive"),
                self.tr("Script Merger archive not found in Downloads.\n"
                "Make sure you downloaded it, then press Try Again,\n"
                "or use Browse to select it manually."),
                lambda _p: self._goto_step(_PG_EXTRACT))
        elif idx == _PG_EXTRACT:
            self._extract_to_applications(_MERGER_DIR, _MERGER_EXE,
                                          "Script Merger")
        elif idx == _PG_PROTON:
            self._exe = tool_exe_path(self._game, _MERGER_EXE, _MERGER_DIR)
            self._enter_proton(
                self._exe, _MERGER_EXE, "Script Merger",
                self._on_proton_chosen,
                title=self.tr("Step 5: Choose Proton Version"),
                missing_text=self.tr("{0} was not found.\n"
                             "Please restart the wizard and install "
                             "Script Merger first.").format(_MERGER_EXE))
        elif idx == _PG_NET8:
            self._set_status(self._net8_status, self.tr("Checking .NET 8…"))
            self._start_net8()
        elif idx == _PG_RUN:
            self._preflight_run()

    def _advance_from_deploy(self):
        # Skip download step if WitcherScriptMerger.exe is already present.
        if tool_exe_path(self._game, _MERGER_EXE, _MERGER_DIR) is not None:
            self._goto_step(_PG_PROTON)
        else:
            self._goto_step(_PG_DOWNLOAD)
            self._nexus_auto_fetch(
                url=_NEXUS_URL, file_id=_NEXUS_FILE_ID,
                keywords=["sm-fae"], label="Script Merger",
                pages=(_PG_DOWNLOAD, _PG_LOCATE),
                on_archive=lambda _p: self._goto_step(_PG_EXTRACT))

    def _on_extract_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_PROTON)

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_NET8)

    # ---- .NET 8 into the chosen tool prefix ------------------------------------------
    def _start_net8(self):
        game = self._game
        exe = self._exe
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.exe_launch import PREFIX_MODE_GAME, resolve_tool_prefix
            from Utils.protontricks import dotnet_dep_key, is_dep_installed
            _wlog = lambda m: self._log(f"Script Merger Wizard: {m}")
            try:
                safe_emit(self._net8_status_sig,
                          self.tr("Preparing Script Merger's Wine prefix…"), "")
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    if prefix_mode == PREFIX_MODE_GAME:
                        safe_emit(self._net8_status_sig,
                            self.tr("Could not resolve the Proton version for "
                            "the game's own prefix - launch the game once, or "
                            "pick a different prefix option."), RED)
                    else:
                        safe_emit(self._net8_status_sig,
                            self.tr("Could not find Proton '{0}' - check that "
                            "it is installed in Steam, Heroic or ProtonPlus, "
                            "then reopen this wizard.").format(proton_name), RED)
                    safe_emit(self._net8_done_sig, False)
                    return
                self._prefix_env = result
                proton_script, compat_data, env = result
                prefix_path = compat_data / "pfx"

                net8_key = dotnet_dep_key("8")
                if is_dep_installed(prefix_path, net8_key):
                    safe_emit(self._net8_status_sig,
                              self.tr(".NET 8 already installed - skipping."),
                              GREEN)
                    safe_emit(self._net8_done_sig, True)
                    return

                from Utils.proton_tools import install_dotnet_runtime
                ok = install_dotnet_runtime(
                    "8", proton_script, env, prefix_path,
                    log_fn=_wlog,
                    status_fn=lambda m: safe_emit(self._net8_status_sig, m, ""))
                if not ok:
                    raise RuntimeError(
                        self.tr(".NET 8 install failed (see log)."))
                safe_emit(self._net8_status_sig,
                          self.tr(".NET 8 ready."), GREEN)
                safe_emit(self._net8_done_sig, True)
            except Exception as exc:
                safe_emit(self._net8_status_sig,
                          self.tr("Error: {0}").format(exc), RED)
                self._log(f"Script Merger Wizard: .NET 8 install error: {exc}")
                safe_emit(self._net8_done_sig, False)

        threading.Thread(target=worker, daemon=True, name="tw3sm-net8").start()

    def _on_net8_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_RUN)

    # ---- run ----------------------------------------------------------------------
    # Script Merger's only record of existing merges is MergeInventory.xml
    # next to its exe; on launch it DELETES entries whose merged file or
    # source mods are missing from the deployed game folder - and doing
    # that with the merged mod deployed crashes it under Wine (unhandled
    # IOException in DeleteEmptyDirs) and leaves half-deleted merges.
    # Restore the profile's snapshot first, then if any recorded merge
    # references mods that are not deployed offer the only two safe paths:
    # cancel (re-enable the mods) or purge the merge state and re-create
    # the merges from what is enabled.
    def _preflight_run(self):
        _wlog = lambda m: self._log(f"Script Merger Wizard: {m}")
        missing: list[tuple[str, list[str]]] = []
        try:
            from Utils.script_merger_inventory import (
                missing_merge_sources, restore_inventory,
            )
            restore_inventory(self._game, log_fn=_wlog)
            missing = missing_merge_sources(self._game)
            # Capture, from the restored inventory, which merges have
            # undeployed sources - the Done-snapshot keeps these if the
            # merger drops them, instead of pruning them as user deletions.
            from Utils.script_merger_inventory import collateral_keys
            self._collateral_keys = collateral_keys(self._game)
        except Exception as exc:
            _wlog(f"merge inventory check warning: {exc}")
        if not missing:
            self._launch_merger()
            return

        _wlog(f"{len(missing)} merge(s) reference mods that are not deployed.")
        lines = [
            "  • {0}  ({1})".format(rel, ", ".join(mods))
            for rel, mods in missing[:6]
        ]
        if len(missing) > 6:
            lines.append("  • …")
        from gui_qt.confirm_overlay import ConfirmOverlay
        ConfirmOverlay.show_over(
            self, self.tr("Existing Merges Use Missing Mods"),
            self.tr(
                "{0} existing merge(s) use mods that are not currently "
                "deployed (disabled or removed):\n\n{1}\n\n"
                "Script Merger cannot run safely in this state.\n\n"
                "Cancel and re-enable the listed mods to keep the merges, "
                "or delete the existing merges (removes the Merged_Mods "
                "mod) and re-create them in this run from the mods that "
                "are enabled."
            ).format(len(missing), "\n".join(lines)),
            lambda ok: self._purge_and_launch() if ok else self._cancel_launch(),
            confirm_label=self.tr("Delete Merges"),
            card_h=420)

    def _purge_and_launch(self):
        _wlog = lambda m: self._log(f"Script Merger Wizard: {m}")
        try:
            from Utils.script_merger_inventory import purge_merges
            purge_merges(self._game, log_fn=_wlog)
        except Exception as exc:
            _wlog(f"merge purge warning: {exc}")
        # Purge wiped all prior state; nothing to preserve as collateral.
        self._collateral_keys = set()
        self._launch_merger()

    def _launch_merger(self):
        self._set_status(self._run_status,
                         self.tr("Launching WitcherScriptMerger…"))
        self._start_run()

    def _cancel_launch(self):
        self._set_status(
            self._run_status,
            self.tr("Launch cancelled - re-enable the merges' source mods, "
                    "then reopen this wizard."),
            RED)

    def _start_run(self):
        game = self._game
        exe = tool_exe_path(game, _MERGER_EXE, _MERGER_DIR)
        if exe is None:
            self._set_status(
                self._run_status,
                self.tr('{0} was not found.\nPlease restart the wizard and install Script Merger first.').format(_MERGER_EXE),
                RED)
            return

        proton_name, prefix_mode = self._proton_name, self._prefix_mode
        prefix_env = self._prefix_env

        def worker():
            from Utils.exe_launch import (
                PREFIX_MODE_GAME, link_game_documents, resolve_tool_prefix,
                run_tool_logged, shutdown_prefix_wineserver,
            )
            _wlog = lambda m: self._log(f"Script Merger Wizard: {m}")
            proton_script = compat_data = None
            try:
                result = prefix_env or resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=_wlog)
                if result is None:
                    if prefix_mode == PREFIX_MODE_GAME:
                        safe_emit(self._run_status_sig,
                            self.tr("Could not resolve the Proton version for "
                            "the game's own prefix - launch the game once, or "
                            "pick a different prefix option."), RED)
                    else:
                        safe_emit(self._run_status_sig,
                            self.tr("Could not find Proton '{0}' - check that "
                            "it is installed in Steam, Heroic or "
                            "ProtonPlus.").format(proton_name), RED)
                    return
                proton_script, compat_data, env = result

                # Script Merger watches Documents\The Witcher 3 (the mod load
                # order) and crashes if it's missing - a fresh tool prefix has
                # no such folder, so link the game prefix's real one in.
                link_game_documents(game, compat_data / "pfx",
                                    game.name, log_fn=_wlog)

                game_path = game.get_game_path()
                if game_path:
                    env["STEAM_COMPAT_INSTALL_PATH"] = str(game_path)

                # Update Script Merger config to point at the game folder.
                if game_path:
                    try:
                        from Utils.exe_args_builder import (
                            update_witcher3_script_merger_config,
                        )
                        update_witcher3_script_merger_config(game_path, exe)
                        _wlog("updated Script Merger config with game path.")
                    except Exception as cfg_exc:
                        _wlog(f"config update warning: {cfg_exc}")

                _wlog(f"launching {exe} via Proton")
                safe_emit(self._run_status_sig,
                          self.tr("WitcherScriptMerger is running.\nMerge your "
                          "conflicts, then close it and click Done."), GREEN)
                safe_emit(self._run_started_sig)
                run_tool_logged(proton_script, exe, env, log_fn=_wlog,
                                label="WitcherScriptMerger", game=game)
                _wlog("WitcherScriptMerger closed.")
                safe_emit(self._run_status_sig,
                          self.tr("WitcherScriptMerger closed."), GREEN)
                safe_emit(self._run_finished_sig)
            except Exception as exc:
                safe_emit(self._run_status_sig,
                          self.tr("Launch error: {0}").format(exc), RED)
                self._log(f"Script Merger Wizard: launch error: {exc}")
            finally:
                # In finally: a tool that crashed is exactly when Proton
                # sidecars are most likely to be left holding the prefix.
                if proton_script is not None and compat_data is not None:
                    shutdown_prefix_wineserver(proton_script, compat_data,
                                               log_fn=_wlog)

        threading.Thread(target=worker, daemon=True, name="tw3sm-run").start()

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)

    # ---- close: restore-before-close (Tk parity) --------------------------------
    # Merged files land in the deployed game folder; game.restore() rescues
    # them into staging.  It must complete BEFORE the view closes so the
    # follow-up ctx.refresh_modlist (GUI thread, from the base _finish) sees
    # them - marshalling after teardown would target a deleted view.
    _restore_done_sig = Signal()

    def _finish(self):
        if self._closing:
            return
        if self._ran and not getattr(self, "_restored", False):
            if getattr(self, "_restoring", False):
                return  # restore in flight - closes itself when done
            self._restoring = True
            self._done_btn.setEnabled(False)
            self._set_status(self._run_status,
                             self.tr("Restoring game files (rescuing merges)…"))
            self._restore_done_sig.connect(self._guard(self._on_restore_done))
            game, log = self._game, self._log
            keep_keys = getattr(self, "_collateral_keys", set())

            def worker():
                try:
                    game.restore(log_fn=log)
                except Exception as exc:
                    log(f"Script Merger Wizard: restore warning: {exc}")
                # Pair the rescued merged files with the merger's inventory -
                # without it the merger forgets these merges exist.
                try:
                    from Utils.script_merger_inventory import snapshot_inventory
                    snapshot_inventory(
                        game, keep_keys=keep_keys,
                        log_fn=lambda m: log(f"Script Merger Wizard: {m}"))
                except Exception as exc:
                    log(f"Script Merger Wizard: inventory snapshot warning: {exc}")
                safe_emit(self._restore_done_sig)

            threading.Thread(target=worker, daemon=True,
                             name="tw3sm-restore").start()
            return
        super()._finish()

    def _on_restore_done(self):
        self._restored = True
        self._restoring = False
        self._finish()
