"""
wine_dll_overrides_panel.py
Overlay panel for managing Wine DLL overrides for a game's Proton prefix.

Overrides are stored at:
  ~/.config/AmethystModManager/games/<game_name>/wine_dll_overrides.json

All overrides are set to native,builtin. When the panel opens it merges in
any overrides declared in the game handler's wine_dll_overrides property so
the user can see and optionally remove them.

Clicking "Save & Apply" writes the list to the config file and applies the
overrides to the game's Proton prefix via apply_wine_dll_overrides.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk

from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_PANEL,
    BG_ROW,
    BG_ROW_ALT,
    BG_SEP,
    BORDER,
    FONT_BOLD,
    FONT_NORMAL,
    FONT_SMALL,
    RED_BTN,
    RED_HOV,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_OK,
)
from Utils.wine_dll_config import (
    load_wine_dll_overrides,
    save_wine_dll_overrides,
)

_MODE = "native,builtin"


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class WineDllOverridesPanel(ctk.CTkFrame):
    """Overlay panel for viewing and editing Wine DLL overrides.

    Opened from ProtonToolsPanel. Placed over the plugin panel container.
    """

    def __init__(
        self,
        parent: tk.Widget,
        game,
        log_fn: Callable[[str], None],
        on_done: Optional[Callable] = None,
    ) -> None:
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._game = game
        self._log = log_fn
        self._on_done = on_done or (lambda p: None)

        # Load stored overrides and merge in handler-defined ones
        handler_overrides: dict[str, str] = {}
        try:
            raw_ho = getattr(game, "wine_dll_overrides", {}) or {}
            handler_overrides = dict(raw_ho)
        except Exception:
            pass
        stored = load_wine_dll_overrides(game.name)
        self._overrides: dict[str, str] = {**handler_overrides, **stored}
        # Snapshot of the active DLLs at open time — used to compute removals on save
        self._initial_dlls: set[str] = set(self._overrides.keys())

        self._row_widgets: list[dict] = []  # list of {dll, frame, lbl, btn}
        self._build()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self.grid_rowconfigure(0, weight=0)  # title bar
        self.grid_rowconfigure(1, weight=1)  # scrollable list
        self.grid_rowconfigure(2, weight=0)  # add row
        self.grid_rowconfigure(3, weight=0)  # save button
        self.grid_columnconfigure(0, weight=1)

        # ---- Title bar ---------------------------------------------------
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_propagate(False)
        title_bar.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(
            title_bar, text="← Back", width=70, height=28,
            font=FONT_BOLD, fg_color="transparent", hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_close,
        ).grid(row=0, column=0, padx=(6, 4), pady=6, sticky="w")

        ctk.CTkLabel(
            title_bar,
            text=f"Wine DLL Overrides — {self._game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).grid(row=0, column=1, padx=4, pady=8, sticky="w")

        ctk.CTkButton(
            title_bar, text="✕", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).grid(row=0, column=2, padx=4, pady=4, sticky="e")

        # ---- Column header -----------------------------------------------
        hdr = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=28)
        hdr.grid(row=0, column=0, sticky="ew", pady=(40, 0))
        # We place this after title_bar using place to overlay correctly; instead
        # use a separate row.

        # ---- Scrollable override list ------------------------------------
        self._list_frame = ctk.CTkScrollableFrame(
            self, fg_color=BG_DEEP, corner_radius=0,
        )
        self._list_frame.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self._list_frame.grid_columnconfigure(0, weight=1)

        # Build the column header inside the list frame header area
        col_hdr = ctk.CTkFrame(self._list_frame, fg_color=BG_PANEL, corner_radius=0, height=26)
        col_hdr.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        col_hdr.grid_columnconfigure(0, weight=1)
        col_hdr.grid_columnconfigure(1, weight=0)
        col_hdr.grid_columnconfigure(2, weight=0)
        ctk.CTkLabel(col_hdr, text="DLL Name", font=FONT_SMALL, text_color=TEXT_DIM,
                     anchor="w").grid(row=0, column=0, padx=(12, 4), pady=4, sticky="w")
        ctk.CTkLabel(col_hdr, text="Mode", font=FONT_SMALL, text_color=TEXT_DIM,
                     anchor="w", width=140).grid(row=0, column=1, padx=4, pady=4, sticky="w")
        ctk.CTkLabel(col_hdr, text="", width=36).grid(row=0, column=2, padx=4)

        self._rows_container = ctk.CTkFrame(self._list_frame, fg_color="transparent", corner_radius=0)
        self._rows_container.grid(row=1, column=0, sticky="nsew")
        self._rows_container.grid_columnconfigure(0, weight=1)

        self._populate_list()

        # ---- Add row -----------------------------------------------------
        add_frame = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=46)
        add_frame.grid(row=2, column=0, sticky="ew", padx=0, pady=(2, 0))
        add_frame.grid_propagate(False)
        add_frame.grid_columnconfigure(0, weight=1)
        add_frame.grid_columnconfigure(1, weight=0)

        self._dll_entry = ctk.CTkEntry(
            add_frame, placeholder_text="DLL name (e.g. winhttp)",
            font=FONT_NORMAL, fg_color=BG_DEEP, border_color=BORDER,
            text_color=TEXT_MAIN,
        )
        self._dll_entry.grid(row=0, column=0, padx=(10, 6), pady=8, sticky="ew")
        self._dll_entry.bind("<Return>", lambda _: self._on_add())

        ctk.CTkButton(
            add_frame, text="+ Add", width=90, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_add,
        ).grid(row=0, column=1, padx=(0, 10), pady=8)

        # ---- Save & Apply button -----------------------------------------
        save_frame = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=50)
        save_frame.grid(row=3, column=0, sticky="ew", pady=(2, 0))
        save_frame.grid_propagate(False)
        save_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(
            save_frame, text="Save & Apply", width=200, height=34, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_save,
        ).grid(row=0, column=0, padx=16, pady=8, sticky="e")

    # ------------------------------------------------------------------
    # List rendering
    # ------------------------------------------------------------------

    def _populate_list(self) -> None:
        """Clear and repopulate the override rows."""
        for widget in self._rows_container.winfo_children():
            widget.destroy()
        self._row_widgets.clear()

        if not self._overrides:
            ctk.CTkLabel(
                self._rows_container,
                text="No DLL overrides configured.",
                font=FONT_NORMAL, text_color=TEXT_DIM, anchor="center",
            ).grid(row=0, column=0, pady=20, sticky="ew")
            return

        for idx, (dll, mode) in enumerate(sorted(self._overrides.items())):
            bg = BG_ROW if idx % 2 == 0 else BG_ROW_ALT
            row = ctk.CTkFrame(self._rows_container, fg_color=bg, corner_radius=4, height=34)
            row.grid(row=idx, column=0, sticky="ew", padx=4, pady=1)
            row.grid_propagate(False)
            row.grid_columnconfigure(0, weight=1)
            row.grid_columnconfigure(1, weight=0)
            row.grid_columnconfigure(2, weight=0)

            ctk.CTkLabel(row, text=dll, font=FONT_NORMAL, text_color=TEXT_MAIN,
                         anchor="w").grid(row=0, column=0, padx=(12, 4), pady=6, sticky="w")
            ctk.CTkLabel(row, text=mode, font=FONT_SMALL, text_color=TEXT_DIM,
                         anchor="w", width=140).grid(row=0, column=1, padx=4, pady=6, sticky="w")

            dll_captured = dll
            ctk.CTkButton(
                row, text="×", width=28, height=24, font=FONT_BOLD,
                fg_color=RED_BTN, hover_color=RED_HOV, text_color="white",
                command=lambda d=dll_captured: self._on_remove(d),
            ).grid(row=0, column=2, padx=(4, 8), pady=5)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_add(self) -> None:
        import re
        raw = self._dll_entry.get().strip().lower()
        if not raw:
            return
        if not re.fullmatch(r'[a-z0-9_.-]+', raw):
            self._log("Wine DLL Overrides: invalid DLL name — only letters, digits, underscores, dots and hyphens are allowed.")
            return
        if raw in self._overrides:
            self._log(f"Wine DLL Overrides: '{raw}' is already in the list.")
            return
        self._overrides[raw] = _MODE
        self._dll_entry.delete(0, "end")
        self._populate_list()

    def _on_remove(self, dll: str) -> None:
        self._overrides.pop(dll, None)
        self._populate_list()

    def _on_save(self) -> None:
        game_name = self._game.name

        # Compute DLLs that were active at open time but are no longer kept
        removed_dlls = self._initial_dlls - set(self._overrides.keys())

        save_wine_dll_overrides(game_name, self._overrides)
        self._log(f"Wine DLL Overrides: saved {len(self._overrides)} override(s) for {game_name}.")

        prefix_path = None
        try:
            prefix_path = self._game.get_prefix_path()
        except Exception:
            pass

        if prefix_path is None or not prefix_path.is_dir():
            self._log("Wine DLL Overrides: no Proton prefix configured — overrides saved but not applied.")
            return

        from Utils.deploy import apply_wine_dll_overrides, remove_wine_dll_overrides

        if removed_dlls:
            self._log(f"Wine DLL Overrides: removing {len(removed_dlls)} override(s) from prefix ...")
            remove_wine_dll_overrides(prefix_path, removed_dlls, log_fn=self._log)

        if self._overrides:
            apply_wine_dll_overrides(prefix_path, self._overrides, log_fn=self._log)
            self._log("Wine DLL Overrides: applied to Proton prefix.")
        elif not removed_dlls:
            self._log("Wine DLL Overrides: no overrides to apply.")

    def _on_close(self) -> None:
        self._on_done(self)
