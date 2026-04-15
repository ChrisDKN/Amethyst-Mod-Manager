"""
Modal dialogs used by ModListPanel, PluginPanel, TopBar, and install_mod.
Uses theme, path_utils; does not import panels or App to avoid circular imports.
"""

import colorsys
from itertools import count
import json
import os
import re
import sys
import shutil
import subprocess
import threading
import tkinter as tk
import tkinter.messagebox
import tkinter.ttk as ttk
import webbrowser
from pathlib import Path
from types import SimpleNamespace

from PIL import Image as _PilImage, ImageDraw as _PilDraw, ImageTk as _PilTk

import customtkinter as ctk

from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_PANEL,
    BG_ROW,
    BORDER,
    FONT_BOLD,
    FONT_HEADER,
    FONT_MONO,
    FONT_NORMAL,
    FONT_SMALL,
    font_sized_px,
    scaled,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_SEP,
    BG_SELECT,
    BG_SEP,
    BG_ROW_ALT,
)
import gui.theme as _theme
from gui.path_utils import _to_wine_path
from Utils.config_paths import get_exe_args_path, get_profile_exe_args_path, get_custom_game_images_dir, get_vcredist_cache_path, get_dotnet_cache_dir, get_custom_games_dir
from Utils.exe_args_builder import EXE_PROFILES
from gui.ctk_components import CTkAlert, CTkLoader, ICON_PATH
from Utils.xdg import xdg_open, open_url


def _resolve_exe_args_file(game) -> "Path":
    """Return the exe_args.json path to use for *game*'s active profile.

    For profiles with the ``profile_specific_mods`` flag the args are stored
    inside the profile directory so each profile can have independent tool
    output paths.  All other profiles share the global exe_args.json.
    """
    from pathlib import Path as _Path
    try:
        active_dir = getattr(game, "_active_profile_dir", None)
        if active_dir is not None:
            from gui.game_helpers import profile_uses_specific_mods  # type: ignore
            if profile_uses_specific_mods(active_dir):
                return get_profile_exe_args_path(_Path(active_dir))
    except Exception:
        pass
    return get_exe_args_path()


# ---------------------------------------------------------------------------
# Themed message helpers (replaces tk.messagebox which ignores dark theme)
# ---------------------------------------------------------------------------

def _center_dialog(dlg, parent, w: int, h: int | None = None):
    """Position dlg centered over parent, on the same monitor.

    Call *after* all widgets are packed so reqheight() is accurate when h=None.
    The dialog is withdrawn before geometry is set and deiconified after, which
    prevents it from briefly appearing on the wrong monitor.
    """
    dlg.withdraw()
    try:
        dlg.geometry(f"{w}")       # fix width; let height float for layout flush
        dlg.update_idletasks()
        if h is None:
            h = dlg.winfo_reqheight()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        x = px + (pw - w) // 2
        y = py + (ph - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")
    except Exception:
        dlg.geometry(f"{w}x{h}" if h else f"{w}")
    dlg.deiconify()


def ask_yes_no(parent, message: str, title: str = "Confirm") -> bool:
    """Yes/No confirmation dialog using CTkAlert. Returns True if Yes clicked."""
    alert = CTkAlert(
        state="warning",
        title=title,
        body_text=message,
        btn1="Yes",
        btn2="No",
        parent=parent,
        width=520,
    )
    return alert.get() == "Yes"


def show_error(title: str, message: str, parent=None) -> None:
    """Error dialog using CTkAlert."""
    alert = CTkAlert(
        state="error",
        title=title,
        body_text=message,
        btn1="OK",
        btn2="",
        parent=parent,
        width=520,
    )
    alert.get()


from gui.text_utils import build_tree_str as _build_tree_str


# Game picker — extracted to gui/game_picker_dialog.py
from gui.game_picker_dialog import (  # noqa: E402
    _GamePickerDialog,
    GamePickerPanel,
    _CUSTOM_HANDLERS_API_URL,
)


# ---------------------------------------------------------------------------
# PLACEHOLDER — the game picker classes used to live here; they've been moved
# to gui/game_picker_dialog.py.  The re-exports above keep all existing
# ``from gui.dialogs import _GamePickerDialog`` call sites working.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Post-install rename prompt
# ---------------------------------------------------------------------------
class _RenameAfterInstallDialog(ctk.CTkToplevel):
    """Dialog shown after a mod install (when the option is enabled) letting the
    user pick a cleaner name for the installed mod. A dropdown of suggestions
    is pre-populated by stripping metadata from the original archive stem."""

    def __init__(self, parent, current_name: str, suggestions: list[str]):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Rename Mod")
        self.geometry("420x190")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None
        self._current = current_name
        # De-duplicate while preserving order, always including the current name.
        seen: set[str] = set()
        self._suggestions: list[str] = []
        for s in ([current_name] + list(suggestions or [])):
            if s and s not in seen:
                seen.add(s)
                self._suggestions.append(s)
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
            self, text=f"Rename '{self._current}':", font=FONT_NORMAL,
            text_color=TEXT_MAIN, anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        self._var = tk.StringVar(value=self._suggestions[0] if self._suggestions else self._current)
        self._entry = ctk.CTkEntry(
            self, textvariable=self._var, font=FONT_NORMAL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER,
        )
        self._entry.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))
        self._entry.bind("<Return>", lambda _e: self._on_ok())

        ctk.CTkLabel(
            self, text="Suggestions:", font=FONT_SMALL,
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=2, column=0, sticky="ew", padx=16, pady=(2, 2))

        self._menu = ctk.CTkOptionMenu(
            self, values=self._suggestions or [self._current],
            command=self._on_pick,
            fg_color=BG_PANEL, button_color=BG_HEADER, button_hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=FONT_NORMAL,
        )
        self._menu.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._menu.set(self._suggestions[0] if self._suggestions else self._current)

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=4, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Skip", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Rename", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=8)

    def _on_pick(self, value: str):
        self._var.set(value)
        try:
            self._entry.focus_set()
            self._entry.select_range(0, "end")
        except Exception:
            pass

    def _on_ok(self):
        name = self._var.get().strip()
        if name and name != self._current:
            self.result = name
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


# Module-level queue so multiple post-install rename prompts do not stack
# on top of each other. If an install completes while the dialog is open,
# the rename request is queued and processed once the current dialog closes.
_rename_after_install_queue: list[tuple] = []
_rename_after_install_active: bool = False
_rename_after_install_lock = threading.Lock()


def queue_rename_after_install(parent_window, mod_panel, mod_name: str,
                               suggestions: list[str]) -> None:
    """Enqueue a post-install rename prompt. Safe to call from any thread.

    Ensures only one rename dialog is visible at a time — further installs
    that complete while a dialog is open are queued and processed in order.
    """
    global _rename_after_install_active
    if parent_window is None or mod_panel is None or not mod_name:
        return
    with _rename_after_install_lock:
        _rename_after_install_queue.append((parent_window, mod_panel, mod_name, list(suggestions or [])))
        if _rename_after_install_active:
            return
        _rename_after_install_active = True
    try:
        parent_window.after(0, _process_next_rename_after_install)
    except Exception:
        with _rename_after_install_lock:
            _rename_after_install_active = False


def _process_next_rename_after_install() -> None:
    global _rename_after_install_active
    with _rename_after_install_lock:
        if not _rename_after_install_queue:
            _rename_after_install_active = False
            return
        parent_window, mod_panel, mod_name, suggestions = _rename_after_install_queue.pop(0)

    def _schedule_next():
        try:
            parent_window.after(0, _process_next_rename_after_install)
        except Exception:
            with _rename_after_install_lock:
                globals()["_rename_after_install_active"] = False

    try:
        # Verify the mod is still present before prompting (user may have
        # removed it in the meantime).
        rename_fn = getattr(mod_panel, "rename_mod_by_name", None)
        dlg = _RenameAfterInstallDialog(parent_window, mod_name, suggestions)
        parent_window.wait_window(dlg)
        new_name = dlg.result
        if new_name and rename_fn is not None:
            try:
                rename_fn(mod_name, new_name)
            except Exception:
                pass
    finally:
        _schedule_next()


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


