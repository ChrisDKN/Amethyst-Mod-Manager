"""
backup_restore_dialog.py
Dialog to list profile backups (modlist/plugins) and restore a selected one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk

import gui.theme as _theme
from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL, BORDER,
    TEXT_DIM, TEXT_MAIN,
    scaled,
)
from Utils.profile_backup import list_backups, restore_backup, is_backup_kept, set_backup_kept

_BG_HOVER_BTN = "#3e3e40"  # subtle grey hover for buttons (not the blue selection hover)
_KEEP_BG      = "#1a3a1a"  # dark green background for kept backups
_KEEP_FG      = "#6ecf6e"  # green text for kept backups

def _font_normal(): return (_theme.FONT_FAMILY, _theme.FS12)
def _font_bold():   return (_theme.FONT_FAMILY, _theme.FS12, "bold")
def _font_small():  return (_theme.FONT_FAMILY, _theme.FS10)
def _font_list():   return (_theme.FONT_FAMILY, _theme.FS11)


class BackupRestorePanel(ctk.CTkFrame):
    """
    Inline panel listing backup slots. Overlays the plugin-panel container;
    uses on_done(panel) callback to signal dismissal.
    """

    def __init__(
        self,
        parent: tk.Widget,
        profile_dir: Path,
        profile_name: str = "default",
        on_restored: Optional[Callable[[], None]] = None,
        on_done: Optional[Callable] = None,
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._profile_dir  = profile_dir
        self._profile_name = profile_name
        self._on_restored  = on_restored or (lambda: None)
        self._on_done      = on_done or (lambda p: None)
        self._backups      = list_backups(profile_dir)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=scaled(36))
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Restore backup \u2014 {profile_name}",
            font=_font_bold(), text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=scaled(12))
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=_font_bold(),
            fg_color=BG_PANEL, hover_color=_BG_HOVER_BTN, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=scaled(4))
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        self._build()

    def _build(self):
        ctk.CTkLabel(
            self, text="Restore backup",
            font=_font_bold(), text_color=TEXT_MAIN,
        ).pack(padx=16, pady=(8, 0), anchor="w")

        ctk.CTkLabel(
            self,
            text="Select a backup to restore modlist and plugins for this profile.",
            font=_font_small(), text_color=TEXT_DIM,
        ).pack(padx=16, pady=(2, 12), anchor="w")

        ctk.CTkFrame(self, fg_color=BORDER, height=1).pack(fill="x", padx=16, pady=2)

        list_frame = ctk.CTkFrame(self, fg_color="transparent")
        list_frame.pack(padx=16, pady=8, fill="both", expand=True)

        self._restore_btn = None
        if not self._backups:
            ctk.CTkLabel(
                list_frame,
                text="No backups yet. Backups are created when you deploy.",
                font=_font_small(), text_color=TEXT_DIM, wraplength=320,
            ).pack(pady=20)
        else:
            lb_frame = tk.Frame(list_frame, bg=BG_PANEL)
            lb_frame.pack(fill="both", expand=True)

            scrollbar = tk.Scrollbar(lb_frame, bg=BG_PANEL, troughcolor=BG_DEEP, activebackground=ACCENT)
            scrollbar.pack(side="right", fill="y")

            self._listbox = tk.Listbox(
                lb_frame,
                font=_font_list(), bg=BG_PANEL, fg=TEXT_MAIN,
                selectbackground=ACCENT, selectforeground="white",
                activestyle="none", highlightthickness=0, borderwidth=0,
                yscrollcommand=scrollbar.set,
            )
            self._listbox.pack(side="left", fill="both", expand=True)
            scrollbar.config(command=self._listbox.yview)

            for i, (dt, bdir) in enumerate(self._backups):
                label = dt.strftime("%Y-%m-%d %H:%M:%S")
                if is_backup_kept(bdir):
                    label += "  [kept]"
                self._listbox.insert("end", label)
            self._apply_keep_colors()

            self._listbox.bind("<<ListboxSelect>>", self._on_selection)
            self._listbox.selection_clear(0, "end")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(8, 16))

        self._restore_btn = ctk.CTkButton(
            btn_frame, text="Restore", width=100, height=32,
            font=_font_bold(), fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color="white", command=self._on_restore, state="disabled",
        )
        self._restore_btn.pack(side="right", padx=(8, 0))

        self._keep_btn = ctk.CTkButton(
            btn_frame, text="Keep", width=80, height=32,
            font=_font_normal(), fg_color=BG_HEADER, hover_color=BORDER,
            text_color=TEXT_MAIN, command=self._on_keep, state="disabled",
        )
        self._keep_btn.pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Cancel", width=100, height=32,
            font=_font_normal(), fg_color=BG_HEADER, hover_color=BORDER,
            text_color=TEXT_MAIN, command=self._on_cancel,
        ).pack(side="right")

    def _on_selection(self, *_):
        if self._restore_btn is not None and self._backups:
            sel = self._listbox.curselection()
            has_sel = bool(sel)
            self._restore_btn.configure(state="normal" if has_sel else "disabled")
            if hasattr(self, "_keep_btn"):
                self._keep_btn.configure(state="normal" if has_sel else "disabled")
                if has_sel:
                    _, bdir = self._backups[int(sel[0])]
                    self._keep_btn.configure(
                        text="Unkeep" if is_backup_kept(bdir) else "Keep"
                    )

    def _apply_keep_colors(self):
        """Colour kept backups green in the listbox."""
        for i, (_dt, bdir) in enumerate(self._backups):
            if is_backup_kept(bdir):
                self._listbox.itemconfig(i, bg=_KEEP_BG, fg=_KEEP_FG)
            else:
                self._listbox.itemconfig(i, bg=BG_PANEL, fg=TEXT_MAIN)

    def _on_keep(self):
        if not self._backups or not hasattr(self, "_listbox"):
            return
        sel = self._listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        dt, bdir = self._backups[idx]
        kept = is_backup_kept(bdir)
        set_backup_kept(bdir, not kept)
        # Update listbox text
        label = dt.strftime("%Y-%m-%d %H:%M:%S")
        if not kept:
            label += "  [kept]"
        self._listbox.delete(idx)
        self._listbox.insert(idx, label)
        self._listbox.selection_set(idx)
        self._apply_keep_colors()
        self._keep_btn.configure(text="Unkeep" if not kept else "Keep")

    def _on_restore(self):
        if not self._backups or not hasattr(self, "_listbox"):
            return
        sel = self._listbox.curselection()
        if not sel:
            return
        _dt, backup_dir = self._backups[int(sel[0])]
        restore_backup(self._profile_dir, backup_dir)
        self._on_restored()
        self._on_done(self)

    def _on_cancel(self):
        self._on_done(self)


class BackupRestoreDialog(ctk.CTkToplevel):
    """Modal window wrapper around BackupRestorePanel."""

    WIDTH  = 380
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
        self.protocol("WM_DELETE_WINDOW", self._on_panel_done)
        self.after(100, self._make_modal)

        self._panel = BackupRestorePanel(
            self,
            profile_dir=profile_dir,
            profile_name=profile_name,
            on_restored=on_restored,
            on_done=lambda _: self._on_panel_done(),
        )
        self._panel.pack(fill="both", expand=True)

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_panel_done(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
