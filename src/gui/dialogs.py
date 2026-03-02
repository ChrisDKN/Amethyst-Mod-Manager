"""
Modal dialogs used by ModListPanel, PluginPanel, TopBar, and install_mod.
Uses theme, path_utils; does not import panels or App to avoid circular imports.
"""

import colorsys
import json
import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
import tkinter.messagebox
import tkinter.ttk as ttk
from pathlib import Path

from PIL import Image as _PilImage, ImageDraw as _PilDraw, ImageTk as _PilTk

import customtkinter as ctk

from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_PANEL,
    BORDER,
    FONT_BOLD,
    FONT_HEADER,
    FONT_MONO,
    FONT_NORMAL,
    FONT_SMALL,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_SEP,
    BG_SELECT,
    BG_SEP,
)
from gui.path_utils import _to_wine_path
from Utils.config_paths import get_exe_args_path


# ---------------------------------------------------------------------------
# Themed message helpers (replaces tk.messagebox which ignores dark theme)
# ---------------------------------------------------------------------------

def _center_dialog(dlg, parent, w: int, h: int):
    """Position dlg centered over parent using a known fixed size."""
    try:
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")
    except Exception:
        dlg.geometry(f"{w}x{h}")


def ask_yes_no(title: str, message: str, parent=None) -> bool:
    """Dark-themed yes/no confirmation dialog. Returns True if Yes clicked."""
    result = [False]

    dlg = ctk.CTkToplevel(parent, fg_color=BG_DEEP)
    dlg.title(title)
    dlg.resizable(False, False)
    if parent is not None:
        dlg.transient(parent)
    _center_dialog(dlg, parent, 400, 160)

    # Icon + message
    body = ctk.CTkFrame(dlg, fg_color="transparent")
    body.pack(fill="x", padx=20, pady=(18, 4))
    ctk.CTkLabel(body, text="?", font=("", 28, "bold"),
                 text_color=ACCENT, width=36).pack(side="left", anchor="n", padx=(0, 12))
    ctk.CTkLabel(body, text=message, font=FONT_NORMAL,
                 text_color=TEXT_MAIN, wraplength=300, justify="left").pack(side="left")

    # Buttons
    btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
    btn_row.pack(fill="x", padx=20, pady=(8, 16))
    ctk.CTkButton(btn_row, text="No", width=80, font=FONT_BOLD,
                  fg_color=BG_PANEL, hover_color=BG_HEADER, text_color=TEXT_MAIN,
                  command=dlg.destroy).pack(side="right", padx=(4, 0))
    def _yes():
        result[0] = True
        dlg.destroy()
    ctk.CTkButton(btn_row, text="Yes", width=80, font=FONT_BOLD,
                  fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
                  command=_yes).pack(side="right", padx=4)

    dlg.after(50, dlg.grab_set)
    dlg.wait_window()
    return result[0]


def show_error(title: str, message: str, parent=None) -> None:
    """Dark-themed error dialog."""
    dlg = ctk.CTkToplevel(parent, fg_color=BG_DEEP)
    dlg.title(title)
    dlg.resizable(False, False)
    if parent is not None:
        dlg.transient(parent)
    _center_dialog(dlg, parent, 400, 140)

    body = ctk.CTkFrame(dlg, fg_color="transparent")
    body.pack(fill="x", padx=20, pady=(18, 4))
    ctk.CTkLabel(body, text="✕", font=("", 24, "bold"),
                 text_color="#e06c75", width=36).pack(side="left", anchor="n", padx=(0, 12))
    ctk.CTkLabel(body, text=message, font=FONT_NORMAL,
                 text_color=TEXT_MAIN, wraplength=300, justify="left").pack(side="left")

    btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
    btn_row.pack(fill="x", padx=20, pady=(8, 16))
    ctk.CTkButton(btn_row, text="OK", width=80, font=FONT_BOLD,
                  fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
                  command=dlg.destroy).pack(side="right")

    dlg.after(50, dlg.grab_set)
    dlg.wait_window()


def _build_tree_str(paths: list[str]) -> str:
    """Convert a flat list of slash-separated paths into an ASCII folder tree."""
    root: dict = {}
    for path in sorted(paths):
        node = root
        for part in path.split("/"):
            node = node.setdefault(part, {})

    lines: list[str] = []

    def _walk(node: dict, prefix: str):
        items = sorted(node.keys())
        for i, name in enumerate(items):
            is_last = (i == len(items) - 1)
            lines.append(f"{prefix}{'└── ' if is_last else '├── '}{name}")
            child = node[name]
            if child:
                _walk(child, prefix + ("    " if is_last else "│   "))

    _walk(root, "")
    return "\n".join(lines) if lines else "(no files)"


# ---------------------------------------------------------------------------
# Game picker dialog — card grid
# ---------------------------------------------------------------------------
class _GamePickerDialog(ctk.CTkToplevel):
    _CARD_W  = 160
    _CARD_H  = 200
    _IMG_H   = 130
    _COLS    = 4
    _PAD     = 12

    def __init__(self, parent, game_names: list[str], games: dict | None = None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Add Game")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.result: str | None = None

        self._games = games or {}
        self._icons_dir = Path(__file__).resolve().parent.parent / "icons" / "games"

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self, text="Select a game to add / reconfigure:",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 6))

        scroll = ctk.CTkScrollableFrame(self, fg_color=BG_DEEP, corner_radius=0)
        scroll.grid(row=1, column=0, sticky="nsew", padx=0, pady=(0, 0))

        def _fwd_scroll(event):
            scroll._parent_canvas.yview_scroll(
                -1 if event.num == 4 else 1, "units"
            )
        self.bind_all("<Button-4>", _fwd_scroll)
        self.bind_all("<Button-5>", _fwd_scroll)
        self.bind("<Destroy>", lambda e: (
            self.unbind_all("<Button-4>"),
            self.unbind_all("<Button-5>"),
        ) if e.widget is self else None)

        # Keep CTkImage refs alive
        self._img_refs: list = []

        for i, name in enumerate(game_names):
            col = i % self._COLS
            row = i // self._COLS
            self._build_card(scroll, name, row, col)

        btn_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        btn_bar.grid(row=2, column=0, sticky="ew")
        btn_bar.grid_propagate(False)
        ctk.CTkFrame(btn_bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            btn_bar, text="Cancel", width=90, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._cancel
        ).pack(side="right", padx=(4, 12), pady=10)

        cols = self._COLS
        w = cols * (self._CARD_W + self._PAD) + self._PAD + 8
        rows_count = (len(game_names) + cols - 1) // cols
        content_h = rows_count * (self._CARD_H + self._PAD) + self._PAD
        h = min(max(300, content_h + 120), 700)
        owner = parent
        x = owner.winfo_rootx() + (owner.winfo_width()  - w) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

        self.after(50, self._make_modal)

    def _build_card(self, parent, name: str, row: int, col: int):
        game = self._games.get(name)
        game_id = game.game_id if game else name.lower().replace(" ", "_")

        card = ctk.CTkFrame(
            parent,
            fg_color=BG_PANEL,
            corner_radius=8,
            border_width=1,
            border_color=BORDER,
            width=self._CARD_W,
            height=self._CARD_H,
        )
        card.grid(
            row=row, column=col,
            padx=(self._PAD if col == 0 else self._PAD // 2, self._PAD // 2),
            pady=(self._PAD, 0),
            sticky="nsew",
        )
        card.grid_propagate(False)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)

        # Game image
        img_frame = ctk.CTkFrame(card, fg_color=BG_DEEP, corner_radius=6,
                                  width=self._CARD_W - 8, height=self._IMG_H)
        img_frame.grid(row=0, column=0, padx=4, pady=(4, 0), sticky="nsew")
        img_frame.grid_propagate(False)

        img_path = self._icons_dir / f"{game_id}.png"
        if not img_path.is_file():
            # Try lowercase fallback
            img_path = self._icons_dir / f"{game_id.lower()}.png"
        if img_path.is_file():
            raw = _PilImage.open(img_path).convert("RGBA")
            tw, th = self._CARD_W - 8, self._IMG_H
            raw.thumbnail((tw, th), _PilImage.LANCZOS)
            ctk_img = ctk.CTkImage(light_image=raw, dark_image=raw, size=(raw.width, raw.height))
            self._img_refs.append(ctk_img)
            img_lbl = ctk.CTkLabel(img_frame, image=ctk_img, text="")
        else:
            img_lbl = ctk.CTkLabel(img_frame, text="?", font=("Segoe UI", 36, "bold"),
                                   text_color=TEXT_DIM)
        img_lbl.place(relx=0.5, rely=0.5, anchor="center")

        # Game name
        ctk.CTkLabel(
            card, text=name, font=("Segoe UI", 12, "bold"), text_color=TEXT_MAIN,
            wraplength=self._CARD_W - 10, anchor="center", justify="center",
        ).grid(row=1, column=0, padx=4, pady=(4, 2), sticky="ew")

        # Add button
        def _select(n=name):
            self.result = n
            self.grab_release()
            self.destroy()

        ctk.CTkButton(
            card, text="Add", height=26, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=_select,
        ).grid(row=2, column=0, padx=8, pady=(0, 8), sticky="ew")

        # Hover highlight
        def _enter(e, c=card): c.configure(border_color=ACCENT)
        def _leave(e, c=card): c.configure(border_color=BORDER)
        for w in (card, img_frame, img_lbl):
            w.bind("<Enter>", _enter)
            w.bind("<Leave>", _leave)

    def _make_modal(self):
        self.grab_set()
        self.focus_set()

    def _cancel(self):
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Name mod dialog
# ---------------------------------------------------------------------------
class NameModDialog(ctk.CTkToplevel):
    """
    Modal dialog that lets the user pick/edit the mod name before installing.
    result: str | None — the chosen name, or None if cancelled.
    """

    def __init__(self, parent, suggestions: list[str]):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Name Mod")
        self.geometry("480x200")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None
        self._suggestions = suggestions

        self._build(suggestions)

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _build(self, suggestions: list[str]):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="Mod name:", font=FONT_NORMAL, text_color=TEXT_MAIN,
            anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        self._entry_var = tk.StringVar(value=suggestions[0] if suggestions else "")
        entry = ctk.CTkEntry(
            self, textvariable=self._entry_var,
            font=FONT_NORMAL, fg_color=BG_PANEL, text_color=TEXT_MAIN,
            border_color=BORDER
        )
        entry.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 4))
        entry.bind("<Return>", lambda _e: self._on_ok())

        if len(suggestions) > 1:
            ctk.CTkLabel(
                self, text="Or choose a suggestion:", font=FONT_SMALL,
                text_color=TEXT_DIM, anchor="w"
            ).grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 2))

            ctk.CTkOptionMenu(
                self, values=suggestions,
                font=FONT_SMALL, fg_color=BG_PANEL, text_color=TEXT_MAIN,
                button_color=BG_HEADER, button_hover_color=BG_HOVER,
                dropdown_fg_color=BG_PANEL, dropdown_text_color=TEXT_MAIN,
                command=lambda v: self._entry_var.set(v)
            ).grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 8))
            btn_row = 4
        else:
            btn_row = 2

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=btn_row, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=90, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Install", width=90, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_ok
        ).pack(side="right", padx=4, pady=8)

        self.update_idletasks()
        h = self.winfo_reqheight()
        owner = self.master
        px = owner.winfo_rootx()
        py = owner.winfo_rooty()
        pw = owner.winfo_width()
        ph = owner.winfo_height()
        x = px + (pw - 480) // 2
        y = py + (ph - h) // 2
        self.geometry(f"480x{h}+{x}+{y}")

    def _on_ok(self):
        name = self._entry_var.get().strip()
        if name:
            self.result = name
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


