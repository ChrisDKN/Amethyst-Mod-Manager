"""
dtkit_patch.py
Wizard for downloading and running dtkit-patch for Darktide.

dtkit-patch is a native Linux tool that patches Darktide's executable to
enable the Darktide Mod Loader.  It must be re-run after every game update.

Workflow:
  1. Fetch the latest Linux release from GitHub and auto-download it,
     or let the user browse for a manually downloaded archive/binary.
     The binary is stored persistently in
     ~/.config/AmethystModManager/Tools/dtkit-patch/
     so subsequent wizard runs reuse it without re-downloading.
  2. Run dtkit-patch natively (no Proton) against
     <game_path>/binaries/Darktide.exe, showing live output.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from Utils.portal_filechooser import pick_file
from Utils.dtkit_patch_helper import (
    _ARCHIVE_EXTS,
    _TOOLS_DIR,
    _is_archive,
    _fetch_latest_linux_asset,
    _install_from_archive,
    _install_bare_binary,
    get_installed_dtkit_path,
    _GITHUB_API_URL,
)

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL, BORDER,
    TEXT_ON_ACCENT,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD, FONT_SMALL,
)

# ---------------------------------------------------------------------------
# Wizard-only helpers
# ---------------------------------------------------------------------------

def _get_downloads_dir() -> Path:
    xdg = os.environ.get("XDG_DOWNLOAD_DIR")
    if xdg:
        return Path(xdg)
    return Path.home() / "Downloads"


# ============================================================================
# Wizard dialog
# ============================================================================

class DtkitPatchWizard(ctk.CTkFrame):
    """Two-step wizard: download dtkit-patch, then run it against Darktide.exe."""

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
        self._binary_path: Path | None = get_installed_dtkit_path()
        self._archive_path: Path | None = None

        # --- Title bar ---
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Patch Game (dtkit-patch) \u2014 {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4, pady=4)

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        self._show_step_get_binary()

    def _on_cancel(self):
        self._on_close_cb()

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    # ------------------------------------------------------------------
    # Step 1 — Get the dtkit-patch binary
    # ------------------------------------------------------------------

    def _show_step_get_binary(self):
        """If we already have dtkit-patch installed, skip straight to running it."""
        if self._binary_path is not None:
            self._show_step_run()
            return
        self._show_step_download()

    def _show_step_download(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Get dtkit-patch",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))

        ctk.CTkLabel(
            self._body,
            text=(
                "dtkit-patch patches Darktide's executable to enable the Mod Loader.\n"
                "A native Linux binary will be downloaded from GitHub.\n\n"
                "The binary is saved to:\n"
                f"{_TOOLS_DIR}\n\n"
                "It will be reused on future runs without re-downloading."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM,
            justify="center", wraplength=500,
        ).pack(pady=(0, 12))

        self._dl_status = ctk.CTkLabel(
            self._body, text="Checking for the latest release\u2026",
            font=FONT_SMALL, text_color=TEXT_DIM, justify="center",
            wraplength=500,
        )
        self._dl_status.pack(pady=(0, 8))

        self._dl_progress = ctk.CTkProgressBar(self._body, width=420, mode="indeterminate")
        self._dl_progress.pack(pady=(0, 12))
        self._dl_progress.start()

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        self._dl_next_btn = ctk.CTkButton(
            btn_frame, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_download_next, state="disabled",
        )
        self._dl_next_btn.pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Browse\u2026", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._browse_binary,
        ).pack(side="right")

        threading.Thread(target=self._do_fetch_and_download, daemon=True).start()

    def _do_fetch_and_download(self):
        try:
            self._set_dl_status("Fetching latest release from GitHub\u2026")
            tag, asset_name, url = _fetch_latest_linux_asset(_GITHUB_API_URL)
            filename = url.split("/")[-1]
            dest = _get_downloads_dir() / filename
            self._set_dl_status(f"Downloading {tag} ({asset_name})\u2026")
            self._log(f"dtkit-patch wizard: downloading {url} → {dest}")

            def _reporthook(block_num, block_size, total_size):
                if total_size > 0:
                    pct = min(block_num * block_size / total_size, 1.0)
                    try:
                        self.after(0, lambda p=pct: (
                            self._dl_progress.configure(mode="determinate"),
                            self._dl_progress.set(p),
                        ))
                    except Exception:
                        pass

            urllib.request.urlretrieve(url, dest, reporthook=_reporthook)
            self._archive_path = dest
            self._log(f"dtkit-patch wizard: downloaded {filename}")
            self.after(0, lambda: self._dl_progress.stop())
            self.after(0, lambda: self._dl_progress.configure(mode="determinate"))
            self.after(0, lambda: self._dl_progress.set(1.0))
            self._set_dl_status(f"Downloaded {tag}: {filename}", color="#6bc76b")
            self.after(0, lambda: self._dl_next_btn.configure(state="normal"))
        except Exception as exc:
            self._log(f"dtkit-patch wizard: download error: {exc}")
            self.after(0, lambda: self._dl_progress.stop())
            self._set_dl_status(
                f"Download failed: {exc}\n\nUse Browse to select a manually downloaded file.",
                color="#e06c6c",
            )
            self.after(0, lambda: self._dl_next_btn.configure(state="normal"))

    def _set_dl_status(self, text: str, color: str = TEXT_DIM):
        try:
            self.after(0, lambda: self._dl_status.configure(text=text, text_color=color))
        except Exception:
            pass

    def _browse_binary(self):
        def _on_picked(path: Path | None) -> None:
            if path and path.is_file():
                self._archive_path = path
                low = path.name.lower()
                if any(low.endswith(ext) for ext in _ARCHIVE_EXTS):
                    label = f"Selected archive: {path.name}"
                else:
                    label = f"Selected binary: {path.name}"
                self._set_dl_status(label, color="#6bc76b")
                try:
                    self.after(0, lambda: self._dl_next_btn.configure(state="normal"))
                except Exception:
                    pass

        pick_file("Select dtkit-patch binary or archive", lambda p: self.after(0, lambda: _on_picked(p)))

    def _on_download_next(self):
        """Install the downloaded file to the persistent tools directory, then proceed."""
        src = self._archive_path
        if src is None or not src.is_file():
            self._set_dl_status("No file selected.", color="#e06c6c")
            return

        self._set_dl_status("Installing dtkit-patch\u2026")
        self.after(0, lambda: self._dl_next_btn.configure(state="disabled"))

        def _worker():
            try:
                if _is_archive(src.name):
                    binary = _install_from_archive(src, _TOOLS_DIR)
                else:
                    binary = _install_bare_binary(src, _TOOLS_DIR)
                self._binary_path = binary
                self._log(f"dtkit-patch wizard: installed to {binary}")
                self.after(0, self._show_step_run)
            except Exception as exc:
                self._log(f"dtkit-patch wizard: install error: {exc}")
                self._set_dl_status(f"Install failed: {exc}", color="#e06c6c")
                self.after(0, lambda: self._dl_next_btn.configure(state="normal"))

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Step 2 — Run dtkit-patch
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body,
            text="Step 2: Run dtkit-patch",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))

        game_path = self._game.get_game_path()
        binary_path = self._binary_path

        if game_path is None or not game_path.is_dir():
            status_text = (
                "Game path not configured.\n\n"
                "Make sure the game path is set correctly."
            )
            ctk.CTkLabel(
                self._body, text=status_text,
                font=FONT_NORMAL, text_color="#e06c6c",
                justify="center", wraplength=500,
            ).pack(pady=(0, 16))
            ctk.CTkButton(
                self._body, text="Close", width=120, height=36,
                font=FONT_BOLD,
                fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
                command=self._on_cancel,
            ).pack(side="bottom")
            return

        ctk.CTkLabel(
            self._body,
            text=(
                f"dtkit-patch binary:\n{binary_path}\n\n"
                f"Game folder (cwd):\n{game_path}\n\n"
                "Patch — enable Darktide Mod Loader.\n"
                "Unpatch — restore the unmodified database.\n"
                "Re-patch after every game update."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM,
            justify="center", wraplength=500,
        ).pack(pady=(0, 12))

        self._run_output = ctk.CTkTextbox(
            self._body, height=140, font=("Courier New", 12),
            fg_color=BG_PANEL, text_color=TEXT_MAIN,
            state="disabled",
        )
        self._run_output.pack(fill="x", pady=(0, 8))

        self._run_status = ctk.CTkLabel(
            self._body, text="", font=FONT_NORMAL, text_color=TEXT_DIM,
            wraplength=500, justify="center",
        )
        self._run_status.pack(pady=(0, 4))

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        self._done_btn = ctk.CTkButton(
            btn_frame, text="Done", width=120, height=36,
            font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._on_cancel, state="disabled",
        )
        self._done_btn.pack(side="right", padx=(8, 0))

        self._run_btn = ctk.CTkButton(
            btn_frame, text="Patch", width=120, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=lambda: self._do_run_dtkit(game_path, "--patch"),
        )
        self._run_btn.pack(side="right")

        self._unpatch_btn = ctk.CTkButton(
            btn_frame, text="Unpatch", width=120, height=36,
            font=FONT_BOLD,
            fg_color="#7a3a2d", hover_color="#9e4a38", text_color="white",
            command=lambda: self._do_run_dtkit(game_path, "--unpatch"),
        )
        self._unpatch_btn.pack(side="right", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Re-download", width=130, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._force_redownload,
        ).pack(side="right", padx=(0, 8))

    def _append_output(self, text: str) -> None:
        try:
            self.after(0, lambda: (
                self._run_output.configure(state="normal"),
                self._run_output.insert("end", text + "\n"),
                self._run_output.configure(state="disabled"),
                self._run_output.see("end"),
            ))
        except Exception:
            pass

    def _do_run_dtkit(self, game_path: Path, flag: str) -> None:
        """Run dtkit-patch with *flag* (--patch or --unpatch) in a background thread."""
        self._run_btn.configure(state="disabled")
        self._unpatch_btn.configure(state="disabled")
        binary = self._binary_path
        if binary is None or not binary.is_file():
            self._set_run_status("dtkit-patch binary not found. Please complete Step 1 first.", color="#e06c6c")
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            self.after(0, lambda: self._run_btn.configure(state="normal"))
            self.after(0, lambda: self._unpatch_btn.configure(state="normal"))
            return

        action = "patch" if flag == "--patch" else "unpatch"

        def _worker():
            try:
                self._set_run_status(f"Running dtkit-patch {flag}\u2026")
                self._log(f"dtkit-patch wizard: running {binary} {flag} bundle (cwd={game_path})")
                result = subprocess.run(
                    [str(binary), flag, "bundle"],
                    capture_output=True,
                    text=True,
                    cwd=str(game_path),
                )
                stdout = result.stdout.strip()
                stderr = result.stderr.strip()
                if stdout:
                    for line in stdout.splitlines():
                        self._append_output(line)
                        self._log(f"dtkit-patch: {line}")
                if stderr:
                    for line in stderr.splitlines():
                        self._append_output(f"[stderr] {line}")
                        self._log(f"dtkit-patch stderr: {line}")
                if result.returncode == 0:
                    if flag == "--patch":
                        msg = "Patch applied successfully!\nDarktide Mod Loader is now enabled."
                    else:
                        msg = "Unpatch applied successfully!\nDarktide Mod Loader is now disabled."
                    self._set_run_status(msg, color="#6bc76b")
                    self._log(f"dtkit-patch wizard: {action} succeeded.")
                else:
                    self._set_run_status(
                        f"dtkit-patch exited with code {result.returncode}.\n"
                        "Check the output above for details.",
                        color="#e06c6c",
                    )
                    self._log(f"dtkit-patch wizard: {action} failed (exit {result.returncode}).")
            except Exception as exc:
                self._set_run_status(f"Error running dtkit-patch: {exc}", color="#e06c6c")
                self._log(f"dtkit-patch wizard: run error: {exc}")
            finally:
                self.after(0, lambda: self._done_btn.configure(state="normal"))
                self.after(0, lambda: self._run_btn.configure(state="normal"))
                self.after(0, lambda: self._unpatch_btn.configure(state="normal"))

        threading.Thread(target=_worker, daemon=True).start()

    def _set_run_status(self, text: str, color: str = TEXT_DIM):
        try:
            self.after(0, lambda: self._run_status.configure(text=text, text_color=color))
        except Exception:
            pass

    def _force_redownload(self):
        """Remove the cached binary and go back to the download step."""
        if _TOOLS_DIR.is_dir():
            shutil.rmtree(_TOOLS_DIR, ignore_errors=True)
        self._binary_path = None
        self._archive_path = None
        self._show_step_download()
