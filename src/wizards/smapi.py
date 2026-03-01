"""
SMAPI installation wizard for Stardew Valley.

Multi-step dialog that walks the user through:
  1. Downloading SMAPI from Nexus Mods
  2. Locating the downloaded zip in ~/Downloads
  3. Extracting the zip and running "install on Linux.sh" in a terminal
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import threading
import webbrowser
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

try:
    import py7zr
except ImportError:
    py7zr = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# ---------------------------------------------------------------------------
# Theme constants (kept in sync with gui.py)
# ---------------------------------------------------------------------------
BG_DEEP    = "#1a1a1a"
BG_PANEL   = "#252526"
BG_HEADER  = "#2a2a2b"
ACCENT     = "#0078d4"
ACCENT_HOV = "#1084d8"
TEXT_MAIN  = "#d4d4d4"
TEXT_DIM   = "#858585"
BORDER     = "#444444"

FONT_NORMAL = ("Segoe UI", 14)
FONT_BOLD   = ("Segoe UI", 14, "bold")
FONT_SMALL  = ("Segoe UI", 12)

_DOWNLOAD_URL = "https://www.nexusmods.com/stardewvalley/mods/2400"
_ARCHIVE_KEYWORDS = ["smapi"]
_ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_downloads_dir() -> Path:
    xdg = os.environ.get("XDG_DOWNLOAD_DIR")
    if xdg:
        return Path(xdg)
    return Path.home() / "Downloads"


def _is_archive(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in _ARCHIVE_EXTS)


def _find_archive(directory: Path, keywords: list[str]) -> Path | None:
    if not directory.is_dir() or not keywords:
        return None
    for entry in sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not entry.is_file() or not _is_archive(entry.name):
            continue
        low = entry.name.lower()
        if all(kw in low for kw in keywords):
            return entry
    return None


def _extract_zip(archive: Path, dest: Path) -> None:
    name_lower = archive.name.lower()
    if name_lower.endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(dest)
    elif name_lower.endswith(".7z"):
        extracted_via_cli = False
        try:
            subprocess.run(
                ["7z", "x", str(archive), f"-o{dest}", "-y"],
                check=True, capture_output=True,
            )
            extracted_via_cli = True
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
        if not extracted_via_cli:
            if py7zr is None:
                raise RuntimeError(
                    "Cannot extract .7z archive: 7z command not found "
                    "and py7zr is not installed."
                )
            with py7zr.SevenZipFile(archive, "r") as zf:
                zf.extractall(dest)
    elif name_lower.endswith((".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
        import tarfile
        with tarfile.open(archive, "r:*") as tf:
            tf.extractall(dest)
    else:
        raise RuntimeError(f"Unsupported archive format: {archive.name}")


# ============================================================================
# Wizard dialog
# ============================================================================

class SmapiWizard(ctk.CTkToplevel):
    """Step-by-step wizard to download and install SMAPI for Stardew Valley."""

    def __init__(
        self,
        parent,
        game: "BaseGame",
        log_fn=None,
    ):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Install SMAPI \u2014 Stardew Valley")
        self.geometry("540x360")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self._game = game
        self._log = log_fn or (lambda msg: None)
        self._archive_path: Path | None = None

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        self._show_step_download()

    # ------------------------------------------------------------------
    # Modal helpers
    # ------------------------------------------------------------------

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_cancel(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    # ------------------------------------------------------------------
    # Step 1 — Download prompt
    # ------------------------------------------------------------------

    def _show_step_download(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download SMAPI",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                "Click the button below to open the SMAPI mod page on Nexus Mods.\n\n"
                "Download the main file (the .zip archive).\n\n"
                "Once the download finishes, click Next \u2192"
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 16))

        ctk.CTkButton(
            self._body, text="Open SMAPI on Nexus Mods", width=240, height=36,
            font=FONT_BOLD,
            fg_color="#da8e35", hover_color="#e5a04a", text_color="white",
            command=lambda: webbrowser.open(_DOWNLOAD_URL),
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
            self._body, text="Step 2: Locate the SMAPI Archive",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._locate_status = ctk.CTkLabel(
            self._body, text="Searching Downloads folder\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
            wraplength=480,
        )
        self._locate_status.pack(pady=(0, 12))

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        self._next_btn = ctk.CTkButton(
            btn_frame, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._show_step_install, state="disabled",
        )
        self._next_btn.pack(side="right", padx=(8, 0))

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
        dl_dir = _get_downloads_dir()
        found = _find_archive(dl_dir, _ARCHIVE_KEYWORDS)
        if found:
            self._archive_path = found
            self._locate_status.configure(
                text=f"Found: {found.name}", text_color="#6bc76b",
            )
            self._next_btn.configure(state="normal")
        else:
            self._archive_path = None
            self._locate_status.configure(
                text=(
                    "SMAPI archive not found in Downloads.\n"
                    "Make sure you downloaded it, then press Try Again,\n"
                    "or use Browse to select it manually."
                ),
                text_color="#e06c6c",
            )
            self._next_btn.configure(state="disabled")

    def _browse_archive(self):
        try:
            result = subprocess.run(
                [
                    "zenity", "--file-selection",
                    "--title=Select the SMAPI archive",
                    "--file-filter=Archives (*.zip *.7z *.rar *.tar*) | *.zip *.7z *.rar *.tar *.tar.gz *.tar.bz2 *.tar.xz *.tgz",
                    "--file-filter=All files | *",
                ],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return
            path = Path(result.stdout.strip())
        except FileNotFoundError:
            self._log("Wizard: zenity not found \u2014 cannot open file picker.")
            return

        if path.is_file():
            self._archive_path = path
            self._locate_status.configure(
                text=f"Selected: {path.name}", text_color="#6bc76b",
            )
            self._next_btn.configure(state="normal")

    # ------------------------------------------------------------------
    # Step 3 — Extract & run installer
    # ------------------------------------------------------------------

    def _show_step_install(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Install SMAPI",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._run_status = ctk.CTkLabel(
            self._body, text="Extracting SMAPI archive\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
            wraplength=480,
        )
        self._run_status.pack(pady=(0, 16))

        self._done_btn = ctk.CTkButton(
            self._body, text="Done", width=120, height=36,
            font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._finish, state="disabled",
        )
        self._done_btn.pack(side="bottom")

        threading.Thread(target=self._do_install, daemon=True).start()

    def _do_install(self):
        tmp_dir: Path | None = None
        try:
            archive = self._archive_path
            if archive is None or not archive.is_file():
                raise RuntimeError("Archive not found.")

            # Extract into a temp directory
            self._set_status("Extracting SMAPI archive\u2026")
            self._log(f"Wizard: extracting {archive.name}")
            tmp_dir = Path(tempfile.mkdtemp(prefix="smapi_install_"))
            _extract_zip(archive, tmp_dir)

            # Find the "install on Linux.sh" script (may be inside a sub-folder)
            script: Path | None = None
            for candidate in tmp_dir.rglob("install on Linux.sh"):
                script = candidate
                break

            if script is None:
                raise RuntimeError(
                    'Could not find "install on Linux.sh" inside the archive.'
                )

            # Make the script and the SMAPI installer binary executable
            script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            installer_bin = script.parent / "internal" / "linux" / "SMAPI.Installer"
            if installer_bin.is_file():
                installer_bin.chmod(
                    installer_bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                )

            self._set_status(
                "Launching the SMAPI installer in a terminal.\n\n"
                "Follow the on-screen prompts, then return here and click Done.",
                color=TEXT_MAIN,
            )
            self._log("Wizard: launching SMAPI installer in terminal")

            # Launch in a terminal emulator (prefer konsole for SteamDeck/KDE)
            script_str = str(script)
            terminal_cmd = _find_terminal_cmd(script_str)
            if terminal_cmd is None:
                raise RuntimeError(
                    "No supported terminal emulator found (tried konsole, alacritty, "
                    "gnome-terminal, xterm). Please run the installer manually:\n"
                    f"  {script_str}"
                )

            proc = subprocess.run(terminal_cmd)
            if proc.returncode != 0:
                self._log(f"Wizard: installer exited with code {proc.returncode}")

            self._set_status(
                "SMAPI installer finished.\n\n"
                "If the installer completed successfully, SMAPI is now installed.\n"
                "Click Done to close.",
                color="#6bc76b",
            )
            self._log("Wizard: SMAPI installer completed.")

            # Clean up archive
            try:
                archive.unlink()
                self._log(f"Wizard: deleted {archive.name} from Downloads.")
            except OSError as exc:
                self._log(f"Wizard: could not delete archive: {exc}")

        except Exception as exc:
            self._set_status(f"Error: {exc}", color="#e06c6c")
            self._log(f"Wizard error: {exc}")
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            self._enable_done()

    # ------------------------------------------------------------------
    # Finish
    # ------------------------------------------------------------------

    def _finish(self):
        self._log("Wizard: SMAPI installation wizard finished.")
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    # ------------------------------------------------------------------
    # Thread-safe UI helpers
    # ------------------------------------------------------------------

    def _set_status(self, text: str, color: str = TEXT_DIM):
        try:
            self.after(0, lambda: self._run_status.configure(text=text, text_color=color))
        except Exception:
            pass

    def _enable_done(self):
        try:
            self.after(0, lambda: self._done_btn.configure(state="normal"))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Terminal detection helper
# ---------------------------------------------------------------------------

def _find_terminal_cmd(script_path: str) -> list[str] | None:
    """Return a command list to run *script_path* inside a terminal emulator,
    or None if no supported emulator is found."""
    candidates = [
        ("konsole", ["konsole", "-e", script_path]),
        ("alacritty", ["alacritty", "-e", script_path]),
        ("gnome-terminal", ["gnome-terminal", "--", script_path]),
        ("xterm", ["xterm", "-e", script_path]),
    ]
    for exe, cmd in candidates:
        if shutil.which(exe):
            return cmd
    return None
