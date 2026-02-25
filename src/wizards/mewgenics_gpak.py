"""
Mewgenics GPAK wizard.

Simple dialog with two options:
  1. Unpack resources.gpak in the game root to Unpacked/
  2. Repack the Unpacked/ folder in the game root to resources.gpak
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

if TYPE_CHECKING:
    from Games.base_game import BaseGame

# ---------------------------------------------------------------------------
# Theme constants (kept in sync with gui.py)
# ---------------------------------------------------------------------------
BG_DEEP = "#1a1a1a"
BG_PANEL = "#252526"
ACCENT = "#0078d4"
ACCENT_HOV = "#1084d8"
TEXT_MAIN = "#d4d4d4"
TEXT_DIM = "#858585"

FONT_NORMAL = ("Segoe UI", 14)
FONT_BOLD = ("Segoe UI", 14, "bold")
FONT_SMALL = ("Segoe UI", 12)

_RESOURCES_GPAK = "resources.gpak"
_UNPACKED_DIR = "Unpacked"


class MewgenicsGpakWizard(ctk.CTkToplevel):
    """Wizard to unpack or repack resources.gpak in the game root."""

    def __init__(
        self,
        parent,
        game: "BaseGame",
        log_fn=None,
    ):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"GPAK tools — {game.name}")
        self.geometry("480x360")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._make_modal)

        self._game = game
        self._log_fn = log_fn or (lambda _: None)
        self._game_root: Path | None = game.get_game_path()
        self._running = False

        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_close(self):
        if self._running:
            return
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _log(self, msg: str):
        self._log_fn(msg)
        try:
            self._log_text.configure(state="normal")
            self._log_text.insert("end", msg + "\n")
            self._log_text.see("end")
            self._log_text.configure(state="disabled")
        except Exception:
            pass

    def _build(self):
        body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        body.pack(fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(
            body,
            text="GPAK tools",
            font=FONT_BOLD,
            text_color=TEXT_MAIN,
        ).pack(pady=(0, 4))

        if not self._game_root or not self._game_root.is_dir():
            ctk.CTkLabel(
                body,
                text="Game path is not set or invalid.",
                font=FONT_NORMAL,
                text_color="#e06c6c",
            ).pack(pady=12)
            ctk.CTkButton(
                body, text="Close", width=100, height=32,
                font=FONT_BOLD,
                fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
                command=self._on_close,
            ).pack(pady=12)
            return

        root_str = str(self._game_root)
        ctk.CTkLabel(
            body,
            text=f"Game root: {root_str}",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
            wraplength=420,
        ).pack(anchor="w", pady=(0, 12))

        btn_frame = ctk.CTkFrame(body, fg_color="transparent")
        btn_frame.pack(fill="x", pady=(0, 12))

        ctk.CTkButton(
            btn_frame,
            text="Unpack resources.gpak",
            width=200,
            height=36,
            font=FONT_BOLD,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            text_color="white",
            command=self._do_unpack,
        ).pack(side="left", padx=(0, 12))

        ctk.CTkButton(
            btn_frame,
            text="Repack Unpacked folder",
            width=200,
            height=36,
            font=FONT_BOLD,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            text_color="white",
            command=self._do_repack,
        ).pack(side="left")

        ctk.CTkLabel(
            body,
            text="Log:",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
        ).pack(anchor="w", pady=(4, 2))

        self._log_text = ctk.CTkTextbox(
            body,
            font=("Consolas", 12),
            fg_color=BG_PANEL,
            text_color=TEXT_MAIN,
            height=140,
            state="disabled",
        )
        self._log_text.pack(fill="both", expand=True, pady=(0, 8))

        ctk.CTkButton(
            body,
            text="Close",
            width=100,
            height=32,
            font=FONT_BOLD,
            fg_color=BG_PANEL,
            hover_color="#3d3d3d",
            text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(anchor="e")

    def _do_unpack(self):
        if self._running or not self._game_root:
            return
        resources = self._game_root / _RESOURCES_GPAK
        unpack_dir = self._game_root / _UNPACKED_DIR
        if not resources.is_file():
            self._log(f"'{_RESOURCES_GPAK}' not found in game root.")
            return
        self._running = True
        self._log("Unpacking resources.gpak…")

        def run():
            try:
                from gpak import extract_gpak
                if unpack_dir.exists():
                    self.after(0, lambda: self._log("Removing previous Unpacked folder…"))
                    shutil.rmtree(unpack_dir)
                extract_gpak(resources, unpack_dir, try_zlib=True)
                self.after(0, lambda: self._log("Unpack complete."))
            except Exception as e:
                self.after(0, lambda: self._log(f"Error: {e}"))
            finally:
                self.after(0, lambda: setattr(self, "_running", False))

        threading.Thread(target=run, daemon=True).start()

    def _do_repack(self):
        if self._running or not self._game_root:
            return
        unpack_dir = self._game_root / _UNPACKED_DIR
        resources = self._game_root / _RESOURCES_GPAK
        if not unpack_dir.is_dir():
            self._log(f"'{_UNPACKED_DIR}' folder not found. Unpack first.")
            return
        self._running = True
        self._log("Repacking to resources.gpak…")

        def run():
            try:
                from gpak import pack_gpak
                pack_gpak(unpack_dir, resources, compress=False)
                self.after(0, lambda: self._log("Repack complete."))
            except Exception as e:
                self.after(0, lambda: self._log(f"Error: {e}"))
            finally:
                self.after(0, lambda: setattr(self, "_running", False))

        threading.Thread(target=run, daemon=True).start()