class _SeparatorNameDialog(ctk.CTkToplevel):
    """Small modal dialog that asks for a separator name."""

    def __init__(self, parent):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Add Separator")
        self.geometry("360x130")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
            self._entry.focus_set()
        except Exception:
            pass
        self.bind("<FocusOut>", self._on_focus_out)

    def _on_focus_out(self, _event):
        if self.focus_get() is None:
            self._on_cancel()

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="Separator name:", font=FONT_NORMAL,
            text_color=TEXT_MAIN, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        self._var = tk.StringVar()
        self._entry = ctk.CTkEntry(
            self, textvariable=self._var, font=FONT_NORMAL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER
        )
        self._entry.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._entry.bind("<Return>", lambda _e: self._on_ok())

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Add", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_ok
        ).pack(side="right", padx=4, pady=8)

    def _on_ok(self):
        name = self._var.get().strip()
        if name:
            self.result = name
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


class _ModNameDialog(ctk.CTkToplevel):
    """Small modal dialog that asks for a new empty mod name."""

    def __init__(self, parent):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Create Empty Mod")
        self.geometry("360x130")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
            self._entry.focus_set()
        except Exception:
            pass
        self.bind("<FocusOut>", self._on_focus_out)

    def _on_focus_out(self, _event):
        if self.focus_get() is None:
            self._on_cancel()

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="Mod name:", font=FONT_NORMAL,
            text_color=TEXT_MAIN, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        self._var = tk.StringVar()
        self._entry = ctk.CTkEntry(
            self, textvariable=self._var, font=FONT_NORMAL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER
        )
        self._entry.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._entry.bind("<Return>", lambda _e: self._on_ok())

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Create", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_ok
        ).pack(side="right", padx=4, pady=8)

    def _on_ok(self):
        name = self._var.get().strip()
        if name:
            self.result = name
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


class _RenameDialog(ctk.CTkToplevel):
    """Small modal dialog pre-filled with the current name for renaming a mod or separator."""

    def __init__(self, parent, current_name: str):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Rename")
        self.geometry("360x130")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None
        self._current = current_name
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
            self._entry.focus_set()
            self._entry.select_range(0, "end")
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="New name:", font=FONT_NORMAL,
            text_color=TEXT_MAIN, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        self._var = tk.StringVar(value=self._current)
        self._entry = ctk.CTkEntry(
            self, textvariable=self._var, font=FONT_NORMAL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER
        )
        self._entry.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._entry.bind("<Return>", lambda _e: self._on_ok())

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Rename", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_ok
        ).pack(side="right", padx=4, pady=8)

    def _on_ok(self):
        name = self._var.get().strip()
        if name:
            self.result = name
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


