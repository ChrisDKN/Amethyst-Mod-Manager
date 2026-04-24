"""
custom_game_dialog.py
Dialog for creating or editing a user-defined custom game.

The user fills in the game's basic properties (name, exe, deploy type, etc.)
and clicks Save.  The definition is written to
~/.config/AmethystModManager/custom_games/<game_id>.json and the game is
immediately available to the rest of the application via game_loader.

Deploy types
------------
Standard — mods install into a single sub-folder inside the game root
           (same as Bethesda games / BepInEx).  The user can specify which
           sub-folder (e.g. "Data", "BepInEx/plugins").  Leave empty to
           target the game root itself.

Root     — mods are deployed directly to the game's root folder
           (same as The Witcher 3, Cyberpunk 2077).

UE5      — uses the Unreal Engine 5 manifest deploy; everything lands in
           the game root and is tracked via a deployed.txt manifest
           (same as Oblivion Remastered, Hogwarts Legacy).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk
import tkinter as tk

from Games.Custom.custom_game import (
    _make_game_id,
    delete_custom_game_definition,
    make_custom_game,
    save_custom_game_definition,
)
from Utils.config_paths import get_custom_game_images_dir
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    TEXT_ON_ACCENT,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_PANEL,
    BG_ROW,
    BORDER,
    FONT_BOLD,
    FONT_MONO,
    FONT_NORMAL,
    FONT_SMALL,
    RED_BTN,
    RED_HOV,
    TEXT_DIM,
    TEXT_ERR,
    TEXT_MAIN,
    TEXT_OK,
    TEXT_SEP,
    TEXT_WARN,
)

_DEPLOY_OPTIONS = [
    (
        "Standard",
        "standard",
        "Mods install into a single sub-folder (e.g. Data/, BepInEx/plugins/). "
        "Same as Bethesda games and BepInEx.",
    ),
    (
        "Root",
        "root",
        "Mods deploy directly to the game's root folder. "
        "Same as The Witcher 3 and Cyberpunk 2077.",
    ),
    (
        "UE5",
        "ue5",
        "Unreal Engine 5 — pak files → Content/Paks/~mods/, UE4SS/lua → Binaries/Win64/, "
        "DLLs → Binaries/Win64/. Same routing as Hogwarts Legacy / Oblivion Remastered.",
    ),
]


# ---------------------------------------------------------------------------
# Value conversion helpers (dialog ↔ JSON)
# ---------------------------------------------------------------------------

def _set_to_str(value) -> str:
    """Convert a list/set from JSON to a comma-separated display string."""
    if isinstance(value, (list, set)):
        return ", ".join(str(v) for v in value)
    return str(value) if value else ""


def _list_to_str(value) -> str:
    """Same as _set_to_str — both are just comma-joined."""
    return _set_to_str(value)


def _dll_to_str(value) -> str:
    """Convert wine_dll_overrides dict to one-per-line 'dll=mode' string."""
    if isinstance(value, dict):
        return "\n".join(f"{k}={v}" for k, v in value.items())
    return str(value) if value else ""


def _str_to_list(text: str) -> list[str]:
    """Convert a comma-separated string to a cleaned list (preserves case)."""
    return [s.strip() for s in text.split(",") if s.strip()]


def _parse_dll_text(text: str) -> dict[str, str]:
    """Parse one-per-line 'dll=mode' text into a dict."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" in line:
            k, _, v = line.partition("=")
            if k.strip():
                result[k.strip()] = v.strip()
    return result


# ---------------------------------------------------------------------------
# CustomGameDialog — thin modal wrapper around CustomGamePanel
# ---------------------------------------------------------------------------

