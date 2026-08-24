"""Download and run MulderLoad's Fallout 4 Steam Downgrader.

MulderLoad publishes several applications in the same repository, so the
wizard searches releases newest-first for the most recent release that
actually contains ``fallout-4-steam-downgrader.exe``. The executable is
stored and run from the Fallout 4 game root. No modlist deploy is needed.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_GITHUB_RELEASES_API = "https://api.github.com/repos/Mulderland/MulderLoad/releases"
_EXE_NAME = "fallout-4-steam-downgrader.exe"

_PG_DOWNLOAD, _PG_PROTON, _PG_RUN = range(3)


def _isolated_prefix_dir(proton_name: str) -> Path:
    """Keep an optional tool prefix out of the Fallout 4 install folder."""
    from Utils.config_paths import get_wine_prefixes_dir
    return get_wine_prefixes_dir() / f"prefix_fallout4_downgrader_{proton_name}"


class Fallout4DowngraderView(WizardViewBase):
    """Fetch the newest available full downgrader and run it via Proton."""

    _download_status_sig = Signal(str, str)
    _download_done_sig = Signal(bool)

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(
            game, log_fn, on_close, ctx,
            title=self.tr("Downgrade Fallout 4 - {0}").format(game.name))
        game_root = game.get_game_path()
        self._exe = (Path(game_root) / _EXE_NAME
                     if game_root is not None else None)
        self._proton_name = ""
        self._prefix_mode = ""

        self._download_status_sig.connect(self._guard(
            lambda text, color: self._set_status(
                self._download_status, text, color)))
        self._download_done_sig.connect(self._guard(self._on_download_done))

        page, lay = self._step_page(
            self.tr("Step 1: Download Fallout 4 Downgrader"))
        self._make_note(lay, self.tr(
            "The newest release containing the Fallout 4 Steam Downgrader "
            "will be downloaded from MulderLoad on GitHub and placed in the "
            "game folder.\n\nNo modlist deploy is required."))
        self._download_status = self._make_status(lay)
        lay.addStretch(1)
        self._stack.addWidget(page)

        self._stack.addWidget(self._build_proton_holder())
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 3: Run Fallout 4 Downgrader")))

        self._stack.setCurrentIndex(_PG_DOWNLOAD)
        self._start_download()

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_PROTON:
            from Utils.exe_launch import PREFIX_MODE_GAME
            self._enter_proton(
                self._exe, _EXE_NAME, "Fallout 4 Steam Downgrader",
                self._on_proton_chosen,
                isolated_prefix_dir_fn=_isolated_prefix_dir,
                default_prefix_mode=PREFIX_MODE_GAME,
                title=self.tr("Step 2: Choose Proton Version"),
                missing_text=self.tr(
                    "The Fallout 4 Steam Downgrader was not downloaded.\n"
                    "Close and reopen the wizard to try again."))
        elif idx == _PG_RUN:
            self._start_run()

    # ---- download ---------------------------------------------------------------
    def _start_download(self):
        game_root = self._game.get_game_path()

        def worker():
            from Utils.ca_bundle import download_file
            from Utils.wizard_archives import fetch_newest_github_asset

            try:
                if game_root is None or not Path(game_root).is_dir():
                    raise RuntimeError(self.tr("Game path is not configured."))

                safe_emit(
                    self._download_status_sig,
                    self.tr("Searching MulderLoad releases…"), "")
                tag, url = fetch_newest_github_asset(
                    _GITHUB_RELEASES_API, [_EXE_NAME], extensions={".exe"})
                target = Path(game_root) / _EXE_NAME
                safe_emit(
                    self._download_status_sig,
                    self.tr("Downloading {0}…").format(tag), "")
                self._log(
                    f"Fallout 4 Downgrader Wizard: downloading {tag} "
                    f"from {url} → {target}")
                download_file(url, target)
                if not target.is_file():
                    raise RuntimeError(self.tr(
                        "The downgrader download did not create {0}.").format(
                            _EXE_NAME))
                self._exe = target
                safe_emit(
                    self._download_status_sig,
                    self.tr("Downloaded {0} to the game folder.").format(tag),
                    GREEN)
                safe_emit(self._download_done_sig, True)
            except Exception as exc:
                safe_emit(
                    self._download_status_sig,
                    self.tr("Download error: {0}").format(exc), RED)
                self._log(f"Fallout 4 Downgrader Wizard: download error: {exc}")
                safe_emit(self._download_done_sig, False)

        threading.Thread(
            target=worker, daemon=True, name="fo4-downgrader-download").start()

    def _on_download_done(self, ok: bool):
        if ok:
            self._goto_step(_PG_PROTON)

    def _on_proton_chosen(self, proton_name: str, prefix_mode: str):
        self._proton_name = proton_name
        self._prefix_mode = prefix_mode
        self._goto_step(_PG_RUN)

    # ---- run --------------------------------------------------------------------
    def _start_run(self):
        exe, game = self._exe, self._game
        if exe is None or not exe.is_file():
            self._set_status(
                self._run_status,
                self.tr("{0} was not found in the game folder.").format(
                    _EXE_NAME),
                RED)
            return

        self._set_status(
            self._run_status, self.tr("Launching Fallout 4 Downgrader…"))
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.exe_launch import (
                resolve_tool_prefix, run_tool_logged,
                shutdown_prefix_wineserver,
            )
            log = lambda message: self._log(
                f"Fallout 4 Downgrader Wizard: {message}")
            proton_script = compat_data = None
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=log,
                    isolated_prefix_dir=_isolated_prefix_dir(proton_name))
                if result is None:
                    safe_emit(
                        self._run_status_sig,
                        self.tr("Could not determine a Proton version for "
                                "Fallout 4."), RED)
                    return
                proton_script, compat_data, env = result
                log(f"launching {exe} via Proton")
                safe_emit(
                    self._run_status_sig,
                    self.tr("Fallout 4 Downgrader is running.\n"
                            "Follow its prompts, then close it when finished."),
                    GREEN)
                safe_emit(self._run_started_sig)
                returncode = run_tool_logged(
                    proton_script, exe, env, log_fn=log, cwd=exe.parent,
                    label="Fallout 4 Steam Downgrader", owner=self)
                if returncode == 0:
                    safe_emit(
                        self._run_status_sig,
                        self.tr("Fallout 4 Downgrader finished. Click Done to "
                                "close."), GREEN)
                else:
                    safe_emit(
                        self._run_status_sig,
                        self.tr("Fallout 4 Downgrader exited with code {0}. "
                                "See the log for details.").format(returncode),
                        RED)
            except Exception as exc:
                safe_emit(
                    self._run_status_sig,
                    self.tr("Launch error: {0}").format(exc), RED)
                log(f"launch error: {exc}")
            finally:
                if proton_script is not None and compat_data is not None:
                    shutdown_prefix_wineserver(
                        proton_script, compat_data, log_fn=log)

        threading.Thread(
            target=worker, daemon=True, name="fo4-downgrader-run").start()

    def _on_run_started(self):
        self._done_btn.setEnabled(True)