class _PriorityDialog(ctk.CTkToplevel):
    """Modal dialog to set a mod's position in the modlist."""

    def __init__(self, parent, mod_name: str, total_mods: int):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Set Priority")
        self.geometry("380x160")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: int | None = None
        self._mod_name = mod_name
        self._total_mods = total_mods
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
            self._entry.focus_set()
            self._entry.select_range(0, "end")
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self,
            text=f"Set position for '{self._mod_name}'",
            font=FONT_NORMAL,
            text_color=TEXT_MAIN,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        ctk.CTkLabel(
            self,
            text=f"0 = bottom, highest number = top (e.g. {self._total_mods - 1} or higher = top).",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
            anchor="w",
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))

        self._var = tk.StringVar(value="")
        self._entry = ctk.CTkEntry(
            self,
            textvariable=self._var,
            font=FONT_NORMAL,
            fg_color=BG_PANEL,
            text_color=TEXT_MAIN,
            border_color=BORDER,
        )
        self._entry.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._entry.bind("<Return>", lambda _e: self._on_ok())

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar,
            text="Cancel",
            width=80,
            height=28,
            font=FONT_NORMAL,
            fg_color=BG_HEADER,
            hover_color=BG_HOVER,
            text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar,
            text="Set",
            width=80,
            height=28,
            font=FONT_BOLD,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            text_color="white",
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=8)

    def _on_ok(self):
        raw = self._var.get().strip()
        try:
            value = int(raw)
        except ValueError:
            show_error(
                "Invalid Value",
                "Please enter a whole number.",
                parent=self,
            )
            return
        if value < 0:
            show_error(
                "Invalid Value",
                "Please enter 0 or a positive number.",
                parent=self,
            )
            return
        self.result = value
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _on_cancel(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class _ProtonToolsDialog(ctk.CTkToplevel):
    """Modal dialog with Proton-related tools for the selected game."""

    def __init__(self, parent, game, log_fn):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Proton Tools")
        self.geometry("340x314")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._make_modal)

        self._game = game
        self._log = log_fn
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _build(self):
        body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        body.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            body, text=f"Proton Tools — {self._game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN
        ).pack(pady=(0, 12))

        btn_cfg = dict(width=260, height=34, font=FONT_BOLD,
                       fg_color=ACCENT, hover_color=ACCENT_HOV,
                       text_color="white")

        ctk.CTkButton(
            body, text="Run winecfg", command=self._run_winecfg, **btn_cfg
        ).pack(pady=(0, 6))

        ctk.CTkButton(
            body, text="Run protontricks", command=self._run_protontricks, **btn_cfg
        ).pack(pady=(0, 6))

        ctk.CTkButton(
            body, text="Run EXE in this prefix …", command=self._run_exe, **btn_cfg
        ).pack(pady=(0, 6))

        ctk.CTkButton(
            body, text="Open wine registry", command=self._run_regedit, **btn_cfg
        ).pack(pady=(0, 6))

        ctk.CTkButton(
            body, text="Browse prefix", command=self._browse_prefix, **btn_cfg
        ).pack(pady=(0, 6))

        ctk.CTkButton(
            body, text="Open game folder", command=self._open_game_folder, **btn_cfg
        ).pack(pady=(0, 6))

    def _get_proton_env(self):
        from Utils.steam_finder import (
            find_any_installed_proton,
            find_proton_for_game,
            find_steam_root_for_proton_script,
        )

        prefix_path = self._game.get_prefix_path()
        if prefix_path is None or not prefix_path.is_dir():
            self._log("Proton Tools: prefix not configured for this game.")
            return None, None

        steam_id = getattr(self._game, "steam_id", "")
        proton_script = find_proton_for_game(steam_id) if steam_id else None

        compat_data = prefix_path.parent

        if proton_script is None:
            from gui.plugin_panel import _read_prefix_runner
            preferred_runner = _read_prefix_runner(compat_data)
            proton_script = find_any_installed_proton(preferred_runner)
            if proton_script is None:
                if steam_id:
                    self._log(
                        f"Proton Tools: could not find Proton version for app {steam_id}, "
                        "and no installed Proton tool was found."
                    )
                else:
                    self._log("Proton Tools: no Steam ID and no installed Proton tool was found.")
                return None, None
            self._log(
                f"Proton Tools: using fallback Proton tool {proton_script.parent.name} "
                "(no per-game Steam mapping found)."
            )

        steam_root = find_steam_root_for_proton_script(proton_script)
        if steam_root is None:
            self._log("Proton Tools: could not determine Steam root for the selected Proton tool.")
            return None, None

        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
        game_path = self._game.get_game_path() if hasattr(self._game, "get_game_path") else None
        if game_path:
            env["STEAM_COMPAT_INSTALL_PATH"] = str(game_path)
        if steam_id:
            env.setdefault("SteamAppId", steam_id)
            env.setdefault("SteamGameId", steam_id)

        return proton_script, env

    def _close_and_run(self, fn):
        log = self._log
        parent = self.master
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        parent.after(50, fn)

    def _run_winecfg(self):
        proton_script, env = self._get_proton_env()
        if proton_script is None:
            return

        log = self._log

        def _launch():
            log("Proton Tools: launching winecfg …")
            try:
                subprocess.Popen(
                    ["python3", str(proton_script), "run", "winecfg"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                log(f"Proton Tools error: {e}")

        self._close_and_run(_launch)

    def _browse_prefix(self):
        prefix_path = self._game.get_prefix_path()
        if prefix_path is None or not prefix_path.is_dir():
            self._log("Proton Tools: prefix not configured for this game.")
            return

        log = self._log
        path = str(prefix_path)

        def _launch():
            log(f"Proton Tools: opening prefix folder …")
            try:
                subprocess.Popen(["xdg-open", path])
            except Exception as e:
                log(f"Proton Tools error: {e}")

        self._close_and_run(_launch)

    def _open_game_folder(self):
        game_path = self._game.get_game_path()
        if game_path is None or not game_path.is_dir():
            self._log("Proton Tools: game folder not configured or not found.")
            return

        log = self._log
        path = str(game_path)

        def _launch():
            log("Proton Tools: opening game folder …")
            try:
                subprocess.Popen(["xdg-open", path])
            except Exception as e:
                log(f"Proton Tools error: {e}")

        self._close_and_run(_launch)

    def _run_regedit(self):
        proton_script, env = self._get_proton_env()
        if proton_script is None:
            return

        log = self._log

        def _launch():
            log("Proton Tools: launching wine registry editor …")
            try:
                subprocess.Popen(
                    ["python3", str(proton_script), "run", "regedit"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                log(f"Proton Tools error: {e}")

        self._close_and_run(_launch)

    def _run_protontricks(self):
        steam_id = getattr(self._game, "steam_id", "")
        if not steam_id:
            self._log("Proton Tools: game has no Steam ID — cannot run protontricks.")
            return

        if shutil.which("protontricks") is not None:
            cmd = ["protontricks", steam_id, "--gui"]
        elif shutil.which("flatpak") is not None and subprocess.run(
            ["flatpak", "info", "com.github.Matoking.protontricks"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0:
            cmd = ["flatpak", "run", "com.github.Matoking.protontricks", steam_id, "--gui"]
        else:
            self._log("Proton Tools: 'protontricks' is not installed or not in PATH.")
            return

        log = self._log

        def _launch():
            log(f"Proton Tools: launching protontricks for app {steam_id}: It may take a while to open")
            try:
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                log(f"Proton Tools error: {e}")

        self._close_and_run(_launch)

    def _run_exe(self):
        proton_script, env = self._get_proton_env()
        if proton_script is None:
            return

        log = self._log

        def _launch():
            try:
                result = subprocess.run(
                    [
                        "zenity", "--file-selection",
                        "--title=Select EXE to run in this prefix",
                        "--file-filter=Executables (*.exe) | *.exe",
                        "--file-filter=All files | *",
                    ],
                    capture_output=True, text=True,
                )
                if result.returncode != 0 or not result.stdout.strip():
                    return
                exe_path = Path(result.stdout.strip())
            except FileNotFoundError:
                log("Proton Tools: zenity not found — cannot open file picker.")
                return

            if not exe_path.is_file():
                log(f"Proton Tools: file not found: {exe_path}")
                return

            log(f"Proton Tools: launching {exe_path.name} via {proton_script.parent.name} …")
            try:
                subprocess.Popen(
                    ["python3", str(proton_script), "run", str(exe_path)],
                    env=env,
                    cwd=exe_path.parent,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                log(f"Proton Tools error: {e}")

        self._close_and_run(_launch)

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class _ProfileNameDialog(ctk.CTkToplevel):
    """Small modal dialog that asks for a new profile name."""

    def __init__(self, parent):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("New Profile")
        self.geometry("360x130")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
            self._entry.focus_set()
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="Profile name:", font=FONT_NORMAL,
            text_color=TEXT_MAIN, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        self._var = tk.StringVar()
        self._entry = ctk.CTkEntry(
            self, textvariable=self._var, font=FONT_NORMAL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER
        )
        self._entry.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._entry.bind("<Return>", lambda _e: self._on_ok())

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Create", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_ok
        ).pack(side="right", padx=4, pady=8)

    def _on_ok(self):
        name = self._var.get().strip()
        if name:
            self.result = name
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


class _MewgenicsDeployChoiceDialog(ctk.CTkToplevel):
    """Modal dialog: choose Steam launch command or repack modded files."""

    def __init__(self, parent):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Mewgenics — Deploy method")
        self.geometry("420x200")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None  # "steam" | "repack" | None
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="How do you want to deploy mods?",
            font=FONT_HEADER, text_color=TEXT_MAIN, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 12))

        ctk.CTkButton(
            self, text="Steam launch command (Safer / Recommended)",
            font=FONT_NORMAL, fg_color=BG_PANEL, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, anchor="w",
            command=lambda: self._choose("steam")
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=4)
        ctk.CTkLabel(
            self, text="Copy -modpaths for Steam/Lutris Launch Options (no repack).",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w"
        ).grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))

        ctk.CTkButton(
            self, text="Repack gpak. (No command needed / not recommended)",
            font=FONT_NORMAL, fg_color=BG_PANEL, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, anchor="w",
            command=lambda: self._choose("repack")
        ).grid(row=3, column=0, sticky="ew", padx=16, pady=4)
        ctk.CTkLabel(
            self, text="Unpack resources.gpak, merge mods, repack.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w"
        ).grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 12))

    def _choose(self, choice: str):
        self.result = choice
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


