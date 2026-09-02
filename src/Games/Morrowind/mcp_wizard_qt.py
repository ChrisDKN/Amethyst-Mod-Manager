"""Morrowind Code Patch wizard - Qt port of Games/Morrowind/mcp_wizard.py.

Loose files extract straight into the game root; then Morrowind Code
Patch.exe runs via the game's Proton prefix so the user can apply patches.
If the exe is already present the extract step is skipped.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal

from gui_qt.safe_emit import safe_emit
from wizards_qt._view_base import GREEN, RED, WizardViewBase

if TYPE_CHECKING:
    from Games.base_game import BaseGame

_NEXUS_URL = "https://www.nexusmods.com/morrowind/mods/19510?tab=files&file_id=1000007846"
_NEXUS_FILE_ID = 1000007846
_ARCHIVE_KEYWORDS = ["morrowind code patch"]
_PATCH_EXE = "Morrowind Code Patch.exe"

_PG_DOWNLOAD, _PG_LOCATE, _PG_EXTRACT, _PG_RUN = range(4)


class MCPView(WizardViewBase):
    """Install and run Morrowind Code Patch."""

    _extract_status_sig2 = Signal(str, str)
    _extract_next_sig = Signal()

    def __init__(self, game: "BaseGame", log_fn=None, on_close=None, ctx=None,
                 **_extra):
        super().__init__(game, log_fn, on_close, ctx,
                         title=self.tr("Install MCP - {0}").format(game.name))
        self._game_root = game.get_game_path()

        self._extract_status_sig2.connect(self._guard(
            lambda t, c: self._set_status(self._extract_status, t, c)))
        self._extract_next_sig.connect(self._guard(
            lambda: self._extract_next_btn.setEnabled(True)))

        self._stack.addWidget(self._build_manual_download_page(
            self.tr("Step 1: Download Morrowind Code Patch"),
            self.tr("Click the button below to open the Morrowind Code Patch\n"
                    "download page on Nexus Mods.\n\n"
                    "Download the archive, then click Next."),
            _NEXUS_URL,
            lambda: self._goto_step(_PG_LOCATE)))
        self._stack.addWidget(self._build_locate_page(
            self.tr("Step 2: Locate the Archive"), with_next=True))
        # page 2: extract (status + Next)
        page, lay = self._step_page(self.tr("Step 3: Extract Files"))
        self._extract_status = self._make_status(lay)
        lay.addStretch(1)
        self._extract_next_btn = self._accent_btn(self.tr("Next →"))
        self._extract_next_btn.setEnabled(False)
        self._extract_next_btn.clicked.connect(lambda: self._goto_step(_PG_RUN))
        lay.addWidget(self._extract_next_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)
        # page 3: run
        self._stack.addWidget(self._build_run_page(
            self.tr("Step 4: Run Morrowind Code Patch")))

        # If the exe is already present, skip download/extract.
        if self._game_root is not None and (self._game_root / _PATCH_EXE).is_file():
            self._goto_step(_PG_RUN)
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)
            self._nexus_auto_fetch(
                url=_NEXUS_URL, file_id=_NEXUS_FILE_ID,
                keywords=_ARCHIVE_KEYWORDS,
                # Product name, substituted into already-translated shells like
                # "Downloading {0} from Nexus…" - it must not translate.
                label="Morrowind Code Patch",  # i18n: skip - product name
                pages=(_PG_DOWNLOAD, _PG_LOCATE),
                on_archive=lambda _p: self._goto_step(_PG_EXTRACT))

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_LOCATE:
            self._enter_locate(
                _ARCHIVE_KEYWORDS,
                self.tr("Select the Morrowind Code Patch archive"),
                self.tr("Archive not found in Downloads.\n"
                        "Make sure you downloaded it, then press Try Again,\n"
                        "or use Browse to select it manually."),
                lambda _p: self._goto_step(_PG_EXTRACT))
        elif idx == _PG_EXTRACT:
            self._set_status(self._extract_status,
                             self.tr("Extracting archive to game folder…"))
            threading.Thread(target=self._do_extract, daemon=True,
                             name="mcp-extract").start()
        elif idx == _PG_RUN:
            self._set_status(self._run_status,
                             self.tr("Running {0} via Proton…\n"
                                     "Apply your desired patches, then come "
                                     "back and click Done.").format(_PATCH_EXE))
            threading.Thread(target=self._do_run, daemon=True,
                             name="mcp-run").start()

    def _do_extract(self):
        from Utils.wizards.archives import extract_archive
        try:
            if self._game_root is None:
                raise RuntimeError(self.tr("Game path is not configured."))
            archive = self._archive_path
            if archive is None or not archive.is_file():
                raise RuntimeError(self.tr("Archive not found."))

            self._log(f"MCP Wizard: extracting {archive.name} → {self._game_root}")
            paths = extract_archive(archive, self._game_root)
            file_count = len([p for p in paths if p.is_file()])
            self._log(f"MCP Wizard: extracted {file_count} file(s).")

            try:
                archive.unlink()
                self._log(f"MCP Wizard: deleted {archive.name} from Downloads.")
            except OSError as exc:
                self._log(f"MCP Wizard: could not delete archive: {exc}")

            safe_emit(self._extract_status_sig2,
                      self.tr("Extracted {0} file(s) to game folder.\n\n"
                              "Click Next to run the patcher.")
                      .format(file_count), GREEN)
        except Exception as exc:
            safe_emit(self._extract_status_sig2,
                      self.tr("Error: {0}").format(exc), RED)
            self._log(f"MCP Wizard extract error: {exc}")
        finally:
            safe_emit(self._extract_next_sig)

    def _do_run(self):
        import subprocess
        from Utils.executables.launch import (
            get_game_prefix_env, shutdown_prefix_wineserver,
        )
        from Utils.launchers.steam import proton_run_command
        proton_script = compat_data = None
        try:
            if self._game_root is None:
                raise RuntimeError(self.tr("Game path is not configured."))
            patch_exe = self._game_root / _PATCH_EXE
            if not patch_exe.is_file():
                raise RuntimeError(
                    self.tr("{0} not found in game folder.").format(_PATCH_EXE))

            result = get_game_prefix_env(
                self._game, log_fn=lambda m: self._log(f"MCP Wizard: {m}"),
                allow_runner_fallback=True)
            if result is None:
                raise RuntimeError(self.tr(
                    "Could not determine Proton version for this game."))
            proton_script, compat_data, env = result

            self._log(f"MCP Wizard: launching {patch_exe} via Proton")
            proc = subprocess.Popen(
                # runinprefix: skips the steam.exe shim so Steam doesn't show
                # the game as "Running" while the patcher is open.
                proton_run_command(proton_script, "runinprefix",
                                   str(patch_exe), env=env),
                env=env,
                cwd=str(self._game_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            safe_emit(self._run_started_sig)
            proc.wait()
            if proc.returncode != 0:
                stderr = (proc.stderr.read() or b"").decode(errors="replace").strip()
                raise RuntimeError(
                    self.tr("{0} exited with code {1}.\n{2}")
                    .format(_PATCH_EXE, proc.returncode, stderr))
            self._log("MCP Wizard: patcher completed.")
            safe_emit(self._run_status_sig,
                      self.tr("Morrowind Code Patch finished.\n\n"
                              "Click Done to close."),
                      GREEN)
            safe_emit(self._run_finished_sig)
        except Exception as exc:
            safe_emit(self._run_status_sig,
                      self.tr("Error: {0}").format(exc), RED)
            self._log(f"MCP Wizard run error: {exc}")
            safe_emit(self._run_started_sig)   # enable Done to close
        finally:
            # Proton sidecars (xalia.exe et al) keep the GAME prefix's
            # wineserver alive after the patcher exits, and a live wineserver
            # on the game prefix blocks Steam from launching the game at all.
            # In finally: a crashed tool is when the leak is most likely.
            if proton_script is not None and compat_data is not None:
                shutdown_prefix_wineserver(
                    proton_script, compat_data,
                    log_fn=lambda m: self._log(f"MCP Wizard: {m}"))

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)
