"""
backup_restore_dialog.py
Dialog to list profile backups (modlist/plugins) and restore a selected one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk

from Utils.profile_backup import list_backups, restore_backup

# ---------------------------------------------------------------------------
# Colors / fonts (kept in sync with gui.py)
# ---------------------------------------------------------------------------
BG_DEEP = "#1a1a1a"
BG_PANEL = "#252526"
BG_HEADER = "#2a2a2b"
ACCENT = "#0078d4"
ACCENT_HOV = "#1084d8"
TEXT_MAIN = "#d4d4d4"
TEXT_DIM = "#858585"
BORDER = "#444444"

FONT_NORMAL = ("Segoe UI", 12)
FONT_BOLD = ("Segoe UI", 12, "bold")
FONT_SMALL = ("Segoe UI", 10)


class BackupRestoreDialog(ctk.CTkToplevel):
    """
    Modal dialog listing backup slots (newest first). User selects one and
    clicks Restore to overwrite modlist.txt (and plugins.txt if present) with
    that backup, then on_restored() is called.
    """

    WIDTH = 380
    HEIGHT = 400

    def __init__(
        self,
        parent: tk.Widget,
        profile_dir: Path,
        profile_name: str = "default",
        on_restored: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"Restore backup — {profile_name}")
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self._profile_dir = profile_dir
        self._profile_name = profile_name
        self._on_restored = on_restored or (lambda: None)
        self._backups = list_backups(profile_dir)  # [(datetime, backup_dir), ...]

        self._build()

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

    def _build(self):
        pad = {"padx": 16, "pady": (8, 0)}

        ctk.CTkLabel(
            self,
            text="Restore backup",
            font=FONT_BOLD,
            text_color=TEXT_MAIN,
        ).pack(**pad, anchor="w")

        ctk.CTkLabel(
            self,
            text="Select a backup to restore modlist and plugins for this profile.",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
        ).pack(padx=16, pady=(2, 12), anchor="w")

        ctk.CTkFrame(self, fg_color=BORDER, height=1).pack(fill="x", padx=16, pady=2)

        # List area
        list_frame = ctk.CTkFrame(self, fg_color="transparent")
        list_frame.pack(padx=16, pady=8, fill="both", expand=True)

        if not self._backups:
            ctk.CTkLabel(
                list_frame,
                text="No backups yet. Backups are created when you deploy.",
                font=FONT_SMALL,
                text_color=TEXT_DIM,
                wraplength=320,
            ).pack(pady=20)
            self._restore_btn = None
        else:
            # Listbox: one line per backup, formatted timestamp (newest at top)
            lb_frame = tk.Frame(list_frame, bg=BG_PANEL)
            lb_frame.pack(fill="both", expand=True)

            scrollbar = tk.Scrollbar(lb_frame, bg=BG_PANEL, troughcolor=BG_DEEP, activebackground=ACCENT)
            scrollbar.pack(side="right", fill="y")

            self._listbox = tk.Listbox(
                lb_frame,
                font=("Segoe UI", 11),
                bg=BG_PANEL,
                fg=TEXT_MAIN,
                selectbackground=ACCENT,
                selectforeground="white",
                activestyle="none",
                highlightthickness=0,
                borderwidth=0,
                yscrollcommand=scrollbar.set,
            )
            self._listbox.pack(side="left", fill="both", expand=True)
            scrollbar.config(command=self._listbox.yview)

            self._display_strs: list[str] = []
            for dt, _backup_dir in self._backups:
                self._display_strs.append(dt.strftime("%Y-%m-%d %H:%M:%S"))
                self._listbox.insert("end", self._display_strs[-1])

            self._listbox.bind("<<ListboxSelect>>", self._on_selection)
            self._listbox.selection_clear(0, "end")

        # Buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(8, 16))

        self._restore_btn = ctk.CTkButton(
            btn_frame,
            text="Restore",
            width=100,
            height=32,
            font=FONT_BOLD,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            text_color="white",
            command=self._on_restore,
            state="disabled",  # enabled when user selects a backup
        )
        self._restore_btn.pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_frame,
            text="Cancel",
            width=100,
            height=32,
            font=FONT_NORMAL,
            fg_color=BG_HEADER,
            hover_color=BORDER,
            text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right")

    def _on_selection(self, *_):
        if hasattr(self, "_restore_btn") and self._restore_btn is not None and self._backups:
            sel = self._listbox.curselection()
            self._restore_btn.configure(state="normal" if sel else "disabled")

    def _on_restore(self):
        if not self._backups or not hasattr(self, "_listbox"):
            return
        sel = self._listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        _dt, backup_dir = self._backups[idx]
        restore_backup(self._profile_dir, backup_dir)
        self._on_restored()
        self._on_cancel()