class _MewgenicsLaunchCommandDialog(ctk.CTkToplevel):
    """Shows the -modpaths launch string and offers Copy to clipboard."""

    def __init__(self, parent, launch_string: str):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Mewgenics — Steam / Lutris launch command")
        self.geometry("560x280")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._launch_string = launch_string
        self._build()

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text="Paste this into Steam Launch Options or Lutris Arguments:",
            font=FONT_SMALL, text_color=TEXT_MAIN, anchor="w", wraplength=520
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))

        self._text = ctk.CTkTextbox(
            self, font=FONT_MONO, fg_color=BG_PANEL, text_color=TEXT_MAIN,
            wrap="word", height=120
        )
        self._text.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))
        self._text.insert("1.0", self._launch_string)
        self._text.configure(state="disabled")

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Copy to clipboard", width=140, height=28, font=FONT_NORMAL,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._copy
        ).pack(side="right", padx=(4, 8), pady=8)
        ctk.CTkButton(
            bar, text="Close", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self.destroy
        ).pack(side="right", padx=4, pady=8)

    def _copy(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(self._launch_string)
            self.update_idletasks()
        except Exception:
            pass


class _OverwritesDialog(tk.Toplevel):
    """Modal two-pane dialog showing conflict details for a single mod."""

    def __init__(self, parent, mod_name: str,
                 files_win: list[tuple[str, str]],
                 files_lose: list[tuple[str, str]]):
        super().__init__(parent)
        self.title(f"Conflicts: {mod_name}")
        self.geometry("860x580")
        self.minsize(600, 380)
        self.configure(bg=BG_DEEP)
        self.transient(parent)
        self.update_idletasks()
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._build(mod_name, files_win, files_lose)

    def _build(self, mod_name, files_win, files_lose):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        tk.Label(
            self, text=f"Conflict detail:  {mod_name}",
            bg=BG_DEEP, fg=TEXT_MAIN,
            font=("Segoe UI", 12, "bold"), anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(10, 6))

        self._build_pane(
            row=1, col=0,
            header=f"Files overriding others  ({len(files_win)})",
            header_color="#98c379",
            col0_title="File path",
            col1_title="Mod(s) beaten",
            rows=files_win,
        )
        self._build_pane(
            row=1, col=1,
            header=f"Files overridden by others  ({len(files_lose)})",
            header_color="#e06c75",
            col0_title="File path",
            col1_title="Winning mod",
            rows=files_lose,
        )

        footer = tk.Frame(self, bg=BG_PANEL, height=44)
        footer.grid(row=2, column=0, columnspan=2, sticky="ew")
        footer.grid_propagate(False)
        tk.Frame(footer, bg=BORDER, height=1).pack(side="top", fill="x")
        tk.Button(
            footer, text="Close",
            bg=BG_HEADER, fg=TEXT_MAIN, activebackground=BG_HOVER,
            relief="flat", font=("Segoe UI", 11),
            padx=16, pady=3, cursor="hand2",
            command=self.destroy,
        ).pack(side="right", padx=12, pady=6)

    def _build_pane(self, row, col, header, header_color,
                    col0_title, col1_title, rows):
        outer = tk.Frame(self, bg=BG_PANEL)
        outer.grid(
            row=row, column=col, sticky="nsew",
            padx=(8 if col == 0 else 4, 4 if col == 0 else 8),
            pady=4,
        )
        outer.grid_rowconfigure(1, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        tk.Label(
            outer, text=header,
            bg=BG_PANEL, fg=header_color,
            font=("Segoe UI", 10, "bold"), anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))

        tree_frame = tk.Frame(outer, bg=BG_DEEP)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        sname = f"OvDlg{col}.Treeview"
        style = ttk.Style()
        style.configure(sname,
                        background=BG_DEEP, foreground=TEXT_MAIN,
                        fieldbackground=BG_DEEP, rowheight=20,
                        font=("Segoe UI", 9))
        style.configure(f"{sname}.Heading",
                        background=BG_HEADER, foreground=TEXT_SEP,
                        font=("Segoe UI", 9, "bold"), relief="flat")
        style.map(sname,
                  background=[("selected", BG_SELECT)],
                  foreground=[("selected", TEXT_MAIN)])

        tv = ttk.Treeview(
            tree_frame,
            columns=("col1",),
            displaycolumns=("col1",),
            show="headings tree",
            style=sname,
            selectmode="browse",
        )
        tv.heading("#0",   text=col0_title, anchor="w")
        tv.heading("col1", text=col1_title, anchor="w")
        tv.column("#0",   minwidth=180, stretch=True)
        tv.column("col1", minwidth=150, width=180, stretch=False)

        vsb = tk.Scrollbar(tree_frame, orient="vertical", command=tv.yview,
                           bg=BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
                           highlightthickness=0, bd=0)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        tv.bind("<Button-4>", lambda e: tv.yview_scroll(-3, "units"))
        tv.bind("<Button-5>", lambda e: tv.yview_scroll( 3, "units"))

        for path, mod_str in rows:
            tv.insert("", "end", text=path, values=(mod_str,))
        if not rows:
            tv.insert("", "end", text="(none)", values=("",))


# ---------------------------------------------------------------------------
# VRAMr preset picker
# ---------------------------------------------------------------------------
class _VRAMrPresetDialog(ctk.CTkToplevel):
    """Modal dialog that lets the user pick a VRAMr preset, then runs the
    optimisation pipeline in a background thread."""

    _PRESETS = [
        ("hq",          "High Quality",  "2K / 2K / 1K / 1K  — 4K modlist downscaled to 2K"),
        ("quality",     "Quality",       "2K / 1K / 1K / 1K  — Balance of quality & savings"),
        ("optimum",     "Optimum",       "2K / 1K / 512 / 512 — Good starting point"),
        ("performance", "Performance",   "2K / 512 / 512 / 512 — Big gains, lower close-up"),
        ("vanilla",     "Vanilla",       "512 / 512 / 512 / 512 — Just run the game"),
    ]

    def __init__(self, parent, *, bat_dir: Path, game_data_dir: Path,
                 output_dir: Path, log_fn):
        super().__init__(parent, fg_color="#1a1a1a")
        self.title("VRAMr — Choose Preset")
        self.geometry("520x380")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._make_modal)

        self._bat_dir = bat_dir
        self._game_data_dir = game_data_dir
        self._output_dir = output_dir
        self._log = log_fn
        self._preset_var = tk.StringVar(value="optimum")
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _build(self):
        ctk.CTkLabel(
            self, text="VRAMr Texture Optimiser",
            font=("Segoe UI", 16, "bold"), text_color="#d4d4d4",
        ).pack(pady=(16, 4))
        ctk.CTkLabel(
            self, text="Select an optimisation preset, then click Run.",
            font=("Segoe UI", 12), text_color="#858585",
        ).pack(pady=(0, 12))

        frame = ctk.CTkFrame(self, fg_color="#252526", corner_radius=6)
        frame.pack(padx=20, pady=4, fill="x")

        for key, label, desc in self._PRESETS:
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=3)
            ctk.CTkRadioButton(
                row, text=label, variable=self._preset_var, value=key,
                font=("Segoe UI", 13), text_color="#d4d4d4",
                fg_color="#0078d4", hover_color="#1084d8",
                border_color="#444444",
            ).pack(side="left")
            ctk.CTkLabel(
                row, text=desc,
                font=("Segoe UI", 11), text_color="#858585",
            ).pack(side="left", padx=(12, 0))

        ctk.CTkLabel(
            self, text=f"Output: {self._output_dir}",
            font=("Segoe UI", 11), text_color="#858585", wraplength=480,
        ).pack(pady=(12, 4))

        ctk.CTkButton(
            self, text="▶  Run VRAMr", width=160, height=36,
            font=("Segoe UI", 13, "bold"),
            fg_color="#0078d4", hover_color="#1084d8", text_color="white",
            command=self._on_run,
        ).pack(pady=(8, 16))

    def _on_run(self):
        preset = self._preset_var.get()
        self._log(f"VRAMr: starting with '{preset}' preset...")

        bat_dir = self._bat_dir
        game_data_dir = self._game_data_dir
        output_dir = self._output_dir
        log_fn = self._log
        app = self.winfo_toplevel().master
        if hasattr(app, "_status"):
            app._status.show_log()
        self._on_close()

        def _log_safe(msg: str):
            try:
                if hasattr(app, "call_threadsafe"):
                    app.call_threadsafe(lambda m=msg: log_fn(m))
                else:
                    log_fn(msg)
            except Exception:
                pass

        def _worker():
            try:
                from wrappers.vramr import run_vramr
                run_vramr(
                    bat_dir=bat_dir,
                    game_data_dir=game_data_dir,
                    output_dir=output_dir,
                    preset=preset,
                    log_fn=_log_safe,
                )
            except Exception as exc:
                _log_safe(f"VRAMr error: {exc}")

        threading.Thread(target=_worker, daemon=True).start()


# BENDr run dialog
# ---------------------------------------------------------------------------
class _BENDrRunDialog(ctk.CTkToplevel):
    """Modal confirmation dialog that runs the BENDr pipeline in a background thread."""

    def __init__(self, parent, *, bat_dir: Path, game_data_dir: Path,
                 output_dir: Path, log_fn):
        super().__init__(parent, fg_color="#1a1a1a")
        self.title("BENDr — Normal Map Processor")
        self.geometry("480x260")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._make_modal)

        self._bat_dir = bat_dir
        self._game_data_dir = game_data_dir
        self._output_dir = output_dir
        self._log = log_fn
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _build(self):
        ctk.CTkLabel(
            self, text="BENDr Normal Map Processor",
            font=("Segoe UI", 16, "bold"), text_color="#d4d4d4",
        ).pack(pady=(16, 4))
        ctk.CTkLabel(
            self,
            text=(
                "Processes normal maps and parallax textures:\n"
                "BSA extract → filter → parallax prep → bend normals → BC7 compress"
            ),
            font=("Segoe UI", 12), text_color="#858585", justify="center",
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self, text=f"Output: {self._output_dir}",
            font=("Segoe UI", 11), text_color="#858585", wraplength=440,
        ).pack(pady=(4, 12))

        ctk.CTkButton(
            self, text="▶  Run BENDr", width=160, height=36,
            font=("Segoe UI", 13, "bold"),
            fg_color="#0078d4", hover_color="#1084d8", text_color="white",
            command=self._on_run,
        ).pack(pady=(0, 16))

    def _on_run(self):
        self._log("BENDr: starting pipeline...")

        bat_dir = self._bat_dir
        game_data_dir = self._game_data_dir
        output_dir = self._output_dir
        log_fn = self._log
        app = self.winfo_toplevel().master
        if hasattr(app, "_status"):
            app._status.show_log()
        self._on_close()

        def _log_safe(msg: str):
            try:
                if hasattr(app, "call_threadsafe"):
                    app.call_threadsafe(lambda m=msg: log_fn(m))
                else:
                    log_fn(msg)
            except Exception:
                pass

        def _worker():
            try:
                from wrappers.bendr import run_bendr
                run_bendr(
                    bat_dir=bat_dir,
                    game_data_dir=game_data_dir,
                    output_dir=output_dir,
                    log_fn=_log_safe,
                )
            except Exception as exc:
                _log_safe(f"BENDr error: {exc}")

        threading.Thread(target=_worker, daemon=True).start()