class _DotNetVersionPanel(ctk.CTkFrame):
    """Inline overlay panel that asks which .NET version to install."""

    _VERSIONS = [
        ("10 (latest)", "10"),
        ("9", "9"),
        ("8 (LTS)", "8"),
        ("7", "7"),
        ("6 (LTS)", "6"),
        ("5", "5"),
    ]

    def __init__(self, parent, on_pick):
        """``on_pick(version: str | None)`` is called when the user selects a
        version or cancels (``None`` on cancel)."""
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_pick = on_pick
        self._build()

    def _build(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_propagate(False)
        ctk.CTkLabel(
            title_bar, text="Install .NET — select version",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="✕", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._cancel,
        ).pack(side="right", padx=4, pady=4)

        # Body
        body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        body.grid(row=1, column=0, sticky="nsew")

        inner = ctk.CTkFrame(body, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")

        btn_cfg = dict(width=260, height=34, font=FONT_BOLD,
                       fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white")

        for label, ver in self._VERSIONS:
            ctk.CTkButton(
                inner, text=f".NET {label}",
                command=lambda v=ver: self._pick(v),
                **btn_cfg,
            ).pack(pady=(0, 6))

    def _pick(self, version: str):
        self._dismiss()
        self._on_pick(version)

    def _cancel(self):
        self._dismiss()
        self._on_pick(None)

    def _dismiss(self):
        try:
            self.place_forget()
            self.destroy()
        except Exception:
            pass


class _ProtonToolsDialog(ctk.CTkToplevel):
    """Thin modal wrapper around ProtonToolsPanel."""

    def __init__(self, parent, game, log_fn):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Proton Tools")
        self.geometry("380x460")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._make_modal)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        panel = ProtonToolsPanel(
            self, game, log_fn,
            on_done=lambda p: self._on_close(),
        )
        panel.grid(row=0, column=0, sticky="nsew")

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


class ProtonToolsPanel(ctk.CTkFrame):
    """Inline panel for Proton tools — overlays the plugin panel while open."""

    def __init__(self, parent, game, log_fn, on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._game = game
        self._log = log_fn
        self._on_done = on_done or (lambda p: None)
        self._build()

    def _build(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Proton Tools — {self._game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="✕", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4, pady=4)

        # Body — centred vertically/horizontally
        body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        body.grid(row=1, column=0, sticky="nsew")

        inner = ctk.CTkFrame(body, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")

        btn_cfg = dict(width=260, height=34, font=FONT_BOLD,
                       fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white")

        ctk.CTkButton(inner, text="Run winecfg",                  command=self._run_winecfg,           **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Run protontricks",              command=self._run_protontricks,     **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Run EXE in this prefix …",      command=self._run_exe,               **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Open wine registry",             command=self._run_regedit,          **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Browse prefix",                 command=self._browse_prefix,         **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Install VC++ Redistributable",  command=self._run_install_vcredist, **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Install d3dcompiler_47",         command=self._run_install_d3dcompiler_47, **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Install .NET …",                 command=self._run_install_dotnet,  **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Open game folder",               command=self._open_game_folder,    **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Wine DLL Overrides",            command=self._open_wine_dll_overrides, **btn_cfg).pack(pady=(0, 6))

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
        compat_data = prefix_path.parent if prefix_path.name == "pfx" else prefix_path

        if proton_script is None:
            from gui.plugin_panel import _read_prefix_runner
            preferred_runner = _read_prefix_runner(compat_data)
            proton_script = find_any_installed_proton(preferred_runner)
            if proton_script is None:
                if steam_id:
                    self._log(f"Proton Tools: could not find Proton version for app {steam_id}, "
                              "and no installed Proton tool was found.")
                else:
                    self._log("Proton Tools: no Steam ID and no installed Proton tool was found.")
                return None, None
            self._log(f"Proton Tools: using fallback Proton tool {proton_script.parent.name} "
                      "(no per-game Steam mapping found).")

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
        toplevel = self.winfo_toplevel()
        self._on_done(self)
        toplevel.after(50, fn)

    def _run_winecfg(self):
        proton_script, env = self._get_proton_env()
        if proton_script is None:
            return
        log = self._log
        def _launch():
            log("Proton Tools: launching winecfg …")
            try:
                subprocess.Popen(["python3", str(proton_script), "run", "winecfg"],
                                 env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            log("Proton Tools: opening prefix folder …")
            try:
                xdg_open(path)
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
                xdg_open(path)
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
                subprocess.Popen(["python3", str(proton_script), "run", "regedit"],
                                 env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log(f"Proton Tools error: {e}")
        self._close_and_run(_launch)

    def _run_protontricks(self):
        from Utils.protontricks import winetricks_installed, install_winetricks, _bundled_winetricks, _get_proton_bin
        steam_id = getattr(self._game, "steam_id", "") or ""
        prefix_path = getattr(self._game, "_prefix_path", None)
        log = self._log

        if shutil.which("protontricks") is not None and steam_id:
            cmd = ["protontricks", steam_id, "--gui"]
        elif shutil.which("flatpak") is not None and steam_id and subprocess.run(
            ["flatpak", "info", "com.github.Matoking.protontricks"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0:
            cmd = ["flatpak", "run", "com.github.Matoking.protontricks", steam_id, "--gui"]
        elif prefix_path and prefix_path.is_dir():
            # Fall back to winetricks GUI against the known prefix
            def _launch_winetricks():
                if not winetricks_installed():
                    log("Proton Tools: winetricks not found — downloading …")
                    if not install_winetricks(log_fn=lambda m: log(f"Proton Tools: {m}")):
                        return
                import os
                env = os.environ.copy()
                env["WINEPREFIX"] = str(prefix_path)
                proton_bin = _get_proton_bin()
                if proton_bin:
                    env["PATH"] = proton_bin + os.pathsep + env.get("PATH", "")
                log("Proton Tools: launching winetricks GUI …")
                try:
                    subprocess.Popen([str(_bundled_winetricks()), "--gui"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
                except Exception as e:
                    log(f"Proton Tools error: {e}")
            self._close_and_run(_launch_winetricks)
            return
        else:
            self._log("Proton Tools: protontricks is not installed and no prefix path is available.")
            return

        def _launch():
            log(f"Proton Tools: launching protontricks for app {steam_id}: It may take a while to open")
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log(f"Proton Tools error: {e}")
        self._close_and_run(_launch)

    def _run_exe(self):
        proton_script, env = self._get_proton_env()
        if proton_script is None:
            return
        log = self._log

        def _on_picked(exe_path):
            if exe_path is None:
                return
            if not exe_path.is_file():
                log(f"Proton Tools: file not found: {exe_path}")
                return
            log(f"Proton Tools: launching {exe_path.name} via {proton_script.parent.name} …")
            try:
                subprocess.Popen(["python3", str(proton_script), "run", str(exe_path)],
                                 env=env, cwd=exe_path.parent,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log(f"Proton Tools error: {e}")

        def _launch():
            from Utils.portal_filechooser import pick_exe_file
            pick_exe_file("Select EXE to run in this prefix", _on_picked)

        self._close_and_run(_launch)

    def _run_install_vcredist(self):
        proton_script, env = self._get_proton_env()
        if proton_script is None:
            return
        cache_path = get_vcredist_cache_path()
        log = self._log
        vcredist_url = "https://aka.ms/vc14/vc_redist.x64.exe"

        def _download_and_run():
            import urllib.request
            try:
                if not cache_path.is_file():
                    log("Proton Tools: downloading VC++ Redistributable …")
                    urllib.request.urlretrieve(vcredist_url, cache_path)
                    log("Proton Tools: download complete.")
                else:
                    log("Proton Tools: using cached VC++ Redistributable installer.")
                log("Proton Tools: launching VC++ Redistributable installer in game prefix …")
                subprocess.Popen(["python3", str(proton_script), "run", str(cache_path)],
                                 env=env, cwd=cache_path.parent,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log(f"Proton Tools error (VC++ Redistributable): {e}")

        def _launch():
            threading.Thread(target=_download_and_run, daemon=True).start()

        self._close_and_run(_launch)

    def _run_install_d3dcompiler_47(self):
        from Utils.protontricks import install_d3dcompiler_47
        steam_id = getattr(self._game, "steam_id", "") or ""
        prefix_path = getattr(self._game, "_prefix_path", None)
        log = self._log

        def _launch():
            install_d3dcompiler_47(
                steam_id,
                log_fn=lambda msg: log(f"Proton Tools: {msg}"),
                prefix_path=prefix_path,
            )

        self._close_and_run(lambda: threading.Thread(target=_launch, daemon=True).start())

    def _run_install_dotnet(self):
        proton_script, env = self._get_proton_env()
        if proton_script is None:
            return

        log = self._log
        container = self.master

        def _on_version_picked(version):
            if version is None:
                return
            cache_dir = get_dotnet_cache_dir()
            filename = f"windowsdesktop-runtime-{version}-win-x64.exe"
            cache_path = cache_dir / filename
            _DOTNET_URLS = {
                "5": "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/5.0.17/windowsdesktop-runtime-5.0.17-win-x64.exe",
                "6": "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/6.0.36/windowsdesktop-runtime-6.0.36-win-x64.exe",
                "7": "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/7.0.20/windowsdesktop-runtime-7.0.20-win-x64.exe",
                "8": "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/8.0.25/windowsdesktop-runtime-8.0.25-win-x64.exe",
                "9": "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/9.0.14/windowsdesktop-runtime-9.0.14-win-x64.exe",
                "10": "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/10.0.5/windowsdesktop-runtime-10.0.5-win-x64.exe",
            }
            dl_url = _DOTNET_URLS.get(version)
            if dl_url is None:
                log(f"Proton Tools: no download URL known for .NET {version}.")
                return

            def _download_and_run():
                import urllib.request
                try:
                    if not cache_path.is_file():
                        log(f"Proton Tools: downloading .NET {version} runtime …")
                        urllib.request.urlretrieve(dl_url, cache_path)
                        log("Proton Tools: download complete.")
                    else:
                        log(f"Proton Tools: using cached .NET {version} installer.")
                    log(f"Proton Tools: launching .NET {version} installer in game prefix …")
                    subprocess.Popen(
                        ["python3", str(proton_script), "run", str(cache_path)],
                        env=env,
                        cwd=cache_path.parent,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception as e:
                    log(f"Proton Tools error (.NET {version}): {e}")

            self._close_and_run(lambda: threading.Thread(target=_download_and_run, daemon=True).start())

        panel = _DotNetVersionPanel(container, on_pick=_on_version_picked)
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        panel.lift()

    def _open_wine_dll_overrides(self):
        app = self.winfo_toplevel()
        show_fn = getattr(app, "show_wine_dll_panel", None)
        game = self._game
        log = self._log
        if show_fn:
            self._on_done(self)
            app.after(10, lambda: show_fn(game, log))
        else:
            from gui.wine_dll_overrides_panel import WineDllOverridesPanel
            self._on_done(self)
            try:
                parent = app._plugin_panel_container
            except AttributeError:
                parent = app
            panel = WineDllOverridesPanel(parent, game, log,
                                          on_done=lambda p: p.place_forget() or p.destroy())
            panel.place(relx=0, rely=0, relwidth=1, relheight=1)
            panel.lift()

    def _on_close(self):
        self._on_done(self)


class _ProfileNameDialog(ctk.CTkToplevel):
    """Small modal dialog that asks for a new profile name."""

    def __init__(self, parent):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("New Profile")
        self.geometry("360x175")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: tuple[str, bool] | None = None
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

        self._specific_mods_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self,
            text="Use Profile Specific Mods",
            variable=self._specific_mods_var,
            font=FONT_NORMAL,
            text_color=TEXT_MAIN,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            border_color=BORDER,
            checkmark_color="white",
        ).grid(row=2, column=0, sticky="w", padx=16, pady=(0, 8))

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=3, column=0, sticky="ew")
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
            self.result = (name, self._specific_mods_var.get())
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


class _MewgenicsDeployChoiceDialog(ctk.CTkToplevel):
    """Thin modal wrapper around MewgenicsDeployChoicePanel.

    Callers access ``result`` (``"steam"`` | ``"repack"`` | ``None``).
    """

    def __init__(self, parent):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Mewgenics — Deploy method")
        self.geometry("420x220")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        def _on_choice(choice):
            self.result = choice
            self._on_cancel()

        panel = MewgenicsDeployChoicePanel(self, on_choice=_on_choice)
        panel.grid(row=0, column=0, sticky="nsew")

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


class _MewgenicsLaunchCommandDialog(ctk.CTkToplevel):
    """Thin non-modal wrapper around MewgenicsLaunchCommandPanel."""

    def __init__(self, parent, launch_string: str, modpaths_file=None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Mewgenics — Steam / Lutris launch command")
        self.geometry("560x310")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        panel = MewgenicsLaunchCommandPanel(
            self,
            launch_string=launch_string,
            modpaths_file=modpaths_file,
            on_close=self.destroy,
        )
        panel.grid(row=0, column=0, sticky="nsew")


class MewgenicsDeployChoicePanel(tk.Frame):
    """Inline overlay panel: choose Steam launch command or repack modded files.

    Place this over the mod-list container with::

        panel.place(relx=0, rely=0, relwidth=1, relheight=1)

    ``on_choice(result)`` is called with ``"steam"``, ``"repack"``, or ``None``
    (cancel) and the caller is responsible for destroying the panel.
    """

    def __init__(self, parent, on_choice):
        super().__init__(parent, bg=BG_DEEP)
        self._on_choice = on_choice
        self._build()

    def _build(self):
        # Centred inner card
        card = tk.Frame(self, bg=BG_PANEL, bd=0, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        card.place(relx=0.5, rely=0.5, anchor="center")

        # Title bar
        title_bar = tk.Frame(card, bg=BG_HEADER, height=scaled(42))
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        tk.Label(
            title_bar, text="Mewgenics — Deploy method",
            font=font_sized_px(_theme.FONT_FAMILY, 13, "bold"),
            fg=TEXT_MAIN, bg=BG_HEADER, anchor="w",
        ).pack(side="left", padx=scaled(14), pady=scaled(8))
        tk.Button(
            title_bar, text="✕",
            bg=BG_HEADER, fg=TEXT_DIM, activebackground=BG_HOVER,
            activeforeground=TEXT_MAIN, relief="flat", bd=0,
            highlightthickness=0, cursor="hand2",
            font=font_sized_px(_theme.FONT_FAMILY, 12),
            command=lambda: self._on_choice(None),
        ).pack(side="right", padx=scaled(8))

        body = tk.Frame(card, bg=BG_PANEL)
        body.pack(fill="both", padx=scaled(16), pady=scaled(12))

        _lbl_font  = font_sized_px(_theme.FONT_FAMILY, 12)
        _desc_font = font_sized_px(_theme.FONT_FAMILY, 10)

        # --- Steam launch command button ---
        ctk.CTkButton(
            body, text="Steam launch command  (Safer / Recommended)",
            font=_lbl_font, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, anchor="w",
            command=lambda: self._on_choice("steam"),
        ).pack(fill="x", pady=(0, scaled(2)))
        tk.Label(
            body,
            text="Generates a launch script for Steam. Set it once in Launch Options (no repack).",
            font=_desc_font, fg=TEXT_DIM, bg=BG_PANEL, anchor="w",
            wraplength=scaled(420),
        ).pack(fill="x", padx=scaled(4), pady=(0, scaled(10)))

        # --- Repack button ---
        ctk.CTkButton(
            body, text="Repack gpak  (No command needed / not recommended)",
            font=_lbl_font, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, anchor="w",
            command=lambda: self._on_choice("repack"),
        ).pack(fill="x", pady=(0, scaled(2)))
        tk.Label(
            body,
            text="Unpack resources.gpak, merge mods, repack.",
            font=_desc_font, fg=TEXT_DIM, bg=BG_PANEL, anchor="w",
        ).pack(fill="x", padx=scaled(4), pady=(0, scaled(4)))


class MewgenicsLaunchCommandPanel(tk.Frame):
    """Inline overlay panel: shows the -modpaths launch string with copy button.

    Place over the mod-list container with::

        panel.place(relx=0, rely=0, relwidth=1, relheight=1)

    ``on_close()`` is called when the user clicks Close; caller destroys panel.
    """

    def __init__(self, parent, launch_string: str, modpaths_file=None, on_close=None):
        super().__init__(parent, bg=BG_DEEP)
        self._launch_string = launch_string
        self._modpaths_file = modpaths_file
        self._on_close = on_close or (lambda: None)
        self._build()

    def _build(self):
        card = tk.Frame(self, bg=BG_PANEL, bd=0, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        card.place(relx=0.5, rely=0.5, anchor="center")

        # Title bar
        title_bar = tk.Frame(card, bg=BG_HEADER, height=scaled(42))
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        tk.Label(
            title_bar, text="Mewgenics — Steam / Lutris launch command",
            font=font_sized_px(_theme.FONT_FAMILY, 13, "bold"),
            fg=TEXT_MAIN, bg=BG_HEADER, anchor="w",
        ).pack(side="left", padx=scaled(14), pady=scaled(8))
        tk.Button(
            title_bar, text="✕",
            bg=BG_HEADER, fg=TEXT_DIM, activebackground=BG_HOVER,
            activeforeground=TEXT_MAIN, relief="flat", bd=0,
            highlightthickness=0, cursor="hand2",
            font=font_sized_px(_theme.FONT_FAMILY, 12),
            command=self._on_close,
        ).pack(side="right", padx=scaled(8))

        body = tk.Frame(card, bg=BG_PANEL)
        body.pack(fill="both", padx=scaled(16), pady=(scaled(10), 0))

        _lbl_font  = font_sized_px(_theme.FONT_FAMILY, 11)
        _desc_font = font_sized_px(_theme.FONT_FAMILY, 10)
        _mono_font = font_sized_px("Consolas", 11)

        tk.Label(
            body,
            text="Paste this into Steam Launch Options (Properties → General):",
            font=_lbl_font, fg=TEXT_MAIN, bg=BG_PANEL, anchor="w",
            wraplength=scaled(500),
        ).pack(fill="x", pady=(0, scaled(6)))

        # Monospace textbox
        txt_frame = tk.Frame(body, bg=BG_ROW, bd=1, relief="flat",
                             highlightthickness=1, highlightbackground=BORDER)
        txt_frame.pack(fill="x", pady=(0, scaled(8)))
        txt = tk.Text(
            txt_frame, font=_mono_font, bg=BG_ROW, fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN, relief="flat", bd=0,
            wrap="word", height=4, width=scaled(52),
        )
        txt.pack(fill="both", padx=scaled(6), pady=scaled(6))
        txt.insert("1.0", self._launch_string)
        txt.configure(state="disabled")

        if self._modpaths_file is not None:
            tk.Label(
                body,
                text=(
                    f"Script written to:\n{self._modpaths_file}"
                    "\n\nUpdate this whenever you change your mod list."
                ),
                font=_desc_font, fg=TEXT_DIM, bg=BG_PANEL, anchor="w",
                justify="left", wraplength=scaled(500),
            ).pack(fill="x", pady=(0, scaled(8)))

        # Button bar
        sep = tk.Frame(card, bg=BORDER, height=1)
        sep.pack(fill="x")
        bar = tk.Frame(card, bg=BG_HEADER, height=scaled(48))
        bar.pack(fill="x")
        bar.pack_propagate(False)

        ctk.CTkButton(
            bar, text="Copy to clipboard",
            width=scaled(140), height=scaled(30),
            font=font_sized_px(_theme.FONT_FAMILY, 11),
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._copy,
        ).pack(side="right", padx=(scaled(4), scaled(10)), pady=scaled(8))
        ctk.CTkButton(
            bar, text="Close",
            width=scaled(80), height=scaled(30),
            font=font_sized_px(_theme.FONT_FAMILY, 11),
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=scaled(4), pady=scaled(8))

    def _copy(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(self._launch_string)
            self.update_idletasks()
        except Exception:
            pass


class _OverwritesDialog(tk.Toplevel):
    """Thin modal wrapper around OverwritesPanel."""

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
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        panel = OverwritesPanel(
            self,
            mod_name=mod_name,
            files_win=files_win,
            files_lose=files_lose,
            on_done=lambda p: self._on_close(),
        )
        panel.grid(row=0, column=0, sticky="nsew")

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ---------------------------------------------------------------------------
# OverwritesPanel — inline overlay version of _OverwritesDialog
# ---------------------------------------------------------------------------

class OverwritesPanel(ctk.CTkFrame):
    """Full-width overlay (spans mod list + plugin panel) showing conflict
    details for a single mod across three side-by-side panes."""

    def __init__(self, parent, mod_name: str,
                 files_win: list[tuple[str, str]],
                 files_lose: list[tuple[str, str]],
                 files_no_conflict: list[str] | None = None,
                 on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_done = on_done or (lambda p: None)
        files_no_conflict = files_no_conflict or []

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Conflicts: {mod_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Body — left column has win+lose stacked, right column has no-conflicts
        body = tk.Frame(self, bg=BG_DEEP)
        body.pack(fill="both", expand=True)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=2)
        body.grid_columnconfigure(1, weight=3)

        left = tk.Frame(body, bg=BG_DEEP)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_rowconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        self._build_two_col_pane(
            left, row=0, col=0,
            header=f"Files overriding others  ({len(files_win)})",
            header_color="#98c379",
            col0_title="File path",
            col1_title="Mod(s) beaten",
            rows=files_win,
            pady=(8, 4),
        )
        self._build_two_col_pane(
            left, row=1, col=0,
            header=f"Files overridden by others  ({len(files_lose)})",
            header_color="#e06c75",
            col0_title="File path",
            col1_title="Winning mod",
            rows=files_lose,
            pady=(4, 8),
        )
        self._build_one_col_pane(
            body, row=0, col=1,
            header=f"Files with no conflicts  ({len(files_no_conflict)})",
            header_color="#61afef",
            col0_title="File path",
            rows=files_no_conflict,
        )

        footer = tk.Frame(self, bg=BG_PANEL, height=44)
        footer.pack(fill="x")
        footer.pack_propagate(False)
        tk.Frame(footer, bg=BORDER, height=1).pack(side="top", fill="x")
        ctk.CTkButton(
            footer, text="Close",
            fg_color="#c0392b", hover_color="#a93226",
            text_color=TEXT_MAIN, font=FONT_BOLD,
            width=80, height=32,
            command=self._on_close,
        ).pack(side="right", padx=12, pady=6)

    def _build_two_col_pane(self, body, row, col, header, header_color,
                             col0_title, col1_title, rows, pady=8):
        outer = tk.Frame(body, bg=BG_PANEL)
        outer.grid(row=row, column=col, sticky="nsew", padx=8, pady=pady)
        outer.grid_rowconfigure(1, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        tk.Label(
            outer, text=header,
            bg=BG_PANEL, fg=header_color,
            font=font_sized_px(_theme.FONT_FAMILY, 10, "bold"), anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))

        tree_frame = tk.Frame(outer, bg=BG_DEEP)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        uid = f"OvPanel{row}{col}{id(self)}"
        style = ttk.Style()
        style.configure(f"{uid}.Treeview",
                        background=BG_DEEP, foreground=TEXT_MAIN,
                        fieldbackground=BG_DEEP, rowheight=scaled(20),
                        font=font_sized_px(_theme.FONT_FAMILY, 9))
        style.configure(f"{uid}.Treeview.Heading",
                        background=BG_HEADER, foreground=TEXT_SEP,
                        font=font_sized_px(_theme.FONT_FAMILY, 9, "bold"), relief="flat")
        style.map(f"{uid}.Treeview",
                  background=[("selected", BG_SELECT)],
                  foreground=[("selected", TEXT_MAIN)])

        tv = ttk.Treeview(
            tree_frame,
            columns=("col1",),
            displaycolumns=("col1",),
            show="headings tree",
            style=f"{uid}.Treeview",
            selectmode="browse",
        )
        tv.heading("#0",   text=col0_title, anchor="w")
        tv.heading("col1", text=col1_title, anchor="w")
        tv.column("#0",   minwidth=100, stretch=True)
        tv.column("col1", minwidth=100, stretch=True)

        def _resize_cols(event, _tv=tv):
            half = event.width // 2
            _tv.column("#0",   width=half)
            _tv.column("col1", width=half)
        tv.bind("<Configure>", _resize_cols)

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

    def _build_one_col_pane(self, body, row, col, header, header_color,
                             col0_title, rows):
        outer = tk.Frame(body, bg=BG_PANEL)
        outer.grid(row=row, column=col, sticky="nsew", padx=(4, 8), pady=8)
        outer.grid_rowconfigure(1, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        tk.Label(
            outer, text=header,
            bg=BG_PANEL, fg=header_color,
            font=font_sized_px(_theme.FONT_FAMILY, 10, "bold"), anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))

        tree_frame = tk.Frame(outer, bg=BG_DEEP)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        uid = f"NcPanel{col}{id(self)}"
        style = ttk.Style()
        style.configure(f"{uid}.Treeview",
                        background=BG_DEEP, foreground=TEXT_MAIN,
                        fieldbackground=BG_DEEP, rowheight=scaled(20),
                        font=font_sized_px(_theme.FONT_FAMILY, 9))
        style.configure(f"{uid}.Treeview.Heading",
                        background=BG_HEADER, foreground=TEXT_SEP,
                        font=font_sized_px(_theme.FONT_FAMILY, 9, "bold"), relief="flat")
        style.map(f"{uid}.Treeview",
                  background=[("selected", BG_SELECT)],
                  foreground=[("selected", TEXT_MAIN)])

        tv = ttk.Treeview(
            tree_frame,
            columns=(),
            show="tree",
            style=f"{uid}.Treeview",
            selectmode="browse",
        )
        tv.heading("#0", text=col0_title, anchor="w")
        tv.column("#0", minwidth=180, stretch=True)

        vsb = tk.Scrollbar(tree_frame, orient="vertical", command=tv.yview,
                           bg=BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
                           highlightthickness=0, bd=0)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        tv.bind("<Button-4>", lambda e: tv.yview_scroll(-3, "units"))
        tv.bind("<Button-5>", lambda e: tv.yview_scroll( 3, "units"))

        for path in rows:
            tv.insert("", "end", text=path)
        if not rows:
            tv.insert("", "end", text="(none)")

    def _on_close(self):
        self._on_done(self)


# VRAMr preset panel — overlay on plugin panel
# ---------------------------------------------------------------------------
class VRAMrPresetPanel(ctk.CTkFrame):
    """Inline panel overlaying the plugin panel. User picks a preset, clicks Run,
    then the panel hides and VRAMr runs in a background thread."""

    _PRESETS = [
        ("hq",          "High Quality",  "2K / 2K / 1K / 1K  — 4K modlist downscaled to 2K"),
        ("quality",     "Quality",       "2K / 1K / 1K / 1K  — Balance of quality & savings"),
        ("optimum",     "Optimum",       "2K / 1K / 512 / 512 — Good starting point"),
        ("performance", "Performance",   "2K / 512 / 512 / 512 — Big gains, lower close-up"),
        ("vanilla",     "Vanilla",       "512 / 512 / 512 / 512 — Just run the game"),
    ]

    def __init__(self, parent, *, bat_dir: Path, game_data_dir: Path,
                 output_dir: Path, log_fn, on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._bat_dir = bat_dir
        self._game_data_dir = game_data_dir
        self._output_dir = output_dir
        self._log = log_fn
        self._on_done = on_done or (lambda p: None)
        self._preset_var = tk.StringVar(value="optimum")
        self._build()

    def _build(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_propagate(False)
        ctk.CTkLabel(
            title_bar, text="VRAMr — Choose Preset",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="✕", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4, pady=4)

        # Body
        body = ctk.CTkFrame(self, fg_color=BG_DEEP, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            body, text="VRAMr Texture Optimiser",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(anchor="w", padx=20, pady=(20, 4))
        ctk.CTkLabel(
            body, text="Select an optimisation preset, then click Run.",
            font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(anchor="w", padx=20, pady=(0, 12))

        frame = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
        frame.pack(fill="x", padx=20, pady=4)
        for key, label, desc in self._PRESETS:
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=3)
            ctk.CTkRadioButton(
                row, text=label, variable=self._preset_var, value=key,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
            ).pack(side="left")
            ctk.CTkLabel(
                row, text=desc,
                font=FONT_SMALL, text_color=TEXT_DIM,
            ).pack(side="left", padx=(12, 0))

        ctk.CTkLabel(
            body, text=f"Output: {self._output_dir}",
            font=FONT_SMALL, text_color=TEXT_DIM, wraplength=480,
        ).pack(anchor="w", padx=20, pady=(12, 4))

        ctk.CTkButton(
            body, text="▶  Run VRAMr", width=160, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_run,
        ).pack(pady=(8, 20))

    def _on_close(self):
        self._on_done(self)

    def _on_run(self):
        preset = self._preset_var.get()
        self._log(f"VRAMr: starting with '{preset}' preset...")
        bat_dir = self._bat_dir
        game_data_dir = self._game_data_dir
        output_dir = self._output_dir
        log_fn = self._log
        app = self.winfo_toplevel()
        if hasattr(app, "_status"):
            app._status.show_log()
        self._on_done(self)

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
            font=(_theme.FONT_FAMILY, 16, "bold"), text_color="#d4d4d4",
        ).pack(pady=(16, 4))
        ctk.CTkLabel(
            self,
            text=(
                "Processes normal maps and parallax textures:\n"
                "BSA extract → filter → parallax prep → bend normals → BC7 compress"
            ),
            font=(_theme.FONT_FAMILY, 12), text_color="#858585", justify="center",
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self, text=f"Output: {self._output_dir}",
            font=(_theme.FONT_FAMILY, 11), text_color="#858585", wraplength=scaled(440),
        ).pack(pady=(4, 12))

        ctk.CTkButton(
            self, text="▶  Run BENDr", width=160, height=36,
            font=(_theme.FONT_FAMILY, 13, "bold"),
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
            font=(_theme.FONT_FAMILY, 16, "bold"), text_color="#d4d4d4",
        ).pack(pady=(16, 4))
        ctk.CTkLabel(
            self,
            text=(
                "Processes normal maps and parallax textures:\n"
                "BSA extract → filter pairs → height maps → output QC"
            ),
            font=(_theme.FONT_FAMILY, 12), text_color="#858585", justify="center",
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self, text=f"Output: {self._output_dir}",
            font=(_theme.FONT_FAMILY, 11), text_color="#858585", wraplength=scaled(440),
        ).pack(pady=(4, 12))

        ctk.CTkButton(
            self, text="▶  Run ParallaxR", width=160, height=36,
            font=(_theme.FONT_FAMILY, 13, "bold"),
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


_PGPATCHER_DEFAULT_PROTON = ""  # empty string = "Game default"


def _get_tool_prefix_env(exe_path: "Path", proton_name: str) -> "tuple[Path, Path, dict] | None":
    """Resolve (proton_script, prefix_dir, env) for a tool's isolated prefix.

    proton_name is the display name from the dropdown (e.g. "Proton 10.0").
    Returns None if the Proton version can't be found.
    The prefix directory is created if it doesn't exist.
    Runs wineboot to initialise the prefix if it's brand new.
    """
    from Utils.steam_finder import find_any_installed_proton, find_steam_root_for_proton_script
    proton_script = find_any_installed_proton(proton_name)
    if proton_script is None:
        return None

    steam_root = find_steam_root_for_proton_script(proton_script)
    if steam_root is None:
        return None

    prefix_dir = exe_path.parent / f"prefix_{proton_script.parent.name}"
    is_new = not (prefix_dir / "pfx").is_dir()
    prefix_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["STEAM_COMPAT_DATA_PATH"] = str(prefix_dir)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)

    if is_new:
        # Initialise the prefix synchronously before returning
        try:
            subprocess.run(
                ["python3", str(proton_script), "run", "wineboot", "--init"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except Exception:
            pass

    return proton_script, prefix_dir, env


class _ExeConfigDialog(ctk.CTkToplevel):
    """Thin modal wrapper around ExeConfigPanel.

    Callers access: ``result``, ``launch_mode``, ``deploy_before_launch``,
    ``hide``, ``removed``, ``proton_override``, ``data_folder_exe``.
    """

    def __init__(self, parent, exe_path: "Path", game, saved_args: str = "",
                 custom_exes: "list | None" = None, launch_mode: "str | None" = None,
                 deploy_before_launch: "bool | None" = None,
                 is_hidden: bool = False, proton_override: "str | None" = None,
                 is_data_folder_exe: bool = False, is_apps_exe: bool = False,
                 saved_launch_options: str = "",
                 log_fn=None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"Configure: {exe_path.name}")
        self.geometry("480x180" if launch_mode is not None else "640x460")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Result attributes proxied from the panel
        self.result: "str | None" = None
        self.launch_mode: "str | None" = None
        self.deploy_before_launch: "bool | None" = None
        self.hide: "bool | None" = None
        self.removed: bool = False
        self.proton_override: "str | None" = None
        self.data_folder_exe: "bool | None" = None
        self.launch_options: "str | None" = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        def _on_done(panel):
            # Copy result attributes from panel before destroying
            self.result = panel.result
            self.launch_mode = panel.launch_mode
            self.deploy_before_launch = panel.deploy_before_launch
            self.hide = panel.hide
            self.removed = panel.removed
            self.proton_override = panel.proton_override
            self.data_folder_exe = panel.data_folder_exe
            self.launch_options = panel.launch_options
            self._on_close()

        self._panel = ExeConfigPanel(
            self,
            exe_path=exe_path, game=game, saved_args=saved_args,
            custom_exes=custom_exes, launch_mode=launch_mode,
            deploy_before_launch=deploy_before_launch, is_hidden=is_hidden,
            on_done=_on_done, proton_override=proton_override,
            is_data_folder_exe=is_data_folder_exe, is_apps_exe=is_apps_exe,
            saved_launch_options=saved_launch_options,
            log_fn=log_fn,
        )
        self._panel.grid(row=0, column=0, sticky="nsew")

        self.after(80, self._make_modal)

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


# ---------------------------------------------------------------------------
# ExeConfigPanel — inline overlay version of _ExeConfigDialog
# ---------------------------------------------------------------------------

class ExeConfigPanel(ctk.CTkFrame):
    """Inline panel version of _ExeConfigDialog. Overlays the plugin-panel container.
    Uses on_done(panel) callback; caller reads panel.result / .launch_mode / etc."""

    _EXE_ARGS_FILE = get_exe_args_path()

    def __init__(self, parent, exe_path: "Path", game, saved_args: str = "",
                 custom_exes: "list | None" = None, launch_mode: "str | None" = None,
                 deploy_before_launch: "bool | None" = None,
                 is_hidden: bool = False, on_done=None, proton_override: "str | None" = None,
                 is_data_folder_exe: bool = False, is_apps_exe: bool = False,
                 saved_launch_options: str = "",
                 log_fn=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._log = log_fn or print

        self._exe_path = exe_path
        self._game = game
        self._saved_args = saved_args
        self._custom_exes: "list" = list(custom_exes) if custom_exes else []
        self._initial_launch_mode: "str | None" = launch_mode
        self._launch_mode_var = tk.StringVar(value=launch_mode or "auto")
        self._deploy_before_launch_var = tk.BooleanVar(
            value=True if deploy_before_launch is None else deploy_before_launch
        )
        self._hide_var = tk.BooleanVar(value=is_hidden)
        self._data_folder_var = tk.BooleanVar(value=is_data_folder_exe)
        self._is_apps_exe = is_apps_exe
        self._launch_options_var = tk.StringVar(value=saved_launch_options)
        self._on_done = on_done or (lambda p: None)
        self.result: "str | None" = None
        self.launch_mode: "str | None" = None
        self.deploy_before_launch: "bool | None" = None
        self.hide: "bool | None" = None
        self.removed: bool = False
        self.proton_override: "str | None" = None
        self.data_folder_exe: "bool | None" = None
        self.launch_options: "str | None" = None

        # Proton version dropdown (non-launcher exes only)
        from Utils.steam_finder import list_installed_proton
        self._proton_versions: list[str] = (
            ["Game default"] + [p.parent.name for p in list_installed_proton()]
        )
        _default_override = _PGPATCHER_DEFAULT_PROTON if exe_path.name.lower() == "pgpatcher.exe" else ""
        _saved = proton_override if proton_override is not None else _default_override
        def _best_match(name: str) -> str:
            if not name:
                return "Game default"
            if name in self._proton_versions:
                return name
            name_lower = name.lower()
            for v in self._proton_versions:
                if v.lower().startswith(name_lower):
                    return v
            return "Game default"
        self._proton_var = tk.StringVar(value=_best_match(_saved))

        # Per-profile exe_args.json when the profile uses profile-specific mods
        self._EXE_ARGS_FILE = _resolve_exe_args_file(game)

        self._game_path: "Path | None" = (
            game.get_game_path() if hasattr(game, "get_game_path") else None
        )
        self._mods_path: "Path | None" = (
            game.get_effective_mod_staging_path() if hasattr(game, "get_effective_mod_staging_path") else None
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

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Configure: {exe_path.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Body frame — grid layout lives here
        self._body = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self._body.pack(fill="both", expand=True)

        self._build()

        if self._initial_launch_mode is None:
            self._load_saved()
            self._game_flag_var.trace_add("write", self._assemble)
            self._output_flag_var.trace_add("write", self._assemble)
            self._mod_var.trace_add("write", self._assemble)

    def _load_mod_entries(self) -> "list[tuple[str, Path]]":
        entries: list[tuple[str, Path]] = []
        if self._overwrite_path and self._overwrite_path.is_dir():
            entries.append(("overwrite", self._overwrite_path))
        if self._mods_path and self._mods_path.is_dir():
            for e in sorted(self._mods_path.iterdir(), key=lambda p: p.name.casefold()):
                if e.is_dir() and "_separator" not in e.name:
                    entries.append((e.name, e))
        return entries

    def _build(self):
        body = self._body
        body.grid_columnconfigure(0, weight=1)

        is_game_exe = self._initial_launch_mode is not None
        body_total_row = count(start=0)

        if not is_game_exe:
            sec1 = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
            sec1.grid(row=next(body_total_row), column=0, sticky="ew", padx=12, pady=(12, 4))
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

            sec2 = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
            sec2.grid(row=next(body_total_row), column=0, sticky="ew", padx=12, pady=4)
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
            _mod_placeholder = (
                "PGPatcher (default)" if self._exe_path.name.lower() == "pgpatcher.exe"
                else "search mods..."
            )
            self._mod_entry = ctk.CTkEntry(
                mod_row, textvariable=self._mod_var, font=FONT_SMALL,
                fg_color=BG_HEADER, text_color=TEXT_MAIN, border_color=BORDER,
                placeholder_text=_mod_placeholder,
            )
            self._mod_entry.grid(row=0, column=0, sticky="ew")
            self._mod_entry._entry.bind(
                "<Control-a>",
                lambda e: (self._mod_entry._entry.select_range(0, "end"),
                           self._mod_entry._entry.icursor("end"), "break")[2],
            )
            ctk.CTkButton(
                mod_row, text="\u25bc", width=28, font=FONT_SMALL,
                fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
                command=self._open_mod_popup,
            ).grid(row=0, column=1, padx=(4, 0))
            self._mod_var.trace_add("write", self._on_mod_typed)

            sec3 = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
            sec3.grid(row=next(body_total_row), column=0, sticky="ew", padx=12, pady=4)
            sec3.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                sec3, text="Final argument (editable)", font=FONT_BOLD,
                text_color=TEXT_MAIN, anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))

            self._final_box = ctk.CTkTextbox(
                sec3, height=90, font=FONT_NORMAL,
                fg_color=BG_HEADER, text_color=TEXT_MAIN, border_color=BORDER,
                border_width=1, wrap="word",
            )
            self._final_box.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))

            # Proton version section
            sec_proton = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
            sec_proton.grid(row=next(body_total_row), column=0, sticky="ew", padx=12, pady=4)
            sec_proton.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(
                sec_proton, text="Proton version", font=FONT_BOLD,
                text_color=TEXT_MAIN, anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 4))
            ctk.CTkOptionMenu(
                sec_proton, values=self._proton_versions,
                variable=self._proton_var,
                width=220, font=FONT_SMALL,
                fg_color=BG_HEADER, button_color=ACCENT, button_hover_color=ACCENT_HOV,
                dropdown_fg_color=BG_PANEL, text_color=TEXT_MAIN,
                command=lambda _: None,
            ).grid(row=0, column=1, sticky="w", padx=(0, 10), pady=(8, 4))
            ctk.CTkLabel(
                sec_proton,
                text="Use a specific Proton version with an isolated prefix next to\n"
                     "the exe, instead of the game's prefix. Useful for tools that\n"
                     "don't work with the game's Proton version.",
                font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
            ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 4))

            btn_row = ctk.CTkFrame(sec_proton, fg_color="transparent")
            btn_row.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 8))
            small_btn = dict(height=28, font=FONT_SMALL,
                             fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN)
            ctk.CTkButton(
                btn_row, text="Run EXE in prefix …", width=160,
                command=self._run_exe_in_prefix, **small_btn,
            ).pack(side="left", padx=(0, 6))
            ctk.CTkButton(
                btn_row, text="Run protontricks", width=140,
                command=self._run_protontricks_in_prefix, **small_btn,
            ).pack(side="left")

        if is_game_exe:
            sec4 = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
            sec4.grid(row=next(body_total_row), column=0, sticky="ew", padx=12, pady=(12, 4))
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

        # Launch Options section (shown for all exe types)
        sec_launch = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
        sec_launch.grid(row=next(body_total_row), column=0, sticky="ew", padx=12, pady=4)
        sec_launch.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            sec_launch, text="Launch Options", font=FONT_BOLD,
            text_color=TEXT_MAIN, anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
        ctk.CTkLabel(
            sec_launch,
            text="Steam-style options: env vars (KEY=VALUE), wrappers (e.g. gamemoderun), and"
                 "%command% as placeholder for the full command. Without %command%, appended as suffix.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",  wraplength=460,
        ).grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))
        _lo_entry = ctk.CTkEntry(
            sec_launch, textvariable=self._launch_options_var, font=FONT_SMALL,
            fg_color=BG_HEADER, text_color=TEXT_MAIN, border_color=BORDER,
            placeholder_text="e.g. PROTON_ENABLE_WAYLAND=0 gamemoderun %command%",
        )
        _lo_entry.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))
        _lo_entry._entry.bind(
            "<Control-a>",
            lambda e: (_lo_entry._entry.select_range(0, "end"),
                       _lo_entry._entry.icursor("end"), "break")[2],
        )

        bar = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=0, height=48)
        bar.grid(row=next(body_total_row), column=0, sticky="ew")
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
        if self._exe_path in self._custom_exes:
            ctk.CTkButton(
                bar, text="Remove EXE", width=110, height=30, font=FONT_NORMAL,
                fg_color="#8B1A1A", hover_color="#B22222", text_color="white",
                command=self._on_remove,
            ).pack(side="left", padx=(12, 4), pady=9)
        if not is_game_exe:
            ctk.CTkCheckBox(
                bar, text="Hide from dropdown",
                variable=self._hide_var,
                font=FONT_SMALL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
                checkmark_color=BG_DEEP,
            ).pack(side="left", padx=(12, 4), pady=9)
        if not is_game_exe and not self._is_apps_exe:
            ctk.CTkCheckBox(
                bar, text="Run from Data folder",
                variable=self._data_folder_var,
                font=FONT_SMALL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
                checkmark_color=BG_DEEP,
            ).pack(side="left", padx=(4, 4), pady=9)

    def _on_mod_typed(self, *_):
        if self._mod_popup and self._mod_popup.winfo_exists():
            self._populate_mod_popup()

    def _open_mod_popup(self):
        if self._mod_popup and self._mod_popup.winfo_exists():
            self._mod_popup.destroy()
            self._mod_popup = None
            return

        popup = tk.Toplevel(self.winfo_toplevel())
        popup.overrideredirect(True)
        popup.configure(bg=BG_PANEL)
        self._mod_popup = popup

        self._mod_entry.update_idletasks()
        x = self._mod_entry.winfo_rootx()
        y = self._mod_entry.winfo_rooty() + self._mod_entry.winfo_height() + 2
        w = self._mod_entry.winfo_width() + 32
        popup.geometry(f"{w}x300+{x}+{y}")

        scroll = ctk.CTkScrollableFrame(popup, fg_color=BG_PANEL, corner_radius=0)
        scroll.pack(fill="both", expand=True)
        scroll.grid_columnconfigure(0, weight=1)
        self._mod_popup_scroll = scroll
        self._populate_mod_popup(show_all=True)

        popup.bind("<Escape>", lambda _: self._close_mod_popup())
        popup.lift()

        def _forward_scroll(event):
            canvas = scroll._parent_canvas
            if event.num == 4 or event.delta > 0:
                canvas.yview_scroll(-1, "units")
            else:
                canvas.yview_scroll(1, "units")
        popup.bind("<MouseWheel>", _forward_scroll)
        popup.bind("<Button-4>", _forward_scroll)
        popup.bind("<Button-5>", _forward_scroll)

        def _bind_click_dismiss():
            if self._mod_popup and self._mod_popup.winfo_exists():
                self._mod_popup_click_id = self.bind(
                    "<Button-1>", self._on_root_click_while_popup, add="+"
                )
        self.after(100, _bind_click_dismiss)
        self._poll_mod_popup_focus()

    def _poll_mod_popup_focus(self):
        if not self._mod_popup or not self._mod_popup.winfo_exists():
            return
        try:
            mx, my = self.winfo_pointerx(), self.winfo_pointery()
            widget_under = self.winfo_containing(mx, my)
        except Exception:
            widget_under = None
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
        px, py, pw, ph = (
            self._mod_popup.winfo_rootx(), self._mod_popup.winfo_rooty(),
            self._mod_popup.winfo_width(), self._mod_popup.winfo_height(),
        )
        if px <= event.x_root <= px + pw and py <= event.y_root <= py + ph:
            return
        self._close_mod_popup()

    def _populate_mod_popup(self, show_all: bool = False):
        scroll = self._mod_popup_scroll
        for w in scroll.winfo_children():
            w.destroy()
        query = self._mod_var.get().casefold()
        names = [n for n, _ in self._mod_entries]
        filtered = names if (show_all or not query) else [n for n in names if query in n.casefold()]
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
            profile = EXE_PROFILES.get(self._exe_path.name)
            suffix = profile.game_path_suffix if profile else ""
            target = self._game_path / suffix if suffix else self._game_path
            wine = _to_wine_path(target)
            parts.append(f'{game_flag}"{wine}"')

        out_flag = self._output_flag_var.get().strip()
        selected = self._mod_var.get().strip()
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
        if self._exe_path.name.lower() == "pgpatcher.exe":
            # Output mod is stored separately; load it independently of saved_args.
            try:
                data = json.loads(self._EXE_ARGS_FILE.read_text(encoding="utf-8"))
                saved_mod = data.get("PGPatcher.exe:output_mod", "")
                if saved_mod:
                    self._mod_var.set(saved_mod)
            except (OSError, ValueError):
                pass
        if self._saved_args:
            self._parse_saved_args(self._saved_args)
            # Re-assemble with current (profile-correct) paths if the output
            # mod was resolved to an actual entry; otherwise keep the saved text
            # so auto-configured args are never blanked out.
            selected = self._mod_var.get()
            path_found = any(n == selected for n, _ in self._mod_entries)
            if path_found:
                self._assemble()
            else:
                self._set_final_text(self._saved_args)

    def _get_selected_tool_env(self):
        selected = self._proton_var.get()
        if selected == "Game default":
            self._log("Prefix tools: select a specific Proton version first.")
            return None
        result = _get_tool_prefix_env(self._exe_path, selected)
        if result is None:
            self._log(f"Prefix tools: could not find Proton '{selected}'.")
        return result

    def _run_exe_in_prefix(self):
        result = self._get_selected_tool_env()
        if result is None:
            return
        proton_script, prefix_dir, env = result
        self._log(f"Prefix tools: initialised prefix at {prefix_dir}, opening file picker …")

        def _on_picked(exe):
            if exe is None:
                return
            if not exe.is_file():
                self._log(f"Prefix tools: file not found: {exe}")
                return
            self._log(f"Prefix tools: launching {exe.name} …")
            try:
                subprocess.Popen(
                    ["python3", str(proton_script), "run", str(exe)],
                    env=env, cwd=exe.parent,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                self._log(f"Prefix tools error: {e}")

        from Utils.portal_filechooser import pick_exe_file
        pick_exe_file("Select EXE to run in prefix", _on_picked)

    def _run_protontricks_in_prefix(self):
        result = self._get_selected_tool_env()
        if result is None:
            return
        proton_script, prefix_dir, env = result

        steam_id = str(getattr(self._game, "steam_id", "") or "")

        if shutil.which("protontricks") is not None:
            cmd = ["protontricks"]
            if steam_id:
                cmd += [steam_id, "--gui"]
            else:
                cmd += ["--gui"]
        elif shutil.which("flatpak") is not None and subprocess.run(
            ["flatpak", "info", "com.github.Matoking.protontricks"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0:
            cmd = ["flatpak", "run", "com.github.Matoking.protontricks"]
            if steam_id:
                cmd += [steam_id, "--gui"]
            else:
                cmd += ["--gui"]
        else:
            self._log("Prefix tools: protontricks not found.")
            return

        env["STEAM_COMPAT_DATA_PATH"] = str(prefix_dir)
        env["PROTON_VERSION"] = proton_script.parent.name
        self._log(f"Prefix tools: launching protontricks for prefix {prefix_dir.name} …")
        try:
            subprocess.Popen(
                cmd, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self._log(f"Prefix tools error: {e}")

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
            if self._exe_path.name.lower() == "pgpatcher.exe":
                data["PGPatcher.exe:output_mod"] = self._mod_var.get().strip()
            try:
                self._EXE_ARGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except OSError:
                pass
            self.result = final
            self.hide = self._hide_var.get()
            selected = self._proton_var.get()
            self.proton_override = "" if selected == "Game default" else selected
            if not self._is_apps_exe:
                self.data_folder_exe = self._data_folder_var.get()
        self.launch_options = self._launch_options_var.get().strip()
        self._on_done(self)

    def _on_remove(self):
        self.removed = True
        self.result = None
        self._on_done(self)

    def _on_cancel(self):
        self._on_done(self)


class _ReplaceModDialog(ctk.CTkToplevel):
    """Modal dialog shown when installing a mod whose name already exists.
    result: "all" | "selected" | "rename" | "cancel"
    selected_files: set[str] — always None here; populated by caller if "selected"
    new_name: str | None — set when result == "rename"
    """

    _WIDTH = 480
    _HEIGHT = 180

    def __init__(self, parent, mod_name: str):
        self._parent_ref = parent
        super().__init__(master=parent)
        self.old_x = None
        self.old_y = None
        self.resizable(False, False)
        self.overrideredirect(True)
        if parent is not None:
            self.transient(parent)
        self.withdraw()

        self.result: str = "cancel"
        self.selected_files: set[str] | None = None
        self.new_name: str | None = None
        self._mod_name = mod_name

        self.transparent_color = self._apply_appearance_mode(self.cget("fg_color"))
        if sys.platform.startswith("win"):
            self.attributes("-transparentcolor", self.transparent_color)

        self.bg_color = self._apply_appearance_mode(
            ctk.ThemeManager.theme["CTkFrame"]["fg_color"])

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._frame = ctk.CTkFrame(
            self, corner_radius=5, width=self._WIDTH, border_width=1,
            bg_color=self.transparent_color, fg_color=self.bg_color,
        )
        self._frame.grid(sticky="nsew")
        self._frame.bind("<B1-Motion>", self._move_window)
        self._frame.bind("<ButtonPress-1>", self._old_xy_set)
        self._frame.grid_columnconfigure(0, weight=1)
        self._frame.grid_rowconfigure(1, weight=1)

        # Icons
        _warn = _PilImage.open(ICON_PATH["warning"])
        self._warn_icon = ctk.CTkImage(_warn, _warn, (30, 30))
        _cl = _PilImage.open(ICON_PATH["close"][0])
        _cl_d = _PilImage.open(ICON_PATH["close"][1])
        self._close_icon = ctk.CTkImage(_cl, _cl_d, (20, 20))

        # Title row
        title_lbl = ctk.CTkLabel(
            self._frame, text="  Mod Already Exists", font=("", 18),
            image=self._warn_icon, compound="left",
        )
        title_lbl.grid(row=0, column=0, sticky="w", padx=15, pady=(12, 4))
        title_lbl.bind("<B1-Motion>", self._move_window)
        title_lbl.bind("<ButtonPress-1>", self._old_xy_set)

        ctk.CTkButton(
            self._frame, text="", image=self._close_icon, width=20, height=20,
            hover=False, fg_color="transparent", command=self._on_cancel,
        ).grid(row=0, column=1, sticky="ne", padx=10, pady=10)

        # Body text
        ctk.CTkLabel(
            self._frame,
            text=f"'{mod_name}' is already installed.\nHow would you like to handle the existing mod?",
            justify="left", anchor="w",
            wraplength=self._WIDTH - 40,
        ).grid(row=1, column=0, padx=(20, 10), pady=(0, 6), sticky="new", columnspan=2)

        # Rename row (collapsed by default)
        self._rename_frame = ctk.CTkFrame(self._frame, fg_color="transparent")
        self._rename_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 4))
        self._rename_frame.grid_columnconfigure(0, weight=1)
        self._rename_frame.grid_remove()

        self._rename_var = tk.StringVar(value=mod_name)
        rename_entry = ctk.CTkEntry(
            self._rename_frame, textvariable=self._rename_var,
            font=FONT_NORMAL, fg_color=BG_PANEL, text_color=TEXT_MAIN,
            border_color=BORDER,
        )
        rename_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        rename_entry.bind("<Return>", lambda _e: self._on_rename_confirm())
        self._rename_entry = rename_entry

        ctk.CTkButton(
            self._rename_frame, text="Confirm", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_rename_confirm,
        ).grid(row=0, column=1)

        # Button row
        btn_frame = ctk.CTkFrame(self._frame, fg_color="transparent")
        btn_frame.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(2, 10))

        ctk.CTkButton(
            btn_frame, text="Replace All", width=110,
            text_color="white", command=self._on_all,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_frame, text="Replace Selected", width=130, fg_color="transparent", border_width=1,
            text_color=("black", "white"), command=self._on_selected,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_frame, text="Rename", width=90, fg_color="transparent", border_width=1,
            text_color=("black", "white"), command=self._on_rename,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_frame, text="Cancel", width=90, fg_color="#c0392b", hover_color="#a93226",
            text_color="white", command=self._on_cancel,
        ).pack(side="left", padx=4)

        self.bind("<Escape>", lambda _e: self._on_cancel())
        self._center_and_show()

    def _center_and_show(self):
        parent = self._parent_ref
        self.geometry(f"{self._WIDTH}")
        self.update_idletasks()
        # winfo_reqheight() returns scaled tk pixels; convert back to design
        # units so CTkToplevel.geometry() doesn't double-scale the value.
        scale = self._get_window_scaling() or 1
        req_h = self.winfo_reqheight() / scale
        final_h = int(max(req_h, self._HEIGHT))
        self.geometry(f"{self._WIDTH}x{final_h}")
        self.update_idletasks()
        if parent is not None:
            try:
                top = parent.winfo_toplevel()
                top.update_idletasks()
                px = top.winfo_rootx()
                py = top.winfo_rooty()
                pw = top.winfo_width()
                ph = top.winfo_height()
                aw = self.winfo_width()
                ah = self.winfo_height()
                if aw <= 1:
                    aw = scaled(self._WIDTH)
                if ah <= 1:
                    ah = scaled(final_h)
                self.geometry(f"+{px + (pw - aw) // 2}+{py + (ph - ah) // 2}")
            except Exception:
                pass
        self.deiconify()
        self.lift()
        self.focus_force()
        self.after(50, self._grab)

    def _grab(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _old_xy_set(self, event):
        self.old_x = event.x_root
        self.old_y = event.y_root

    def _move_window(self, event):
        if self.old_x is None or self.old_y is None:
            return
        dx = event.x_root - self.old_x
        dy = event.y_root - self.old_y
        self.geometry(f"+{self.winfo_x() + dx}+{self.winfo_y() + dy}")
        self.old_x = event.x_root
        self.old_y = event.y_root

    def _on_rename(self):
        if self._rename_frame is None:
            return
        self._rename_frame.grid()
        self.update_idletasks()
        scale = self._get_window_scaling() or 1
        req_h = self.winfo_reqheight() / scale
        new_h = int(max(req_h, self._HEIGHT))
        self.geometry(f"{self._WIDTH}x{new_h}")
        self.update_idletasks()
        parent = self._parent_ref
        if parent is not None:
            try:
                top = parent.winfo_toplevel()
                top.update_idletasks()
                px = top.winfo_rootx()
                py = top.winfo_rooty()
                pw = top.winfo_width()
                ph = top.winfo_height()
                aw = self.winfo_width() or scaled(self._WIDTH)
                ah = self.winfo_height() or scaled(new_h)
                self.geometry(f"+{px + (pw - aw) // 2}+{py + (ph - ah) // 2}")
            except Exception:
                pass
        self._rename_entry.focus_set()
        self._rename_entry.select_range(0, "end")

    def _on_rename_confirm(self):
        name = self._rename_var.get().strip() if self._rename_var else ""
        if not name or name == self._mod_name:
            return
        self.result = "rename"
        self.new_name = name
        self.grab_release()
        self.destroy()

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

    _FONT_TITLE = (_theme.FONT_FAMILY, 14, "bold")
    _FONT_BODY  = (_theme.FONT_FAMILY, 13)
    _FONT_ENTRY = (_theme.FONT_FAMILY, 13)
    _FONT_TREE  = ("Courier New", 12)
    _FONT_BTN   = (_theme.FONT_FAMILY, 13)
    _FONT_BTN_B = (_theme.FONT_FAMILY, 13, "bold")

    def __init__(self, parent, required_folders: set[str],
                 file_list: list[tuple[str, str, bool]],
                 mod_name: str = ""):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Unexpected Mod Structure")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: tuple[str, str | None] | None = None
        self._required  = required_folders
        self._file_list = file_list
        self._mod_name  = (mod_name or "").strip()
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

        row = 0
        if self._mod_name:
            ctk.CTkLabel(
                self,
                text=f"Mod: {self._mod_name}",
                font=self._FONT_TITLE,
                text_color=ACCENT,
                anchor="w",
            ).grid(row=row, column=0, sticky="ew", padx=16, pady=(16, 2))
            row += 1

        ctk.CTkLabel(
            self,
            text="This mod has no recognised top-level folders.",
            font=self._FONT_TITLE,
            text_color=TEXT_MAIN,
            anchor="w",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(2 if self._mod_name else (16, 2), 2))
        row += 1

        folders_str = ",  ".join(sorted(self._required))
        ctk.CTkLabel(
            self,
            text=f"Expected one of:  {folders_str}",
            font=self._FONT_BODY,
            text_color=TEXT_DIM,
            anchor="w",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 12))
        row += 1

        ctk.CTkLabel(
            self,
            text="Install all files under this path (e.g. archive/pc/mod):",
            font=self._FONT_BODY,
            text_color=TEXT_MAIN,
            anchor="w",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        row += 1

        self._entry = ctk.CTkEntry(
            self,
            textvariable=self._entry_var,
            font=self._FONT_ENTRY,
            fg_color=BG_PANEL,
            border_color=BORDER,
            text_color=TEXT_MAIN,
            height=36,
        )
        self._entry.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._entry.focus_set()
        row += 1

        tree_row = row
        self.grid_rowconfigure(tree_row, weight=1)
        tree_frame = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=6)
        tree_frame.grid(row=tree_row, column=0, sticky="nsew", padx=16, pady=(0, 10))
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
        bar.grid(row=tree_row + 1, column=0, sticky="ew")
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

        row = 0
        for src_rel, dst_rel, is_folder in self._file_list:
            var = tk.BooleanVar(value=True)
            self._vars.append((var, dst_rel))
            ctk.CTkCheckBox(
                scroll,
                text=dst_rel or src_rel,
                variable=var,
                font=FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                checkmark_color="white",
                border_color=BORDER,
            ).grid(row=row, column=0, sticky="w", padx=8, pady=2)
            row += 1

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

        self.after(50, self._position_window)

    def _position_window(self):
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
# Per-mod plugin disabling dialog
# ---------------------------------------------------------------------------
class _DisablePluginsDialog(ctk.CTkToplevel):
    """Thin modal wrapper around DisablePluginsPanel.

    Callers access ``result`` (set[str] | None).
    """

    def __init__(self, parent, mod_name: str,
                 plugin_names: list[str], disabled: set[str]):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"Disable Plugins — {mod_name}")
        self.resizable(False, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._make_modal)

        self.result: set[str] | None = None

        h = min(500, max(200, 80 + len(plugin_names) * 32 + 60 + 52))
        w = 400
        self.update_idletasks()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            self.geometry(f"{w}x{h}")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        def _on_done(panel):
            self.result = panel.result
            self._on_close()

        panel = DisablePluginsPanel(
            self,
            mod_name=mod_name,
            plugin_names=plugin_names,
            disabled=disabled,
            on_done=_on_done,
        )
        panel.grid(row=0, column=0, sticky="nsew")

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


# ---------------------------------------------------------------------------
# DisablePluginsPanel — inline overlay version of _DisablePluginsDialog
# ---------------------------------------------------------------------------

class DisablePluginsPanel(ctk.CTkFrame):
    """
    Inline panel version of _DisablePluginsDialog. Overlays _plugin_panel_container.
    result: set[str] of plugin names to disable, or None if cancelled.
    """

    def __init__(self, parent, mod_name: str,
                 plugin_names: list[str], disabled: set[str],
                 on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self.result: set[str] | None = None
        self._plugin_names = plugin_names
        self._disabled_lower = {n.lower() for n in disabled}
        self._on_done = on_done or (lambda p: None)
        self._vars: list[tuple[tk.BooleanVar, str]] = []

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Disable Plugins \u2014 {mod_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        self._build()

    def _build(self):
        ctk.CTkLabel(
            self,
            text="Checked plugins are enabled and will appear in plugins.txt.\n"
                 "Uncheck a plugin to exclude it.",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
            anchor="w",
            justify="left",
        ).pack(anchor="w", padx=16, pady=(12, 6))

        scroll = ctk.CTkScrollableFrame(self, fg_color=BG_PANEL, corner_radius=6)
        scroll.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        scroll.grid_columnconfigure(0, weight=1)

        for i, name in enumerate(self._plugin_names):
            var = tk.BooleanVar(value=name.lower() not in self._disabled_lower)
            self._vars.append((var, name))
            ctk.CTkCheckBox(
                scroll,
                text=name,
                variable=var,
                font=FONT_NORMAL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                checkmark_color="white",
                border_color=BORDER,
            ).grid(row=i, column=0, sticky="w", padx=8, pady=3)

        helper = ctk.CTkFrame(self, fg_color="transparent")
        helper.pack(fill="x", padx=12, pady=(0, 4))
        ctk.CTkButton(
            helper, text="Enable All", width=90, height=24, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=lambda: [v.set(True) for v, _ in self._vars],
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            helper, text="Disable All", width=90, height=24, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=lambda: [v.set(False) for v, _ in self._vars],
        ).pack(side="left")

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=12)
        ctk.CTkButton(
            bar, text="Save", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=12)

    def _on_ok(self):
        self.result = {name for var, name in self._vars if not var.get()}
        self._on_done(self)

    def _on_cancel(self):
        self._on_done(self)


# ---------------------------------------------------------------------------
# Download Custom Handler panel — overlay to download JSON handlers from GitHub
# ---------------------------------------------------------------------------

class DownloadCustomHandlerPanel(ctk.CTkFrame):
    """
    Overlay on the plugin panel listing custom game handlers from GitHub.
    User can download .json files into ~/.config/AmethystModManager/custom_games/
    """

    def __init__(self, parent, on_done=None, on_downloaded=None, log_fn=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_done = on_done or (lambda p: None)
        self._on_downloaded = on_downloaded or (lambda: None)
        self._log_fn = log_fn or (lambda msg: None)
        self._handlers: list[dict] = []
        self._status_var = tk.StringVar(value="Loading …")

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text="Download Custom Handler",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Content
        ctk.CTkLabel(
            self,
            text="Handlers from the Amethyst Mod Manager repository",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
            anchor="w",
            justify="left",
        ).pack(anchor="w", padx=16, pady=(12, 4))

        self._status_lbl = ctk.CTkLabel(
            self, textvariable=self._status_var,
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
        )
        self._status_lbl.pack(anchor="w", padx=16, pady=(0, 8))

        self._scroll = ctk.CTkScrollableFrame(self, fg_color=BG_PANEL, corner_radius=6)
        self._scroll.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self._scroll.grid_columnconfigure(0, weight=1)

        # Bottom bar
        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(side="top", fill="x")
        ctk.CTkButton(
            bar, text="Close", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=(4, 12), pady=12)

        # Fetch handlers in background
        threading.Thread(target=self._fetch_handlers, daemon=True).start()

    def _fetch_handlers(self):
        """Fetch the list of JSON files from GitHub API and extract game names."""
        import json as _json
        import urllib.request as _urllib
        try:
            req = _urllib.Request(
                _CUSTOM_HANDLERS_API_URL,
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            with _urllib.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode("utf-8", errors="replace"))
            handlers = [e for e in data if isinstance(e, dict) and e.get("name", "").endswith(".json")]
            # Fetch each file to get the "name" field from inside the JSON
            for h in handlers:
                display_name = h.get("name", "").removesuffix(".json").replace("_", " ")
                download_url = h.get("download_url")
                if download_url:
                    try:
                        r = _urllib.Request(download_url, headers={"User-Agent": "Amethyst-Mod-Manager"})
                        with _urllib.urlopen(r, timeout=10) as resp:
                            parsed = _json.loads(resp.read().decode("utf-8", errors="replace"))
                        if isinstance(parsed, dict) and parsed.get("name"):
                            display_name = parsed["name"]
                    except Exception:
                        pass
                h["_display_name"] = display_name
            self.after(0, lambda: self._on_handlers_loaded(handlers))
        except Exception as e:
            self.after(0, lambda: self._on_fetch_error(str(e)))

    def _on_handlers_loaded(self, handlers: list):
        self._handlers = handlers
        self._status_var.set(f"{len(handlers)} handler(s) available" if handlers else "No handlers found")
        for row, h in enumerate(handlers):
            display_name = h.get("_display_name", h.get("name", ""))
            filename = h.get("name", "")
            download_url = h.get("download_url")
            if not download_url:
                continue
            row_frame = ctk.CTkFrame(self._scroll, fg_color="transparent")
            row_frame.grid(row=row, column=0, sticky="ew", padx=8, pady=3)
            row_frame.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                row_frame, text=display_name, font=FONT_NORMAL, text_color=TEXT_MAIN,
                anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=(0, 8))
            ctk.CTkButton(
                row_frame, text="Download", width=90, height=24, font=FONT_SMALL,
                fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
                command=lambda u=download_url, n=filename: self._download_handler(u, n),
            ).grid(row=0, column=1, padx=4, pady=2)

    def _on_fetch_error(self, err: str):
        self._status_var.set(f"Error loading list: {err}")
        self._log_fn(f"Download Custom Handler: {err}")

    def _download_handler(self, download_url: str, filename: str):
        """Download a handler JSON and save to custom_games dir."""
        def _do():
            import json as _json
            import urllib.request as _urllib
            try:
                req = _urllib.Request(download_url, headers={"User-Agent": "Amethyst-Mod-Manager"})
                with _urllib.urlopen(req, timeout=15) as resp:
                    data = resp.read().decode("utf-8", errors="replace")
                # Validate JSON
                _json.loads(data)
                dest = get_custom_games_dir() / filename
                dest.write_text(data, encoding="utf-8")
                self.after(0, lambda: self._on_download_done(filename, None))
            except Exception as e:
                self.after(0, lambda: self._on_download_done(filename, str(e)))

        self._status_var.set(f"Downloading {filename} …")
        threading.Thread(target=_do, daemon=True).start()

    def _on_download_done(self, filename: str, err: str | None):
        if err:
            self._status_var.set(f"Error: {err}")
            self._log_fn(f"Download Custom Handler: failed to download {filename}: {err}")
        else:
            self._status_var.set(f"Saved to custom_games: {filename}")
            self._log_fn(f"Download Custom Handler: saved {filename} to custom_games folder")
            self._on_downloaded()  # Refresh game picker so new handler appears

    def _on_close(self):
        self._on_done(self)


# ---------------------------------------------------------------------------
# SepColorPanel — inline overlay for separator color picker
# ---------------------------------------------------------------------------
class SepColorPanel(ctk.CTkFrame):
    """
    Inline panel that overlays _plugin_panel_container for picking a separator colour.
    Shows a HSV colour wheel, a brightness slider, a live hex entry,
    and a live colour-preview swatch.

    on_result(hex_color: str | None, reset: bool) is called when the user
    confirms, resets, or cancels.  hex_color is "#rrggbb" or None (cancel/reset).
    on_done(panel) is called afterwards so the host can hide the overlay.
    """

    _WHEEL_SIZE = 200
    _SLIDER_H   = 20

    def __init__(self, parent, sep_name: str, initial_color: str | None = None,
                 on_result=None, on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._sep_name  = sep_name
        self._on_result = on_result or (lambda hex_color, reset: None)
        self._on_done   = on_done   or (lambda p: None)

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

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build(self):
        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Separator Color \u2014 {self._sep_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel_color,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Inner content centred
        inner = ctk.CTkFrame(self, fg_color=BG_DEEP, corner_radius=0)
        inner.pack(fill="both", expand=True)
        inner.grid_columnconfigure(0, weight=1)

        PAD = 16
        ws  = self._WHEEL_SIZE

        # Colour wheel
        wheel_frame = tk.Frame(inner, bg=BG_DEEP)
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
            inner, width=ws, height=self._SLIDER_H,
            bg=BG_DEEP, highlightthickness=0, cursor="sb_h_double_arrow",
        )
        self._slider_canvas.grid(row=1, column=0, padx=PAD, pady=(0, 10), sticky="ew")
        self._slider_canvas.bind("<ButtonPress-1>", self._on_slider_press)
        self._slider_canvas.bind("<B1-Motion>",      self._on_slider_drag)
        self._slider_thumb = self._slider_canvas.create_rectangle(
            0, 0, 0, self._SLIDER_H, outline="white", width=2,
        )

        # Preview swatch
        self._swatch = tk.Frame(inner, height=28, bg=BG_DEEP, relief="flat", bd=0)
        self._swatch.grid(row=2, column=0, padx=PAD, pady=(0, 6), sticky="ew")

        # Hex entry row
        hex_row = tk.Frame(inner, bg=BG_DEEP)
        hex_row.grid(row=3, column=0, padx=PAD, pady=(0, 10), sticky="ew")
        hex_row.grid_columnconfigure(1, weight=1)
        tk.Label(
            hex_row, text="#", bg=BG_DEEP, fg=TEXT_SEP,
            font=font_sized_px(_theme.FONT_FAMILY, 13),
        ).grid(row=0, column=0, padx=(0, 2))
        self._hex_var = tk.StringVar()
        self._hex_entry = tk.Entry(
            hex_row, textvariable=self._hex_var,
            bg=BG_PANEL, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=font_sized_px(_theme.FONT_FAMILY, 13), bd=4, width=7,
        )
        self._hex_entry.grid(row=0, column=1, sticky="ew")
        self._hex_var.trace_add("write", self._on_hex_typed)

        # Button bar
        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        bar.pack(side="bottom", fill="x")
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")
        btn_inner = ctk.CTkFrame(bar, fg_color=BG_PANEL, corner_radius=0)
        btn_inner.pack(fill="x", padx=12, pady=10)
        ctk.CTkButton(
            btn_inner, text="Cancel", width=80, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel_color,
        ).pack(side="right", padx=(4, 0))
        ctk.CTkButton(
            btn_inner, text="OK", width=80, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_ok,
        ).pack(side="right", padx=4)
        ctk.CTkButton(
            btn_inner, text="Reset to default", width=120, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_reset,
        ).pack(side="left")

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
        self._on_result(self._current_hex(), False)
        self._on_done(self)

    def _on_reset(self):
        self._on_result(None, True)
        self._on_done(self)

    def _on_cancel_color(self):
        self._on_result(None, False)
        self._on_done(self)


# ---------------------------------------------------------------------------
# _ExeFilterDialog
# ---------------------------------------------------------------------------

class _ExeFilterDialog(ctk.CTkToplevel):
    """Thin modal wrapper around ExeFilterPanel."""

    def __init__(self, parent, load_fn, save_fn, refresh_fn, **_kwargs):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("EXE Filter List")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        panel = ExeFilterPanel(
            self,
            load_fn=load_fn, save_fn=save_fn, refresh_fn=refresh_fn,
            on_done=lambda p: self._on_close(),
        )
        panel.grid(row=0, column=0, sticky="nsew")

        _center_dialog(self, parent, 440, 475)

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


# ---------------------------------------------------------------------------
# ExeFilterPanel — inline overlay version of _ExeFilterDialog
# ---------------------------------------------------------------------------

class ExeFilterPanel(ctk.CTkFrame):
    """Inline panel version of _ExeFilterDialog. Overlays the plugin-panel container."""

    def __init__(self, parent, load_fn, save_fn, refresh_fn, on_done=None, **_kwargs):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._load_fn = load_fn
        self._save_fn = save_fn
        self._refresh_fn = refresh_fn
        self._on_done = on_done or (lambda p: None)
        self._items: list[str] = list(load_fn())

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text="EXE Filter List",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        self._build()
        self.bind("<Return>", lambda _: self._on_add())

    def _build(self):
        ctk.CTkLabel(
            self,
            text=(
                "User-hidden executables are listed here.\n"
                "Use the \u2699 Configure button on any EXE to hide or unhide it."
            ),
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(padx=14, pady=(10, 8), anchor="w")

        self._list_frame = ctk.CTkScrollableFrame(
            self, fg_color=BG_PANEL, corner_radius=6,
        )
        self._list_frame.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        self._list_frame.grid_columnconfigure(0, weight=1)
        self._refresh_list()

        add_row = ctk.CTkFrame(self, fg_color="transparent")
        add_row.pack(fill="x", padx=14, pady=(0, 10))
        add_row.grid_columnconfigure(0, weight=1)

        self._entry_var = tk.StringVar()
        self._entry_widget = ctk.CTkEntry(
            add_row, textvariable=self._entry_var, font=FONT_SMALL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER,
            placeholder_text="e.g.  my_helper.exe",
        )
        self._entry_widget.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(
            add_row, text="Add", width=72, height=28, font=FONT_SMALL,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_add,
        ).grid(row=0, column=1)

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Close", width=80, height=30, font=FONT_NORMAL,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_close,
        ).pack(side="right", padx=12, pady=10)

    def _refresh_list(self):
        for w in self._list_frame.winfo_children():
            w.destroy()

        user_items = sorted(self._items)

        if not user_items:
            ctk.CTkLabel(
                self._list_frame,
                text="(no user-hidden executables)",
                font=FONT_SMALL, text_color=TEXT_DIM,
            ).grid(row=0, column=0, pady=10)
            return

        for row_idx, name in enumerate(user_items):
            row = ctk.CTkFrame(self._list_frame, fg_color="transparent")
            row.grid(row=row_idx, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                row, text=name, font=FONT_SMALL, text_color=TEXT_MAIN, anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=8)
            ctk.CTkButton(
                row, text="\u2715", width=28, height=24, font=FONT_SMALL,
                fg_color=BG_HEADER, hover_color="#8B1A1A", text_color=TEXT_MAIN,
                command=lambda n=name: self._on_remove(n),
            ).grid(row=0, column=1, padx=(4, 4))

    def _on_add(self):
        raw = self._entry_var.get().strip()
        if not raw:
            return
        name = raw.lower()
        if name not in self._items:
            self._items.append(name)
            self._save_fn(self._items)
            self._refresh_fn()
        self._entry_var.set("")
        self._refresh_list()
        try:
            self._entry_widget.focus_set()
        except Exception:
            pass

    def _on_remove(self, name: str):
        if name in self._items:
            self._items.remove(name)
            self._save_fn(self._items)
            self._refresh_fn()
        self._refresh_list()

    def _on_close(self):
        self._on_done(self)


# ---------------------------------------------------------------------------
# DeploymentPathsPanel — inline overlay version of _show_mod_strip_dialog
# ---------------------------------------------------------------------------

class DeploymentPathsPanel(ctk.CTkFrame):
    """Inline panel (overlays _mod_panel_container) for setting deployment path
    strip prefixes. Calls on_save(chosen_paths) then on_done(panel) on OK,
    or just on_done(panel) on cancel/close."""

    def __init__(self, parent, mod_name: str, mod_folder: "Path",
                 current_prefixes: "list[str]", use_path_format: bool,
                 on_save=None, on_done=None):
        super().__init__(parent, fg_color=BG_PANEL, corner_radius=0)
        self._mod_name = mod_name
        self._mod_folder = mod_folder
        self._on_save = on_save or (lambda chosen: None)
        self._on_done = on_done or (lambda p: None)

        use_path_format = use_path_format
        current_set = {p.lower() for p in current_prefixes}
        self._vars_map: dict[str, tk.BooleanVar] = {}

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Deployment paths \u2014 {mod_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Message
        tk.Label(
            self,
            text="Select folders to ignore during deployment (at any depth).\n"
                 "Their contents will be deployed one level up:",
            bg=BG_PANEL, fg=TEXT_MAIN, font=FONT_SMALL, justify="left",
        ).pack(anchor="w", padx=12, pady=(10, 6))

        # Tree area
        _scrollbar_bg = "#383838"
        _tree_bg = BG_DEEP
        list_frame = tk.Frame(self, bg=_scrollbar_bg, bd=0, highlightthickness=0)
        list_frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        _uid = f"DPPanel{id(self)}"
        style = ttk.Style()
        style.configure(
            f"{_uid}.Treeview",
            background=_tree_bg, foreground=TEXT_MAIN,
            fieldbackground=_tree_bg, rowheight=scaled(22),
            font=("Cantarell", _theme.FS10),
            bordercolor=BG_ROW, borderwidth=1, focuscolor=_tree_bg,
        )
        style.configure(
            f"{_uid}.Treeview.Heading",
            background=BG_HEADER, foreground=TEXT_SEP,
            font=("Cantarell", _theme.FS10), borderwidth=0,
        )
        style.map(
            f"{_uid}.Treeview",
            background=[("selected", BG_SELECT), ("focus", _tree_bg)],
            foreground=[("selected", TEXT_MAIN)],
        )

        self._tree = ttk.Treeview(
            list_frame,
            columns=("check",),
            show="tree headings",
            style=f"{_uid}.Treeview",
            selectmode="browse",
        )
        self._tree.heading("#0", text="Folder", anchor="w")
        self._tree.heading("check", text="", anchor="w")
        self._tree.column("#0", minwidth=scaled(200), stretch=True)
        self._tree.column("check", width=scaled(28), stretch=False)

        vsb = tk.Scrollbar(
            list_frame, orient="vertical", command=self._tree.yview,
            bg=_scrollbar_bg, troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
        )
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        for widget in (self._tree, list_frame, self):
            widget.bind("<Button-4>", lambda e: self._tree.yview_scroll(-3, "units"))
            widget.bind("<Button-5>", lambda e: self._tree.yview_scroll( 3, "units"))

        def _iid(rel_path: str) -> str:
            return rel_path.replace("/", "\u241f")

        def _rel(iid: str) -> str:
            return iid.replace("\u241f", "/")

        def _scan(parent_path: str, parent_iid: str, depth: int) -> None:
            if depth > 3:
                return
            full = mod_folder / parent_path if parent_path else mod_folder
            try:
                entries = sorted(full.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return
            for p in entries:
                if not p.is_dir() or p.is_symlink():
                    continue
                rel = f"{parent_path}/{p.name}" if parent_path else p.name
                name = p.name
                if use_path_format:
                    var = tk.BooleanVar(value=rel.lower() in current_set)
                else:
                    var = tk.BooleanVar(value=name.lower() in current_set)
                self._vars_map[rel] = var
                check_char = "\u2611" if var.get() else "\u2610"
                iid = _iid(rel)
                self._tree.insert(parent_iid, "end", iid=iid, text=name,
                                  values=(check_char,), open=False)
                _scan(rel, iid, depth + 1)

        _scan("", "", 0)

        if not self._vars_map:
            self._tree.insert("", "end", iid="__none__",
                              text="(No folders found in this mod.)", values=("",))
            self._vars_map["__none__"] = tk.BooleanVar(value=False)

        self._iid_fn = _iid
        self._rel_fn = _rel

        def _on_toggle(evt):
            region = self._tree.identify_region(evt.x, evt.y)
            if region == "tree":
                return
            item = self._tree.identify_row(evt.y)
            if not item:
                return
            rel = _rel(item)
            if rel not in self._vars_map:
                return
            v = self._vars_map[rel]
            v.set(not v.get())
            self._tree.set(item, "check", "\u2611" if v.get() else "\u2610")

        self._tree.bind("<ButtonRelease-1>", _on_toggle)

        # Button bar
        btn_frame = tk.Frame(self, bg=BG_ROW, bd=0, highlightthickness=0)
        btn_frame.pack(fill="x", padx=12, pady=(0, 12))

        def _mkbtn(parent, text, cmd, bg):
            return tk.Button(
                parent, text=text, command=cmd, bg=bg, fg=TEXT_MAIN,
                font=FONT_SMALL, relief="flat", overrelief="flat",
                padx=16, pady=4, cursor="hand2",
                highlightthickness=0, borderwidth=0,
                activebackground=bg, activeforeground=TEXT_MAIN,
            )

        _mkbtn(btn_frame, "OK", self._on_ok, ACCENT).pack(side="right", padx=(8, 0))
        _mkbtn(btn_frame, "Cancel", self._on_cancel, BG_ROW).pack(side="right")
        _mkbtn(btn_frame, "Clear all", self._on_clear_all, BG_ROW).pack(side="right")

    def _on_ok(self):
        chosen = [
            rel for rel, v in self._vars_map.items()
            if rel != "__none__" and v.get()
        ]
        self._on_save(chosen)
        self._on_done(self)

    def _on_cancel(self):
        self._on_done(self)

    def _on_clear_all(self):
        for rel, v in self._vars_map.items():
            if rel == "__none__":
                continue
            v.set(False)
            try:
                self._tree.set(self._iid_fn(rel), "check", "\u2610")
            except tk.TclError:
                pass


# ---------------------------------------------------------------------------
# IniFileEditorPanel — inline overlay for editing ini/json files
# ---------------------------------------------------------------------------

class IniFileEditorPanel(ctk.CTkFrame):
    """Inline panel (overlays _mod_panel_container) for editing ini/json files.
    Shows a text editor with Save and Cancel. Calls on_done(panel) on close."""

    def __init__(self, parent, file_path: str, rel_path: str, mod_name: str,
                 on_done=None):
        super().__init__(parent, fg_color=BG_PANEL, corner_radius=0)
        self._file_path = Path(file_path)
        self._rel_path = rel_path
        self._mod_name = mod_name
        self._on_done = on_done or (lambda p: None)
        self._original_content: str | None = None

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar,
            text=f"{rel_path} \u2014 {mod_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Text editor
        self._textbox = ctk.CTkTextbox(
            self, font=FONT_MONO, fg_color=BG_DEEP, text_color=TEXT_MAIN,
            wrap="none", corner_radius=4, border_width=1, border_color=BORDER,
        )
        self._textbox.pack(fill="both", expand=True, padx=12, pady=12)

        # Button bar
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(
            btn_frame, text="Save", width=80, height=28,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_MAIN,
            font=FONT_SMALL, corner_radius=4,
            command=self._on_save,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btn_frame, text="Cancel", width=80, height=28,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            font=FONT_SMALL, corner_radius=4,
            command=self._on_cancel,
        ).pack(side="right")

        self._load_file()

    def _load_file(self):
        try:
            self._original_content = self._file_path.read_text(encoding="utf-8")
        except Exception:
            self._original_content = ""
        self._textbox.delete("0.0", "end")
        self._textbox.insert("0.0", self._original_content)

    def _on_save(self):
        try:
            content = self._textbox.get("0.0", "end-1c")
            self._file_path.write_text(content, encoding="utf-8")
            self._on_done(self)
        except OSError as e:
            tk.messagebox.showerror(
                "Save failed",
                f"Could not save {self._rel_path}:\n{e}",
                parent=self,
            )

    def _on_cancel(self):
        self._on_done(self)


# ---------------------------------------------------------------------------
# SepSettingsPanel — inline overlay for separator-level settings
# ---------------------------------------------------------------------------

class SepSettingsPanel(ctk.CTkFrame):
    """Inline panel that overlays _plugin_panel_container for per-separator settings."""

    def __init__(self, parent, sep_name: str, current_path: str,
                 current_raw: bool = False, on_save=None, on_done=None):
        super().__init__(parent, fg_color=BG_PANEL, corner_radius=0)
        self._sep_name = sep_name
        self._on_save = on_save or (lambda path, raw: None)
        self._on_done = on_done or (lambda p: None)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Separator Settings \u2014 {sep_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Content
        content = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        content.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            content, text="Deployment Location",
            font=FONT_SMALL, text_color=TEXT_SEP, anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        self._path_var = tk.StringVar(value=current_path)
        self._entry = ctk.CTkEntry(
            content, textvariable=self._path_var,
            font=FONT_SMALL, fg_color=BG_DEEP, text_color=TEXT_MAIN,
            border_color=BORDER, corner_radius=4,
        )
        self._entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            content, text="Browse", width=80, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._browse,
        ).grid(row=1, column=1, padx=(0, 4))
        ctk.CTkButton(
            content, text="Clear", width=60, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=lambda: self._path_var.set(""),
        ).grid(row=1, column=2)
        content.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            content,
            text="All mods within this separator are deployed to the chosen location\n"
                 "instead of the game\u2019s default directory. Leave blank to use the default.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # Divider
        ctk.CTkFrame(content, fg_color=BORDER, height=1, corner_radius=0).grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(16, 0))

        # Ignore deployment rules toggle
        self._raw_var = tk.BooleanVar(value=current_raw)
        ctk.CTkCheckBox(
            content, text="Ignore deployment rules",
            variable=self._raw_var,
            font=FONT_SMALL, text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            border_color=BORDER, checkmark_color=TEXT_MAIN,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ctk.CTkLabel(
            content,
            text="Mods in this separator bypass routing rules and are deployed\n"
                 "as-is to the target location.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # Save / Cancel buttons
        btn_row = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=12)

        ctk.CTkButton(
            btn_row, text="Save", width=90, font=FONT_SMALL,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_MAIN,
            command=self._on_save_click,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            btn_row, text="Cancel", width=90, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right")

        self._entry.focus_set()

    def _browse(self):
        from Utils.portal_filechooser import pick_folder
        def _cb(chosen):
            if chosen is not None:
                self._path_var.set(str(chosen))
        pick_folder("Select deployment directory", _cb)

    def _on_save_click(self):
        self._on_save(self._path_var.get().strip(), self._raw_var.get())
        self._on_done(self)

    def _on_cancel(self):
        self._on_done(self)


# ---------------------------------------------------------------------------
# MissingReqsPanel — inline overlay for missing Nexus requirements
# ---------------------------------------------------------------------------

class MissingReqsPanel(ctk.CTkFrame):
    """
    Inline panel version of the missing-requirements window.
    Overlays _plugin_panel_container (same as other overlay panels).
    """

    def __init__(self, parent, mod_name: str, domain: str, mod_id: int,
                 missing_ids: set, api,
                 install_from_browse,
                 ignored_set: set, save_ignored_fn,
                 on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._mod_name = mod_name
        self._domain = domain
        self._mod_id = mod_id
        self._missing_ids = missing_ids
        self._api = api
        self._install_from_browse = install_from_browse
        self._ignored_set = ignored_set
        self._save_ignored_fn = save_ignored_fn
        self._on_done = on_done or (lambda p: None)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=scaled(36))
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Missing requirements \u2014 {mod_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=scaled(12))
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._close,
        ).pack(side="right", padx=scaled(4))
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Status label
        self._status_var = tk.StringVar(value="Loading\u2026")
        self._status_lbl = ctk.CTkLabel(
            self, textvariable=self._status_var,
            font=FONT_SMALL, text_color=TEXT_DIM,
        )
        self._status_lbl.pack(pady=20)

        # Scrollable list area
        list_frame = tk.Frame(self, bg=BG_DEEP)
        list_frame.pack(fill="both", expand=True, padx=4, pady=4)
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)
        self._canvas = tk.Canvas(
            list_frame, bg=BG_DEEP, bd=0, highlightthickness=0,
            yscrollincrement=1, takefocus=0,
        )
        vsb = tk.Scrollbar(list_frame, orient="vertical", command=self._canvas.yview,
                           bg=BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
                           highlightthickness=0, bd=0)
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self._list_frame = list_frame

        def _on_wheel(e):
            if getattr(e, "delta", 0):
                self._canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
            return "break"
        self._canvas.bind("<MouseWheel>", _on_wheel)

        # Footer
        footer = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=scaled(44))
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        ctk.CTkFrame(footer, fg_color=BORDER, height=1, corner_radius=0).pack(side="top", fill="x")
        self._ignore_var = tk.BooleanVar(value=mod_name in ignored_set)
        ctk.CTkCheckBox(
            footer, text="Ignore requirements",
            variable=self._ignore_var,
            font=FONT_SMALL, text_color=TEXT_MAIN,
            checkbox_width=18, checkbox_height=18,
            command=self._on_ignore_toggle,
        ).pack(side="left", padx=scaled(12), pady=scaled(10))
        ctk.CTkButton(
            footer, text="Close", width=80, height=28,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            command=self._close,
        ).pack(side="right", padx=12, pady=8)

        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        err = None
        missing_list = []
        try:
            all_reqs = self._api.get_mod_requirements(self._domain, self._mod_id)
            for r in all_reqs:
                if r.mod_id in self._missing_ids:
                    missing_list.append(r)
        except Exception as e:
            err = f"Could not load requirements: {e}"
        self.after(0, lambda: self._fetch_done(missing_list, err))

    def _fetch_done(self, missing_list, err):
        if not self.winfo_exists():
            return
        if err:
            self._status_var.set(err)
            return
        if not missing_list:
            self._status_var.set("No missing requirements (list is empty).")
            return
        self._populate(missing_list)

    def _populate(self, missing_list):
        self._status_lbl.pack_forget()
        ROW_H = scaled(56)
        BTN_W = scaled(70)
        VIEW_W = scaled(56)
        BTN_H = scaled(28)
        NAME_PAD = scaled(10)
        canvas = self._canvas
        canvas_w = [600]

        def _on_resize(ev):
            canvas_w[0] = max(ev.width, 200)
            _repaint()

        self._list_frame.bind("<Configure>", _on_resize)
        row_bounds = []
        view_btns = []
        install_btns = []

        def _repaint():
            canvas.delete("all")
            row_bounds.clear()
            cw = canvas_w[0]
            btn_left = cw - 2 * BTN_W - scaled(16)
            name_max_px = max(btn_left - NAME_PAD - scaled(8), 20)
            y = 0
            for i, req in enumerate(missing_list):
                y_top = y
                notes = (req.notes or "").strip() or "No notes"
                title = req.mod_name + (" (External)" if req.is_external else "")
                desc_h = min(16 * 2, 32)
                row_h = max(ROW_H, scaled(24 + desc_h + 12))
                y_bot = y_top + row_h
                row_bounds.append((y_top, y_bot))
                bg = BG_ROW_ALT if i % 2 else BG_ROW
                canvas.create_rectangle(0, y_top, cw, y_bot, fill=bg, outline="")
                canvas.create_text(
                    NAME_PAD, y_top + scaled(12),
                    text=title[:80] + ("\u2026" if len(title) > 80 else ""),
                    anchor="w", font=(_theme.FONT_FAMILY, _theme.FS11), fill=TEXT_MAIN,
                )
                canvas.create_text(
                    NAME_PAD, y_top + scaled(30),
                    text=notes[:120] + ("\u2026" if len(notes) > 120 else ""),
                    anchor="nw", width=name_max_px,
                    font=(_theme.FONT_FAMILY, _theme.FS10), fill=TEXT_DIM,
                )
                y = y_bot
            total_h = max(y, 1)
            canvas.configure(scrollregion=(0, 0, cw, total_h))

            while len(view_btns) < len(missing_list):
                idx = len(view_btns)
                req = missing_list[idx]
                url = req.url or f"https://www.nexusmods.com/{self._domain or req.game_domain}/mods/{req.mod_id}"
                vb = ctk.CTkButton(
                    self, text="View", width=VIEW_W, height=BTN_H,
                    fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="#ffffff",
                    font=(_theme.FONT_FAMILY, _theme.FS10), cursor="hand2",
                    command=lambda u=url: open_url(u),
                )
                ib = ctk.CTkButton(
                    self, text="Install", width=BTN_W, height=BTN_H,
                    fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="#ffffff",
                    font=(_theme.FONT_FAMILY, _theme.FS10), cursor="hand2",
                    command=lambda r=req: self._on_install(r),
                )
                view_btns.append(vb)
                install_btns.append(ib)
            for idx in range(len(missing_list)):
                y_top, y_bot = row_bounds[idx]
                cy = y_top + (y_bot - y_top) // 2
                vx = cw - BTN_W - scaled(4) - BTN_W - scaled(4)
                ix = cw - BTN_W - scaled(4)
                canvas.create_window(vx, cy, window=view_btns[idx], width=VIEW_W, height=BTN_H, tags="btns")
                canvas.create_window(ix, cy, window=install_btns[idx], width=BTN_W, height=BTN_H, tags="btns")

        _repaint()

    def _on_install(self, req):
        if self._install_from_browse is not None:
            entry = SimpleNamespace(
                mod_id=req.mod_id,
                domain_name=self._domain or req.game_domain or "",
                name=req.mod_name or f"Mod {req.mod_id}",
            )
            self._install_from_browse(entry)
        else:
            url = req.url or f"https://www.nexusmods.com/{self._domain or req.game_domain or ''}/mods/{req.mod_id}"
            open_url(url)

    def _on_ignore_toggle(self):
        if self._ignore_var.get():
            self._ignored_set.add(self._mod_name)
        else:
            self._ignored_set.discard(self._mod_name)
        self._save_ignored_fn()

    def _close(self):
        self._on_ignore_toggle()
        self._on_done(self)


class CollectionInstallModeDialog(tk.Frame):
    """Overlay panel that asks how to install a collection.

    Placed over the mod list panel with place(relx=0, rely=0, relwidth=1, relheight=1).
    Calls on_done(result) when finished, where result is one of:
      ("new", None, False, False)                              — create a new profile
      ("append", profile_name, overwrite_existing, skip_existing)  — append into existing profile
      None                                                     — cancelled
    """

    def __init__(self, parent, existing_profiles: list[str], on_done, force_new_profile: bool = False):
        super().__init__(parent, bg=BG_DEEP)
        self._on_done = on_done
        self._existing_profiles = existing_profiles
        self._force_new_profile = force_new_profile

        self._mode_var = tk.StringVar(value="new")
        self._overwrite_var = tk.BooleanVar(value=False)
        self._skip_existing_var = tk.BooleanVar(value=False)
        self._profile_var = tk.StringVar(
            value=existing_profiles[0] if existing_profiles else ""
        )

        self._build()

    def _build(self):
        # Full-size container so we can centre the card
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Centred card
        card = tk.Frame(self, bg=BG_PANEL, bd=0, highlightthickness=1,
                        highlightbackground=BORDER)
        card.grid(row=0, column=0)
        card.grid_columnconfigure(0, weight=1)

        row = 0

        # Header bar
        header = tk.Frame(card, bg=BG_HEADER, height=42)
        header.grid(row=row, column=0, sticky="ew")
        header.grid_propagate(False)
        tk.Label(
            header, text="Install Collection",
            font=FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
        ).pack(side="left", padx=12, pady=8)
        row += 1

        # Separator
        tk.Frame(card, bg=BORDER, height=1).grid(row=row, column=0, sticky="ew")
        row += 1

        # Body
        body = tk.Frame(card, bg=BG_PANEL)
        body.grid(row=row, column=0, sticky="ew", padx=24, pady=(16, 8))
        body.grid_columnconfigure(0, weight=1)
        row += 1

        tk.Label(
            body, text="How would you like to install this collection?",
            font=FONT_BOLD, fg=TEXT_MAIN, bg=BG_PANEL, anchor="center", justify="center",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 14))

        ctk.CTkRadioButton(
            body, text="Create a new profile",
            variable=self._mode_var, value="new",
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
            command=self._on_mode_change,
        ).grid(row=1, column=0, sticky="w", pady=(0, 6))

        if self._force_new_profile:
            tk.Label(
                body,
                text="This collection requires a new profile and cannot be\nappended to an existing one.",
                font=FONT_SMALL,
                fg=TEXT_DIM, bg=BG_PANEL, anchor="w", justify="left",
            ).grid(row=2, column=0, sticky="w", pady=(0, 8))
        else:
            ctk.CTkRadioButton(
                body, text="Append to existing profile",
                variable=self._mode_var, value="append",
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
                command=self._on_mode_change,
            ).grid(row=2, column=0, sticky="w", pady=(0, 8))

        if not self._force_new_profile:
            self._profile_menu = ctk.CTkOptionMenu(
                body, values=self._existing_profiles or ["(no profiles)"],
                variable=self._profile_var,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=BG_DEEP, button_color=BG_HEADER, button_hover_color=BG_HOVER,
                dropdown_fg_color=BG_PANEL, dropdown_text_color=TEXT_MAIN,
                dropdown_hover_color=BG_HOVER,
                state="disabled", width=280,
            )
            self._profile_menu.grid(row=3, column=0, sticky="w", padx=(16, 0), pady=(0, 6))

            self._overwrite_cb = ctk.CTkCheckBox(
                body, text="Overwrite existing mods",
                variable=self._overwrite_var,
                font=FONT_NORMAL, text_color=TEXT_DIM,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
                checkmark_color="white",
                state="disabled",
            )
            self._overwrite_cb.grid(row=4, column=0, sticky="w", padx=(16, 0), pady=(0, 4))

            self._skip_existing_cb = ctk.CTkCheckBox(
                body, text="Skip already installed mods",
                variable=self._skip_existing_var,
                font=FONT_NORMAL, text_color=TEXT_DIM,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
                checkmark_color="white",
                state="disabled",
            )
            self._skip_existing_cb.grid(row=5, column=0, sticky="w", padx=(16, 0), pady=(0, 4))

        # Separator before buttons
        tk.Frame(card, bg=BORDER, height=1).grid(row=row, column=0, sticky="ew")
        row += 1

        # Button bar
        bar = tk.Frame(card, bg=BG_HEADER, height=44)
        bar.grid(row=row, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_DEEP, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Install", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=8)

    def _on_mode_change(self):
        is_append = self._mode_var.get() == "append"
        state = "normal" if is_append else "disabled"
        if hasattr(self, "_profile_menu"):
            self._profile_menu.configure(state=state)
        if hasattr(self, "_overwrite_cb"):
            self._overwrite_cb.configure(
                state=state,
                text_color=TEXT_MAIN if is_append else TEXT_DIM,
            )
        if hasattr(self, "_skip_existing_cb"):
            self._skip_existing_cb.configure(
                state=state,
                text_color=TEXT_MAIN if is_append else TEXT_DIM,
            )

    def _on_ok(self):
        mode = self._mode_var.get()
        if mode == "new":
            result = ("new", None, False, False)
        else:
            profile = self._profile_var.get()
            if not profile or profile == "(no profiles)":
                return
            result = ("append", profile, self._overwrite_var.get(), self._skip_existing_var.get())
        self._on_done(result)

    def _on_cancel(self):
        self._on_done(None)


class CollectionContinueInstallDialog(tk.Frame):
    """Overlay panel shown when a collection is already installed in a profile.

    Instead of offering new/append, shows a single 'Continue Install' action
    targeting the profile that already contains this collection's URL.
    """

    def __init__(self, parent, profile_name: str, on_done):
        super().__init__(parent, bg=BG_DEEP)
        self._on_done = on_done
        self._profile_name = profile_name
        self._build()

    def _build(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        card = tk.Frame(self, bg=BG_PANEL, bd=0, highlightthickness=1,
                        highlightbackground=BORDER)
        card.grid(row=0, column=0)
        card.grid_columnconfigure(0, weight=1)

        row = 0

        # Header bar
        header = tk.Frame(card, bg=BG_HEADER, height=42)
        header.grid(row=row, column=0, sticky="ew")
        header.grid_propagate(False)
        tk.Label(
            header, text="Continue Collection Install",
            font=FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
        ).pack(side="left", padx=12, pady=8)
        row += 1

        # Separator
        tk.Frame(card, bg=BORDER, height=1).grid(row=row, column=0, sticky="ew")
        row += 1

        # Body
        body = tk.Frame(card, bg=BG_PANEL)
        body.grid(row=row, column=0, sticky="ew", padx=24, pady=(16, 8))
        body.grid_columnconfigure(0, weight=1)
        row += 1

        tk.Label(
            body, text=f"This collection is already installed in profile\n'{self._profile_name}'",
            font=FONT_BOLD, fg=TEXT_MAIN, bg=BG_PANEL, anchor="center", justify="center",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 14))

        # Separator before buttons
        tk.Frame(card, bg=BORDER, height=1).grid(row=row, column=0, sticky="ew")
        row += 1

        # Button bar
        bar = tk.Frame(card, bg=BG_HEADER, height=44)
        bar.grid(row=row, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_DEEP, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Continue Install", width=120, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=8)

    def _on_ok(self):
        self._on_done(("continue", self._profile_name, False, False))

    def _on_cancel(self):
        self._on_done(None)


class _UserlistEntryDialog(ctk.CTkToplevel):
    """Dialog to configure a plugin's userlist.yaml entry (before/after/group)."""

    def __init__(self, parent, plugin_name: str, existing: dict):
        """
        existing: dict with optional keys 'before', 'after', 'group'
                  where before/after are lists of str and group is str.
        """
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Add to Userlist")
        self.geometry("460x300")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self._plugin_name = plugin_name
        self._existing = existing
        self.result: dict | None = None
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
            self, text=f"Userlist entry: {self._plugin_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 2))

        ctk.CTkLabel(
            self, text="Separate multiple plugin names with commas.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))

        def _row(parent, row, label, var):
            ctk.CTkLabel(
                parent, text=label, font=FONT_NORMAL,
                text_color=TEXT_DIM, anchor="w", width=60,
            ).grid(row=row, column=0, sticky="w", padx=(16, 4), pady=3)
            ctk.CTkEntry(
                parent, textvariable=var, font=FONT_NORMAL,
                fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER,
            ).grid(row=row, column=1, sticky="ew", padx=(0, 16), pady=3)

        fields = ctk.CTkFrame(self, fg_color="transparent")
        fields.grid(row=2, column=0, sticky="ew")
        fields.grid_columnconfigure(1, weight=1)

        before_list = self._existing.get("before", [])
        after_list  = self._existing.get("after",  [])
        group_val   = self._existing.get("group",  "")

        self._before_var = tk.StringVar(value=", ".join(before_list))
        self._after_var  = tk.StringVar(value=", ".join(after_list))
        self._group_var  = tk.StringVar(value=group_val)

        _row(fields, 0, "Before:", self._before_var)
        _row(fields, 1, "After:",  self._after_var)
        _row(fields, 2, "Group:",  self._group_var)

        # Button bar
        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(side="top", fill="x")
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Save", width=80, height=28, font=FONT_NORMAL,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=8)

    def _parse_list(self, var: tk.StringVar) -> list[str]:
        raw = var.get().strip()
        if not raw:
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]

    def _on_ok(self):
        entry: dict = {}
        before = self._parse_list(self._before_var)
        after  = self._parse_list(self._after_var)
        group  = self._group_var.get().strip() or "default"
        if before:
            entry["before"] = before
        if after:
            entry["after"] = after
        entry["group"] = group
        self.result = entry
        self.destroy()

    def _on_cancel(self):
        self.destroy()
