"""
Modal dialogs used by ModListPanel, PluginPanel, TopBar, and install_mod.
Uses theme, path_utils; does not import panels or App to avoid circular imports.
"""

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
)
from gui.path_utils import _to_wine_path
from Utils.config_paths import get_exe_args_path


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
# Game picker dialog
# ---------------------------------------------------------------------------
class _GamePickerDialog(ctk.CTkToplevel):
    _ROW_H   = 36
    _MIN_H   = 200
    _MAX_H   = 520
    _WIDTH   = 340

    def __init__(self, parent, game_names: list[str]):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Add / Reconfigure Game")
        self.resizable(False, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.result: str | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self, text="Select a game to configure:",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 6))

        scroll = ctk.CTkScrollableFrame(self, fg_color=BG_PANEL, corner_radius=6)
        scroll.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        scroll.grid_columnconfigure(0, weight=1)

        self._var = tk.StringVar(value=game_names[0])

        def _fwd_scroll(event):
            scroll._parent_canvas.yview_scroll(
                -1 if event.num == 4 else 1, "units"
            )

        for i, name in enumerate(game_names):
            rb = ctk.CTkRadioButton(
                scroll, text=name, variable=self._var, value=name,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
            )
            rb.grid(row=i, column=0, sticky="w", padx=12, pady=4)

        self.bind_all("<Button-4>", _fwd_scroll)
        self.bind_all("<Button-5>", _fwd_scroll)
        self.bind("<Destroy>", lambda e: (
            self.unbind_all("<Button-4>"),
            self.unbind_all("<Button-5>"),
        ) if e.widget is self else None)

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
        ctk.CTkButton(
            btn_bar, text="Select", width=90, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._ok
        ).pack(side="right", padx=4, pady=10)

        ideal_list_h = len(game_names) * self._ROW_H + 16
        h = max(self._MIN_H, min(self._MAX_H, ideal_list_h + 120))
        owner = parent
        x = owner.winfo_rootx() + (owner.winfo_width()  - self._WIDTH) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - h) // 2
        self.geometry(f"{self._WIDTH}x{h}+{x}+{y}")

        self.after(50, self._make_modal)

    def _make_modal(self):
        self.grab_set()
        self.focus_set()

    def _ok(self):
        self.result = self._var.get()
        self.grab_release()
        self.destroy()

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
            tk.messagebox.showerror(
                "Invalid Value",
                "Please enter a whole number.",
                parent=self,
            )
            return
        if value < 0:
            tk.messagebox.showerror(
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
        if proton_script is None:
            proton_script = find_any_installed_proton()
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

        compat_data = prefix_path.parent
        steam_root = find_steam_root_for_proton_script(proton_script)
        if steam_root is None:
            self._log("Proton Tools: could not determine Steam root for the selected Proton tool.")
            return None, None

        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)

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

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tv.yview)
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


class _ExeConfigDialog(ctk.CTkToplevel):
    """Modal dialog for configuring command-line arguments for a Windows exe."""

    _EXE_ARGS_FILE = get_exe_args_path()

    def __init__(self, parent, exe_path: "Path", game, saved_args: str = ""):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"Configure: {exe_path.name}")
        self.geometry("640x560")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._exe_path = exe_path
        self._game = game
        self._saved_args = saved_args
        self.result: "str | None" = None

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
        self._search_var = tk.StringVar(value="")
        self._mod_entries: list[tuple[str, "Path"]] = self._load_mod_entries()
        self._filtered_entries: list[tuple[str, "Path"]] = list(self._mod_entries)
        self._radio_buttons: list[ctk.CTkRadioButton] = []

        self._build()
        self._load_saved()

        self._game_flag_var.trace_add("write", self._assemble)
        self._output_flag_var.trace_add("write", self._assemble)
        self._mod_var.trace_add("write", self._assemble)
        self._search_var.trace_add("write", self._on_search_changed)

        self.after(80, self._make_modal)

    def _load_mod_entries(self) -> "list[tuple[str, Path]]":
        entries: list[tuple[str, Path]] = []
        if self._overwrite_path and self._overwrite_path.is_dir():
            entries.append(("overwrite", self._overwrite_path))
        if self._mods_path and self._mods_path.is_dir():
            for e in sorted(self._mods_path.iterdir(), key=lambda p: p.name.casefold()):
                if e.is_dir():
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
        self.grid_rowconfigure(2, weight=1)

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
        ).grid(row=2, column=0, sticky="w", padx=(10, 4), pady=(0, 4))
        ctk.CTkEntry(
            sec2, textvariable=self._search_var, font=FONT_SMALL,
            fg_color=BG_HEADER, text_color=TEXT_MAIN, border_color=BORDER,
            placeholder_text="filter mods...",
        ).grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=(0, 4))

        self._mod_scroll = ctk.CTkScrollableFrame(
            self, fg_color=BG_PANEL, corner_radius=6,
        )
        self._mod_scroll.grid(row=2, column=0, sticky="nsew", padx=12, pady=4)
        self._mod_scroll.grid_columnconfigure(0, weight=1)
        self._rebuild_mod_list()

        sec3 = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=6)
        sec3.grid(row=3, column=0, sticky="ew", padx=12, pady=4)
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

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=48)
        bar.grid(row=4, column=0, sticky="ew")
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

    def _rebuild_mod_list(self):
        for rb in self._radio_buttons:
            rb.destroy()
        self._radio_buttons.clear()

        for display, path in self._filtered_entries:
            rb = ctk.CTkRadioButton(
                self._mod_scroll, text=display,
                variable=self._mod_var, value=display,
                font=FONT_SMALL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
            )
            rb.grid(sticky="w", padx=6, pady=1)
            self._radio_buttons.append(rb)

    def _on_search_changed(self, *_):
        query = self._search_var.get().casefold()
        if query:
            self._filtered_entries = [
                (n, p) for n, p in self._mod_entries if query in n.casefold()
            ]
        else:
            self._filtered_entries = list(self._mod_entries)
        self._rebuild_mod_list()

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