# ParallaxR run dialog
# ---------------------------------------------------------------------------
class _ParallaxRRunDialog(ctk.CTkToplevel):
    """Modal confirmation dialog that runs the ParallaxR pipeline in a background thread."""

    def __init__(self, parent, *, bat_dir: Path, game_data_dir: Path,
                 output_dir: Path, log_fn):
        super().__init__(parent, fg_color="#1a1a1a")
        self.title("ParallaxR — Parallax Texture Processor")
        self.geometry("480x260")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._make_modal)

        self._bat_dir = bat_dir
        self._game_data_dir = game_data_dir
        self._output_dir = output_dir
        self._log = log_fn
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _build(self):
        ctk.CTkLabel(
            self, text="ParallaxR Parallax Texture Processor",
            font=("Segoe UI", 16, "bold"), text_color="#d4d4d4",
        ).pack(pady=(16, 4))
        ctk.CTkLabel(
            self,
            text=(
                "Processes normal maps and parallax textures:\n"
                "BSA extract → filter pairs → height maps → output QC"
            ),
            font=("Segoe UI", 12), text_color="#858585", justify="center",
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self, text=f"Output: {self._output_dir}",
            font=("Segoe UI", 11), text_color="#858585", wraplength=440,
        ).pack(pady=(4, 12))

        ctk.CTkButton(
            self, text="▶  Run ParallaxR", width=160, height=36,
            font=("Segoe UI", 13, "bold"),
            fg_color="#0078d4", hover_color="#1084d8", text_color="white",
            command=self._on_run,
        ).pack(pady=(0, 16))

    def _on_run(self):
        self._log("ParallaxR: starting pipeline...")

        bat_dir = self._bat_dir
        game_data_dir = self._game_data_dir
        output_dir = self._output_dir
        log_fn = self._log
        app = self.winfo_toplevel().master
        if hasattr(app, "_status"):
            app._status.show_log()
        self._on_close()

        def _log_safe(msg: str):
            try:
                if hasattr(app, "call_threadsafe"):
                    app.call_threadsafe(lambda m=msg: log_fn(m))
                else:
                    log_fn(msg)
            except Exception:
                pass

        def _worker():
            try:
                from wrappers.parallaxr import run_parallaxr
                run_parallaxr(
                    bat_dir=bat_dir,
                    game_data_dir=game_data_dir,
                    output_dir=output_dir,
                    log_fn=_log_safe,
                )
            except Exception as exc:
                _log_safe(f"ParallaxR error: {exc}")

        threading.Thread(target=_worker, daemon=True).start()