class CustomGameDialog(ctk.CTkToplevel):
    """
    Modal dialog for creating or editing a custom game definition.

    Pass an existing definition dict to ``existing`` to open in edit mode.
    After the dialog closes, check ``dialog.saved_game`` for the new/updated
    ``BaseGame`` instance (None if cancelled or deleted).
    ``dialog.deleted`` is True when the user removed the definition.

    Usage::

        dlg = CustomGameDialog(parent)
        parent.wait_window(dlg)
        if dlg.saved_game:
            ...
    """

    WIDTH  = 600
    HEIGHT = 800

    def __init__(self, parent, existing: dict | None = None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Define Custom Game" if existing is None else "Edit Custom Game")
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.resizable(False, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_panel_done)

        self._panel = CustomGamePanel(self, existing=existing, on_done=self._on_panel_done)
        self._panel.pack(fill="both", expand=True)

        self.after(100, self._make_modal)

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_panel_done(self, panel=None):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    # Delegate result attributes to the embedded panel
    @property
    def saved_game(self):
        return self._panel.saved_game

    @property
    def deleted(self):
        return self._panel.deleted

    @property
    def result(self):
        return self._panel.saved_game


# ---------------------------------------------------------------------------
# CustomGamePanel — canonical implementation (inline overlay)
# ---------------------------------------------------------------------------

class CustomGamePanel(ctk.CTkFrame):
    """
    Inline panel overlay for creating or editing a custom game definition.

    Placed over the main content area with ``place(relx=0, rely=0,
    relwidth=1, relheight=1)``.  Calls ``on_done(panel)`` when finished;
    inspect ``panel.saved_game`` and ``panel.deleted`` for result.

    Usage (App)::

        panel = CustomGamePanel(parent_frame, existing=defn, on_done=on_panel_done)
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
    """

    CONTENT_WIDTH = 620

    def __init__(self, parent, existing: dict | None = None, on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)

        self._existing = existing
        self._on_done  = on_done or (lambda p: None)

        self.saved_game = None
        self.deleted    = False

        # ---- tk variables ----
        self._name_var        = tk.StringVar()
        self._exe_var         = tk.StringVar()
        self._deploy_var      = tk.StringVar(value="standard")
        self._data_path_var   = tk.StringVar()
        self._steam_var       = tk.StringVar()
        self._nexus_var       = tk.StringVar()
        self._image_url_var   = tk.StringVar()
        self._strip_var       = tk.StringVar()
        self._conflict_var    = tk.StringVar()
        self._strip_post_var  = tk.StringVar()
        self._prefix_var      = tk.StringVar()
        self._req_folders_var   = tk.StringVar()
        self._auto_strip_var    = tk.BooleanVar(value=False)
        self._req_file_types_var = tk.StringVar()
        self._install_as_is_var = tk.BooleanVar(value=False)
        self._restore_var       = tk.BooleanVar(value=True)
        self._norm_case_var     = tk.BooleanVar(value=True)
        self._routing_rules_rows: list[dict] = []
        self._routing_rules_header: ctk.CTkFrame | None = None
        self._routing_rules_container = None
        self._framework_rows: list[dict] = []
        self._framework_container = None

        if existing:
            self._name_var.set(existing.get("name", ""))
            self._exe_var.set(existing.get("exe_name", ""))
            self._deploy_var.set(existing.get("deploy_type", "standard"))
            self._data_path_var.set(existing.get("mod_data_path", ""))
            self._steam_var.set(existing.get("steam_id", ""))
            self._nexus_var.set(existing.get("nexus_game_domain", ""))
            self._image_url_var.set(existing.get("image_url", ""))
            self._strip_var.set(_set_to_str(existing.get("mod_folder_strip_prefixes", [])))
            self._conflict_var.set(_set_to_str(existing.get("conflict_ignore_filenames", [])))
            self._strip_post_var.set(_set_to_str(existing.get("mod_folder_strip_prefixes_post", [])))
            self._prefix_var.set(existing.get("mod_install_prefix", ""))
            self._req_folders_var.set(_set_to_str(existing.get("mod_required_top_level_folders", [])))
            self._auto_strip_var.set(bool(existing.get("mod_auto_strip_until_required", False)))
            self._req_file_types_var.set(_set_to_str(existing.get("mod_required_file_types", [])))
            self._install_as_is_var.set(bool(existing.get("mod_install_as_is_if_no_match", False)))
            self._restore_var.set(bool(existing.get("restore_before_deploy", True)))
            self._norm_case_var.set(bool(existing.get("normalize_folder_case", True)))
            self._dll_initial = _dll_to_str(existing.get("wine_dll_overrides", {}))
            self._routing_rules_initial = existing.get("custom_routing_rules", [])
            self._frameworks_initial = existing.get("custom_frameworks", {})
        else:
            self._dll_initial = ""
            self._routing_rules_initial = []
            self._frameworks_initial = {}

        self._build_ui()
        self._update_data_path_visibility()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)
        self.grid_columnconfigure(0, weight=1)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_propagate(False)
        ctk.CTkLabel(
            title_bar,
            text="Edit Custom Game" if self._existing else "Define Custom Game",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)

        # Body — 3-column layout so content column is centered
        self._body = ctk.CTkScrollableFrame(
            self, fg_color=BG_PANEL, corner_radius=0,
            scrollbar_button_color=BG_HEADER,
            scrollbar_button_hover_color=ACCENT,
        )
        self._body.grid(row=1, column=0, sticky="nsew")
        self._body.grid_columnconfigure(0, weight=1)                             # left spacer
        self._body.grid_columnconfigure(1, weight=0, minsize=self.CONTENT_WIDTH) # content
        self._body.grid_columnconfigure(2, weight=1)                             # right spacer

        # Inner container — all form widgets live here
        inner = ctk.CTkFrame(self._body, fg_color="transparent")
        inner.grid(row=0, column=1, sticky="nsew", pady=12)
        inner.grid_columnconfigure(0, weight=1)
        body = inner  # alias so the form code below reads naturally

        WRAP = self.CONTENT_WIDTH - 60
        row = 0

        # ---- Game Name ----
        row = self._section(body, row, "Game Name")
        ctk.CTkLabel(
            body, text="The display name shown in the game selector (must be unique).",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        row += 1
        ctk.CTkEntry(
            body, textvariable=self._name_var,
            font=FONT_NORMAL, fg_color=BG_ROW, text_color=TEXT_MAIN,
            border_color=BORDER, placeholder_text="e.g. My Favourite Game",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 10))
        row += 1

        # ---- Exe Name ----
        row = self._divider(body, row)
        row = self._section(body, row, "Executable Filename")
        ctk.CTkLabel(
            body,
            text="The .exe location from the games root folder. eg. bin/bg3.exe for BG3 or SkyrimSELauncher.exe for Skyrim SE",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        row += 1
        ctk.CTkEntry(
            body, textvariable=self._exe_var,
            font=FONT_MONO, fg_color=BG_ROW, text_color=TEXT_MAIN,
            border_color=BORDER, placeholder_text="e.g. MyGame.exe",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 10))
        row += 1

        # ---- Deploy Type ----
        row = self._divider(body, row)
        row = self._section(body, row, "Deploy Method")
        for label, value, desc in _DEPLOY_OPTIONS:
            ctk.CTkRadioButton(
                body, text=label, variable=self._deploy_var, value=value,
                font=FONT_BOLD, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
                command=self._update_data_path_visibility,
            ).grid(row=row, column=0, sticky="w", padx=16, pady=(4, 0))
            row += 1
            ctk.CTkLabel(
                body, text=desc, font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
                wraplength=WRAP,
            ).grid(row=row, column=0, sticky="ew", padx=36, pady=(0, 4))
            row += 1

        # ---- Mod Sub-folder (standard only) ----
        row = self._divider(body, row)
        self._data_path_section_row = row
        self._data_path_widgets: list[ctk.CTkBaseClass] = []

        lbl_sec = ctk.CTkLabel(
            body, text="Mod Sub-folder",
            font=FONT_BOLD, text_color=TEXT_SEP, anchor="center",
        )
        lbl_sec.grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 2))
        self._data_path_widgets.append(lbl_sec)
        self._data_path_lbl_sec = lbl_sec
        row += 1

        lbl_hint = ctk.CTkLabel(
            body,
            text=(
                "Path relative to the game root where mod files are installed. "
                "e.g. 'Data' for Bethesda games, 'BepInEx/plugins' for BepInEx. "
                "Leave empty to target the game root directly."
            ),
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
            wraplength=WRAP,
        )
        lbl_hint.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        self._data_path_widgets.append(lbl_hint)
        self._data_path_lbl_hint = lbl_hint
        row += 1

        ent_dp = ctk.CTkEntry(
            body, textvariable=self._data_path_var,
            font=FONT_MONO, fg_color=BG_ROW, text_color=TEXT_MAIN,
            border_color=BORDER, placeholder_text="e.g. Data   (leave empty for game root)",
        )
        ent_dp.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 10))
        self._data_path_widgets.append(ent_dp)
        self._data_path_entry = ent_dp
        row += 1

        # ---- Optional: Steam ID ----
        row = self._divider(body, row)
        row = self._section(body, row, "Steam App ID  (optional)")
        ctk.CTkLabel(
            body,
            text="Used to auto-detect the Proton prefix. Leave empty if not on Steam.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        row += 1
        ctk.CTkEntry(
            body, textvariable=self._steam_var,
            font=FONT_MONO, fg_color=BG_ROW, text_color=TEXT_MAIN,
            border_color=BORDER, placeholder_text="e.g. 377160",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 10))
        row += 1

        # ---- Optional: Nexus domain ----
        row = self._divider(body, row)
        row = self._section(body, row, "Nexus Mods Domain  (optional)")
        ctk.CTkLabel(
            body,
            text="The game's slug on nexusmods.com.  e.g. 'skyrimspecialedition'.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        row += 1
        ctk.CTkEntry(
            body, textvariable=self._nexus_var,
            font=FONT_MONO, fg_color=BG_ROW, text_color=TEXT_MAIN,
            border_color=BORDER, placeholder_text="e.g. myfavouritegame",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 10))
        row += 1

        # ---- Optional: Image URL ----
        row = self._divider(body, row)
        row = self._section(body, row, "Banner Image URL  (optional)")
        ctk.CTkLabel(
            body,
            text=(
                "A direct URL to a PNG/JPG image shown in the game picker card. "
                "The image is downloaded once and cached locally."
            ),
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
            wraplength=WRAP,
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        row += 1
        ctk.CTkEntry(
            body, textvariable=self._image_url_var,
            font=FONT_MONO, fg_color=BG_ROW, text_color=TEXT_MAIN,
            border_color=BORDER, placeholder_text="https://example.com/banner.jpg",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        row += 1

        self._image_status = ctk.CTkLabel(
            body, text="", font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
        )
        self._image_status.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 10))
        row += 1

        # ---- Advanced Options ----
        row = self._divider(body, row)
        row = self._section(body, row, "Advanced Options  (optional)")
        ctk.CTkLabel(
            body,
            text="Used to change the folder structure of an installed mod to match what is required by the manager.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
            wraplength=WRAP,
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 6))
        row += 1

        _adv_fields = [
            (
                "mod_folder_strip_prefixes",
                self._strip_var,
                "Strip Prefixes",
                "Comma-separated top-level folder names to strip from mod files during "
                "filemap building (case-insensitive).  e.g. Data, data",
            ),
            (
                "conflict_ignore_filenames",
                self._conflict_var,
                "Conflict Ignore Filenames",
                "Comma-separated filenames excluded from conflict detection.  "
                "e.g. modinfo.ini, manifest.json",
            ),
            (
                "mod_folder_strip_prefixes_post",
                self._strip_post_var,
                "Strip Prefixes (post-install)",
                "Like Strip Prefixes but applied after mod_required_top_level_folders "
                "validation.  e.g. reframework",
            ),
            (
                "mod_install_prefix",
                self._prefix_var,
                "Prepend Prefix",
                "Path segment prepended to every installed file.  "
                "e.g. 'mods' so files land at mods/<ModName>/…",
            ),
            (
                "mod_required_top_level_folders",
                self._req_folders_var,
                "Required Top-Level Folders",
                "Comma-separated folder names a mod must contain at its root.  "
                "If none match, the user is prompted to set a data directory.",
            ),
            (
                "mod_required_file_types",
                self._req_file_types_var,
                "Required File Types",
                "Comma-separated file extensions a mod must contain at its root.  "
                "e.g. .esp, .esm — works standalone or as a fallback after Required Top-Level Folders.",
            ),
        ]

        for _key, _var, _label, _hint in _adv_fields:
            ctk.CTkLabel(
                body, text=_label, font=FONT_BOLD, text_color=TEXT_MAIN, anchor="center",
            ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 0))
            row += 1
            ctk.CTkLabel(
                body, text=_hint, font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
                wraplength=WRAP,
            ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 2))
            row += 1
            ctk.CTkEntry(
                body, textvariable=_var,
                font=FONT_MONO, fg_color=BG_ROW, text_color=TEXT_MAIN,
                border_color=BORDER,
            ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 6))
            row += 1

        # Auto-strip toggle
        ctk.CTkLabel(
            body, text="Auto Strip Until Required",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="center",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 0))
        row += 1
        ctk.CTkLabel(
            body,
            text="When enabled and Required Top-Level Folders is set, strip leading "
                 "path segments automatically instead of prompting the user.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
            wraplength=WRAP,
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 2))
        row += 1
        ctk.CTkSwitch(
            body, text="Enable", variable=self._auto_strip_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=BG_ROW, progress_color=ACCENT,
        ).grid(row=row, column=0, sticky="", padx=16, pady=(0, 6))
        row += 1

        # Install as-is toggle
        ctk.CTkLabel(
            body, text="Install As-Is If No Match",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="center",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 0))
        row += 1
        ctk.CTkLabel(
            body,
            text="When enabled, if both Required Top-Level Folders and Required File Types "
                 "checks fail, the mod is installed as-is without showing the prefix dialog.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
            wraplength=WRAP,
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 2))
        row += 1
        ctk.CTkSwitch(
            body, text="Enable", variable=self._install_as_is_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=BG_ROW, progress_color=ACCENT,
        ).grid(row=row, column=0, sticky="", padx=16, pady=(0, 6))
        row += 1

        # Restore before deploy toggle
        ctk.CTkLabel(
            body, text="Restore Before Deploy",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="center",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 0))
        row += 1
        ctk.CTkLabel(
            body,
            text="When enabled (default), the manager runs Restore before every Deploy "
                 "to clean the game state first. Disable only if the game's deploy cycle "
                 "handles its own cleanup internally.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
            wraplength=WRAP,
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 2))
        row += 1
        ctk.CTkSwitch(
            body, text="Enable", variable=self._restore_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=BG_ROW, progress_color=ACCENT,
        ).grid(row=row, column=0, sticky="", padx=16, pady=(0, 6))
        row += 1

        # Normalize folder case toggle
        ctk.CTkLabel(
            body, text="Normalize Folder Case",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="center",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 0))
        row += 1
        ctk.CTkLabel(
            body,
            text="When enabled (default), folder names that differ only in case across mods are "
                 "unified to a single casing. Disable for Linux-native games where folder casing "
                 "is significant (e.g. Music/ and music/ are different directories).",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
            wraplength=WRAP,
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 2))
        row += 1
        ctk.CTkSwitch(
            body, text="Enable", variable=self._norm_case_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=BG_ROW, progress_color=ACCENT,
        ).grid(row=row, column=0, sticky="", padx=16, pady=(0, 6))
        row += 1

        # Wine DLL overrides (multiline)
        ctk.CTkLabel(
            body, text="Wine DLL Overrides",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="center",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 0))
        row += 1
        ctk.CTkLabel(
            body,
            text="One override per line: dll_name=load_order  "
                 "e.g. winhttp=native,builtin",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
            wraplength=WRAP,
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 2))
        row += 1
        self._dll_textbox = ctk.CTkTextbox(
            body, height=72, font=FONT_MONO,
            fg_color=BG_ROW, text_color=TEXT_MAIN, corner_radius=4,
        )
        self._dll_textbox.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 10))
        if self._dll_initial:
            self._dll_textbox.insert("0.0", self._dll_initial)
        row += 1

        # ---- Custom Routing Rules ----
        ctk.CTkLabel(
            body, text="Custom Routing Rules",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="center",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 0))
        row += 1
        ctk.CTkLabel(
            body,
            text="Route specific files to alternate destinations during deploy. "
                 "Each rule maps files (by extension or folder) to a game-root-relative directory. "
                 "For extensions, append (.ext, .ext) to also route same-stem siblings "
                 "(e.g. .asi (.ini) sends Foo.ini alongside Foo.asi).",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
            wraplength=WRAP,
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        row += 1

        ctk.CTkButton(
            body, text="+ Add Rule", width=100, height=26, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._add_routing_rule_row,
        ).grid(row=row, column=0, sticky="", padx=16, pady=(0, 4))
        row += 1

        self._routing_rules_container = ctk.CTkFrame(body, fg_color="transparent")
        self._routing_rules_container.grid_columnconfigure(0, weight=1)
        self._routing_rules_container_row = row
        self._routing_rules_container_body = body
        row += 1

        # Populate existing rules
        for rule_data in self._routing_rules_initial:
            if isinstance(rule_data, dict):
                companions = rule_data.get("companion_extensions") or []
                if rule_data.get("filenames"):
                    mt = "filenames"
                    mv = ", ".join(rule_data["filenames"])
                elif rule_data.get("extensions"):
                    mt = "extensions"
                    mv = ", ".join(rule_data["extensions"])
                    if companions:
                        mv = f"{mv} ({', '.join(companions)})"
                else:
                    mt = "folders"
                    mv = ", ".join(rule_data.get("folders") or [])
                self._add_routing_rule_row(
                    dest=rule_data.get("dest", ""),
                    match_type=mt,
                    match_value=mv,
                    loose_only=bool(rule_data.get("loose_only", False)),
                )

        # ---- Framework Detection ----
        row = self._divider(body, row)
        ctk.CTkLabel(
            body, text="Framework Detection",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="center",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 0))
        row += 1
        ctk.CTkLabel(
            body,
            text="Display a status banner in the Plugins tab when a framework is installed. "
                 "Enter the framework name on the left and its file path relative to the game root on the right.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="center",
            wraplength=WRAP,
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        row += 1

        ctk.CTkButton(
            body, text="+ Add Framework", width=130, height=26, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._add_framework_row,
        ).grid(row=row, column=0, sticky="", padx=16, pady=(0, 4))
        row += 1

        self._framework_container = ctk.CTkFrame(body, fg_color="transparent")
        self._framework_container.grid_columnconfigure(0, weight=1)
        self._framework_container_row = row
        self._framework_container_body = body
        row += 1

        # Populate existing frameworks
        if isinstance(self._frameworks_initial, dict):
            for fw_name, fw_path in self._frameworks_initial.items():
                self._add_framework_row(name=fw_name, path=fw_path)

        # ---- Validation label ----
        self._validation_label = ctk.CTkLabel(
            body, text="", font=FONT_SMALL, text_color=TEXT_ERR, anchor="center",
            wraplength=self.CONTENT_WIDTH - 32,
        )
        self._validation_label.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 10))

        # Bind scroll wheel for Linux
        self.after(100, self._bind_mousewheel_recursive)

        # Button bar
        btn_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        btn_bar.grid(row=2, column=0, sticky="ew")
        btn_bar.grid_propagate(False)
        ctk.CTkFrame(btn_bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )

        ctk.CTkButton(
            btn_bar, text="Cancel", width=90, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=10)

        self._save_btn = ctk.CTkButton(
            btn_bar, text="Save Game", width=110, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_save,
        )
        self._save_btn.pack(side="right", padx=4, pady=10)

        if self._existing:
            ctk.CTkButton(
                btn_bar, text="Delete", width=90, height=30, font=FONT_BOLD,
                fg_color=RED_BTN, hover_color=RED_HOV, text_color="white",
                command=self._on_delete,
            ).pack(side="left", padx=(12, 4), pady=10)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _section(self, body, row: int, title: str) -> int:
        ctk.CTkLabel(
            body, text=title, font=FONT_BOLD, text_color=TEXT_SEP, anchor="center",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 2))
        return row + 1

    def _divider(self, body, row: int) -> int:
        ctk.CTkFrame(body, fg_color=BORDER, height=1, corner_radius=0).grid(
            row=row, column=0, sticky="ew", padx=16, pady=2
        )
        return row + 1

    def _update_data_path_visibility(self):
        deploy = self._deploy_var.get()
        state = "disabled" if deploy == "root" else "normal"
        for w in self._data_path_widgets:
            try:
                w.configure(state=state)
            except Exception:
                pass
        WRAP = self.CONTENT_WIDTH - 60
        if deploy == "ue5":
            self._data_path_lbl_sec.configure(text="Game Sub-folder  (optional)")
            self._data_path_lbl_hint.configure(
                text=(
                    "Location of the folder from root where deployed mods are sent to.  "
                    "e.g. Pheonix for Hogwarts Legacy."
                ),
                wraplength=WRAP,
            )
            self._data_path_entry.configure(placeholder_text="e.g. OblivionRemastered")
        else:
            self._data_path_lbl_sec.configure(text="Mod Sub-folder")
            self._data_path_lbl_hint.configure(
                text=(
                    "Path relative to the game root where mod files are installed. "
                    "e.g. 'Data' for Bethesda games, 'BepInEx/plugins' for BepInEx. "
                    "Leave empty to target the game root directly."
                ),
                wraplength=WRAP,
            )
            self._data_path_entry.configure(placeholder_text="e.g. Data   (leave empty for game root)")

    def _add_routing_rule_row(self, dest: str = "", match_type: str = "extensions",
                              match_value: str = "", loose_only: bool = False) -> None:
        """Add a routing rule row to the container."""
        container = self._routing_rules_container
        if not self._routing_rules_rows:
            container.grid(row=self._routing_rules_container_row, column=0,
                           sticky="ew", padx=16, pady=(0, 4))
            # Column headers
            hdr = ctk.CTkFrame(container, fg_color="transparent", height=20)
            hdr.grid(row=0, column=0, sticky="ew")
            hdr.grid_columnconfigure(0, weight=1)
            hdr.grid_columnconfigure(1, weight=0, minsize=108)
            hdr.grid_columnconfigure(2, weight=1)
            hdr.grid_columnconfigure(3, weight=0)
            hdr.grid_columnconfigure(4, weight=0)
            ctk.CTkLabel(hdr, text="Path", font=FONT_SMALL, text_color=TEXT_DIM,
                         anchor="w").grid(row=0, column=0, sticky="w", padx=(6, 0))
            ctk.CTkLabel(hdr, text="Match Value", font=FONT_SMALL, text_color=TEXT_DIM,
                         anchor="w").grid(row=0, column=2, sticky="w", padx=(4, 0))
            self._routing_rules_header = hdr
        row_idx = len(self._routing_rules_rows) + 1  # +1 for header row

        row_frame = ctk.CTkFrame(container, fg_color=BG_ROW, corner_radius=4, height=36)
        row_frame.grid(row=row_idx, column=0, sticky="ew", pady=2)
        row_frame.grid_columnconfigure(0, weight=1)
        row_frame.grid_columnconfigure(1, weight=0)
        row_frame.grid_columnconfigure(2, weight=1)
        row_frame.grid_columnconfigure(3, weight=0)
        row_frame.grid_columnconfigure(4, weight=0)

        dest_var  = tk.StringVar(value=dest)
        type_var  = tk.StringVar(value=match_type)
        value_var = tk.StringVar(value=match_value)
        loose_var = tk.BooleanVar(value=loose_only)

        ctk.CTkEntry(
            row_frame, textvariable=dest_var, font=FONT_MONO,
            fg_color=BG_DEEP, text_color=TEXT_MAIN, border_color=BORDER,
            placeholder_text="dest (e.g. pak_mods)", width=140,
        ).grid(row=0, column=0, sticky="ew", padx=(6, 4), pady=4)

        ctk.CTkOptionMenu(
            row_frame, variable=type_var, values=["extensions", "folders", "filenames"],
            font=FONT_SMALL, fg_color=BG_DEEP, text_color=TEXT_MAIN,
            button_color=BG_HEADER, button_hover_color=BG_HOVER, width=100,
        ).grid(row=0, column=1, padx=2, pady=4)

        ctk.CTkEntry(
            row_frame, textvariable=value_var, font=FONT_MONO,
            fg_color=BG_DEEP, text_color=TEXT_MAIN, border_color=BORDER,
            placeholder_text="e.g. .pak, .utoc   ·   .asi (.ini) routes same-stem .ini alongside each .asi",
            width=140,
        ).grid(row=0, column=2, sticky="ew", padx=(4, 4), pady=4)

        ctk.CTkSwitch(
            row_frame, text="Loose only", variable=loose_var,
            font=FONT_SMALL, text_color=TEXT_MAIN,
            fg_color=BG_DEEP, progress_color=ACCENT, width=40,
        ).grid(row=0, column=3, padx=(4, 2), pady=4)

        row_data = {"frame": row_frame, "dest": dest_var, "type": type_var,
                    "value": value_var, "loose_only": loose_var}
        self._routing_rules_rows.append(row_data)

        ctk.CTkButton(
            row_frame, text="X", width=28, height=28, font=FONT_SMALL,
            fg_color=RED_BTN, hover_color=RED_HOV, text_color="white",
            command=lambda rd=row_data: self._remove_routing_rule_row(rd),
        ).grid(row=0, column=4, padx=(2, 6), pady=4)

    def _remove_routing_rule_row(self, row_data: dict) -> None:
        """Remove a routing rule row."""
        if row_data in self._routing_rules_rows:
            self._routing_rules_rows.remove(row_data)
            row_data["frame"].destroy()
            if not self._routing_rules_rows:
                self._routing_rules_header.destroy()
                self._routing_rules_container.grid_remove()
            else:
                for i, rd in enumerate(self._routing_rules_rows):
                    rd["frame"].grid(row=i + 1, column=0, sticky="ew", pady=2)

    def _collect_routing_rules(self) -> list[dict]:
        """Collect routing rules from the UI rows into JSON-serializable dicts.

        For ``extensions`` rules, values may end with ``(.ext, .ext)`` — those
        parenthesised extensions are parsed as ``companion_extensions`` so a
        same-stem sibling (e.g. ``Foo.ini`` next to ``Foo.asi``) rides along
        with the primary match.
        """
        rules = []
        for rd in self._routing_rules_rows:
            dest       = rd["dest"].get().strip()
            match_type = rd["type"].get()
            raw_value  = rd["value"].get().strip()

            companions: list[str] = []
            if match_type == "extensions" and "(" in raw_value and ")" in raw_value:
                before, _, rest = raw_value.partition("(")
                inside, _, after = rest.partition(")")
                companions = [v.strip() for v in inside.split(",") if v.strip()]
                raw_value = (before + " " + after).strip().rstrip(",").strip()

            values = [v.strip() for v in raw_value.split(",") if v.strip()]
            if not values and not dest:
                continue
            rule: dict = {"dest": dest}
            if match_type == "extensions":
                rule["extensions"] = values
                if companions:
                    rule["companion_extensions"] = companions
            elif match_type == "filenames":
                rule["filenames"] = values
            else:
                rule["folders"] = values
            if rd["loose_only"].get():
                rule["loose_only"] = True
            rules.append(rule)
        return rules

    def _add_framework_row(self, name: str = "", path: str = "") -> None:
        """Add a framework detection row to the container."""
        container = self._framework_container
        if not self._framework_rows:
            container.grid(row=self._framework_container_row, column=0,
                           sticky="ew", padx=16, pady=(0, 4))
        row_idx = len(self._framework_rows)

        row_frame = ctk.CTkFrame(container, fg_color=BG_ROW, corner_radius=4, height=36)
        row_frame.grid(row=row_idx, column=0, sticky="ew", pady=2)
        row_frame.grid_columnconfigure(0, weight=1)
        row_frame.grid_columnconfigure(1, weight=2)
        row_frame.grid_columnconfigure(2, weight=0)

        name_var = tk.StringVar(value=name)
        path_var = tk.StringVar(value=path)

        ctk.CTkEntry(
            row_frame, textvariable=name_var, font=FONT_MONO,
            fg_color=BG_DEEP, text_color=TEXT_MAIN, border_color=BORDER,
            placeholder_text="e.g. Script Extender", width=160,
        ).grid(row=0, column=0, sticky="ew", padx=(6, 4), pady=4)

        ctk.CTkEntry(
            row_frame, textvariable=path_var, font=FONT_MONO,
            fg_color=BG_DEEP, text_color=TEXT_MAIN, border_color=BORDER,
            placeholder_text="e.g. skse64_loader.exe", width=200,
        ).grid(row=0, column=1, sticky="ew", padx=(4, 4), pady=4)

        row_data = {"frame": row_frame, "name": name_var, "path": path_var}
        self._framework_rows.append(row_data)

        ctk.CTkButton(
            row_frame, text="X", width=28, height=28, font=FONT_SMALL,
            fg_color=RED_BTN, hover_color=RED_HOV, text_color="white",
            command=lambda rd=row_data: self._remove_framework_row(rd),
        ).grid(row=0, column=2, padx=(2, 6), pady=4)

    def _remove_framework_row(self, row_data: dict) -> None:
        """Remove a framework detection row."""
        if row_data in self._framework_rows:
            self._framework_rows.remove(row_data)
            row_data["frame"].destroy()
            if not self._framework_rows:
                self._framework_container.grid_remove()
            else:
                for i, rd in enumerate(self._framework_rows):
                    rd["frame"].grid(row=i, column=0, sticky="ew", pady=2)

    def _collect_frameworks(self) -> dict[str, str]:
        """Collect framework rows into a JSON-serializable dict."""
        result: dict[str, str] = {}
        for rd in self._framework_rows:
            name = rd["name"].get().strip()
            path = rd["path"].get().strip()
            if name and path:
                result[name] = path
        return result

    def _scroll_body(self, direction: int):
        """Scroll the body canvas by *direction* units."""
        try:
            self._body._parent_canvas.yview_scroll(direction, "units")
        except Exception:
            pass

    def _bind_mousewheel_recursive(self, widget=None):
        """Recursively bind Linux scroll-wheel events to every child widget."""
        if widget is None:
            widget = self._body
        if not LEGACY_WHEEL_REDUNDANT:
            widget.bind("<Button-4>", lambda e: self._scroll_body(-2), add="+")
            widget.bind("<Button-5>", lambda e: self._scroll_body(2), add="+")
        for child in widget.winfo_children():
            self._bind_mousewheel_recursive(child)

    # ------------------------------------------------------------------
    # Validate
    # ------------------------------------------------------------------

    def _validate(self) -> str | None:
        """Return an error message, or None if inputs are valid."""
        name = self._name_var.get().strip()
        exe  = self._exe_var.get().strip()
        if not name:
            return "Game Name is required."
        if not exe:
            return "Executable Filename is required."
        if len(name) > 120:
            return "Game Name is too long (max 120 characters)."
        return None

    # ------------------------------------------------------------------
    # Image download (fire-and-forget)
    # ------------------------------------------------------------------

    def _download_image(self, url: str, game_id: str) -> None:
        """Download the banner image in a background thread and cache it."""
        def _update_status(text, color):
            try:
                if self.winfo_exists():
                    self._image_status.configure(text=text, text_color=color)
            except Exception:
                pass

        def _worker():
            try:
                import requests
                from PIL import Image as PilImage
                import io

                self.after(0, lambda: _update_status("Downloading image…", TEXT_WARN))
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                img = PilImage.open(io.BytesIO(resp.content)).convert("RGBA")
                out = get_custom_game_images_dir() / f"{game_id}.png"
                img.save(out, "PNG")
                self.after(0, lambda: _update_status("Image cached.", TEXT_OK))
            except Exception as exc:
                self.after(0, lambda e=exc: _update_status(f"Image download failed: {e}", TEXT_ERR))

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_save(self):
        err = self._validate()
        if err:
            self._validation_label.configure(text=err)
            return
        self._validation_label.configure(text="")

        name      = self._name_var.get().strip()
        exe       = self._exe_var.get().strip()
        deploy    = self._deploy_var.get()
        data_path = self._data_path_var.get().strip() if deploy in ("standard", "ue5") else ""
        steam_id  = self._steam_var.get().strip()
        nexus     = self._nexus_var.get().strip()
        image_url = self._image_url_var.get().strip()

        # Preserve the game_id when editing so paths.json references remain valid
        game_id = (
            self._existing.get("game_id") if self._existing else None
        ) or _make_game_id(name)

        defn = {
            "name":              name,
            "game_id":           game_id,
            "exe_name":          exe,
            "deploy_type":       deploy,
            "mod_data_path":     data_path,
            "steam_id":          steam_id,
            "nexus_game_domain": nexus,
            "image_url":         image_url,
            # Advanced
            "mod_folder_strip_prefixes":      _str_to_list(self._strip_var.get()),
            "conflict_ignore_filenames":      _str_to_list(self._conflict_var.get()),
            "mod_folder_strip_prefixes_post": _str_to_list(self._strip_post_var.get()),
            "mod_install_prefix":             self._prefix_var.get().strip(),
            "mod_required_top_level_folders": _str_to_list(self._req_folders_var.get()),
            "mod_auto_strip_until_required":  self._auto_strip_var.get(),
            "mod_required_file_types":        _str_to_list(self._req_file_types_var.get()),
            "mod_install_as_is_if_no_match":  self._install_as_is_var.get(),
            "restore_before_deploy":          self._restore_var.get(),
            "normalize_folder_case":          self._norm_case_var.get(),
            "wine_dll_overrides":             _parse_dll_text(
                self._dll_textbox.get("0.0", "end")
            ),
            "custom_routing_rules":           self._collect_routing_rules(),
            "custom_frameworks":              self._collect_frameworks(),
        }

        save_custom_game_definition(defn)

        if image_url:
            self._download_image(image_url, game_id)

        self.saved_game = make_custom_game(defn)
        self._on_done(self)

    def _on_delete(self):
        if self._existing is None:
            return
        game_id = self._existing.get("game_id", "")
        if game_id:
            delete_custom_game_definition(game_id)
            img = get_custom_game_images_dir() / f"{game_id}.png"
            img.unlink(missing_ok=True)
        self.deleted = True
        self._on_done(self)

    def _on_cancel(self):
        self._on_done(self)
