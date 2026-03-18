"""
SMAPI installation wizard for Stardew Valley.

Multi-step dialog that walks the user through:
  1. Fetching the latest SMAPI release from GitHub and downloading it
  2. Optionally browsing for a manually downloaded archive
  3. Extracting the zip and running "install on Linux.sh" in a terminal
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import threading
import urllib.request
import urllib.error
import json as _json
from Utils.portal_filechooser import pick_file
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

_GITHUB_API_URL = "https://api.github.com/repos/Pathoschild/SMAPI/releases/latest"


def _fetch_latest_smapi_asset() -> tuple[str, str]:
    """Return (version_tag, download_url) for the latest SMAPI zip release asset."""
    req = urllib.request.Request(
        _GITHUB_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ModManager/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = _json.loads(resp.read().decode())
    tag = data.get("tag_name", "unknown")
    for asset in data.get("assets", []):
        name: str = asset.get("name", "")
        if name.lower().endswith(".zip") and "smapi" in name.lower():
            return tag, asset["browser_download_url"]
    raise RuntimeError("No SMAPI zip asset found in the latest GitHub release.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_downloads_dir() -> Path:
    xdg = os.environ.get("XDG_DOWNLOAD_DIR")
    if xdg:
        return Path(xdg)
    return Path.home() / "Downloads"


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

class SmapiWizard(ctk.CTkFrame):
    """Step-by-step wizard to download and install SMAPI for Stardew Valley."""

    def __init__(
        self,
        parent,
        game: "BaseGame",
        log_fn=None,
        *,
        on_close=None,
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close_cb = on_close or (lambda: None)

        self._game = game
        self._log = log_fn or (lambda msg: None)
        self._archive_path: Path | None = None
        self._download_url: str | None = None
        self._install_to_root_folder = ctk.BooleanVar(value=False)

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text="Install SMAPI \u2014 Stardew Valley",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4, pady=4)

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        self._show_step_download()

    def _on_cancel(self):
        self._on_close_cb()

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    # ------------------------------------------------------------------
    # Step 1 — Fetch & download latest release from GitHub
    # ------------------------------------------------------------------

    def _show_step_download(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download SMAPI",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._dl_status = ctk.CTkLabel(
            self._body, text="Checking for the latest SMAPI release\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
            wraplength=480,
        )
        self._dl_status.pack(pady=(0, 16))

        self._dl_progress = ctk.CTkProgressBar(self._body, width=400, mode="indeterminate")
        self._dl_progress.pack(pady=(0, 16))
        self._dl_progress.start()

        ctk.CTkCheckBox(
            self._body,
            text="Install to Root_Folder (staging) instead of game folder",
            variable=self._install_to_root_folder,
            font=FONT_SMALL, text_color=TEXT_DIM,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            checkmark_color="white",
        ).pack(pady=(0, 4))

        ctk.CTkLabel(
            self._body,
            text="Note: SMAPI uses an external installer — this option\n"
                 "has no effect on where the installer places files.",
            font=FONT_SMALL, text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 8))

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        self._dl_next_btn = ctk.CTkButton(
            btn_frame, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._show_step_install, state="disabled",
        )
        self._dl_next_btn.pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Browse\u2026", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._browse_archive_step1,
        ).pack(side="right")

        threading.Thread(target=self._do_fetch_and_download, daemon=True).start()

    def _do_fetch_and_download(self):
        try:
            self._set_dl_status("Fetching latest SMAPI release from GitHub\u2026")
            tag, url = _fetch_latest_smapi_asset()
            self._download_url = url
            filename = url.split("/")[-1]
            dest = _get_downloads_dir() / filename
            self._set_dl_status(f"Downloading SMAPI {tag}\u2026")
            self._log(f"Wizard: downloading {url} → {dest}")

            def _reporthook(block_num, block_size, total_size):
                if total_size > 0:
                    pct = min(block_num * block_size / total_size, 1.0)
                    try:
                        self.after(0, lambda p=pct: self._dl_progress.configure(
                            mode="determinate"
                        ) or self._dl_progress.set(p))
                    except Exception:
                        pass

            urllib.request.urlretrieve(url, dest, reporthook=_reporthook)
            self._archive_path = dest
            self._log(f"Wizard: downloaded {filename}")
            self.after(0, lambda: self._dl_progress.stop())
            self.after(0, lambda: self._dl_progress.configure(mode="determinate"))
            self.after(0, lambda: self._dl_progress.set(1.0))
            self._set_dl_status(f"Downloaded SMAPI {tag}: {filename}", color="#6bc76b")
            self.after(0, lambda: self._dl_next_btn.configure(state="normal"))
        except Exception as exc:
            self._log(f"Wizard: download error: {exc}")
            self.after(0, lambda: self._dl_progress.stop())
            self._set_dl_status(
                f"Download failed: {exc}\n\nUse Browse to select a manually downloaded archive.",
                color="#e06c6c",
            )
            self.after(0, lambda: self._dl_next_btn.configure(state="normal"))

    def _set_dl_status(self, text: str, color: str = TEXT_DIM):
        try:
            self.after(0, lambda: self._dl_status.configure(text=text, text_color=color))
        except Exception:
            pass

    def _browse_archive_step1(self):
        def _on_picked(path: Path | None) -> None:
            if path and path.is_file():
                self._archive_path = path
                self._set_dl_status(f"Selected: {path.name}", color="#6bc76b")
                try:
                    self.after(0, lambda: self._dl_next_btn.configure(state="normal"))
                except Exception:
                    pass

        pick_file("Select the SMAPI archive", lambda p: self.after(0, lambda: _on_picked(p)))

    # ------------------------------------------------------------------
    # Step 2 — Extract & run installer
    # ------------------------------------------------------------------

    def _show_step_install(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 2: Install SMAPI",
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
        self._on_close_cb()

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
