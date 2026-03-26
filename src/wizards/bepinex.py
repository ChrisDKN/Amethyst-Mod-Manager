"""
BepInEx installation wizard.

Multi-step dialog that walks the user through:
  1. Downloading BepInEx from a provided URL
  2. Locating the downloaded archive in ~/Downloads
  3. Extracting it to the game root, chmod-ing shell scripts, and deleting
     the archive
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tarfile
import threading
from Utils.xdg import open_url
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


def _extract_to_dir(archive: Path, dest: Path) -> None:
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
                    "Cannot extract .7z archive: the 7z command was not found "
                    "and py7zr is not installed."
                )
            with py7zr.SevenZipFile(archive, "r") as zf:
                zf.extractall(dest)

    elif name_lower.endswith((".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
        with tarfile.open(archive, "r:*") as tf:
            tf.extractall(dest)
    else:
        raise RuntimeError(f"Unsupported archive format: {archive.name}")


def _extract_bepinex(archive: Path, dest: Path, inner_folder: str) -> list[Path]:
    """Extract *archive*, locate *inner_folder* inside it, and move its
    contents into *dest*.

    Returns created paths in reverse depth order.
    """
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    try:
        _extract_to_dir(archive, tmp)

        src = tmp / inner_folder if inner_folder else tmp
        if not src.is_dir():
            for d in tmp.rglob(inner_folder):
                if d.is_dir():
                    src = d
                    break
        if not src.is_dir():
            raise RuntimeError(
                f"Could not find '{inner_folder}' folder inside the archive."
            )

        created: list[Path] = []
        for root, _dirs, files in os.walk(src):
            for f in files:
                src_file = Path(root) / f
                rel = src_file.relative_to(src)
                dst_file = dest / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_file), str(dst_file))
                created.append(dst_file)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    dirs: set[Path] = set()
    for p in created:
        rel = p.relative_to(dest)
        for parent in rel.parents:
            if parent != Path("."):
                dirs.add(dest / parent)

    return list(created) + sorted(dirs, key=lambda p: len(p.parts), reverse=True)


# ============================================================================
# Wizard dialog
# ============================================================================

class BepInExWizard(ctk.CTkFrame):
    """Step-by-step wizard to download and install BepInEx."""

    def __init__(
        self,
        parent,
        game: "BaseGame",
        log_fn=None,
        *,
        on_close=None,
        download_url: str = "",
        archive_keywords: list[str] | None = None,
        inner_folder: str = "",
        chmod_files: list[str] | None = None,
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close_cb = on_close or (lambda: None)

        self._game = game
        self._log = log_fn or (lambda msg: None)
        self._parent_widget = parent
        self._download_url = download_url
        self._archive_keywords = [kw.lower() for kw in (archive_keywords or [])]
        self._inner_folder = inner_folder
        self._chmod_files = chmod_files or []
        self._archive_path: Path | None = None
        self._game_root: Path | None = game.get_game_path()
        self._install_to_root_folder = ctk.BooleanVar(value=False)

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Install BepInEx \u2014 {game.name}",
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
    # Step 1 \u2014 Download prompt
    # ------------------------------------------------------------------

    def _show_step_download(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download BepInEx",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                f"You need to manually download BepInEx\n"
                f"for {self._game.name}.\n\n"
                "Click the button below to open the download page,\n"
                "then click Next once the download is complete."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 16))

        btn_state = "normal" if self._download_url else "disabled"
        ctk.CTkButton(
            self._body, text="Download BepInEx", width=220, height=36,
            font=FONT_BOLD,
            fg_color="#da8e35", hover_color="#e5a04a", text_color="white",
            command=lambda: open_url(self._download_url),
            state=btn_state,
        ).pack(pady=(0, 20))

        if not self._download_url:
            ctk.CTkLabel(
                self._body,
                text="(Download URL not configured yet.)",
                font=FONT_SMALL, text_color="#e06c6c",
            ).pack(pady=(0, 8))

        ctk.CTkCheckBox(
            self._body,
            text="Install to Root_Folder (staging) instead of game folder",
            variable=self._install_to_root_folder,
            font=FONT_SMALL, text_color=TEXT_DIM,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            checkmark_color="white",
        ).pack(pady=(0, 12))

        ctk.CTkButton(
            self._body, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._show_step_locate,
        ).pack(side="bottom")

    # ------------------------------------------------------------------
    # Step 2 \u2014 Locate archive
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
        )
        self._locate_status.pack(pady=(0, 12))

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        self._next_btn = ctk.CTkButton(
            btn_frame, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._show_step_extract, state="disabled",
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
        found = _find_archive(dl_dir, self._archive_keywords)
        if found:
            self._archive_path = found
            self._locate_status.configure(
                text=f"Found: {found.name}", text_color="#6bc76b",
            )
            self._next_btn.configure(state="normal")
        else:
            self._archive_path = None
            if not self._archive_keywords:
                msg = (
                    "Archive keywords not configured yet.\n"
                    "Use Browse to select the archive manually."
                )
            else:
                msg = (
                    "Archive not found in Downloads.\n"
                    "Make sure you downloaded it, then press Try Again,\n"
                    "or use Browse to select it manually."
                )
            self._locate_status.configure(text=msg, text_color="#e06c6c")
            self._next_btn.configure(state="disabled")

    def _browse_archive(self):
        def _on_picked(path: Path | None) -> None:
            if path and path.is_file():
                self._archive_path = path
                self._locate_status.configure(
                    text=f"Selected: {path.name}", text_color="#6bc76b",
                )
                self._next_btn.configure(state="normal")

        pick_file("Select the BepInEx archive", lambda p: self.after(0, lambda: _on_picked(p)))

    # ------------------------------------------------------------------
    # Step 3 \u2014 Extract & set up
    # ------------------------------------------------------------------

    def _show_step_extract(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Install BepInEx",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._run_status = ctk.CTkLabel(
            self._body, text="Extracting archive to game folder\u2026",
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

        threading.Thread(target=self._do_extract, daemon=True).start()

    def _do_extract(self):
        try:
            if self._install_to_root_folder.get():
                game_root = self._game.get_effective_root_folder_path()
                game_root.mkdir(parents=True, exist_ok=True)
            else:
                game_root = self._game_root
            if game_root is None:
                raise RuntimeError("Game path is not configured.")

            archive = self._archive_path
            if archive is None or not archive.is_file():
                raise RuntimeError("Archive not found.")

            self._set_status("Restoring game to vanilla state\u2026")
            try:
                self._game.restore(log_fn=self._log)
            except Exception as exc:
                self._log(f"Wizard: restore skipped or failed: {exc}")

            self._set_status("Extracting archive to game folder\u2026")
            self._log(f"Wizard: extracting {archive.name} \u2192 {game_root}")

            paths = _extract_bepinex(archive, game_root, self._inner_folder)
            file_count = len([p for p in paths if p.is_file()])
            self._log(f"Wizard: extracted {file_count} file(s).")

            for name in self._chmod_files:
                target = game_root / name
                if target.is_file():
                    current = target.stat().st_mode
                    target.chmod(current | stat.S_IXUSR)
                    self._log(f"Wizard: chmod u+x {name}")

            try:
                archive.unlink()
                self._log(f"Wizard: deleted {archive.name} from Downloads.")
            except OSError as exc:
                self._log(f"Wizard: could not delete archive: {exc}")

            steam_hint = ""
            if self._chmod_files and any(
                "start_game_bepinex" in f for f in self._chmod_files
            ):
                steam_hint = (
                    "\n\nSet the following Steam launch option:\n"
                    "  ./start_game_bepinex.sh %command%"
                )

            dest_label = "Root_Folder (staging)" if self._install_to_root_folder.get() else "game folder"
            self._set_status(
                f"BepInEx installed successfully!\n"
                f"{file_count} file(s) extracted to the {dest_label}."
                f"{steam_hint}\n\n"
                "Click Done to close.",
                color="#6bc76b",
            )
            self._enable_done()

        except Exception as exc:
            self._set_status(f"Error: {exc}", color="#e06c6c")
            self._log(f"Wizard error: {exc}")
            self._enable_done()

    # ------------------------------------------------------------------
    # Finish
    # ------------------------------------------------------------------

    def _finish(self):
        self._log("Wizard: BepInEx installation wizard finished.")
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
