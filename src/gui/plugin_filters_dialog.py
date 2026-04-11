# This file is no longer used.
# Plugin filter functionality is now an inline side panel in plugin_panel.py.


from __future__ import annotations

from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk

from gui.theme import (
    BG_DEEP,
    BG_PANEL,
    BG_HEADER,
    TEXT_MAIN,
    TEXT_DIM,
    BORDER,
    FONT_NORMAL,
    FONT_BOLD,
    FONT_SMALL,
)


def _default_state():
    return {
        "filter_enabled": False,
        "filter_disabled": False,
        "filter_missing_masters": False,
        "filter_esl_ext": False,
        "filter_esm_ext": False,
        "filter_esp_ext": False,
        "filter_esl_flagged": False,
        "filter_esl_safe": False,
        "filter_esl_unsafe": False,
        "filter_userlist": False,
    }


class PluginFiltersDialog(ctk.CTkToplevel):
    """
    Non-modal dialog for plugin filter options.
    Calls on_apply(state) whenever any checkbox changes.
    """

    WIDTH  = 420
    HEIGHT = 440

    def __init__(
        self,
        parent,
        initial_state: Optional[dict] = None,
        on_apply: Optional[Callable[[dict], None]] = None,
    ):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Plugin Filters")
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.resizable(True, False)
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
            self, text="Plugin Filters",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(**pad, anchor="w")

        ctk.CTkLabel(
            self,
            text="Filter which plugins appear in the list. Multiple options can be active.",
            font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(padx=16, pady=(2, 12), anchor="w")

        ctk.CTkFrame(self, fg_color=BORDER, height=1).pack(fill="x", padx=16, pady=2)

        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(padx=16, pady=8, fill="both", expand=True, anchor="w")

        opts = [
            ("filter_enabled",         "Show only enabled plugins"),
            ("filter_disabled",        "Show only disabled plugins"),
            ("filter_missing_masters", "Show only plugins with missing masters"),
            ("filter_esl_ext",         "Show only ESL plugins (.esl extension)"),
            ("filter_esm_ext",         "Show only ESM plugins (.esm extension)"),
            ("filter_esp_ext",         "Show only ESP plugins (.esp extension)"),
            ("filter_esl_flagged",     "Show only ESL-flagged (light) plugins"),
            ("filter_esl_safe",        "Show only ESL-safe plugins (eligible for ESL flag)"),
            ("filter_esl_unsafe",      "Show only ESL-unsafe plugins (too many records for ESL)"),
            ("filter_userlist",        "Show only plugins managed by userlist.yaml"),
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
            ).pack(anchor="w", fill="x", pady=4)

    def _sync_and_apply(self, key: str):
        if key in self._vars:
            self._state[key] = self._vars[key].get()
        self._fire_apply()

    def _fire_apply(self):
        self._on_apply(dict(self._state))
