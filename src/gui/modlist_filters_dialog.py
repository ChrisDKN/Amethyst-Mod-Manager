"""
modlist_filters_dialog.py
Dialog for selecting filter options for the modlist panel.

Allows multi-select of:
  - Show only disabled mods
  - Show only enabled mods
  - Hide separators
  - Show only winning conflicts
  - Show only losing conflicts
  - Show only winning and losing conflicts
  - Show only fully conflicted (all files overridden)
"""

from __future__ import annotations

from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk

# ---------------------------------------------------------------------------
# Colors / fonts (kept in sync with gui.py)
# ---------------------------------------------------------------------------
BG_DEEP    = "#1a1a1a"
BG_PANEL   = "#252526"
BG_HEADER  = "#2a2a2b"
TEXT_MAIN  = "#d4d4d4"
TEXT_DIM   = "#858585"
BORDER     = "#444444"

FONT_NORMAL = ("Segoe UI", 12)
FONT_BOLD   = ("Segoe UI", 12, "bold")
FONT_SMALL  = ("Segoe UI", 10)


def _default_state():
    return {
        "filter_show_disabled": False,
        "filter_show_enabled": False,
        "filter_hide_separators": False,
        "filter_winning": False,
        "filter_losing": False,
        "filter_partial": False,
        "filter_full": False,
        "filter_missing_reqs": False,
    }


class ModlistFiltersDialog(ctk.CTkToplevel):
    """
    Non-modal dialog for modlist filter options.
    Calls on_apply(state) whenever any checkbox changes.
    """

    WIDTH  = 380
    HEIGHT = 380

    def __init__(
        self,
        parent: tk.Widget,
        initial_state: Optional[dict] = None,
        on_apply: Optional[Callable[[dict], None]] = None,
    ):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Modlist Filters")
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self._on_apply = on_apply or (lambda _: None)
        state = initial_state if initial_state is not None else _default_state()
        self._state = {k: state.get(k, v) for k, v in _default_state().items()}

        self._build()
        self._fire_apply()

    def _build(self):
        pad = {"padx": 16, "pady": (8, 0)}

        ctk.CTkLabel(
            self, text="Modlist Filters",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(**pad, anchor="w")

        ctk.CTkLabel(
            self,
            text="Filter which rows appear in the modlist. Multiple options can be active.",
            font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(padx=16, pady=(2, 12), anchor="w")

        ctk.CTkFrame(self, fg_color=BORDER, height=1).pack(fill="x", padx=16, pady=2)

        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(padx=16, pady=8, fill="both", expand=True, anchor="w")

        opts = [
            ("filter_show_disabled", "Show only disabled mods"),
            ("filter_show_enabled", "Show only enabled mods"),
            ("filter_hide_separators", "Hide separators"),
            ("filter_winning", "Show only winning conflicts"),
            ("filter_losing", "Show only losing conflicts"),
            ("filter_partial", "Show only winning and losing conflicts"),
            ("filter_full", "Show only fully conflicted (all files overridden)"),
            ("filter_missing_reqs", "Show only mods with missing requirements"),
        ]

        self._vars = {}
        for key, label in opts:
            var = tk.BooleanVar(value=self._state[key])
            self._vars[key] = var
            ctk.CTkCheckBox(
                frame,
                text=label,
                variable=var,
                font=FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=BG_HEADER,
                hover_color="#094771",
                command=lambda k=key: self._sync_and_apply(k),
            ).pack(anchor="w", pady=4)

    def _sync_and_apply(self, key: str):
        if key in self._vars:
            self._state[key] = self._vars[key].get()
        self._fire_apply()

    def _fire_apply(self):
        self._on_apply(dict(self._state))
