"""
Fallout 3 Downgrade Wizard.

Multi-step dialog that walks the user through:
  1. Downloading the Fallout Anniversary Patcher from Nexus Mods
  2. Locating the downloaded archive in ~/Downloads
  3. Extracting it to the game root and running Patcher.exe via Proton
  4. Cleaning up the extracted files when finished
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
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
# Nexus mod URL
# ---------------------------------------------------------------------------
_NEXUS_URL = "https://www.nexusmods.com/fallout3/mods/24913"

# Glob fragments used to locate the downloaded archive (case-insensitive)
_ARCHIVE_KEYWORDS = ["fallout", "anniversary", "patcher"]
_ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz"}

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
    for ext in _ARCHIVE_EXTS:
        if low.endswith(ext):
            return True
    return False


def _find_patcher_archive(directory: Path) -> Path | None:
    """Search *directory* for an archive whose name matches the patcher."""
    if not directory.is_dir():
        return None
    for entry in sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not entry.is_file() or not _is_archive(entry.name):
            continue
        low = entry.name.lower()
        if all(kw in low for kw in _ARCHIVE_KEYWORDS):
            return entry
    return None


def _extract_archive(archive: Path, dest: Path) -> list[Path]:
    """Extract *archive* into *dest* and return a list of all created paths.

    Returns paths in **reverse depth order** (deepest first) so that
    callers can delete files before their parent directories.
    """
    created: list[Path] = []

    name_lower = archive.name.lower()

    if name_lower.endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                zf.extract(info, dest)
                created.append(dest / info.filename)

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
                    "Cannot extract .7z archive: the 7z command was not found "
                    "and py7zr is not installed."
                )
            with py7zr.SevenZipFile(archive, "r") as zf:
                zf.extractall(dest)

        for root, _dirs, files in os.walk(dest):
            for f in files:
                p = Path(root) / f
                created.append(p)

    elif name_lower.endswith((".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
        with tarfile.open(archive, "r:*") as tf:
            tf.extractall(dest)
            for m in tf.getmembers():
                if m.isfile():
                    created.append(dest / m.name)
    else:
        raise RuntimeError(f"Unsupported archive format: {archive.name}")

    # Also collect directories so we can clean them up later
    dirs: set[Path] = set()
    for p in created:
        rel = p.relative_to(dest)
        for parent in rel.parents:
            if parent != Path("."):
                dirs.add(dest / parent)

    all_paths = list(created) + sorted(dirs, key=lambda p: len(p.parts), reverse=True)
    return all_paths


# ============================================================================
# Wizard dialog
# ============================================================================

class FalloutDowngradeWizard(ctk.CTkToplevel):
    """Step-by-step wizard to downgrade Fallout 3 for script extender compat."""

    def __init__(self, parent, game: "BaseGame", log_fn=None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Downgrade Fallout 3")
        self.geometry("520x380")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self._game = game
        self._log = log_fn or (lambda msg: None)
        self._parent_widget = parent
        self._archive_path: Path | None = None
        self._extracted_paths: list[Path] = []
        self._game_root: Path | None = game.get_game_path()

        # Build a container that each step fills
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
        self._cleanup_extracted()
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
            self._body, text="Step 1: Download the Patcher",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                "To downgrade Fallout 3 you need the\n"
                "Fallout Anniversary Patcher from Nexus Mods.\n\n"
                "Click the button below to open the mod page,\n"
                "then download the main file."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 16))

        ctk.CTkButton(
            self._body, text="Open Nexus Mods Page", width=220, height=36,
            font=FONT_BOLD,
            fg_color="#da8e35", hover_color="#e5a04a", text_color="white",
            command=lambda: webbrowser.open(_NEXUS_URL),
        ).pack(pady=(0, 20))

        ctk.CTkButton(
            self._body, text="Next →", width=120, height=36,
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
            self._body, text="Searching Downloads folder…",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
        )
        self._locate_status.pack(pady=(0, 12))

        # Button row
        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        self._next_btn = ctk.CTkButton(
            btn_frame, text="Next →", width=120, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._show_step_run, state="disabled",
        )
        self._next_btn.pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Try Again", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._scan_downloads,
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Browse…", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._browse_archive,
        ).pack(side="right")

        self._scan_downloads()

    def _scan_downloads(self):
        """Look for the patcher archive in ~/Downloads."""
        dl_dir = _get_downloads_dir()
        found = _find_patcher_archive(dl_dir)
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
                    "Archive not found in Downloads.\n"
                    "Make sure you downloaded the mod, then press Try Again,\n"
                    "or use Browse to select it manually."
                ),
                text_color="#e06c6c",
            )
            self._next_btn.configure(state="disabled")

    def _browse_archive(self):
        """Let the user pick the archive manually via zenity."""
        try:
            result = subprocess.run(
                [
                    "zenity", "--file-selection",
                    "--title=Select the Fallout Anniversary Patcher archive",
                    "--file-filter=Archives (*.zip *.7z *.rar *.tar*) | *.zip *.7z *.rar *.tar *.tar.gz *.tar.bz2 *.tar.xz *.tgz",
                    "--file-filter=All files | *",
                ],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return
            path = Path(result.stdout.strip())
        except FileNotFoundError:
            self._log("Wizard: zenity not found — cannot open file picker.")
            return

        if path.is_file():
            self._archive_path = path
            self._locate_status.configure(
                text=f"Selected: {path.name}", text_color="#6bc76b",
            )
            self._next_btn.configure(state="normal")

    # ------------------------------------------------------------------
    # Step 3 — Extract & Run
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Extract & Run Patcher",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._run_status = ctk.CTkLabel(
            self._body, text="Extracting archive to game folder…",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
            wraplength=460,
        )
        self._run_status.pack(pady=(0, 16))

        self._done_btn = ctk.CTkButton(
            self._body, text="Done", width=120, height=36,
            font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._finish, state="disabled",
        )
        self._done_btn.pack(side="bottom")

        # Run extraction + patcher in a background thread
        threading.Thread(target=self._extract_and_run, daemon=True).start()

    def _extract_and_run(self):
        """Background worker: extract archive → run Patcher.exe via Proton."""
        try:
            self._do_extract()
            self._do_run_patcher()
        except Exception as exc:
            self._set_status(f"Error: {exc}", color="#e06c6c")
            self._log(f"Wizard error: {exc}")

    def _do_extract(self):
        game_root = self._game_root
        if game_root is None:
            raise RuntimeError("Game path is not configured.")

        archive = self._archive_path
        if archive is None or not archive.is_file():
            raise RuntimeError("Archive not found.")

        self._set_status("Extracting archive to game folder…")
        self._log(f"Wizard: extracting {archive.name} → {game_root}")

        paths = _extract_archive(archive, game_root)
        self._extracted_paths = paths
        self._log(f"Wizard: extracted {len([p for p in paths if p.is_file()])} file(s).")

    def _do_run_patcher(self):
        """Locate Patcher.exe and run it through Proton."""
        game_root = self._game_root
        if game_root is None:
            raise RuntimeError("Game path is not configured.")

        # Search for Patcher.exe (may be inside a subfolder)
        patcher_exe: Path | None = None
        for p in self._extracted_paths:
            if p.is_file() and p.name.lower() == "patcher.exe":
                patcher_exe = p
                break

        if patcher_exe is None:
            # Broader search
            for p in game_root.rglob("Patcher.exe"):
                patcher_exe = p
                break

        if patcher_exe is None:
            raise RuntimeError(
                "Could not find Patcher.exe after extraction.\n"
                "Make sure you downloaded the correct mod."
            )

        self._set_status(f"Running {patcher_exe.name} via Proton…\nThis may take a moment.")
        self._log(f"Wizard: running {patcher_exe} via Proton")

        proton_script, env = self._get_proton_env()
        if proton_script is None:
            raise RuntimeError("Could not determine Proton version for this game.")

        proc = subprocess.Popen(
            ["python3", str(proton_script), "run", str(patcher_exe)],
            env=env,
            cwd=str(game_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.wait()

        if proc.returncode != 0:
            stderr = (proc.stderr.read() or b"").decode(errors="replace").strip()
            self._log(f"Wizard: Patcher exited with code {proc.returncode}: {stderr}")

        self._set_status(
            "Patcher has finished.\n\n"
            "Click Done to clean up the extracted files and close.",
            color="#6bc76b",
        )
        self._enable_done()
        self._log("Wizard: patcher complete. Waiting for user to click Done.")

    # ------------------------------------------------------------------
    # Proton env (mirrors _ProtonToolsDialog._get_proton_env)
    # ------------------------------------------------------------------

    def _get_proton_env(self):
        from Utils.steam_finder import (
            find_any_installed_proton,
            find_proton_for_game,
            find_steam_root_for_proton_script,
        )

        prefix_path = self._game.get_prefix_path()
        if prefix_path is None or not prefix_path.is_dir():
            self._log("Wizard: prefix not configured for this game.")
            return None, None

        steam_id = getattr(self._game, "steam_id", "")
        proton_script = find_proton_for_game(steam_id) if steam_id else None
        if proton_script is None:
            proton_script = find_any_installed_proton()
            if proton_script is None:
                if steam_id:
                    self._log(f"Wizard: could not find Proton for app {steam_id}, and no installed Proton tool was found.")
                else:
                    self._log("Wizard: game has no Steam ID and no installed Proton tool was found.")
                return None, None
            self._log(
                f"Wizard: using fallback Proton tool {proton_script.parent.name} "
                "(no per-game Steam mapping found)."
            )

        compat_data = prefix_path.parent
        steam_root = find_steam_root_for_proton_script(proton_script)
        if steam_root is None:
            self._log("Wizard: could not determine Steam root for the selected Proton tool.")
            return None, None

        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)

        return proton_script, env

    # ------------------------------------------------------------------
    # Step 4 — Cleanup
    # ------------------------------------------------------------------

    def _finish(self):
        """Clean up extracted files and close the wizard."""
        self._cleanup_extracted()
        self._log("Wizard: cleanup complete. Downgrade wizard finished.")
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _cleanup_extracted(self):
        """Remove every file and directory that was extracted into game root."""
        if not self._extracted_paths:
            return
        game_root = self._game_root
        removed = 0
        for p in self._extracted_paths:
            try:
                if p.is_file() or p.is_symlink():
                    p.unlink()
                    removed += 1
                elif p.is_dir():
                    # Only remove if empty (we already deleted files above)
                    try:
                        p.rmdir()
                        removed += 1
                    except OSError:
                        pass
            except Exception:
                pass
        self._extracted_paths.clear()
        if removed:
            self._log(f"Wizard: removed {removed} extracted item(s) from game root.")

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