class _ExeConfigDialog(ctk.CTkToplevel):
    """Modal dialog for configuring command-line arguments for a Windows exe."""

    _EXE_ARGS_FILE = get_exe_args_path()

    def __init__(self, parent, exe_path: "Path", game, saved_args: str = "",
                 custom_exes: "list | None" = None, launch_mode: "str | None" = None,
                 deploy_before_launch: "bool | None" = None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"Configure: {exe_path.name}")
        self.geometry("480x180" if launch_mode is not None else "640x410")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._exe_path = exe_path
        self._game = game
        self._saved_args = saved_args
        self._custom_exes: "list" = list(custom_exes) if custom_exes else []
        # launch_mode is None when the exe is not the game's own launcher (hides dropdown)
        self._initial_launch_mode: "str | None" = launch_mode
        self._launch_mode_var = tk.StringVar(value=launch_mode or "auto")
        self._deploy_before_launch_var = tk.BooleanVar(
            value=True if deploy_before_launch is None else deploy_before_launch
        )
        self.result: "str | None" = None
        self.launch_mode: "str | None" = None  # set on Save when dropdown is shown
        self.deploy_before_launch: "bool | None" = None  # set on Save when shown
        self.removed: bool = False

        self._game_path: "Path | None" = (
            game.get_game_path() if hasattr(game, "get_game_path") else None
        )
        self._mods_path: "Path | None" = (
            game.get_mod_staging_path() if hasattr(game, "get_mod_staging_path") else None
        )
        self._overwrite_path: "Path | None" = (
            self._mods_path.parent / "overwrite" if self._mods_path else None
        )

        self._game_flag_var = tk.StringVar(value="")
        self._output_flag_var = tk.StringVar(value="")
        self._mod_var = tk.StringVar(value="")
        self._mod_entries: list[tuple[str, "Path"]] = self._load_mod_entries()
        self._mod_popup: "tk.Toplevel | None" = None
        self._mod_popup_click_id: str = ""

        self._build()
        if self._initial_launch_mode is None:
            self._load_saved()
            self._game_flag_var.trace_add("write", self._assemble)
            self._output_flag_var.trace_add("write", self._assemble)
        if self._initial_launch_mode is None:
            self._mod_var.trace_add("write", self._assemble)

        self.after(80, self._make_modal)

    def _load_mod_entries(self) -> "list[tuple[str, Path]]":
        entries: list[tuple[str, Path]] = []
        if self._overwrite_path and self._overwrite_path.is_dir():
            entries.append(("overwrite", self._overwrite_path))
        if self._mods_path and self._mods_path.is_dir():
            for e in sorted(self._mods_path.iterdir(), key=lambda p: p.name.casefold()):
                if e.is_dir() and "_separator" not in e.name:
                    entries.append((e.name, e))
        return entries

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        is_game_exe = self._initial_launch_mode is not None

        if not is_game_exe:
            sec1 = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=6)
            sec1.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
            sec1.grid_columnconfigure(1, weight=1)

            ctk.CTkLabel(
                sec1, text="Game path argument", font=FONT_BOLD,
                text_color=TEXT_MAIN, anchor="w",
            ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 2))

            ctk.CTkLabel(
                sec1, text="Flag:", font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
            ).grid(row=1, column=0, sticky="w", padx=(10, 4), pady=4)
            ctk.CTkEntry(
                sec1, textvariable=self._game_flag_var, font=FONT_SMALL,
                fg_color=BG_HEADER, text_color=TEXT_MAIN, border_color=BORDER,
                placeholder_text="e.g. --tesv:",
            ).grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=4)

            wine_game = _to_wine_path(self._game_path) if self._game_path else "(game path not set)"
            ctk.CTkLabel(
                sec1, text=f"Path:  {wine_game}", font=FONT_SMALL,
                text_color=TEXT_DIM, anchor="w", wraplength=560,
            ).grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))

            sec2 = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=6)
            sec2.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
            sec2.grid_columnconfigure(1, weight=1)

            ctk.CTkLabel(
                sec2, text="Output argument", font=FONT_BOLD,
                text_color=TEXT_MAIN, anchor="w",
            ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 2))

            ctk.CTkLabel(
                sec2, text="Flag:", font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
            ).grid(row=1, column=0, sticky="w", padx=(10, 4), pady=4)
            ctk.CTkEntry(
                sec2, textvariable=self._output_flag_var, font=FONT_SMALL,
                fg_color=BG_HEADER, text_color=TEXT_MAIN, border_color=BORDER,
                placeholder_text="e.g. --output:",
            ).grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=4)

            ctk.CTkLabel(
                sec2, text="Mod:", font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
            ).grid(row=2, column=0, sticky="w", padx=(10, 4), pady=(0, 8))
            mod_row = ctk.CTkFrame(sec2, fg_color="transparent")
            mod_row.grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=(0, 8))
            mod_row.grid_columnconfigure(0, weight=1)
            self._mod_entry = ctk.CTkEntry(
                mod_row, textvariable=self._mod_var, font=FONT_SMALL,
                fg_color=BG_HEADER, text_color=TEXT_MAIN, border_color=BORDER,
                placeholder_text="search mods...",
            )
            self._mod_entry.grid(row=0, column=0, sticky="ew")
            self._mod_entry._entry.bind(
                "<Control-a>",
                lambda e: (self._mod_entry._entry.select_range(0, "end"),
                           self._mod_entry._entry.icursor("end"), "break")[2],
            )
            ctk.CTkButton(
                mod_row, text="▼", width=28, font=FONT_SMALL,
                fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
                command=self._open_mod_popup,
            ).grid(row=0, column=1, padx=(4, 0))
            self._mod_var.trace_add("write", self._on_mod_typed)

            sec3 = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=6)
            sec3.grid(row=2, column=0, sticky="ew", padx=12, pady=4)
            sec3.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                sec3, text="Final argument (editable)", font=FONT_BOLD,
                text_color=TEXT_MAIN, anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))

            self._final_box = ctk.CTkTextbox(
                sec3, height=56, font=FONT_NORMAL,
                fg_color=BG_HEADER, text_color=TEXT_MAIN, border_color=BORDER,
                border_width=1, wrap="word",
            )
            self._final_box.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))

        # Launcher mode — only shown when this exe is the game's own launcher
        if is_game_exe:
            sec4 = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=6)
            sec4.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
            sec4.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(
                sec4, text="Launch via", font=FONT_BOLD,
                text_color=TEXT_MAIN, anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 4))
            ctk.CTkOptionMenu(
                sec4, values=["Auto", "Steam", "Heroic", "None"],
                variable=self._launch_mode_var,
                width=140, font=FONT_SMALL,
                fg_color=BG_HEADER, button_color=ACCENT, button_hover_color=ACCENT_HOV,
                dropdown_fg_color=BG_PANEL, text_color=TEXT_MAIN,
                command=lambda _: None,
            ).grid(row=0, column=1, sticky="w", padx=(0, 10), pady=(8, 4))
            ctk.CTkLabel(
                sec4,
                text="Auto detects Steam/Heroic ownership. Force a specific launcher or\n"
                     "None to always launch the exe directly via Proton.",
                font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
            ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 4))
            ctk.CTkCheckBox(
                sec4, text="Deploy mods before launching",
                variable=self._deploy_before_launch_var,
                font=FONT_SMALL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
                checkmark_color=BG_DEEP,
            ).grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 8))

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=48)
        bar.grid(row=1 if is_game_exe else 3, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=90, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=9)
        ctk.CTkButton(
            bar, text="Save", width=90, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_save,
        ).pack(side="right", padx=4, pady=9)
        # Remove button — only shown for custom exes (those saved via Add custom EXE)
        if self._exe_path in self._custom_exes:
            ctk.CTkButton(
                bar, text="Remove EXE", width=110, height=30, font=FONT_NORMAL,
                fg_color="#8B1A1A", hover_color="#B22222", text_color="white",
                command=self._on_remove,
            ).pack(side="left", padx=(12, 4), pady=9)


    def _on_mod_typed(self, *_):
        """Refresh popup list as the user types."""
        if self._mod_popup and self._mod_popup.winfo_exists():
            self._populate_mod_popup()

    def _open_mod_popup(self):
        """Open (or close) the bounded scrollable mod picker."""
        if self._mod_popup and self._mod_popup.winfo_exists():
            self._mod_popup.destroy()
            self._mod_popup = None
            return

        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.configure(bg=BG_PANEL)
        self._mod_popup = popup

        # Position below the entry widget
        self._mod_entry.update_idletasks()
        x = self._mod_entry.winfo_rootx()
        y = self._mod_entry.winfo_rooty() + self._mod_entry.winfo_height() + 2
        w = self._mod_entry.winfo_width() + 32  # include button width
        popup.geometry(f"{w}x300+{x}+{y}")

        scroll = ctk.CTkScrollableFrame(popup, fg_color=BG_PANEL, corner_radius=0)
        scroll.pack(fill="both", expand=True)
        scroll.grid_columnconfigure(0, weight=1)
        self._mod_popup_scroll = scroll
        self._populate_mod_popup()

        popup.bind("<Escape>", lambda _: self._close_mod_popup())
        popup.lift()

        # Scroll wheel — forward to the inner canvas of the CTkScrollableFrame
        def _forward_scroll(event):
            canvas = scroll._parent_canvas
            if event.num == 4 or event.delta > 0:
                canvas.yview_scroll(-1, "units")
            else:
                canvas.yview_scroll(1, "units")
        popup.bind("<MouseWheel>", _forward_scroll)
        popup.bind("<Button-4>", _forward_scroll)
        popup.bind("<Button-5>", _forward_scroll)

        # Delay binding so the current click (on ▼) doesn't immediately close the popup
        def _bind_click_dismiss():
            if self._mod_popup and self._mod_popup.winfo_exists():
                self._mod_popup_click_id = self.bind(
                    "<Button-1>", self._on_root_click_while_popup, add="+"
                )
        self.after(100, _bind_click_dismiss)
        # Start polling to close when the app loses focus to another window
        self._poll_mod_popup_focus()

    def _poll_mod_popup_focus(self):
        if not self._mod_popup or not self._mod_popup.winfo_exists():
            return
        # Check if mouse pointer is over any of our app's widgets
        try:
            mx, my = self.winfo_pointerx(), self.winfo_pointery()
            widget_under = self.winfo_containing(mx, my)
        except Exception:
            widget_under = None
        # Close only when pointer is outside all our windows AND dialog has no focus
        # (i.e. user switched to another application entirely)
        if widget_under is None and not self.focus_get():
            self._close_mod_popup()
            return
        self.after(300, self._poll_mod_popup_focus)

    def _close_mod_popup(self):
        if self._mod_popup and self._mod_popup.winfo_exists():
            self._mod_popup.destroy()
        self._mod_popup = None
        try:
            self.unbind("<Button-1>", self._mod_popup_click_id)
        except Exception:
            pass

    def _on_root_click_while_popup(self, event):
        if not self._mod_popup or not self._mod_popup.winfo_exists():
            self._close_mod_popup()
            return
        # Don't close if the click was inside the popup
        px, py, pw, ph = (
            self._mod_popup.winfo_rootx(), self._mod_popup.winfo_rooty(),
            self._mod_popup.winfo_width(), self._mod_popup.winfo_height(),
        )
        if px <= event.x_root <= px + pw and py <= event.y_root <= py + ph:
            return
        self._close_mod_popup()

    def _populate_mod_popup(self):
        scroll = self._mod_popup_scroll
        for w in scroll.winfo_children():
            w.destroy()
        query = self._mod_var.get().casefold()
        names = [n for n, _ in self._mod_entries]
        filtered = [n for n in names if query in n.casefold()] if query else names
        for name in filtered:
            btn = ctk.CTkButton(
                scroll, text=name, anchor="w", font=FONT_SMALL,
                fg_color="transparent", hover_color=BG_HOVER, text_color=TEXT_MAIN,
                height=26, corner_radius=4,
                command=lambda n=name: self._select_mod(n),
            )
            btn.pack(fill="x", padx=4, pady=1)

    def _select_mod(self, name: str):
        if self._mod_popup and self._mod_popup.winfo_exists():
            self._mod_popup.destroy()
            self._mod_popup = None
        self._mod_var.set(name)

    def _assemble(self, *_):
        parts: list[str] = []

        game_flag = self._game_flag_var.get().strip()
        if game_flag and self._game_path:
            wine = _to_wine_path(self._game_path)
            parts.append(f'{game_flag}"{wine}"')

        out_flag = self._output_flag_var.get().strip()
        selected = self._mod_var.get()
        if out_flag and selected:
            path = next((p for n, p in self._mod_entries if n == selected), None)
            if path:
                parts.append(f'{out_flag}"{_to_wine_path(path)}"')

        assembled = " ".join(parts)
        self._set_final_text(assembled)

    def _set_final_text(self, text: str):
        self._final_box.delete("1.0", "end")
        self._final_box.insert("1.0", text)

    def _get_final_text(self) -> str:
        return self._final_box.get("1.0", "end").strip()

    def _parse_saved_args(self, args: str):
        segments = re.findall(r'(\S+?)"([^"]+)"', args)

        game_wine = _to_wine_path(self._game_path).rstrip("\\") if self._game_path else None

        for flag, quoted_path in segments:
            normalised = quoted_path.rstrip("\\")

            if game_wine and (normalised == game_wine
                              or normalised.startswith(game_wine + "\\")):
                self._game_flag_var.set(flag)
                continue

            matched = False
            for name, path in self._mod_entries:
                mod_wine = _to_wine_path(path).rstrip("\\")
                if normalised == mod_wine or normalised.startswith(mod_wine + "\\"):
                    self._output_flag_var.set(flag)
                    self._mod_var.set(name)
                    matched = True
                    break

            if not matched:
                tail = normalised.rsplit("\\", 1)[-1] if "\\" in normalised else ""
                if tail:
                    self._output_flag_var.set(flag)
                    for name, _path in self._mod_entries:
                        if name == tail:
                            self._mod_var.set(name)
                            break
                    else:
                        self._mod_var.set(tail)

    def _load_saved(self):
        if self._saved_args:
            self._parse_saved_args(self._saved_args)
            self._set_final_text(self._saved_args)

    def _on_save(self):
        if self._initial_launch_mode is not None:
            self.launch_mode = self._launch_mode_var.get().lower()
            self.deploy_before_launch = self._deploy_before_launch_var.get()
        else:
            final = self._get_final_text()
            try:
                data = json.loads(self._EXE_ARGS_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data[self._exe_path.name] = final
            try:
                self._EXE_ARGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except OSError:
                pass
            self.result = final
        self.grab_release()
        self.destroy()

    def _on_remove(self):
        self.removed = True
        self.result = None
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


class _ReplaceModDialog(ctk.CTkToplevel):
    """Modal dialog shown when installing a mod whose name already exists.
    result: "all" | "selected" | "cancel"
    selected_files: set[str] — always None here; populated by caller if "selected"
    """

    def __init__(self, parent, mod_name: str):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Mod Already Exists")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str = "cancel"
        self.selected_files: set[str] | None = None

        self._build(mod_name)

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _build(self, mod_name: str):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self,
            text=f"'{mod_name}' is already installed.",
            font=FONT_BOLD,
            text_color=TEXT_MAIN,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        ctk.CTkLabel(
            self,
            text="How would you like to handle the existing mod?",
            font=FONT_NORMAL,
            text_color=TEXT_DIM,
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=90, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=12)
        ctk.CTkButton(
            bar, text="Replace Selected", width=130, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_selected,
        ).pack(side="right", padx=4, pady=12)
        ctk.CTkButton(
            bar, text="Replace All", width=100, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_all,
        ).pack(side="right", padx=4, pady=12)

        self.update_idletasks()
        w, h = 460, self.winfo_reqheight()
        owner = self.master
        x = owner.winfo_rootx() + (owner.winfo_width() - w) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _on_all(self):
        self.result = "all"
        self.grab_release()
        self.destroy()

    def _on_selected(self):
        self.result = "selected"
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.result = "cancel"
        self.grab_release()
        self.destroy()


class _SetPrefixDialog(ctk.CTkToplevel):
    """
    Modal dialog shown when a mod's top-level folders don't match any of the
    game's required folders.  result: ("prefix", path_str) | ("as_is", None) | None
    """

    _FONT_TITLE = ("Segoe UI", 14, "bold")
    _FONT_BODY  = ("Segoe UI", 13)
    _FONT_ENTRY = ("Segoe UI", 13)
    _FONT_TREE  = ("Courier New", 12)
    _FONT_BTN   = ("Segoe UI", 13)
    _FONT_BTN_B = ("Segoe UI", 13, "bold")

    def __init__(self, parent, required_folders: set[str],
                 file_list: list[tuple[str, str, bool]]):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Unexpected Mod Structure")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: tuple[str, str | None] | None = None
        self._required  = required_folders
        self._file_list = file_list
        self._entry_var = tk.StringVar()
        self._entry_var.trace_add("write", self._on_entry_change)

        self._build()
        self._refresh_tree("")

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(
            self,
            text="This mod has no recognised top-level folders.",
            font=self._FONT_TITLE,
            text_color=TEXT_MAIN,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 2))

        folders_str = ",  ".join(sorted(self._required))
        ctk.CTkLabel(
            self,
            text=f"Expected one of:  {folders_str}",
            font=self._FONT_BODY,
            text_color=TEXT_DIM,
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))

        ctk.CTkLabel(
            self,
            text="Install all files under this path (e.g. archive/pc/mod):",
            font=self._FONT_BODY,
            text_color=TEXT_MAIN,
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 4))

        self._entry = ctk.CTkEntry(
            self,
            textvariable=self._entry_var,
            font=self._FONT_ENTRY,
            fg_color=BG_PANEL,
            border_color=BORDER,
            text_color=TEXT_MAIN,
            height=36,
        )
        self._entry.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._entry.focus_set()

        tree_frame = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=6)
        tree_frame.grid(row=4, column=0, sticky="nsew", padx=16, pady=(0, 10))
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        self._tree_text = tk.Text(
            tree_frame,
            font=self._FONT_TREE,
            bg=BG_PANEL,
            fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            relief="flat",
            bd=0,
            highlightthickness=0,
            state="disabled",
            wrap="none",
            padx=8,
            pady=6,
        )
        tree_vsb = tk.Scrollbar(tree_frame, orient="vertical",
                                command=self._tree_text.yview)
        tree_hsb = tk.Scrollbar(tree_frame, orient="horizontal",
                                command=self._tree_text.xview)
        self._tree_text.configure(yscrollcommand=tree_vsb.set,
                                  xscrollcommand=tree_hsb.set)
        self._tree_text.grid(row=0, column=0, sticky="nsew")
        tree_vsb.grid(row=0, column=1, sticky="ns")
        tree_hsb.grid(row=1, column=0, sticky="ew")

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=56)
        bar.grid(row=5, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=100, height=32, font=self._FONT_BTN,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=12)
        ctk.CTkButton(
            bar, text="Install Anyway", width=140, height=32, font=self._FONT_BTN,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_as_is,
        ).pack(side="right", padx=4, pady=12)
        ctk.CTkButton(
            bar, text="Install with Prefix", width=160, height=32, font=self._FONT_BTN_B,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_prefix,
        ).pack(side="right", padx=4, pady=12)

        self.update_idletasks()
        w, h = 560, 540
        owner = self.master
        x = owner.winfo_rootx() + (owner.winfo_width()  - w) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _on_entry_change(self, *_):
        self._refresh_tree(self._entry_var.get())

    def _refresh_tree(self, prefix: str):
        prefix = prefix.strip().strip("/").replace("\\", "/")
        paths: list[str] = []
        for _, dst_rel, is_folder in self._file_list:
            if is_folder:
                continue
            dst = dst_rel.replace("\\", "/")
            if prefix:
                dst = f"{prefix}/{dst}"
            paths.append(dst)

        tree_str = _build_tree_str(paths)
        self._tree_text.configure(state="normal")
        self._tree_text.delete("1.0", "end")
        self._tree_text.insert("end", tree_str)
        self._tree_text.configure(state="disabled")

    def _on_prefix(self):
        self.result = ("prefix", self._entry_var.get())
        self.grab_release()
        self.destroy()

    def _on_as_is(self):
        self.result = ("as_is", None)
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()


