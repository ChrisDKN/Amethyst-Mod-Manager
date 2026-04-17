"""
SRML installation wizard for Slime Rancher.

Workflow:
  1. Prompt user to download SRML from Nexus Mods.
  2. Auto-detect the archive in Downloads (keyword: SRMLInstaller).
  3. Extract to the game folder.
  4. Run SRMLInstaller.exe via Proton.
  5. Clean up archive and extracted installer.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import customtkinter as ctk

from Utils.xdg import open_url
from Utils.portal_filechooser import pick_file

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD,
)

PLUGIN_INFO = {
    "id":           "sr_srml",
    "label":        "Install SRML",
    "description":  "Download and install SRML (Slime Rancher Mod Loader).",
    "game_ids":     ["Slime_Rancher"],
    "all_games":    False,
    "dialog_class": "SRMLWizard",
}

_NEXUS_URL       = "https://www.nexusmods.com/slimerancher/mods/2?tab=files&file_id=724"
_ARCHIVE_KEYWORD = "srmlinstaller"
_EXE_NAME        = "SRMLInstaller.exe"
_ARCHIVE_EXTS    = {".zip", ".7z", ".rar"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_archive(downloads_dir: Path) -> Path | None:
    if not downloads_dir.is_dir():
        return None
    candidates = [
        p for p in downloads_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() in _ARCHIVE_EXTS
        and _ARCHIVE_KEYWORD in p.name.lower()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

class SRMLWizard(ctk.CTkFrame):

    def __init__(self, parent, game, log_fn=None, *, on_close=None, **_extra):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close_cb = on_close or (lambda: None)
        self._game = game
        self._log = log_fn or (lambda msg: None)
        self._archive_path: Path | None = None
        self._extracted_paths: list[Path] = []

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar,
            text=f"Install SRML \u2014 {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_close_cb,
        ).pack(side="right", padx=4, pady=4)

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        self._show_step_download()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    def _set_label(self, attr: str, text: str, color: str = TEXT_DIM):
        def _apply(t=text, c=color):
            try:
                widget = getattr(self, attr, None)
                if widget is not None and widget.winfo_exists():
                    widget.configure(text=t, text_color=c)
            except Exception:
                pass
        self.after(0, _apply)

    def _get_proton_env(self):
        from Utils.steam_finder import (
            find_any_installed_proton,
            find_proton_for_game,
            find_steam_root_for_proton_script,
        )

        prefix_path = self._game.get_prefix_path()
        if prefix_path is None or not prefix_path.is_dir():
            return None, None

        steam_id    = getattr(self._game, "steam_id", "")
        from gui.plugin_panel import _resolve_compat_data, _read_prefix_runner
        compat_data = _resolve_compat_data(prefix_path)
        proton_script = find_proton_for_game(steam_id) if steam_id else None

        if proton_script is None:
            preferred_runner = _read_prefix_runner(compat_data)
            proton_script = find_any_installed_proton(preferred_runner)
            if proton_script is None:
                return None, None

        steam_root = find_steam_root_for_proton_script(proton_script)
        if steam_root is None:
            return None, None

        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"]           = str(compat_data)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
        game_path = self._game.get_game_path()
        if game_path:
            env["STEAM_COMPAT_INSTALL_PATH"] = str(game_path)
        if steam_id:
            env.setdefault("SteamAppId",  steam_id)
            env.setdefault("SteamGameId", steam_id)

        return proton_script, env

    # ------------------------------------------------------------------
    # Step 1 — Download SRML
    # ------------------------------------------------------------------

    def _show_step_download(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download SRML",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                "Click the button below to open the SRML page on Nexus Mods.\n\n"
                "Download the archive manually (do NOT use the Mod Manager\n"
                "download button), then click Next."
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
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._locate_status.pack(pady=(0, 12))

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

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
        found = _find_archive(Path.home() / "Downloads")
        if found:
            self._archive_path = found
            self._locate_status.configure(
                text=f"Found: {found.name}", text_color="#6bc76b",
            )
            self.after(300, self._show_step_extract)
        else:
            self._archive_path = None
            self._locate_status.configure(
                text=(
                    "SRML archive not found in Downloads.\n"
                    "Make sure you downloaded it, then press Try Again,\n"
                    "or use Browse to select it manually."
                ),
                text_color="#e06c6c",
            )

    def _browse_archive(self):
        def _on_picked(path: Path | None) -> None:
            if path and path.is_file():
                self._archive_path = path
                self._locate_status.configure(
                    text=f"Selected: {path.name}", text_color="#6bc76b",
                )
                self.after(300, self._show_step_extract)

        pick_file(
            "Select the SRML archive",
            lambda p: self.after(0, lambda: _on_picked(p)),
        )

    # ------------------------------------------------------------------
    # Step 3 — Extract to game folder
    # ------------------------------------------------------------------

    def _show_step_extract(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Extract to Game Folder",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._extract_status = ctk.CTkLabel(
            self._body, text="Extracting\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._extract_status.pack(pady=(0, 16))

        threading.Thread(target=self._do_extract, daemon=True).start()

    def _do_extract(self):
        try:
            from wizards.script_extender import _extract_archive

            archive = self._archive_path
            if archive is None or not archive.is_file():
                raise RuntimeError("Archive not found.")

            game_path = self._game.get_game_path()
            if game_path is None:
                raise RuntimeError("Game path not configured.")

            self._set_label("_extract_status", f"Extracting {archive.name}\u2026")
            self._log(f"SRML Wizard: extracting {archive.name} \u2192 {game_path}")

            paths = _extract_archive(archive, game_path)
            self._extracted_paths = paths
            file_count = len([p for p in paths if p.is_file()])
            self._log(f"SRML Wizard: extracted {file_count} file(s).")

            self._set_label(
                "_extract_status",
                f"Extracted {file_count} file(s).",
                color="#6bc76b",
            )
            self.after(300, self._show_step_run)

        except Exception as exc:
            self._set_label("_extract_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"SRML Wizard: extract error: {exc}")

    # ------------------------------------------------------------------
    # Step 4 — Run SRMLInstaller.exe
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 4: Run SRMLInstaller",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        game_path = self._game.get_game_path()
        exe = game_path / _EXE_NAME if game_path else None

        if exe is None or not exe.is_file():
            ctk.CTkLabel(
                self._body,
                text=(
                    f"{_EXE_NAME} was not found in the game folder.\n"
                    "Check that the archive extracted correctly."
                ),
                font=FONT_NORMAL, text_color="#e06c6c", justify="center",
            ).pack(pady=(0, 16))
            ctk.CTkButton(
                self._body, text="Close", width=120, height=36,
                font=FONT_BOLD,
                fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
                command=self._on_close_cb,
            ).pack(side="bottom")
            return

        self._run_status = ctk.CTkLabel(
            self._body, text="Launching SRMLInstaller\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._run_status.pack(pady=(0, 12))

        self._done_btn = ctk.CTkButton(
            self._body, text="Done", width=120, height=36,
            font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=lambda: self._finish_cleanup(exe),
            state="disabled",
        )
        self._done_btn.pack(side="bottom")

        threading.Thread(target=lambda: self._do_run(exe), daemon=True).start()

    def _do_run(self, exe: Path):
        proton_script, env = self._get_proton_env()
        if proton_script is None:
            self._set_label(
                "_run_status",
                "Could not find Proton \u2014 check that the prefix is configured.",
                color="#e06c6c",
            )
            return

        self._log(f"SRML Wizard: launching {exe} via Proton")
        try:
            proc = subprocess.Popen(
                ["python3", str(proton_script), "run", str(exe)],
                env=env,
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._set_label(
                "_run_status",
                "SRMLInstaller is running.\nClose it when you are done, then click Done.",
                color="#6bc76b",
            )
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            proc.wait()
            self._log("SRML Wizard: SRMLInstaller closed.")
            self._set_label("_run_status", "SRMLInstaller finished.", color="#6bc76b")
        except Exception as exc:
            self._set_label("_run_status", f"Launch error: {exc}", color="#e06c6c")
            self._log(f"SRML Wizard: launch error: {exc}")

    # ------------------------------------------------------------------
    # Step 5 — Cleanup
    # ------------------------------------------------------------------

    def _finish_cleanup(self, installer_exe: Path):
        """Remove archive and extracted installer, then close."""
        removed_count = 0

        if self._archive_path and self._archive_path.is_file():
            try:
                self._archive_path.unlink()
                removed_count += 1
            except OSError:
                pass

        for p in self._extracted_paths:
            try:
                if p.is_file():
                    p.unlink()
                    removed_count += 1
                elif p.is_dir() and not any(p.iterdir()):
                    p.rmdir()
                    removed_count += 1
            except OSError:
                pass

        if removed_count:
            self._log(f"SRML Wizard: cleaned up {removed_count} file(s).")

        self._on_close_cb()
