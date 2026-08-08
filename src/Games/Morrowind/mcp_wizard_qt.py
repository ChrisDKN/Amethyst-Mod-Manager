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
                         title=f"Install MCP - {game.name}")
        self._game_root = game.get_game_path()

        self._extract_status_sig2.connect(self._guard(
            lambda t, c: self._set_status(self._extract_status, t, c)))
        self._extract_next_sig.connect(self._guard(
            lambda: self._extract_next_btn.setEnabled(True)))

        self._stack.addWidget(self._build_manual_download_page(
            "Step 1: Download Morrowind Code Patch",
            "Click the button below to open the Morrowind Code Patch\n"
            "download page on Nexus Mods.\n\n"
            "Download the archive, then click Next.",
            _NEXUS_URL,
            lambda: self._goto_step(_PG_LOCATE)))
        self._stack.addWidget(self._build_locate_page(
            "Step 2: Locate the Archive", with_next=True))
        # page 2: extract (status + Next)
        page, lay = self._step_page("Step 3: Extract Files")
        self._extract_status = self._make_status(lay)
        lay.addStretch(1)
        self._extract_next_btn = self._accent_btn("Next →")
        self._extract_next_btn.setEnabled(False)
        self._extract_next_btn.clicked.connect(lambda: self._goto_step(_PG_RUN))
        lay.addWidget(self._extract_next_btn, 0, Qt.AlignHCenter)
        self._stack.addWidget(page)
        # page 3: run
        self._stack.addWidget(self._build_run_page(
            "Step 4: Run Morrowind Code Patch"))

        # If the exe is already present, skip download/extract.
        if self._game_root is not None and (self._game_root / _PATCH_EXE).is_file():
            self._goto_step(_PG_RUN)
        else:
            self._stack.setCurrentIndex(_PG_DOWNLOAD)
            self._nexus_auto_fetch(
                url=_NEXUS_URL, file_id=_NEXUS_FILE_ID,
                keywords=_ARCHIVE_KEYWORDS, label="Morrowind Code Patch",
                pages=(_PG_DOWNLOAD, _PG_LOCATE),
                on_archive=lambda _p: self._goto_step(_PG_EXTRACT))

    def _goto_step(self, idx: int):
        self._stack.setCurrentIndex(idx)
        if idx == _PG_LOCATE:
            self._enter_locate(
                _ARCHIVE_KEYWORDS, "Select the Morrowind Code Patch archive",
                "Archive not found in Downloads.\n"
                "Make sure you downloaded it, then press Try Again,\n"
                "or use Browse to select it manually.",
                lambda _p: self._goto_step(_PG_EXTRACT))
        elif idx == _PG_EXTRACT:
            self._set_status(self._extract_status,
                             "Extracting archive to game folder…")
            threading.Thread(target=self._do_extract, daemon=True,
                             name="mcp-extract").start()
        elif idx == _PG_RUN:
            self._set_status(self._run_status,
                             f"Running {_PATCH_EXE} via Proton…\n"
                             "Apply your desired patches, then come back and "
                             "click Done.")
            threading.Thread(target=self._do_run, daemon=True,
                             name="mcp-run").start()

    def _do_extract(self):
        from Utils.wizard_archives import extract_archive
        try:
            if self._game_root is None:
                raise RuntimeError("Game path is not configured.")
            archive = self._archive_path
            if archive is None or not archive.is_file():
                raise RuntimeError("Archive not found.")

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
                      f"Extracted {file_count} file(s) to game folder.\n\n"
                      "Click Next to run the patcher.", GREEN)
        except Exception as exc:
            safe_emit(self._extract_status_sig2, f"Error: {exc}", RED)
            self._log(f"MCP Wizard extract error: {exc}")
        finally:
            safe_emit(self._extract_next_sig)

    def _do_run(self):
        import subprocess
        from Utils.exe_launch import get_game_prefix_env
        from Utils.steam_finder import proton_run_command
        try:
            if self._game_root is None:
                raise RuntimeError("Game path is not configured.")
            patch_exe = self._game_root / _PATCH_EXE
            if not patch_exe.is_file():
                raise RuntimeError(f"{_PATCH_EXE} not found in game folder.")

            result = get_game_prefix_env(
                self._game, log_fn=lambda m: self._log(f"MCP Wizard: {m}"),
                allow_runner_fallback=True)
            if result is None:
                raise RuntimeError(
                    "Could not determine Proton version for this game.")
            proton_script, _compat_data, env = result

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
                    f"{_PATCH_EXE} exited with code {proc.returncode}.\n{stderr}")
            self._log("MCP Wizard: patcher completed.")
            safe_emit(self._run_status_sig,
                      "Morrowind Code Patch finished.\n\nClick Done to close.",
                      GREEN)
            safe_emit(self._run_finished_sig)
        except Exception as exc:
            safe_emit(self._run_status_sig, f"Error: {exc}", RED)
            self._log(f"MCP Wizard run error: {exc}")
            safe_emit(self._run_started_sig)   # enable Done to close

    def _on_run_started(self):
        self._ran = True
        self._done_btn.setEnabled(True)
