"""Fallout 3 Downgrade wizard - Qt port of wizards/fallout_downgrade.py.

Walks through downloading the Fallout Anniversary Patcher from Nexus,
locating the archive, extracting it into the game root, running Patcher.exe
via the game's own Proton prefix, and cleaning the extracted files back out
when finished (extract_archive returns created paths deepest-first, which is
exactly the cleanup order).
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

_NEXUS_URL = "https://www.nexusmods.com/fallout3/mods/24913"
_NEXUS_FILE_ID = 1000021415     # "Fallout Anniversary Patcher" main file
_ARCHIVE_KEYWORDS = ["fallout", "anniversary", "patcher"]

# SHA-1 produced by every supported patch route in the upstream patcher
# (Steam/Epic, GOG, NoGore and the older-patch update route).
_PATCHED_EXE_SHA1 = "2E57141A77A5AEE21518755EB32245663036EEF4"

_PG_DOWNLOAD, _PG_LOCATE, _PG_RUN = range(3)


class FalloutDowngradeView(WizardViewBase):
    """Downgrade Fallout 3 for script extender compatibility."""

    _done_enable_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Downgrade Fallout 3 - {0}").format(game.name))
        self._game_root = game.get_game_path()
        self._extracted_paths: list[Path] = []
        self._did_restore = False

        self._done_enable_sig.connect(self._guard(
            lambda: self._done_btn.setEnabled(True)))

        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 1: Download the Patcher"),
            self.tr("To downgrade Fallout 3 you need the\n"
            "Fallout Anniversary Patcher from Nexus Mods.\n\n"
            "Click the button below to open the mod page,\n"
            "then download the main file."),
            _NEXUS_URL,
            lambda: self._goto_step(_PG_LOCATE),
            button_text=self.tr("Open Nexus Mods Page")))
        self._stack.addWidget(self._build_locate_page(
            self.tr("Step 2: Locate the Archive"), with_next=True))
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 3: Extract & Run Patcher")))
        self._stack.setCurrentIndex(_PG_DOWNLOAD)
        self._nexus_auto_fetch(
            url=_NEXUS_URL, file_id=_NEXUS_FILE_ID,
            keywords=_ARCHIVE_KEYWORDS,
            label="Fallout Anniversary Patcher",  # i18n: skip — mod name, used in log lines
            pages=(_PG_DOWNLOAD, _PG_LOCATE),
            on_archive=lambda _p: self._goto_step(_PG_RUN))

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_LOCATE:
            self._enter_locate(
                _ARCHIVE_KEYWORDS,
                self.tr("Select the Fallout Anniversary Patcher archive"),
                self.tr("Archive not found in Downloads.\n"
                "Make sure you downloaded the mod, then press Try Again,\n"
                "or use Browse to select it manually."),
                lambda _p: self._goto_step(_PG_RUN))
        elif idx == _PG_RUN:
            # The patcher downgrades the exe and writes backups into the game
            # root. If a profile is deployed those files are absent from the
            # deploy snapshot, so the next restore would sweep them into
            # overwrite/ as runtime files - restore the modlist first and run
            # the patcher against the vanilla root (Done redeploys).
            if getattr(self._game, "get_deploy_active", lambda: False)():
                self._log("Downgrade Wizard: modlist is deployed - restoring "
                          "before patching (redeploys when the wizard closes).")
                if self._run_ctx_restore(self._run_status, self._start_patch_step):
                    self._did_restore = True
                    return
                # Restore couldn't start - fall through and patch the deployed
                # root rather than dead-ending (pre-fix behaviour).
            self._start_patch_step()

    def _start_patch_step(self):
        self._set_status(self._run_status,
                         self.tr("Extracting archive to game folder…"))
        threading.Thread(target=self._extract_and_run, daemon=True,
                         name="fo3-downgrade").start()

    # ---- worker: extract into game root + run Patcher.exe -----------------------
    def _extract_and_run(self):
        try:
            self._do_extract()
            self._do_run_patcher()
        except Exception as exc:
            safe_emit(self._run_status_sig, self.tr("Error: {0}").format(exc), RED)
            self._log(f"Downgrade Wizard: {exc}")

    def _do_extract(self):
        from Utils.wizards.archives import extract_archive
        game_root = self._game_root
        if game_root is None:
            raise RuntimeError(self.tr("Game path is not configured."))
        archive = self._archive_path
        if archive is None or not archive.is_file():
            raise RuntimeError(self.tr("Archive not found."))
        safe_emit(self._run_status_sig,
                  self.tr("Extracting archive to game folder…"), "")
        self._log(f"Downgrade Wizard: extracting {archive.name} → {game_root}")
        # extract_archive returns files then dirs deepest-first - kept for
        # the reverse-depth cleanup when the wizard closes.
        self._extracted_paths = extract_archive(archive, game_root)
        n = len([p for p in self._extracted_paths if p.is_file()])
        self._log(f"Downgrade Wizard: extracted {n} file(s).")

    def _do_run_patcher(self):
        import hashlib
        from Utils.executables.launch import (
            get_game_prefix_env, shutdown_prefix_wineserver,
        )
        from Utils.wine.protontricks import run_prefix_installer
        from Utils.launchers.steam import proton_run_command

        game_root = self._game_root
        patcher_exe = next(
            (p for p in self._extracted_paths
             if p.is_file() and p.name.lower() == "patcher.exe"), None)
        if patcher_exe is None:
            patcher_exe = next(game_root.rglob("Patcher.exe"), None)
        if patcher_exe is None:
            raise RuntimeError(
                self.tr("Could not find Patcher.exe after extraction.\n"
                "Make sure you downloaded the correct mod."))

        safe_emit(self._run_status_sig,
                  self.tr("Running {0} via Proton…\n"
                  "This may take a moment.").format(patcher_exe.name), "")
        self._log(f"Downgrade Wizard: running {patcher_exe} via Proton")

        result = get_game_prefix_env(
            self._game, log_fn=lambda m: self._log(f"Downgrade Wizard: {m}"),
            allow_runner_fallback=True)
        if result is None:
            raise RuntimeError(self.tr("Could not determine Proton version for this game."))
        proton_script, compat_data, env = result

        # The upstream patcher writes verbose xdelta output and always ends in
        # system("@pause").  Waiting on it with unread PIPEs can deadlock once
        # the pipe buffer fills, and inherited stdin leaves the final pause
        # waiting forever.  The shared runner captures output in a real file,
        # gives the child /dev/null for stdin (so pause sees EOF), and kills the
        # complete Proton process group if it genuinely wedges.
        try:
            returncode, output = run_prefix_installer(
                # runinprefix: skips the steam.exe shim so Steam doesn't show the
                # game as "Running" while the patcher works.
                proton_run_command(proton_script, "runinprefix", str(patcher_exe),
                                   env=env),
                env,
                game_root,
                label="Fallout 3 Anniversary Patcher",  # i18n: skip — patcher name, used in log lines
                log_fn=lambda m: self._log(f"Downgrade Wizard: {m}"),
                proton_script=proton_script,
                compat_data=compat_data,
            )
        finally:
            # run_prefix_installer only shuts the wineserver down on its
            # timeout path, so the normal-exit path still leaks Proton
            # sidecars into the GAME prefix - which blocks Steam from
            # launching the game. Shutting down twice is harmless.
            shutdown_prefix_wineserver(
                proton_script, compat_data,
                log_fn=lambda m: self._log(f"Downgrade Wizard: {m}"))
        if output:
            self._log(f"Downgrade Wizard: Patcher output:\n{output}")
        if returncode is None:
            raise RuntimeError(self.tr(
                "The patcher did not respond within two minutes and was stopped."))
        if returncode != 0:
            detail = f"\n{output}" if output else ""
            raise RuntimeError(self.tr(
                "Patcher exited with code {0}.{1}").format(returncode, detail))

        # Patcher.exe returns zero even for "Invalid executable" and similar
        # failures, so its exit code cannot establish success.  Verify the
        # result exactly as the upstream program does before enabling Done.
        patched_exe = None
        for name in ("Fallout3.exe", "Fallout3ng.exe"):
            candidate = game_root / name
            if not candidate.is_file():
                continue
            digest = hashlib.sha1(candidate.read_bytes()).hexdigest().upper()
            if digest == _PATCHED_EXE_SHA1:
                patched_exe = candidate
                break
        if patched_exe is None:
            detail = f"\n\n{output}" if output else ""
            raise RuntimeError(self.tr(
                "The patcher exited without producing a recognised patched "
                "Fallout 3 executable.{0}").format(detail))

        safe_emit(self._run_status_sig,
                  self.tr("{0} was downgraded successfully.\n\n"
                          "Click Done to clean up the extracted files and close.").format(
                              patched_exe.name),
                  GREEN)
        safe_emit(self._done_enable_sig)
        self._log("Downgrade Wizard: patcher complete. Waiting for Done.")

    # ---- cleanup on close ---------------------------------------------------------
    def _finish(self):
        if self._closing:
            return
        self._cleanup_extracted()
        # Put back the modlist the restore-first step took down. The deploy
        # snapshot written by this deploy records the patcher's backup files,
        # so later restores leave them in the game root.
        if self._did_restore:
            self._did_restore = False
            run_deploy = getattr(self._ctx, "run_deploy", None)
            if run_deploy is not None and run_deploy(lambda _ok: None):
                self._log("Downgrade Wizard: redeploying the modlist that was "
                          "restored before patching.")
            else:
                self._log("Downgrade Wizard: could not redeploy automatically "
                          "- use Deploy to put your modlist back.")
        super()._finish()

    def _cleanup_extracted(self):
        """Remove every file and directory that was extracted into game root."""
        if not self._extracted_paths:
            return
        removed = 0
        for p in self._extracted_paths:
            try:
                if p.is_file() or p.is_symlink():
                    p.unlink()
                    removed += 1
                elif p.is_dir():
                    try:
                        p.rmdir()   # only when empty - files removed above
                        removed += 1
                    except OSError:
                        pass
            except Exception:
                pass
        self._extracted_paths.clear()
        if removed:
            self._log(f"Downgrade Wizard: removed {removed} extracted item(s) "
                      "from game root.")
