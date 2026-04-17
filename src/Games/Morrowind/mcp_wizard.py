"""
mcp_wizard.py
Wizard for installing and running Morrowind Code Patch (MCP).

The archive contains loose files that extract directly to the game root.
After extraction, Morrowind Code Patch.exe is run via Proton so the user
can apply patches.

If Morrowind Code Patch.exe is already present in the game root the
extraction step is skipped and the wizard goes straight to running it.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from Utils.xdg import open_url
from Utils.portal_filechooser import pick_file
from wizards.script_extender import (
    _get_downloads_dir,
    _find_archive,
    _extract_archive,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD, FONT_SMALL,
)

_NEXUS_URL       = "https://www.nexusmods.com/morrowind/mods/19510?tab=files&file_id=1000007846"
_ARCHIVE_KEYWORDS = ["morrowind code patch"]
_PATCH_EXE       = "Morrowind Code Patch.exe"


class MCPWizard(ctk.CTkFrame):
    """Step-by-step wizard to install and run Morrowind Code Patch."""

    def __init__(
        self,
        parent,
        game: "BaseGame",
        log_fn=None,
        *,
        on_close=None,
        **_kwargs,
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close_cb = on_close or (lambda: None)
        self._game        = game
        self._log         = log_fn or (lambda msg: None)
        self._game_root   = game.get_game_path()
        self._archive_path: Path | None = None

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Install Morrowind Code Patch \u2014 {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_close_cb,
        ).pack(side="right", padx=4, pady=4)

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        if self._game_root and (self._game_root / _PATCH_EXE).is_file():
            self._show_step_run()
        else:
            self._show_step_download()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    def _set_label(self, attr: str, text: str, color: str = TEXT_DIM):
        try:
            self.after(0, lambda: getattr(self, attr).configure(
                text=text, text_color=color,
            ))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Step 1 — Download
    # ------------------------------------------------------------------

    def _show_step_download(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download Morrowind Code Patch",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                "Click the button below to open the Morrowind Code Patch\n"
                "download page on Nexus Mods.\n\n"
                "Download the archive, then click Next."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 16))

        ctk.CTkButton(
            self._body, text="Open Download Page", width=220, height=36,
            font=FONT_BOLD,
            fg_color="#da8e35", hover_color="#e5a04a", text_color="white",
            command=lambda: open_url(_NEXUS_URL),
        ).pack(pady=(0, 20))

        ctk.CTkButton(
            self._body, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._show_step_locate,
        ).pack(side="bottom")

    # ------------------------------------------------------------------
    # Step 2 — Locate archive
    # ------------------------------------------------------------------

    def _show_step_locate(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 2: Locate the Archive",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._locate_status = ctk.CTkLabel(
            self._body, text="Searching Downloads folder\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
            wraplength=460,
        )
        self._locate_status.pack(pady=(0, 12))

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        self._locate_next_btn = ctk.CTkButton(
            btn_frame, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._show_step_extract, state="disabled",
        )
        self._locate_next_btn.pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Try Again", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._scan_downloads,
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Browse\u2026", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._browse_archive,
        ).pack(side="right")

        self._scan_downloads()

    def _scan_downloads(self):
        found = _find_archive(_get_downloads_dir(), _ARCHIVE_KEYWORDS)
        if found:
            self._archive_path = found
            self._locate_status.configure(text=f"Found: {found.name}", text_color="#6bc76b")
            self._locate_next_btn.configure(state="normal")
        else:
            self._archive_path = None
            self._locate_status.configure(
                text=(
                    "Archive not found in Downloads.\n"
                    "Make sure you downloaded it, then press Try Again,\n"
                    "or use Browse to select it manually."
                ),
                text_color="#e06c6c",
            )
            self._locate_next_btn.configure(state="disabled")

    def _browse_archive(self):
        def _on_picked(path: Path | None) -> None:
            if path and path.is_file():
                self._archive_path = path
                self._locate_status.configure(text=f"Selected: {path.name}", text_color="#6bc76b")
                self._locate_next_btn.configure(state="normal")

        pick_file("Select the Morrowind Code Patch archive",
                  lambda p: self.after(0, lambda: _on_picked(p)))

    # ------------------------------------------------------------------
    # Step 3 — Extract
    # ------------------------------------------------------------------

    def _show_step_extract(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Extract Files",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._extract_status = ctk.CTkLabel(
            self._body, text="Extracting archive to game folder\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
            wraplength=460,
        )
        self._extract_status.pack(pady=(0, 16))

        self._extract_next_btn = ctk.CTkButton(
            self._body, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._show_step_run, state="disabled",
        )
        self._extract_next_btn.pack(side="bottom")

        threading.Thread(target=self._do_extract, daemon=True).start()

    def _do_extract(self):
        try:
            if self._game_root is None:
                raise RuntimeError("Game path is not configured.")
            archive = self._archive_path
            if archive is None or not archive.is_file():
                raise RuntimeError("Archive not found.")

            self._log(f"MCP Wizard: extracting {archive.name} \u2192 {self._game_root}")
            paths = _extract_archive(archive, self._game_root)
            file_count = len([p for p in paths if p.is_file()])
            self._log(f"MCP Wizard: extracted {file_count} file(s).")

            try:
                archive.unlink()
                self._log(f"MCP Wizard: deleted {archive.name} from Downloads.")
            except OSError as exc:
                self._log(f"MCP Wizard: could not delete archive: {exc}")

            self._set_label(
                "_extract_status",
                f"Extracted {file_count} file(s) to game folder.\n\nClick Next to run the patcher.",
                color="#6bc76b",
            )
            self.after(0, lambda: self._extract_next_btn.configure(state="normal"))

        except Exception as exc:
            self._set_label("_extract_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"MCP Wizard extract error: {exc}")
            self.after(0, lambda: self._extract_next_btn.configure(state="normal"))

    # ------------------------------------------------------------------
    # Step 4 — Run patcher
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 4: Run Morrowind Code Patch",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._run_status = ctk.CTkLabel(
            self._body,
            text=f"Running {_PATCH_EXE} via Proton\u2026\n"
                 "Apply your desired patches, then come back and click Done.",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
            wraplength=460,
        )
        self._run_status.pack(pady=(0, 16))

        self._done_btn = ctk.CTkButton(
            self._body, text="Done", width=120, height=36,
            font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._on_close_cb, state="disabled",
        )
        self._done_btn.pack(side="bottom")

        threading.Thread(target=self._do_run, daemon=True).start()

    def _do_run(self):
        try:
            if self._game_root is None:
                raise RuntimeError("Game path is not configured.")

            patch_exe = self._game_root / _PATCH_EXE
            if not patch_exe.is_file():
                raise RuntimeError(f"{_PATCH_EXE} not found in game folder.")

            proton_script, env = self._get_proton_env()
            if proton_script is None:
                raise RuntimeError("Could not determine Proton version for this game.")

            self._log(f"MCP Wizard: launching {patch_exe} via Proton")
            proc = subprocess.Popen(
                ["python3", str(proton_script), "run", str(patch_exe)],
                env=env,
                cwd=str(self._game_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            proc.wait()

            if proc.returncode != 0:
                stderr = (proc.stderr.read() or b"").decode(errors="replace").strip()
                raise RuntimeError(
                    f"{_PATCH_EXE} exited with code {proc.returncode}.\n{stderr}"
                )

            self._log("MCP Wizard: patcher completed.")
            self._set_label(
                "_run_status",
                "Morrowind Code Patch finished.\n\nClick Done to close.",
                color="#6bc76b",
            )
            self.after(0, lambda: self._done_btn.configure(state="normal"))

        except Exception as exc:
            self._set_label("_run_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"MCP Wizard run error: {exc}")
            self.after(0, lambda: self._done_btn.configure(state="normal"))

    # ------------------------------------------------------------------
    # Proton env
    # ------------------------------------------------------------------

    def _get_proton_env(self):
        import os
        from Utils.steam_finder import (
            find_any_installed_proton,
            find_proton_for_game,
            find_steam_root_for_proton_script,
        )

        prefix_path = self._game.get_prefix_path()
        if prefix_path is None or not prefix_path.is_dir():
            self._log("MCP Wizard: prefix not configured for this game.")
            return None, None

        steam_id = getattr(self._game, "steam_id", "")
        from gui.plugin_panel import _resolve_compat_data, _read_prefix_runner
        compat_data = _resolve_compat_data(prefix_path)
        proton_script = find_proton_for_game(steam_id) if steam_id else None
        if proton_script is None:
            preferred_runner = _read_prefix_runner(compat_data)
            proton_script = find_any_installed_proton(preferred_runner)
            if proton_script is None:
                self._log("MCP Wizard: could not find any installed Proton tool.")
                return None, None
            self._log(
                f"MCP Wizard: using fallback Proton tool {proton_script.parent.name} "
                "(no per-game Steam mapping found)."
            )
        steam_root = find_steam_root_for_proton_script(proton_script)
        if steam_root is None:
            self._log("MCP Wizard: could not determine Steam root for the selected Proton tool.")
            return None, None

        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)

        return proton_script, env
