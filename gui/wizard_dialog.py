"""
Wizard tool-selection dialog.

Shows a list of game-specific helper tools declared via
``BaseGame.wizard_tools``.  Clicking a tool opens its dedicated wizard dialog.
"""

from __future__ import annotations

import importlib
import tkinter as tk
from typing import TYPE_CHECKING

import customtkinter as ctk

if TYPE_CHECKING:
    from Games.base_game import BaseGame, WizardTool

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


def _resolve_dialog_class(dotted_path: str) -> type:
    """Import and return the class referenced by *dotted_path*.

    Example: ``"gui.wizard_fallout_downgrade.FalloutDowngradeWizard"``
    """
    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


class WizardDialog(ctk.CTkToplevel):
    """Modal dialog listing the available wizard tools for a game."""

    def __init__(self, parent, game: "BaseGame", log_fn=None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"Wizard — {game.name}")
        self.geometry("440x320")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._make_modal)

        self._game = game
        self._log = log_fn or (lambda msg: None)
        self._parent = parent
        self._build()

    # ------------------------------------------------------------------
    # Modal helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build(self):
        body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        body.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            body,
            text=f"Wizard — {self._game.name}",
            font=FONT_BOLD,
            text_color=TEXT_MAIN,
        ).pack(pady=(0, 4))

        ctk.CTkLabel(
            body,
            text="Select a helper tool:",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
        ).pack(pady=(0, 12))

        tools = self._game.wizard_tools
        if not tools:
            ctk.CTkLabel(
                body,
                text="No tools available for this game.",
                font=FONT_NORMAL,
                text_color=TEXT_DIM,
            ).pack(pady=20)
            return

        for tool in tools:
            self._add_tool_row(body, tool)

    def _add_tool_row(self, parent, tool: "WizardTool"):
        """Render a clickable row for a single wizard tool."""
        row = ctk.CTkFrame(parent, fg_color=BG_PANEL, corner_radius=6)
        row.pack(fill="x", pady=(0, 8))

        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=10)

        # Label + description on the left
        text_frame = ctk.CTkFrame(inner, fg_color="transparent")
        text_frame.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            text_frame, text=tool.label,
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(anchor="w")

        if tool.description:
            ctk.CTkLabel(
                text_frame, text=tool.description,
                font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
                wraplength=280,
            ).pack(anchor="w")

        # Open button on the right
        ctk.CTkButton(
            inner, text="Open", width=70, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=lambda t=tool: self._open_tool(t),
        ).pack(side="right", padx=(8, 0))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _open_tool(self, tool: "WizardTool"):
        """Close this picker and open the tool's dedicated wizard dialog."""
        game = self._game
        log = self._log
        parent = self._parent
        path = tool.dialog_class_path

        # Close the picker first
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

        # Resolve and open the tool dialog on the next event-loop tick
        def _launch():
            try:
                cls = _resolve_dialog_class(path)
                dlg = cls(parent, game, log)
                parent.wait_window(dlg)
            except Exception as exc:
                log(f"Wizard error: {exc}")

        parent.after(50, _launch)
