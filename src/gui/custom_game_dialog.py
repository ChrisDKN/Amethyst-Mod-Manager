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
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._existing = existing  # None = new game
        self.saved_game = None     # set to a BaseGame instance on success
        self.deleted    = False

        # ---- tk variables ----
        self._name_var        = tk.StringVar()
        self._exe_var         = tk.StringVar()
        self._deploy_var      = tk.StringVar(value="standard")
        self._data_path_var   = tk.StringVar()
        self._steam_var       = tk.StringVar()
        self._nexus_var       = tk.StringVar()
        self._image_url_var   = tk.StringVar()
        # Advanced properties
        self._strip_var       = tk.StringVar()  # mod_folder_strip_prefixes
        self._conflict_var    = tk.StringVar()  # conflict_ignore_filenames
        self._strip_post_var  = tk.StringVar()  # mod_folder_strip_prefixes_post
        self._prefix_var      = tk.StringVar()  # mod_install_prefix
        self._req_folders_var = tk.StringVar()  # mod_required_top_level_folders
        self._auto_strip_var  = tk.BooleanVar(value=False)  # mod_auto_strip_until_required
        self._heroic_var      = tk.StringVar()  # heroic_app_names
        self._restore_var     = tk.BooleanVar(value=True)   # restore_before_deploy
        # wine_dll_overrides stored as a plain string (dll=mode lines), set in _build_ui via textbox

        if existing:
            self._name_var.set(existing.get("name", ""))
            self._exe_var.set(existing.get("exe_name", ""))
            self._deploy_var.set(existing.get("deploy_type", "standard"))
            self._data_path_var.set(existing.get("mod_data_path", ""))
            self._steam_var.set(existing.get("steam_id", ""))
            self._nexus_var.set(existing.get("nexus_game_domain", ""))
            self._image_url_var.set(existing.get("image_url", ""))
            # Advanced
            self._strip_var.set(_set_to_str(existing.get("mod_folder_strip_prefixes", [])))
            self._conflict_var.set(_set_to_str(existing.get("conflict_ignore_filenames", [])))
            self._strip_post_var.set(_set_to_str(existing.get("mod_folder_strip_prefixes_post", [])))
            self._prefix_var.set(existing.get("mod_install_prefix", ""))
            self._req_folders_var.set(_set_to_str(existing.get("mod_required_top_level_folders", [])))
            self._auto_strip_var.set(bool(existing.get("mod_auto_strip_until_required", False)))
            self._heroic_var.set(_list_to_str(existing.get("heroic_app_names", [])))
            self._restore_var.set(bool(existing.get("restore_before_deploy", True)))
            self._dll_initial = _dll_to_str(existing.get("wine_dll_overrides", {}))
        else:
            self._dll_initial = ""

        self._build_ui()
        self._update_data_path_visibility()

        self.after(100, self._make_modal)

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

        # Body
        body = ctk.CTkScrollableFrame(
            self, fg_color=BG_PANEL, corner_radius=0,
            scrollbar_button_color=BG_HEADER,
            scrollbar_button_hover_color=ACCENT,
        )
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        row = 0

        # ---- Game Name ----
        row = self._section(body, row, "Game Name")
        ctk.CTkLabel(
            body, text="The display name shown in the game selector (must be unique).",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
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
            text="The .exe filename used to locate the game in Steam / Heroic libraries.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
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
                wraplength=self.WIDTH - 60,
            ).grid(row=row, column=0, sticky="ew", padx=36, pady=(0, 4))
            row += 1
        row_after_deploy = row

        # ---- Mod Sub-folder (standard only) ----
        row = self._divider(body, row)
        self._data_path_section_row = row
        self._data_path_widgets: list[ctk.CTkBaseClass] = []

        lbl_sec = ctk.CTkLabel(
            body, text="Mod Sub-folder",
            font=FONT_BOLD, text_color=TEXT_SEP, anchor="w",
        )
        lbl_sec.grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 2))
        self._data_path_widgets.append(lbl_sec)
        row += 1

        lbl_hint = ctk.CTkLabel(
            body,
            text=(
                "Path relative to the game root where mod files are installed. "
                "e.g. 'Data' for Bethesda games, 'BepInEx/plugins' for BepInEx. "
                "Leave empty to target the game root directly."
            ),
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
            wraplength=self.WIDTH - 60,
        )
        lbl_hint.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        self._data_path_widgets.append(lbl_hint)
        row += 1

        ent_dp = ctk.CTkEntry(
            body, textvariable=self._data_path_var,
            font=FONT_MONO, fg_color=BG_ROW, text_color=TEXT_MAIN,
            border_color=BORDER, placeholder_text="e.g. Data   (leave empty for game root)",
        )
        ent_dp.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 10))
        self._data_path_widgets.append(ent_dp)
        row += 1

        # ---- Optional: Steam ID ----
        row = self._divider(body, row)
        row = self._section(body, row, "Steam App ID  (optional)")
        ctk.CTkLabel(
            body,
            text="Used to auto-detect the Proton prefix. Leave empty if not on Steam.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
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
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
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
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
            wraplength=self.WIDTH - 60,
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        row += 1
        ctk.CTkEntry(
            body, textvariable=self._image_url_var,
            font=FONT_MONO, fg_color=BG_ROW, text_color=TEXT_MAIN,
            border_color=BORDER, placeholder_text="https://example.com/banner.jpg",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        row += 1

        self._image_status = ctk.CTkLabel(
            body, text="", font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
        )
        self._image_status.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 10))
        row += 1

        # ---- Advanced Options ----
        row = self._divider(body, row)
        row = self._section(body, row, "Advanced Options  (optional)")

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
                "Mod Install Prefix",
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
                "heroic_app_names",
                self._heroic_var,
                "Heroic App Names",
                "Comma-separated Heroic Games Launcher app identifiers "
                "(Epic appName or GOG product ID).  e.g. Pewee, 1207658924",
            ),
        ]

        for _key, _var, _label, _hint in _adv_fields:
            ctk.CTkLabel(
                body, text=_label, font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
            ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 0))
            row += 1
            ctk.CTkLabel(
                body, text=_hint, font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
                wraplength=self.WIDTH - 60,
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
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 0))
        row += 1
        ctk.CTkLabel(
            body,
            text="When enabled and Required Top-Level Folders is set, strip leading "
                 "path segments automatically instead of prompting the user.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
            wraplength=self.WIDTH - 60,
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 2))
        row += 1
        ctk.CTkSwitch(
            body, text="Enable", variable=self._auto_strip_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=BG_ROW, progress_color=ACCENT,
        ).grid(row=row, column=0, sticky="w", padx=16, pady=(0, 6))
        row += 1

        # Restore before deploy toggle
        ctk.CTkLabel(
            body, text="Restore Before Deploy",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 0))
        row += 1
        ctk.CTkLabel(
            body,
            text="When enabled (default), the manager runs Restore before every Deploy "
                 "to clean the game state first. Disable only if the game's deploy cycle "
                 "handles its own cleanup internally.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
            wraplength=self.WIDTH - 60,
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 2))
        row += 1
        ctk.CTkSwitch(
            body, text="Enable", variable=self._restore_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=BG_ROW, progress_color=ACCENT,
        ).grid(row=row, column=0, sticky="w", padx=16, pady=(0, 6))
        row += 1

        # Wine DLL overrides (multiline)
        ctk.CTkLabel(
            body, text="Wine DLL Overrides",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 0))
        row += 1
        ctk.CTkLabel(
            body,
            text="One override per line: dll_name=load_order  "
                 "e.g. winhttp=native,builtin",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
            wraplength=self.WIDTH - 60,
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

        # ---- Validation label ----
        self._validation_label = ctk.CTkLabel(
            body, text="", font=FONT_SMALL, text_color=TEXT_ERR, anchor="w",
            wraplength=self.WIDTH - 32,
        )
        self._validation_label.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 10))

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
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
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
            body, text=title, font=FONT_BOLD, text_color=TEXT_SEP, anchor="w",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 2))
        return row + 1

    def _divider(self, body, row: int) -> int:
        ctk.CTkFrame(body, fg_color=BORDER, height=1, corner_radius=0).grid(
            row=row, column=0, sticky="ew", padx=16, pady=2
        )
        return row + 1

    def _update_data_path_visibility(self):
        state = "normal" if self._deploy_var.get() == "standard" else "disabled"
        for w in self._data_path_widgets:
            try:
                w.configure(state=state)
            except Exception:
                pass

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

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
        def _worker():
            try:
                import requests
                from PIL import Image as PilImage
                import io

                self.after(0, lambda: self._image_status.configure(
                    text="Downloading image…", text_color=TEXT_WARN
                ))
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                img = PilImage.open(io.BytesIO(resp.content)).convert("RGBA")
                out = get_custom_game_images_dir() / f"{game_id}.png"
                img.save(out, "PNG")
                self.after(0, lambda: self._image_status.configure(
                    text="Image cached.", text_color=TEXT_OK
                ))
            except Exception as exc:
                self.after(0, lambda e=exc: self._image_status.configure(
                    text=f"Image download failed: {e}", text_color=TEXT_ERR
                ))

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

        name       = self._name_var.get().strip()
        exe        = self._exe_var.get().strip()
        deploy     = self._deploy_var.get()
        data_path  = self._data_path_var.get().strip() if deploy == "standard" else ""
        steam_id   = self._steam_var.get().strip()
        nexus      = self._nexus_var.get().strip()
        image_url  = self._image_url_var.get().strip()

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
            "heroic_app_names":               _str_to_list(self._heroic_var.get()),
            "restore_before_deploy":          self._restore_var.get(),
            "wine_dll_overrides":             _parse_dll_text(
                self._dll_textbox.get("0.0", "end")
            ),
        }

        save_custom_game_definition(defn)

        # Fire off image download if a URL was provided
        if image_url:
            self._download_image(image_url, game_id)

        self.saved_game = make_custom_game(defn)
        self.grab_release()
        self.destroy()

    def _on_delete(self):
        if self._existing is None:
            return
        game_id = self._existing.get("game_id", "")
        if game_id:
            delete_custom_game_definition(game_id)
            # Remove cached image if present
            img = get_custom_game_images_dir() / f"{game_id}.png"
            img.unlink(missing_ok=True)
        self.deleted = True
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()
