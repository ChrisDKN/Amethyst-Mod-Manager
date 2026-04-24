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

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL,
    TEXT_ON_ACCENT,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD, FONT_SMALL,
)

_RESOURCES_GPAK = "resources.gpak"
_UNPACKED_DIR = "Unpacked"


class MewgenicsGpakWizard(ctk.CTkFrame):
    """Wizard to unpack or repack resources.gpak in the game root."""

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
        self._log_fn = log_fn or (lambda _: None)
        self._game_root: Path | None = game.get_game_path()
        self._running = False

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"GPAK tools \u2014 {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4, pady=4)

        self._build()

    def _on_close(self):
        if self._running:
            return
        self._on_close_cb()

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
                fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
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
            text_color=TEXT_ON_ACCENT,
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
            text_color=TEXT_ON_ACCENT,
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
