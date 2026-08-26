"""Download and run MulderLoad's Steam downgraders.

MulderLoad publishes several applications in the same repository, so the
wizard searches releases newest-first for the most recent release that
actually contains the requested downgrader.  Each game exposes two variants -
the game itself and its Creation Kit - which ship as separate installers from
the same releases page; the wizard asks which one to fetch before downloading.
The executable is stored and run from the game root.  No modlist deploy is
needed.

The downgrader unpacks a working set into the game folder - an ``@mulderload``
staging folder at the root plus ``.xdelta`` patch files alongside whatever they
patch (usually ``Data``) - and leaves all of it behind.  Those leftovers would
otherwise be swept into ``overwrite/`` by the next restore, so the wizard
removes them itself, together with its own downloaded exe, when it closes.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QRadioButton

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_GITHUB_RELEASES_API = "https://api.github.com/repos/Mulderland/MulderLoad/releases"

_STAGING_DIR = "@mulderload"

_PG_CHOOSE, _PG_DOWNLOAD, _PG_PROTON, _PG_RUN = range(4)


def _isolated_prefix_dir(slug: str, proton_name: str) -> Path:
    """Keep an optional tool prefix out of the game install folder."""
    from Utils.config_paths import get_wine_prefixes_dir
    return get_wine_prefixes_dir() / f"prefix_{slug}_downgrader_{proton_name}"


def _cleanup_leftovers(game_root: Path, exe_names: "list[str]", log) -> None:
    """Remove the downgrader's working files from *game_root*.

    Clears the ``@mulderload`` staging folder, every downloaded downgrader exe,
    and any ``.xdelta`` left anywhere under the game root.  Symlinks are never
    removed: with a profile deployed the Data folder is full of links into the
    mods store, and deleting one would destroy a managed mod file rather than a
    leftover.  Best-effort throughout - cleanup runs while the wizard is
    closing, so a failure is logged and never raised.
    """
    try:
        if not game_root.is_dir():
            return
    except OSError:
        return

    staging = game_root / _STAGING_DIR
    try:
        if staging.is_dir() and not staging.is_symlink():
            import shutil
            shutil.rmtree(staging, ignore_errors=True)
            log(f"cleanup: removed {staging}")
    except Exception as exc:
        log(f"cleanup: could not remove {staging}: {exc}")

    for exe_name in exe_names:
        target = game_root / exe_name
        try:
            if target.is_file() and not target.is_symlink():
                target.unlink()
                log(f"cleanup: removed {target}")
        except Exception as exc:
            log(f"cleanup: could not remove {target}: {exc}")

    try:
        for patch in game_root.rglob("*.xdelta"):
            try:
                if patch.is_symlink() or not patch.is_file():
                    continue
                patch.unlink()
                log(f"cleanup: removed {patch}")
            except Exception as exc:
                log(f"cleanup: could not remove {patch}: {exc}")
    except Exception as exc:
        log(f"cleanup: scanning for .xdelta files failed: {exc}")


class MulderLoadDowngraderView(WizardViewBase):
    """Fetch the newest available downgrader and run it via Proton."""

    _download_status_sig = Signal(str, str)
    _download_done_sig = Signal(bool)
    _run_ended_sig = Signal()             # tool exited (any outcome) → unlock

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 *, game_label: str, slug: str, game_exe: str, ck_exe: str,
                 **_extra):
        super().__init__(
            game, log_fn, on_close, ctx,
            title=self.tr("Downgrade {0} - {1}").format(game_label, game.name))
        self._game_label = game_label
        self._slug = slug
        self._game_exe = game_exe
        self._ck_exe = ck_exe
        # Chosen on step 1; the run/Proton pages read these.
        self._exe_name = game_exe
        self._display_name = self.tr("{0} Steam Downgrader").format(game_label)
        self._exe: Path | None = None
        self._proton_name = ""
        self._prefix_mode = ""

        self._download_status_sig.connect(self._guard(
            lambda text, color: self._set_status(
                self._download_status, text, color)))
        self._download_done_sig.connect(self._guard(self._on_download_done))
        # Not _guard()ed: the unlock must land even once _closing is set, or a
        # close attempt made during the run would leave the tab unclosable.
        self._run_ended_sig.connect(self._on_run_ended)

        self._stack.addWidget(self._build_choose_page())

        page, lay = self._step_page(
            self.tr("Step 2: Download Downgrader"))
        self._make_note(lay, self.tr(
            "The newest release containing the selected downgrader will be "
            "downloaded from MulderLoad on GitHub and placed in the game "
            "folder.\n\nNo modlist deploy is required."))
        self._download_status = self._make_status(lay)
        lay.addStretch(1)
        self._stack.addWidget(page)

        self._stack.addWidget(self._build_proton_holder())
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 4: Run Downgrader")))

        self._stack.setCurrentIndex(_PG_CHOOSE)

    # ---- step 1: which downgrader ------------------------------------------------
    def _build_choose_page(self):
        page, lay = self._step_page(self.tr("Step 1: Choose Downgrader"))
        self._make_note(lay, self.tr(
            "The game and its Creation Kit are downgraded by separate "
            "installers. Pick which one to download and run."))
        self._game_radio = QRadioButton(
            self.tr("{0} (game)").format(self._game_label))
        self._game_radio.setChecked(True)
        self._ck_radio = QRadioButton(self.tr("Creation Kit"))
        lay.addWidget(self._game_radio)
        lay.addWidget(self._ck_radio)
        lay.addStretch(1)

        next_btn = self._accent_btn(self.tr("Next"))
        next_btn.clicked.connect(self._on_choice_made)
        lay.addWidget(next_btn)
        return page

    def _on_choice_made(self):
        if self._ck_radio.isChecked():
            self._exe_name = self._ck_exe
            self._display_name = self.tr(
                "{0} Creation Kit Steam Downgrader").format(self._game_label)
        else:
            self._exe_name = self._game_exe
            self._display_name = self.tr(
                "{0} Steam Downgrader").format(self._game_label)
        game_root = self._game.get_game_path()
        self._exe = (Path(game_root) / self._exe_name
                     if game_root is not None else None)
        self._goto_step(_PG_DOWNLOAD)

    # ---- teardown ---------------------------------------------------------------
    def _finish(self):
        # Every exit path lands here - ✕, Done, and a failed run alike - so the
        # downgrader's leftovers get swept even when it errored or the user
        # bailed out mid-way.  Runs before super()._finish() so the modlist
        # refresh it may trigger sees the cleaned game folder.
        #
        # _tool_running is rechecked here rather than deferred to super():
        # cleaning first and delegating second would let a programmatic close
        # during a run delete the exe the tool is executing, even though
        # super() would then veto the close itself.
        if not self._closing and not self._tool_running:
            log = lambda message: self._log(
                f"{self._display_name} Wizard: {message}")
            try:
                # The tab can close with the tool still up (the app offers
                # "Leave running"), and pulling the exe or its .xdelta inputs
                # out from under a live downgrader would corrupt the patch run.
                # Leave everything in place in that case; the next open cleans
                # up, and a restore would only route leftovers to overwrite/.
                from Utils.exe_launch import live_tool_labels
                live = live_tool_labels(owner=self)
            except Exception:
                live = []
            if live:
                log("cleanup skipped: the downgrader is still running")
            else:
                try:
                    game_root = self._game.get_game_path()
                    if game_root is not None:
                        _cleanup_leftovers(
                            Path(game_root), [self._game_exe, self._ck_exe],
                            log)
                except Exception as exc:
                    log(f"cleanup failed: {exc}")
        super()._finish()

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_DOWNLOAD:
            self._start_download()
        elif idx == _PG_PROTON:
            from Utils.exe_launch import PREFIX_MODE_GAME
            self._enter_proton(
                self._exe, self._exe_name, self._display_name,
                self._on_proton_chosen,
                isolated_prefix_dir_fn=lambda proton_name: _isolated_prefix_dir(
                    self._slug, proton_name),
                default_prefix_mode=PREFIX_MODE_GAME,
                title=self.tr("Step 3: Choose Proton Version"),
                missing_text=self.tr(
                    "The {0} was not downloaded.\n"
                    "Close and reopen the wizard to try again.").format(
                        self._display_name))
        elif idx == _PG_RUN:
            self._start_run()

    # ---- download ---------------------------------------------------------------
    def _start_download(self):
        game_root = self._game.get_game_path()
        exe_name, display_name = self._exe_name, self._display_name

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
                    _GITHUB_RELEASES_API, [exe_name], extensions={".exe"})
                target = Path(game_root) / exe_name
                safe_emit(
                    self._download_status_sig,
                    self.tr("Downloading {0}…").format(tag), "")
                self._log(
                    f"{display_name} Wizard: downloading {tag} "
                    f"from {url} → {target}")
                download_file(url, target)
                if not target.is_file():
                    raise RuntimeError(self.tr(
                        "The downgrader download did not create {0}.").format(
                            exe_name))
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
                self._log(f"{display_name} Wizard: download error: {exc}")
                safe_emit(self._download_done_sig, False)

        threading.Thread(
            target=worker, daemon=True,
            name=f"{self._slug}-downgrader-download").start()

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
        display_name, slug = self._display_name, self._slug
        if exe is None or not exe.is_file():
            self._set_status(
                self._run_status,
                self.tr("{0} was not found in the game folder.").format(
                    self._exe_name),
                RED)
            return

        self._set_status(
            self._run_status,
            self.tr("Launching {0}…").format(display_name))
        proton_name, prefix_mode = self._proton_name, self._prefix_mode

        def worker():
            from Utils.exe_launch import (
                resolve_tool_prefix, run_tool_logged,
                shutdown_prefix_wineserver,
            )
            log = lambda message: self._log(f"{display_name} Wizard: {message}")
            proton_script = compat_data = None
            try:
                result = resolve_tool_prefix(
                    exe, game, proton_name, prefix_mode, log_fn=log,
                    isolated_prefix_dir=_isolated_prefix_dir(slug, proton_name))
                if result is None:
                    safe_emit(
                        self._run_status_sig,
                        self.tr("Could not determine a Proton version for "
                                "{0}.").format(game.name), RED)
                    return
                proton_script, compat_data, env = result
                log(f"launching {exe} via Proton")
                safe_emit(
                    self._run_status_sig,
                    self.tr("{0} is running.\n"
                            "Follow its prompts, then close it when "
                            "finished.").format(display_name),
                    GREEN)
                safe_emit(self._run_started_sig)
                returncode = run_tool_logged(
                    proton_script, exe, env, log_fn=log, cwd=exe.parent,
                    label=display_name, owner=self)
                if returncode == 0:
                    safe_emit(
                        self._run_status_sig,
                        self.tr("{0} finished. Click Done to close.").format(
                            display_name), GREEN)
                else:
                    safe_emit(
                        self._run_status_sig,
                        self.tr("{0} exited with code {1}. "
                                "See the log for details.").format(
                                    display_name, returncode),
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
                # Always unlock, including the early return above and any
                # launch error - otherwise the wizard can never be closed.
                safe_emit(self._run_ended_sig)

        threading.Thread(
            target=worker, daemon=True,
            name=f"{slug}-downgrader-run").start()

    def _on_run_started(self):
        # Closing now would delete the exe and .xdelta inputs out from under a
        # live downgrader, so hold the wizard open until it exits.
        self._lock_close(True, self.tr(
            "{0} is running - close it to continue.").format(
                self._display_name))
        self._done_btn.setEnabled(True)

    def _on_run_ended(self):
        self._lock_close(False)