class _SelectFilesDialog(ctk.CTkToplevel):
    """
    Modal dialog that lists all files from the new archive and lets the user
    tick which ones to copy into the existing mod folder.
    result: set[str] of dst_rel paths to install, or None if cancelled.
    """

    def __init__(self, parent, file_list: list[tuple[str, str, bool]]):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Select Files to Replace")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: set[str] | None = None
        self._file_list = file_list
        self._vars: list[tuple[tk.BooleanVar, str]] = []

        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text="Select files to copy into the existing mod folder:",
            font=FONT_NORMAL,
            text_color=TEXT_MAIN,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 6))

        scroll = ctk.CTkScrollableFrame(
            self, fg_color=BG_PANEL, corner_radius=6,
        )
        scroll.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        scroll.grid_columnconfigure(0, weight=1)

        for i, (src_rel, dst_rel, is_folder) in enumerate(self._file_list):
            if is_folder:
                continue
            var = tk.BooleanVar(value=True)
            self._vars.append((var, dst_rel))
            ctk.CTkCheckBox(
                scroll,
                text=dst_rel,
                variable=var,
                font=FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                checkmark_color="white",
                border_color=BORDER,
            ).grid(row=i, column=0, sticky="w", padx=8, pady=2)

        helper = ctk.CTkFrame(self, fg_color="transparent")
        helper.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 4))
        ctk.CTkButton(
            helper, text="Select All", width=90, height=24, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=lambda: [v.set(True) for v, _ in self._vars],
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            helper, text="Select None", width=90, height=24, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=lambda: [v.set(False) for v, _ in self._vars],
        ).pack(side="left")

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=90, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=12)
        ctk.CTkButton(
            bar, text="Install Selected", width=120, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=12)

        self.update_idletasks()
        owner = self.master
        w = 520
        h = min(600, max(300, self.winfo_reqheight()))
        x = owner.winfo_rootx() + (owner.winfo_width() - w) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _on_ok(self):
        chosen = {dst for var, dst in self._vars if var.get()}
        if chosen:
            self.result = chosen
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Separator color picker dialog
# ---------------------------------------------------------------------------
class _SepColorPickerDialog(ctk.CTkToplevel):
    """
    Custom color picker styled to match the app theme.
    Shows a HSV colour wheel, a brightness slider, a live hex entry,
    and a live colour-preview swatch.

    result: str | None  — hex colour like "#rrggbb", or None if cancelled.
    reset:  bool        — True if the user clicked "Reset to default".
    """

    _WHEEL_SIZE = 200
    _SLIDER_H   = 20

    def __init__(self, parent, initial_color: str | None = None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Choose Separator Color")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel_color)

        self.result: str | None = None
        self.reset: bool = False

        self._hue: float = 0.0
        self._sat: float = 0.8
        self._val: float = 0.7

        if initial_color:
            try:
                r, g, b = (int(initial_color[i:i+2], 16) for i in (1, 3, 5))
                self._hue, self._sat, self._val = colorsys.rgb_to_hsv(r/255, g/255, b/255)
            except Exception:
                pass

        self._wheel_img: _PilTk.PhotoImage | None = None
        self._slider_img: _PilTk.PhotoImage | None = None
        self._suppress_hex_trace = False

        self._build()
        self._draw_wheel()
        self._draw_slider()
        self._update_all()

        self.after(80, self._make_modal)

        self.update_idletasks()
        w = self._WHEEL_SIZE + 32
        h = self.winfo_reqheight()
        owner = parent
        x = owner.winfo_rootx() + (owner.winfo_width()  - w) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build(self):
        PAD = 16
        ws  = self._WHEEL_SIZE
        self.grid_columnconfigure(0, weight=1)

        # Colour wheel
        wheel_frame = tk.Frame(self, bg=BG_DEEP)
        wheel_frame.grid(row=0, column=0, pady=(PAD, 4))
        self._wheel_canvas = tk.Canvas(
            wheel_frame, width=ws, height=ws,
            bg=BG_DEEP, highlightthickness=0, cursor="crosshair",
        )
        self._wheel_canvas.pack()
        self._wheel_canvas.bind("<ButtonPress-1>", self._on_wheel_press)
        self._wheel_canvas.bind("<B1-Motion>",      self._on_wheel_drag)
        self._cross_h = self._wheel_canvas.create_line(0,0,0,0, fill="white", width=1)
        self._cross_v = self._wheel_canvas.create_line(0,0,0,0, fill="white", width=1)

        # Brightness slider
        self._slider_canvas = tk.Canvas(
            self, width=ws, height=self._SLIDER_H,
            bg=BG_DEEP, highlightthickness=0, cursor="sb_h_double_arrow",
        )
        self._slider_canvas.grid(row=1, column=0, padx=PAD, pady=(0, 10), sticky="ew")
        self._slider_canvas.bind("<ButtonPress-1>", self._on_slider_press)
        self._slider_canvas.bind("<B1-Motion>",      self._on_slider_drag)
        self._slider_thumb = self._slider_canvas.create_rectangle(
            0, 0, 0, self._SLIDER_H, outline="white", width=2,
        )

        # Preview swatch
        self._swatch = tk.Frame(self, height=28, bg=BG_DEEP, relief="flat", bd=0)
        self._swatch.grid(row=2, column=0, padx=PAD, pady=(0, 6), sticky="ew")

        # Hex entry row
        hex_row = tk.Frame(self, bg=BG_DEEP)
        hex_row.grid(row=3, column=0, padx=PAD, pady=(0, 10), sticky="ew")
        hex_row.grid_columnconfigure(1, weight=1)
        tk.Label(
            hex_row, text="#", bg=BG_DEEP, fg=TEXT_SEP,
            font=("Segoe UI", 13),
        ).grid(row=0, column=0, padx=(0, 2))
        self._hex_var = tk.StringVar()
        self._hex_entry = tk.Entry(
            hex_row, textvariable=self._hex_var,
            bg=BG_PANEL, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=("Segoe UI", 13), bd=4, width=7,
        )
        self._hex_entry.grid(row=0, column=1, sticky="ew")
        self._hex_var.trace_add("write", self._on_hex_typed)

        # Button bar
        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.grid(row=4, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel_color,
        ).pack(side="right", padx=(4, 12), pady=10)
        ctk.CTkButton(
            bar, text="OK", width=80, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=10)
        ctk.CTkButton(
            bar, text="Reset to default", width=120, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_reset,
        ).pack(side="left", padx=12, pady=10)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
    def _draw_wheel(self):
        import math
        ws  = self._WHEEL_SIZE
        img = _PilImage.new("RGB", (ws, ws), BG_DEEP)
        px  = img.load()
        cx  = cy = ws / 2
        r   = ws / 2 - 2
        for y in range(ws):
            for x in range(ws):
                dx, dy = x - cx, y - cy
                dist = (dx*dx + dy*dy) ** 0.5
                if dist <= r:
                    hue = (math.atan2(-dy, dx) / (2 * math.pi)) % 1.0
                    sat = dist / r
                    rv, gv, bv = colorsys.hsv_to_rgb(hue, sat, self._val)
                    px[x, y] = (int(rv*255), int(gv*255), int(bv*255))
        self._wheel_img = _PilTk.PhotoImage(img)
        self._wheel_canvas.create_image(0, 0, anchor="nw", image=self._wheel_img)
        self._wheel_canvas.tag_raise(self._cross_h)
        self._wheel_canvas.tag_raise(self._cross_v)

    def _draw_slider(self):
        ws  = self._WHEEL_SIZE
        sh  = self._SLIDER_H
        img = _PilImage.new("RGB", (ws, sh))
        drw = _PilDraw.Draw(img)
        rv, gv, bv = colorsys.hsv_to_rgb(self._hue, self._sat, 1.0)
        for x in range(ws):
            t  = x / max(ws - 1, 1)
            drw.line([(x, 0), (x, sh)],
                     fill=(int(rv*t*255), int(gv*t*255), int(bv*t*255)))
        self._slider_img = _PilTk.PhotoImage(img)
        self._slider_canvas.create_image(0, 0, anchor="nw", image=self._slider_img)
        self._slider_canvas.tag_raise(self._slider_thumb)

    def _update_crosshair(self):
        import math
        ws  = self._WHEEL_SIZE
        cx  = cy = ws / 2
        r   = ws / 2 - 2
        angle = self._hue * 2 * math.pi
        px_ = cx + self._sat * r * math.cos(angle)
        py_ = cy - self._sat * r * math.sin(angle)
        arm = 6
        self._wheel_canvas.coords(self._cross_h, px_-arm, py_, px_+arm, py_)
        self._wheel_canvas.coords(self._cross_v, px_, py_-arm, px_, py_+arm)
        rv, gv, bv = colorsys.hsv_to_rgb(self._hue, self._sat, self._val)
        lum = 0.2126*rv + 0.7152*gv + 0.0722*bv
        col = "#000000" if lum > 0.5 else "#ffffff"
        self._wheel_canvas.itemconfigure(self._cross_h, fill=col)
        self._wheel_canvas.itemconfigure(self._cross_v, fill=col)

    def _update_slider_thumb(self):
        ws  = self._WHEEL_SIZE
        sh  = self._SLIDER_H
        tx  = int(self._val * (ws - 1))
        hw  = 5
        self._slider_canvas.coords(
            self._slider_thumb,
            max(0, tx-hw), 0, min(ws, tx+hw), sh,
        )

    def _current_hex(self) -> str:
        rv, gv, bv = colorsys.hsv_to_rgb(self._hue, self._sat, self._val)
        return "#{:02x}{:02x}{:02x}".format(int(rv*255), int(gv*255), int(bv*255))

    def _update_all(self, redraw_wheel=False, redraw_slider=False):
        if redraw_wheel:
            self._draw_wheel()
        if redraw_slider:
            self._draw_slider()
        self._update_crosshair()
        self._update_slider_thumb()
        self._swatch.configure(bg=self._current_hex())
        new_hex = self._current_hex()[1:]
        self._suppress_hex_trace = True
        if self._hex_var.get().lower() != new_hex:
            self._hex_var.set(new_hex)
        self._suppress_hex_trace = False

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def _wheel_xy_to_hs(self, x, y):
        import math
        ws  = self._WHEEL_SIZE
        cx  = cy = ws / 2
        r   = ws / 2 - 2
        dx, dy = x - cx, y - cy
        dist   = min((dx*dx + dy*dy) ** 0.5, r)
        hue    = (math.atan2(-dy, dx) / (2 * math.pi)) % 1.0
        sat    = dist / r
        return hue, sat

    def _on_wheel_press(self, event):
        self._hue, self._sat = self._wheel_xy_to_hs(event.x, event.y)
        self._update_all(redraw_slider=True)

    def _on_wheel_drag(self, event):
        self._hue, self._sat = self._wheel_xy_to_hs(event.x, event.y)
        self._update_all(redraw_slider=True)

    def _on_slider_press(self, event):
        self._val = max(0.0, min(1.0, event.x / max(self._WHEEL_SIZE - 1, 1)))
        self._update_all(redraw_wheel=True)

    def _on_slider_drag(self, event):
        self._val = max(0.0, min(1.0, event.x / max(self._WHEEL_SIZE - 1, 1)))
        self._update_all(redraw_wheel=True)

    def _on_hex_typed(self, *_):
        if self._suppress_hex_trace:
            return
        raw = self._hex_var.get().strip().lstrip("#")
        if len(raw) == 6:
            try:
                r = int(raw[0:2], 16)
                g = int(raw[2:4], 16)
                b = int(raw[4:6], 16)
                h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
                self._hue, self._sat, self._val = h, s, v
                self._update_all(redraw_wheel=True, redraw_slider=True)
            except ValueError:
                pass

    def _on_ok(self):
        self.result = self._current_hex()
        self.grab_release()
        self.destroy()

    def _on_reset(self):
        self.reset = True
        self.grab_release()
        self.destroy()

    def _on_cancel_color(self):
        self.grab_release()
        self.destroy()
