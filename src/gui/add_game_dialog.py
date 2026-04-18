"""
add_game_dialog.py
Modal dialog for locating and registering a game installation.

Scans all Steam library paths for the game's exe automatically,
with a manual folder-picker fallback via XDG portal or zenity.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

import customtkinter as ctk
import tkinter as tk

from Games.base_game import BaseGame
from Utils.portal_filechooser import pick_folder
from Utils.deploy import LinkMode
from Utils.xdg import xdg_open
from Utils.steam_finder import (
    find_steam_libraries,
    find_game_in_libraries,
    find_game_by_steam_id,
    find_prefix,
)
from Utils.config_paths import get_game_config_path
from Utils.heroic_finder import find_heroic_game, find_heroic_prefix, find_heroic_app_name_by_exe, find_heroic_game_info_by_exe
from Utils.app_log import app_log

from gui.theme import (
    BG_DEEP,
    BG_PANEL,
    BG_HEADER,
    BG_ROW,
    BG_HOVER,
    ACCENT,
    ACCENT_HOV,
    TEXT_MAIN,
    TEXT_DIM,
    TEXT_SEP,
    BORDER,
    TEXT_OK,
    TEXT_ERR,
    TEXT_WARN,
    RED_BTN,
    RED_HOV,
    FONT_NORMAL,
    FONT_BOLD,
    FONT_SMALL,
    FONT_MONO,
    scaled,
)
from Utils.ui_config import get_ui_scale, load_default_staging_path
from gui.tk_tooltip import TkTooltip


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_heroic_app_names(game: BaseGame) -> list[str]:
    """Get heroic app names from paths.json (handler heroic_app_names removed)."""
    names = list(getattr(game, "heroic_app_names", []) or [])
    if not names and hasattr(game, "name"):
        try:
            p = get_game_config_path(game.name)
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                saved = data.get("heroic_app_name", "").strip()
                if saved:
                    names = [saved]
        except (OSError, json.JSONDecodeError):
            pass
    return names


# ---------------------------------------------------------------------------
# ReconfigureGamePanel — canonical implementation
# ---------------------------------------------------------------------------

class ReconfigureGamePanel(ctk.CTkFrame):
    """
    Inline panel for reconfiguring a game's installation paths.

    Placed directly inside the main content area (replaces ModListPanel while
    open).  Calls ``on_done(panel)`` when the user saves, cancels, or removes
    the game instance.

    Usage (App):
        panel = ReconfigureGamePanel(parent_frame, game, on_done=self.hide_reconfigure_panel)
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
    """

    def __init__(self, parent, game: BaseGame, on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)

        self._game = game
        self._on_done = on_done or (lambda p: None)

        self._found_path: Optional[Path] = None
        self._found_prefix: Optional[Path] = None
        self._custom_staging: Optional[Path] = None
        self.result: Optional[Path] = None
        self.removed: bool = False
        _default_mode = getattr(game, "default_deploy_mode", "symlink")
        self._deploy_mode_var = tk.StringVar(value=_default_mode)
        self._symlink_plugins_var = tk.BooleanVar(value=False)
        self._auto_deploy_var = tk.BooleanVar(value=False)
        self._archive_invalidation_var = tk.BooleanVar(value=True)
        self._profile_ini_files_var = tk.BooleanVar(value=False)

        # Optional: when embedded in a modal CTkToplevel, set this to that
        # window so _run_folder_picker can release/re-acquire the grab.
        self._modal_host: Optional[ctk.CTkToplevel] = None

        self._build_ui()

        # If already configured, pre-populate all fields
        if game.is_configured():
            self._set_path(game.get_game_path(), status="configured")
            existing_pfx = game.get_prefix_path()
            if existing_pfx and existing_pfx.is_dir():
                self._set_prefix(existing_pfx, status="configured")
            elif game.steam_id:
                self._start_prefix_scan()
            elif _get_heroic_app_names(game):
                self._start_heroic_prefix_scan()
            if hasattr(game, "get_deploy_mode"):
                mode = game.get_deploy_mode()
                mode_mapped = LinkMode.SYMLINK if mode == LinkMode.COPY else mode
                self._deploy_mode_var.set({
                    LinkMode.SYMLINK: "symlink",
                }.get(mode_mapped, "hardlink"))
            if hasattr(game, "symlink_plugins"):
                self._symlink_plugins_var.set(game.symlink_plugins)
            if hasattr(game, "_staging_path") and game._staging_path is not None:
                self._custom_staging = game._staging_path
                self._set_staging(game._staging_path, status="configured")
            else:
                self._set_staging_text(str(game.get_mod_staging_path()))
            self._auto_deploy_var.set(game.auto_deploy)
            self._archive_invalidation_var.set(game.archive_invalidation)
            if hasattr(game, "profile_ini_files"):
                self._profile_ini_files_var.set(game.profile_ini_files)
        else:
            self._start_scan()
            default_root = load_default_staging_path()
            if default_root:
                preset = Path(default_root) / game.name
                self._set_staging(preset, status="found")
            else:
                self._set_staging_text(str(game.get_mod_staging_path()))

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=0)  # title bar
        self.grid_rowconfigure(1, weight=1)  # body
        self.grid_rowconfigure(2, weight=0)  # button bar
        self.grid_columnconfigure(0, weight=1)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Reconfigure Game — {self._game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w"
        ).pack(side="left", padx=12, pady=8)

        # Body
        _scroll = ctk.CTkScrollableFrame(
            self, fg_color=BG_PANEL, corner_radius=0,
            scrollbar_button_color=BG_HEADER,
            scrollbar_button_hover_color=ACCENT,
        )
        _scroll.grid(row=1, column=0, sticky="nsew")
        _scroll.grid_columnconfigure(0, weight=1)
        self._scroll_frame = _scroll

        body = ctk.CTkFrame(_scroll, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", pady=12)
        body.grid_columnconfigure(0, weight=1)

        # Forward scroll wheel — bind per-widget so buttons don't swallow events
        self.after(100, self._bind_scroll_recursive)

        self._help_tooltip = TkTooltip(
            self, bg="#1a1a2e", fg="#d6e4ff",
            font=(FONT_NORMAL[0], FONT_NORMAL[1]),
        )

        # --- Game path section ---
        self._make_section_header(
            body, row=0, title="Game Installation Folder",
            tooltip="The location of the game's root install folder",
        )

        self._status_label = ctk.CTkLabel(
            body, text="Scanning Steam libraries…",
            font=FONT_NORMAL, text_color=TEXT_WARN, anchor="w"
        )
        self._status_label.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 2))

        self._path_box = ctk.CTkTextbox(
            body, height=scaled(42), font=FONT_MONO,
            fg_color=BG_ROW, text_color=TEXT_MAIN,
            wrap="none", corner_radius=4
        )
        self._path_box.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 2))
        self._path_box.bind("<FocusOut>", lambda _e: self._on_path_typed())
        self._path_box.bind("<Return>", self._on_path_return)
        self._bind_select_all(self._path_box)

        _path_btn_frame = ctk.CTkFrame(body, fg_color="transparent")
        _path_btn_frame.grid(row=3, column=0, sticky="w", padx=16, pady=(0, 8))

        self._browse_btn = ctk.CTkButton(
            _path_btn_frame, text="Browse manually…", width=scaled(160), height=scaled(26),
            font=FONT_SMALL, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_browse
        )
        self._browse_btn.pack(side="left", padx=(0, 6))

        self._open_btn = ctk.CTkButton(
            _path_btn_frame, text="Open", width=scaled(70), height=scaled(26),
            font=FONT_SMALL, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_open_path, state="disabled"
        )
        self._open_btn.pack(side="left", padx=(0, 6))

        self._scan_btn = ctk.CTkButton(
            _path_btn_frame, text="Scan", width=scaled(70), height=scaled(26),
            font=FONT_SMALL, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_scan_drives
        )
        self._scan_btn.pack(side="left")

        # Divider
        ctk.CTkFrame(body, fg_color=BORDER, height=1, corner_radius=0).grid(
            row=4, column=0, sticky="ew", padx=16, pady=2
        )

        # --- Proton prefix section ---
        self._make_section_header(
            body, row=5, title="Proton Prefix (compatdata/pfx)",
            tooltip=(
                "The location of the prefix that the game uses, some files "
                "will deploy to this location and needs to be set. If the "
                "game is linux native then this is not needed"
            ),
            pady=(6, 2),
        )

        _has_prefix_source = bool(self._game.steam_id or _get_heroic_app_names(self._game))
        self._prefix_status_label = ctk.CTkLabel(
            body,
            text="Scanning for prefix…" if _has_prefix_source else "No launcher ID — prefix not applicable.",
            font=FONT_NORMAL,
            text_color=TEXT_WARN if _has_prefix_source else TEXT_DIM,
            anchor="w"
        )
        self._prefix_status_label.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 2))

        self._prefix_box = ctk.CTkTextbox(
            body, height=scaled(42), font=FONT_MONO,
            fg_color=BG_ROW, text_color=TEXT_MAIN,
            wrap="none", corner_radius=4
        )
        self._prefix_box.grid(row=7, column=0, sticky="ew", padx=16, pady=(0, 2))
        self._prefix_box_editable = _has_prefix_source
        if _has_prefix_source:
            self._prefix_box.bind("<FocusOut>", lambda _e: self._on_prefix_typed())
            self._prefix_box.bind("<Return>", self._on_prefix_return)
            self._bind_select_all(self._prefix_box)
        else:
            self._prefix_box.configure(state="disabled")

        _prefix_btn_frame = ctk.CTkFrame(body, fg_color="transparent")
        _prefix_btn_frame.grid(row=8, column=0, sticky="w", padx=16, pady=(0, 6))

        self._prefix_browse_btn = ctk.CTkButton(
            _prefix_btn_frame, text="Browse manually…", width=scaled(160), height=scaled(26),
            font=FONT_SMALL, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_browse_prefix,
            state="normal" if _has_prefix_source else "disabled"
        )
        self._prefix_browse_btn.pack(side="left", padx=(0, 6))

        self._prefix_open_btn = ctk.CTkButton(
            _prefix_btn_frame, text="Open", width=scaled(70), height=scaled(26),
            font=FONT_SMALL, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_open_prefix, state="disabled"
        )
        self._prefix_open_btn.pack(side="left")

        # Divider
        ctk.CTkFrame(body, fg_color=BORDER, height=1, corner_radius=0).grid(
            row=9, column=0, sticky="ew", padx=16, pady=2
        )

        # --- Mod Staging Folder section ---
        self._make_section_header(
            body, row=10, title="Mod Staging Folder",
            tooltip=(
                "The location where installed mods and profile settings "
                "are stored"
            ),
            pady=(6, 2),
        )

        self._staging_status_label = ctk.CTkLabel(
            body, text="Default location will be used.",
            font=FONT_NORMAL, text_color=TEXT_DIM, anchor="w"
        )
        self._staging_status_label.grid(row=11, column=0, sticky="ew", padx=16, pady=(0, 2))

        self._staging_box = ctk.CTkTextbox(
            body, height=scaled(42), font=FONT_MONO,
            fg_color=BG_ROW, text_color=TEXT_MAIN,
            wrap="none", corner_radius=4
        )
        self._staging_box.grid(row=12, column=0, sticky="ew", padx=16, pady=(0, 2))
        self._staging_box.bind("<FocusOut>", lambda _e: self._on_staging_typed())
        self._staging_box.bind("<Return>", self._on_staging_return)
        self._bind_select_all(self._staging_box)

        _staging_btn_frame = ctk.CTkFrame(body, fg_color="transparent")
        _staging_btn_frame.grid(row=13, column=0, sticky="w", padx=16, pady=(0, 6))

        ctk.CTkButton(
            _staging_btn_frame, text="Browse manually…", width=scaled(160), height=scaled(26),
            font=FONT_SMALL, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_browse_staging
        ).pack(side="left", padx=(0, 6))

        self._staging_open_btn = ctk.CTkButton(
            _staging_btn_frame, text="Open", width=scaled(70), height=scaled(26),
            font=FONT_SMALL, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_open_staging
        )
        self._staging_open_btn.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            _staging_btn_frame, text="Reset to default", width=scaled(130), height=scaled(26),
            font=FONT_SMALL, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_reset_staging
        ).pack(side="left")

        # Divider
        ctk.CTkFrame(body, fg_color=BORDER, height=1, corner_radius=0).grid(
            row=14, column=0, sticky="ew", padx=16, pady=2
        )

        # --- Deploy method section ---
        ctk.CTkLabel(
            body, text="Deploy Method",
            font=FONT_BOLD, text_color=TEXT_SEP, anchor="w"
        ).grid(row=15, column=0, sticky="ew", padx=16, pady=(6, 4))

        _deploy_row = ctk.CTkFrame(body, fg_color="transparent")
        _deploy_row.grid(row=16, column=0, sticky="w", padx=16, pady=(0, 10))

        _rec_mode = getattr(self._game, "default_deploy_mode", "symlink")
        _mode_options = [
            ("Symlink (Recommended)" if _rec_mode == "symlink" else "Symlink", "symlink"),
            ("Hardlink (Recommended)" if _rec_mode == "hardlink" else "Hardlink", "hardlink"),
        ]
        for label, value in _mode_options:
            ctk.CTkRadioButton(
                _deploy_row, text=label,
                variable=self._deploy_mode_var, value=value,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
            ).pack(side="left", padx=(0, 20))

        if hasattr(self._game, "symlink_plugins"):
            ctk.CTkCheckBox(
                body, text="Symlink plugin files (.esp / .esm / .esl)",
                variable=self._symlink_plugins_var,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
            ).grid(row=17, column=0, sticky="w", padx=16, pady=(0, 8))

        ctk.CTkCheckBox(
            body, text="Auto deploy (automatically deploy when a mod is enabled, disabled, or reordered)",
            variable=self._auto_deploy_var,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
        ).grid(row=18, column=0, sticky="w", padx=16, pady=(0, 8))

        if hasattr(self._game, "archive_invalidation_enabled"):
            ctk.CTkCheckBox(
                body, text="Automatic archive invalidation (allow the game to prefer loose files over BSA archives)",
                variable=self._archive_invalidation_var,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
            ).grid(row=19, column=0, sticky="w", padx=16, pady=(0, 8))

        if hasattr(self._game, "profile_ini_files"):
            ctk.CTkCheckBox(
                body, text="Use profile-specific INI files (placed in profile folder, symlinked to My Games on deploy)",
                variable=self._profile_ini_files_var,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
            ).grid(row=20, column=0, sticky="w", padx=16, pady=(0, 8))

        # Button bar
        btn_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        btn_bar.grid(row=2, column=0, sticky="ew")
        btn_bar.grid_propagate(False)
        ctk.CTkFrame(btn_bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )

        self._cancel_btn = ctk.CTkButton(
            btn_bar, text="Cancel", width=scaled(100), height=scaled(30), font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel
        )
        self._cancel_btn.pack(side="right", padx=(4, 12), pady=10)

        self._add_btn = ctk.CTkButton(
            btn_bar, text="Save", width=scaled(110), height=scaled(30), font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            state="disabled", command=self._on_add
        )
        self._add_btn.pack(side="right", padx=4, pady=10)

        if self._game.is_configured():
            self._remove_btn = ctk.CTkButton(
                btn_bar, text="Remove Instance", width=scaled(140), height=scaled(30),
                font=FONT_BOLD, fg_color=RED_BTN, hover_color=RED_HOV,
                text_color="white", command=self._on_remove
            )
            self._remove_btn.pack(side="left", padx=(12, 4), pady=10)

            self._clean_btn = ctk.CTkButton(
                btn_bar, text="Clean Game Folder", width=scaled(150), height=scaled(30),
                font=FONT_NORMAL, fg_color=RED_BTN, hover_color=RED_HOV,
                text_color="white", command=self._on_clean_game_folder
            )
            self._clean_btn.pack(side="left", padx=(0, 4), pady=10)

    def _make_section_header(self, parent, *, row: int, title: str, tooltip: str,
                              pady: tuple = (10, 2)) -> None:
        """Section-title label with a blue help marker that shows *tooltip* on hover."""
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=row, column=0, sticky="ew", padx=16, pady=pady)

        ctk.CTkLabel(
            header, text=title,
            font=FONT_BOLD, text_color=TEXT_SEP, anchor="w"
        ).pack(side="left")

        help_marker = ctk.CTkLabel(
            header, text="?", font=FONT_BOLD, text_color=ACCENT,
            width=scaled(16), cursor="question_arrow"
        )
        help_marker.pack(side="left", padx=(6, 0))
        self._help_tooltip.attach(
            help_marker, tooltip,
            offset_x=scaled(12), offset_y=scaled(12),
        )

    def _bind_scroll_recursive(self, widget=None):
        """Bind Linux scroll-wheel events to every child widget so buttons don't swallow them."""
        if widget is None:
            widget = self._scroll_frame
        try:
            widget.bind("<Button-4>", lambda e=None: self._scroll_frame._parent_canvas.yview_scroll(-3, "units"), add="+")
            widget.bind("<Button-5>", lambda e=None: self._scroll_frame._parent_canvas.yview_scroll( 3, "units"), add="+")
            widget.bind("<MouseWheel>", lambda e=None: self._scroll_frame._parent_canvas.yview_scroll(
                -3 if (getattr(e, "delta", 0) or 0) > 0 else 3, "units"), add="+")
        except Exception:
            pass
        for child in widget.winfo_children():
            self._bind_scroll_recursive(child)

    # ------------------------------------------------------------------
    # Steam / prefix scan workers
    # ------------------------------------------------------------------

    def _start_scan(self):
        self._status_label.configure(text="Scanning Steam libraries…", text_color=TEXT_WARN)
        self._add_btn.configure(state="disabled")
        self._set_path_text("")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        found: Optional[Path] = None
        source = "steam"
        discovered_app_name: Optional[str] = None
        found_prefix: Optional[Path] = None

        game_name = getattr(self._game, "name", repr(self._game))
        app_log(f"[Add Game] Auto-detecting: {game_name}")

        # Heroic first: exe -> installed.json -> appname + path -> GamesConfig/<appname>.json -> prefix
        # Ensures we get both path and prefix when the game is installed via Heroic.
        exe_names = [getattr(self._game, "exe_name", None)]
        exe_names += getattr(self._game, "exe_name_alts", [])
        exe_names = [e for e in exe_names if e]
        app_log(f"[Add Game] Checking Heroic (exe names: {exe_names})")
        for exe_name in exe_names:
            info = find_heroic_game_info_by_exe(exe_name)
            if info:
                found, found_prefix, discovered_app_name = info
                source = "heroic"
                app_log(f"[Add Game] Found via Heroic exe scan ({exe_name}): {found}")
                break
        else:
            app_log(f"[Add Game] Not found via Heroic exe scan")

        if not found and _get_heroic_app_names(self._game):
            heroic_names = _get_heroic_app_names(self._game)
            app_log(f"[Add Game] Checking Heroic app names: {heroic_names}")
            found = find_heroic_game(heroic_names)
            if found:
                source = "heroic"
                app_log(f"[Add Game] Found via Heroic app name: {found}")
            else:
                app_log(f"[Add Game] Not found via Heroic app names")

        if not found:
            libraries = find_steam_libraries()
            app_log(f"[Add Game] Steam libraries found: {libraries if libraries else 'none'}")
            steam_id = getattr(self._game, "steam_id", None)
            if steam_id:
                app_log(f"[Add Game] Checking Steam manifest (app ID: {steam_id}, exe: {self._game.exe_name})")
                for _exe in exe_names:
                    found = find_game_by_steam_id(libraries, steam_id, _exe)
                    if found:
                        app_log(f"[Add Game] Found via Steam app manifest ({_exe}): {found}")
                        break
                else:
                    app_log(
                        f"[Add Game] Not found via Steam app manifest — "
                        f"checked {len(libraries)} library/libraries for appmanifest_{steam_id}.acf"
                    )
            else:
                app_log(f"[Add Game] No Steam app ID configured for this game")
            if not found:
                app_log(f"[Add Game] Falling back to exe scan across Steam libraries")
                for exe_name in exe_names:
                    found = find_game_in_libraries(libraries, exe_name)
                    if found:
                        app_log(f"[Add Game] Found via Steam exe scan ({exe_name}): {found}")
                        break
                else:
                    app_log(f"[Add Game] Not found via Steam exe scan (tried: {exe_names})")

        if not found:
            app_log(f"[Add Game] Game location not auto-detected for: {game_name}")

        try:
            if self.winfo_exists():
                self.after(0, lambda: self._on_scan_complete(found, source, discovered_app_name, found_prefix))
        except Exception:
            pass

    def _on_scan_complete(self, found: Optional[Path], source: str = "steam", discovered_app_name: Optional[str] = None, found_prefix: Optional[Path] = None):
        if discovered_app_name and hasattr(self._game, "set_heroic_app_name"):
            self._game.set_heroic_app_name(discovered_app_name)
        if found:
            self._set_path(found, status="found", source=source)
            if found_prefix is not None:
                self._found_prefix = found_prefix
                self._set_prefix_text(str(found_prefix))
                self._prefix_status_label.configure(
                    text="Found via Heroic Games Launcher.",
                    text_color=TEXT_OK
                )
                if hasattr(self, "_prefix_open_btn"):
                    self._prefix_open_btn.configure(state="normal")
        else:
            self._status_label.configure(
                text="Not found automatically. Browse manually to locate the game folder.",
                text_color=TEXT_ERR
            )
            self._set_path_text("")
            self._add_btn.configure(state="disabled")
        if self._found_prefix is not None:
            pass
        elif self._game.steam_id:
            self._start_prefix_scan()
        elif _get_heroic_app_names(self._game):
            self._start_heroic_prefix_scan()

    def _start_prefix_scan(self):
        self._prefix_status_label.configure(
            text="Scanning for Proton prefix…", text_color=TEXT_WARN
        )
        self._set_prefix_text("")
        threading.Thread(target=self._prefix_scan_worker, daemon=True).start()

    def _prefix_scan_worker(self):
        steam_id = self._game.steam_id
        game_name = getattr(self._game, "name", repr(self._game))
        app_log(f"[Add Game] Scanning for Proton prefix (app ID: {steam_id})")
        found = find_prefix(steam_id)
        if found:
            app_log(f"[Add Game] Proton prefix found: {found}")
        else:
            from Utils.steam_finder import _STEAM_CANDIDATES
            checked = [
                str(root / "steamapps" / "compatdata" / steam_id / "pfx")
                for root in _STEAM_CANDIDATES
            ]
            app_log(
                f"[Add Game] Proton prefix not found for {game_name} (app ID: {steam_id}). "
                f"Checked: {checked}"
            )
        try:
            if self.winfo_exists():
                self.after(0, lambda: self._on_prefix_scan_complete(found))
        except Exception:
            pass

    def _on_prefix_scan_complete(self, found: Optional[Path]):
        if found:
            self._set_prefix(found, status="found")
        else:
            self._prefix_status_label.configure(
                text="Prefix not found automatically. Not needed if game is Linux native",
                text_color=TEXT_WARN
            )

    def _start_heroic_prefix_scan(self):
        self._prefix_status_label.configure(
            text="Scanning for Heroic Wine prefix…", text_color=TEXT_WARN
        )
        self._set_prefix_text("")
        threading.Thread(target=self._heroic_prefix_scan_worker, daemon=True).start()

    def _heroic_prefix_scan_worker(self):
        found = find_heroic_prefix(_get_heroic_app_names(self._game))
        try:
            if self.winfo_exists():
                self.after(0, lambda: self._on_heroic_prefix_scan_complete(found))
        except Exception:
            pass

    def _on_heroic_prefix_scan_complete(self, found: Optional[Path]):
        if found:
            self._found_prefix = found
            self._set_prefix_text(str(found))
            self._prefix_status_label.configure(
                text="Found via Heroic Games Launcher.",
                text_color=TEXT_OK
            )
        else:
            self._prefix_status_label.configure(
                text="Prefix not found automatically. Not needed if game is Linux native.",
                text_color=TEXT_WARN
            )

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _set_path(self, path: Path, status: str = "found", source: str = "steam"):
        self._found_path = path
        self._set_path_text(str(path))
        if status == "configured":
            self._status_label.configure(
                text="Game already configured. You can update the path below.",
                text_color=TEXT_OK
            )
        elif source == "heroic":
            self._status_label.configure(
                text="Found via Heroic Games Launcher.",
                text_color=TEXT_OK
            )
        else:
            self._status_label.configure(
                text="Found via Steam libraries.",
                text_color=TEXT_OK
            )
        self._add_btn.configure(state="normal")
        self._open_btn.configure(state="normal")

    def _set_path_text(self, text: str):
        self._path_box.delete("1.0", "end")
        if text:
            self._path_box.insert("end", text)

    def _set_prefix(self, path: Path, status: str = "found"):
        self._found_prefix = path
        self._set_prefix_text(str(path))
        if status == "configured":
            self._prefix_status_label.configure(
                text="Prefix already configured. You can update the path below.",
                text_color=TEXT_OK
            )
        else:
            self._prefix_status_label.configure(
                text="Found via Steam compatdata.",
                text_color=TEXT_OK
            )
        self._prefix_open_btn.configure(state="normal")

    def _set_prefix_text(self, text: str):
        _editable = getattr(self, "_prefix_box_editable", True)
        if not _editable:
            self._prefix_box.configure(state="normal")
        self._prefix_box.delete("1.0", "end")
        if text:
            self._prefix_box.insert("end", text)
        if not _editable:
            self._prefix_box.configure(state="disabled")

    def _set_staging(self, path: Path, status: str = "found"):
        self._custom_staging = path
        self._set_staging_text(str(path))
        if status == "configured":
            self._staging_status_label.configure(
                text="Custom staging folder already configured.",
                text_color=TEXT_OK
            )
        else:
            self._staging_status_label.configure(
                text="Custom staging folder selected.",
                text_color=TEXT_OK
            )

    def _set_staging_text(self, text: str):
        self._staging_box.delete("1.0", "end")
        if text:
            self._staging_box.insert("end", text)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _run_folder_picker(self, title: str, callback):
        """Run a folder picker in a background thread and call callback on the main thread.

        When ``_modal_host`` is set (i.e. this panel is embedded inside a modal
        CTkToplevel), the grab is released before the picker opens and
        re-acquired once it closes — without this, the modal grab blocks X11
        and freezes the desktop.
        """
        host = self._modal_host
        if host is not None:
            try:
                host.grab_release()
            except Exception:
                pass

        def _on_picked(chosen: Optional[Path]) -> None:
            def _finish():
                if host is not None:
                    try:
                        host.grab_set()
                    except Exception:
                        pass
                callback(chosen)
            self.after(0, _finish)

        pick_folder(title, _on_picked)

    def _on_browse(self):
        def _apply(chosen: Optional[Path]):
            if not self._status_label.winfo_exists():
                return
            if chosen:
                # Verify the game exe is present in the chosen folder
                all_exes = [self._game.exe_name] + list(self._game.exe_name_alts)
                found_exe = any(
                    (chosen / exe).is_file()
                    for exe in all_exes
                    if exe
                )
                if not found_exe:
                    exe_list = ", ".join(e for e in all_exes if e)
                    self._status_label.configure(
                        text=f"Game executable not found in that folder ({exe_list}).",
                        text_color=TEXT_ERR
                    )
                    return
                self._set_path(chosen, status="found")
                self._status_label.configure(
                    text="Folder selected manually.", text_color=TEXT_OK
                )
            else:
                self._status_label.configure(
                    text="No folder selected or folder picker unavailable.",
                    text_color=TEXT_WARN
                )
        self._run_folder_picker(
            f"Select {self._game.name} installation folder", _apply
        )

    def _on_scan_drives(self):
        """Scan all mounted drives for the game exe, stopping at first match."""
        exe_names = [getattr(self._game, "exe_name", None)]
        exe_names += list(getattr(self._game, "exe_name_alts", []))
        exe_names = [e for e in exe_names if e]
        if not exe_names:
            self._status_label.configure(
                text="No executable name configured for this game.", text_color=TEXT_ERR
            )
            return

        self._status_label.configure(text="Scanning all drives…", text_color=TEXT_WARN)
        self._scan_btn.configure(state="disabled")
        self._browse_btn.configure(state="disabled")

        def _worker():
            import concurrent.futures

            # Collect mount points from /proc/mounts, skip pseudo/system filesystems
            skip_types = {"sysfs", "proc", "devtmpfs", "devpts", "tmpfs", "cgroup",
                          "cgroup2", "pstore", "bpf", "tracefs", "debugfs",
                          "securityfs", "fusectl", "hugetlbfs", "mqueue", "configfs",
                          "efivarfs", "overlay", "squashfs"}
            skip_dirs = {"proc", "sys", "dev", "run", "snap"}
            roots: list[Path] = []
            try:
                with open("/proc/mounts", "r") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) < 3:
                            continue
                        fstype = parts[2]
                        mountpoint = parts[1]
                        if fstype in skip_types:
                            continue
                        p = Path(mountpoint)
                        if p == Path("/"):
                            roots.insert(0, p)  # scan root first
                        else:
                            roots.append(p)
            except OSError:
                roots = [Path("/")]

            # Build list of top-level subdirs to scan in parallel
            exe_set = set(exe_names)
            stop_event = threading.Event()

            def _scan_subtree(start: Path) -> Optional[Path]:
                for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
                    if stop_event.is_set():
                        return None
                    dirnames[:] = [d for d in dirnames if d not in skip_dirs]
                    if exe_set & set(filenames):
                        return Path(dirpath)
                return None

            # Collect scan roots: for each mount, use its immediate subdirs so
            # we can fan out across many workers instead of one serial walk.
            scan_roots: list[Path] = []
            for root in roots:
                try:
                    children = [p for p in root.iterdir() if p.is_dir() and p.name not in skip_dirs]
                    scan_roots.extend(children)
                except PermissionError:
                    pass

            found: Optional[Path] = None
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(_scan_subtree, sr): sr for sr in scan_roots}
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    if result is not None:
                        found = result
                        stop_event.set()
                        break

            try:
                if self.winfo_exists():
                    self.after(0, lambda f=found: _done(f))
            except Exception:
                pass

        def _done(found: Optional[Path]):
            try:
                self._scan_btn.configure(state="normal")
                self._browse_btn.configure(state="normal")
                if not self._status_label.winfo_exists():
                    return
                if found:
                    self._set_path(found, status="found")
                    self._status_label.configure(
                        text="Found via drive scan.", text_color=TEXT_OK
                    )
                else:
                    self._status_label.configure(
                        text="Game executable not found on any drive.", text_color=TEXT_ERR
                    )
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_browse_prefix(self):
        def _apply(chosen: Optional[Path]):
            if not self._prefix_status_label.winfo_exists():
                return
            if chosen:
                if chosen.name.lower() != "pfx" and (chosen / "pfx").is_dir():
                    chosen = chosen / "pfx"
                if chosen.name.lower() != "pfx":
                    self._prefix_status_label.configure(
                        text="Selected folder must be a pfx folder or contain one.",
                        text_color=TEXT_ERR
                    )
                    return
                self._set_prefix(chosen, status="found")
                self._prefix_status_label.configure(
                    text="Prefix folder selected manually.", text_color=TEXT_OK
                )
            else:
                self._prefix_status_label.configure(
                    text="No folder selected or folder picker unavailable.",
                    text_color=TEXT_WARN
                )
        self._run_folder_picker(
            f"Select Proton prefix folder (pfx/) for {self._game.name}", _apply
        )

    def _on_browse_staging(self):
        def _apply(chosen: Optional[Path]):
            if not self._staging_status_label.winfo_exists():
                return
            if chosen:
                chosen = chosen / self._game.game_id
                self._set_staging(chosen, status="found")
            else:
                self._staging_status_label.configure(
                    text="No folder selected or folder picker unavailable.",
                    text_color=TEXT_WARN
                )
        self._run_folder_picker(
            f"Select mod staging folder for {self._game.name}", _apply
        )

    def _read_box(self, box: ctk.CTkTextbox) -> str:
        return box.get("1.0", "end").strip().replace("\n", "")

    def _bind_select_all(self, box: ctk.CTkTextbox) -> None:
        def _select_all(_event=None):
            box.tag_add("sel", "1.0", "end-1c")
            box.mark_set("insert", "end-1c")
            return "break"
        box.bind("<Control-a>", _select_all)
        box.bind("<Control-A>", _select_all)

    def _on_path_return(self, _event=None):
        self._on_path_typed()
        return "break"

    def _on_path_typed(self):
        if not self._status_label.winfo_exists():
            return
        raw = self._read_box(self._path_box)
        if not raw:
            self._found_path = None
            self._status_label.configure(
                text="No folder entered.", text_color=TEXT_WARN
            )
            self._add_btn.configure(state="disabled")
            self._open_btn.configure(state="disabled")
            return
        if raw == str(self._found_path or ""):
            return
        chosen = Path(os.path.expanduser(raw))
        if not chosen.is_dir():
            self._status_label.configure(
                text="That folder does not exist.", text_color=TEXT_ERR
            )
            self._add_btn.configure(state="disabled")
            self._open_btn.configure(state="disabled")
            return
        all_exes = [self._game.exe_name] + list(self._game.exe_name_alts)
        found_exe = any((chosen / exe).is_file() for exe in all_exes if exe)
        if not found_exe:
            exe_list = ", ".join(e for e in all_exes if e)
            self._status_label.configure(
                text=f"Game executable not found in that folder ({exe_list}).",
                text_color=TEXT_ERR
            )
            self._add_btn.configure(state="disabled")
            self._open_btn.configure(state="disabled")
            return
        self._set_path(chosen, status="found")
        self._status_label.configure(
            text="Folder entered manually.", text_color=TEXT_OK
        )

    def _on_prefix_return(self, _event=None):
        self._on_prefix_typed()
        return "break"

    def _on_prefix_typed(self):
        if not self._prefix_status_label.winfo_exists():
            return
        raw = self._read_box(self._prefix_box)
        if not raw:
            self._found_prefix = None
            self._prefix_status_label.configure(
                text="No prefix entered.", text_color=TEXT_WARN
            )
            self._prefix_open_btn.configure(state="disabled")
            return
        if raw == str(self._found_prefix or ""):
            return
        chosen = Path(os.path.expanduser(raw))
        if not chosen.is_dir():
            self._prefix_status_label.configure(
                text="That folder does not exist.", text_color=TEXT_ERR
            )
            self._prefix_open_btn.configure(state="disabled")
            return
        if chosen.name.lower() != "pfx" and (chosen / "pfx").is_dir():
            chosen = chosen / "pfx"
            self._set_prefix_text(str(chosen))
        if chosen.name.lower() != "pfx":
            self._prefix_status_label.configure(
                text="Selected folder must be a pfx folder or contain one.",
                text_color=TEXT_ERR
            )
            self._prefix_open_btn.configure(state="disabled")
            return
        self._set_prefix(chosen, status="found")
        self._prefix_status_label.configure(
            text="Prefix folder entered manually.", text_color=TEXT_OK
        )

    def _on_staging_return(self, _event=None):
        self._on_staging_typed()
        return "break"

    def _on_staging_typed(self):
        if not self._staging_status_label.winfo_exists():
            return
        raw = self._read_box(self._staging_box)
        if not raw:
            self._custom_staging = None
            self._staging_status_label.configure(
                text="Default location will be used.", text_color=TEXT_DIM
            )
            return
        chosen = Path(os.path.expanduser(raw))
        if chosen.name == "mods":
            chosen = chosen.parent
        from Utils.config_paths import get_profiles_dir
        default_root = get_profiles_dir() / self._game.name
        if chosen == default_root:
            self._custom_staging = None
            self._staging_status_label.configure(
                text="Default location will be used.", text_color=TEXT_DIM
            )
            return
        self._custom_staging = chosen
        self._staging_status_label.configure(
            text="Custom staging folder entered manually.", text_color=TEXT_OK
        )

    def _on_open_path(self):
        if self._found_path:
            xdg_open(self._found_path)

    def _on_open_prefix(self):
        if self._found_prefix:
            xdg_open(self._found_prefix)

    def _on_open_staging(self):
        path = self._custom_staging or self._game.get_mod_staging_path()
        xdg_open(path)

    def _on_reset_staging(self):
        self._custom_staging = None
        from Utils.config_paths import get_profiles_dir
        default_path = get_profiles_dir() / self._game.name / "mods"
        self._set_staging_text(str(default_path))
        self._staging_status_label.configure(
            text="Default location will be used.", text_color=TEXT_DIM
        )

    def _on_remove(self):
        from Utils.config_paths import get_game_config_path
        from Utils.deploy import restore_root_folder

        profile_root = self._game.get_profile_root()
        paths_json = get_game_config_path(self._game.name)

        lines = [
            f"Removes the instance configuration for {self._game.name}.\n",
            f"Deleted:\n",
            f"  • Game configuration ({paths_json.name})\n",
            f"  • Generated caches (filemap, modindex, etc.)\n",
            f"  • The game will be restored to its vanilla state\n",
            f"\nKept (your data is safe):\n",
            f"  • Mods folder:  {profile_root / 'mods'}\n",
            f"  • Profiles (modlist, plugins):  {profile_root / 'profiles'}\n",
            f"  • Overwrite:  {profile_root / 'overwrite'}\n",
            f"\nThis action cannot be undone. Continue?",
        ]
        msg = "".join(lines)

        confirm = _RemoveConfirmDialog(self.winfo_toplevel(), self._game.name, msg)
        self.winfo_toplevel().wait_window(confirm)
        if not confirm.confirmed:
            return

        try:
            if hasattr(self._game, "restore"):
                self._game.restore()
        except Exception:
            pass

        try:
            root_folder_dir = profile_root / "Root_Folder"
            game_root = self._game.get_game_path()
            if root_folder_dir.is_dir() and game_root:
                restore_root_folder(root_folder_dir, game_root)
        except Exception:
            pass

        _KEEP = {"mods", "profiles", "overwrite"}
        if profile_root.is_dir():
            for child in profile_root.iterdir():
                if child.name in _KEEP:
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)

        if paths_json.is_file():
            paths_json.unlink(missing_ok=True)
            try:
                paths_json.parent.rmdir()
            except OSError:
                pass

        self.result = None
        self.removed = True
        self._on_done(self)

    def _on_clean_game_folder(self):
        game_path = self._game.get_game_path()
        if not game_path:
            return

        target_dir = game_path
        if hasattr(self._game, "get_mod_data_path"):
            data_path = self._game.get_mod_data_path()
            if data_path and data_path != game_path:
                target_dir = data_path

        if not target_dir or not target_dir.is_dir():
            return

        confirm = _CleanGameFolderDialog(self.winfo_toplevel(), self._game.name, target_dir)
        self.winfo_toplevel().wait_window(confirm)
        if not confirm.confirmed:
            return

        from Utils.deploy import remove_deployed_files, restore_filemap_from_root
        removed = 0

        if hasattr(self._game, "get_effective_filemap_path"):
            try:
                filemap_path = self._game.get_effective_filemap_path()
                removed += restore_filemap_from_root(filemap_path, target_dir, move_runtime_files=False)
            except Exception:
                pass

        removed += remove_deployed_files(target_dir)

        if hasattr(self._game, "post_clean_game_folder"):
            self._game.post_clean_game_folder()

        self._status_label.configure(
            text=f"Clean complete — {removed} deployed file(s) removed.",
            text_color=TEXT_OK,
        )

    def _on_add(self):
        if self._found_path is None:
            return

        # -- Hard-link cross-device validation --------------------------------
        mode_str = self._deploy_mode_var.get()
        if mode_str == "hardlink":
            # Temporarily set paths so the game object can resolve targets
            self._game.set_game_path(self._found_path)
            if self._found_prefix is not None:
                self._game.set_prefix_path(self._found_prefix)
            if hasattr(self._game, "set_staging_path"):
                self._game.set_staging_path(self._custom_staging)

            staging = self._game.get_mod_staging_path()
            staging_anchor = staging if staging.exists() else staging.parent
            try:
                staging_dev = os.stat(staging_anchor).st_dev
            except OSError:
                staging_dev = None

            if staging_dev is not None:
                targets = self._game.get_hardlink_deploy_targets()
                mismatched: list[str] = []
                for label, path in targets:
                    if path is None:
                        continue
                    try:
                        if os.stat(path).st_dev != staging_dev:
                            mismatched.append(label)
                    except OSError:
                        continue

                if mismatched:
                    names = " and ".join(mismatched)
                    self._status_label.configure(
                        text=(
                            f"Cannot use hardlinks: the staging folder and "
                            f"{names} are on different drives. "
                            f"Switch to Symlink instead."
                        ),
                        text_color=TEXT_ERR,
                    )
                    return
        # ---------------------------------------------------------------------

        self._game.set_game_path(self._found_path)
        if self._found_prefix is not None:
            self._game.set_prefix_path(self._found_prefix)
        if hasattr(self._game, "set_deploy_mode"):
            mode = {
                "symlink": LinkMode.SYMLINK,
                "copy":    LinkMode.SYMLINK,
            }.get(mode_str, LinkMode.HARDLINK)
            self._game.set_deploy_mode(mode)
        if hasattr(self._game, "set_symlink_plugins"):
            self._game.set_symlink_plugins(self._symlink_plugins_var.get())
        if hasattr(self._game, "set_staging_path"):
            self._game.set_staging_path(self._custom_staging)
        self._game.auto_deploy = self._auto_deploy_var.get()
        self._game.archive_invalidation = self._archive_invalidation_var.get()
        if hasattr(self._game, "set_profile_ini_files"):
            self._game.set_profile_ini_files(self._profile_ini_files_var.get())
        _create_profile_structure(self._game)
        self.result = self._found_path

        components = list(getattr(self._game, "winetricks_components", []))
        prefix = self._game.get_prefix_path() if hasattr(self._game, "get_prefix_path") else None
        if components and prefix and Path(prefix).is_dir():
            def _install_components():
                from Utils.protontricks import _install_via_winetricks
                for comp in components:
                    app_log(f"{self._game.name}: installing {comp} via winetricks …")
                    ok = _install_via_winetricks(Path(prefix), comp, app_log)
                    if not ok:
                        app_log(f"{self._game.name}: {comp} install failed (see log above).")
            threading.Thread(target=_install_components, daemon=True).start()

        self._on_done(self)

    def _on_cancel(self):
        self.result = None
        self._on_done(self)


# ---------------------------------------------------------------------------
# AddGameDialog — thin CTkToplevel wrapper around ReconfigureGamePanel
# ---------------------------------------------------------------------------

class AddGameDialog(ctk.CTkToplevel):
    """
    Modal dialog that locates a game on disk and saves its path.

    Usage:
        dialog = AddGameDialog(parent, game)
        parent.wait_window(dialog)
        if dialog.result:
            print(f"Configured: {dialog.result}")
    """

    WIDTH  = 700
    HEIGHT = 620

    def __init__(self, parent, game: BaseGame):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"Reconfigure Game — {game.name}")
        s = get_ui_scale()
        self.geometry(f"{round(self.WIDTH * s)}x{round(self.HEIGHT * s)}")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._panel = ReconfigureGamePanel(self, game, on_done=self._on_panel_done)
        self._panel.grid(row=0, column=0, sticky="nsew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Let the panel release/re-acquire our grab around the folder picker
        self._panel._modal_host = self

        # Defer grab_set until the window is fully rendered
        self.after(100, self._make_modal)

    @property
    def result(self) -> Optional[Path]:
        return self._panel.result

    @property
    def removed(self) -> bool:
        return self._panel.removed

    def _make_modal(self):
        """Grab input focus once the window is viewable."""
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_panel_done(self, panel):
        """Called by the embedded panel when the user saves, cancels, or removes."""
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        """Called by WM_DELETE_WINDOW protocol."""
        self._panel.result = None
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Remove-confirmation dialog
# ---------------------------------------------------------------------------

class _RemoveConfirmDialog(ctk.CTkToplevel):
    """Modal yes/no dialog warning the user before removing a game instance."""

    WIDTH  = 480
    HEIGHT = 360

    def __init__(self, parent, game_name: str, message: str):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"Remove {game_name}?")
        s = get_ui_scale()
        self.geometry(f"{round(self.WIDTH * s)}x{round(self.HEIGHT * s)}")
        self.resizable(False, False)
        self.transient(parent)
        self.confirmed = False

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color=RED_BTN, corner_radius=0, height=40)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        ctk.CTkLabel(
            header, text=f"Remove {game_name}?",
            font=FONT_BOLD, text_color="white", anchor="w"
        ).pack(side="left", padx=12, pady=8)

        # Body
        body = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        msg_label = ctk.CTkLabel(
            body, text=message,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            anchor="nw", justify="left", wraplength=scaled(self.WIDTH - 40)
        )
        msg_label.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)

        # Button bar
        btn_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        btn_bar.grid(row=2, column=0, sticky="ew")
        btn_bar.grid_propagate(False)
        ctk.CTkFrame(btn_bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )

        ctk.CTkButton(
            btn_bar, text="Cancel", width=scaled(100), height=scaled(30), font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._cancel
        ).pack(side="right", padx=(4, 12), pady=10)

        ctk.CTkButton(
            btn_bar, text="Remove", width=scaled(110), height=scaled(30), font=FONT_BOLD,
            fg_color=RED_BTN, hover_color=RED_HOV, text_color="white",
            command=self._confirm
        ).pack(side="right", padx=4, pady=10)

        self.after(100, self._make_modal)

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _confirm(self):
        self.confirmed = True
        self.grab_release()
        self.destroy()

    def _cancel(self):
        self.confirmed = False
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Clean-game-folder confirmation dialog
# ---------------------------------------------------------------------------

class _CleanGameFolderDialog(ctk.CTkToplevel):
    """Warn the user before removing all hardlinked/symlinked files from the
    game directory.  This is a recovery tool — not part of the normal workflow."""

    WIDTH  = 500
    HEIGHT = 380

    def __init__(self, parent, game_name: str, target_dir):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"Clean Game Folder — {game_name}")
        s = get_ui_scale()
        self.geometry(f"{round(self.WIDTH * s)}x{round(self.HEIGHT * s)}")
        self.resizable(False, False)
        self.transient(parent)
        self.confirmed = False

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color=RED_BTN, corner_radius=0, height=40)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        ctk.CTkLabel(
            header, text="Clean Game Folder",
            font=FONT_BOLD, text_color="white", anchor="w"
        ).pack(side="left", padx=12, pady=8)

        # Body
        body = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        message = (
            "This is an emergency recovery tool.\n\n"
            "It will:\n"
            "  1. Delete every hardlinked or symlinked file from the game folder "
            "(mod-placed files), leaving vanilla files untouched.\n"
            "  2. Rename any vanilla backup folder back to its original name "
            "(e.g. Data_Core → Data).\n"
            "  3. Remove empty directories left behind.\n\n"
            f"Target folder:\n  {target_dir}\n\n"
            "Only use this if the normal Restore button cannot run "
            "(e.g. your profile was lost or deleted).  Continue?"
        )

        ctk.CTkLabel(
            body, text=message,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            anchor="nw", justify="left", wraplength=scaled(self.WIDTH - 40)
        ).grid(row=0, column=0, sticky="nsew", padx=16, pady=16)

        # Button bar
        btn_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        btn_bar.grid(row=2, column=0, sticky="ew")
        btn_bar.grid_propagate(False)
        ctk.CTkFrame(btn_bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )

        ctk.CTkButton(
            btn_bar, text="Cancel", width=scaled(100), height=scaled(30), font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._cancel
        ).pack(side="right", padx=(4, 12), pady=10)

        ctk.CTkButton(
            btn_bar, text="Clean Folder", width=scaled(120), height=scaled(30), font=FONT_BOLD,
            fg_color=RED_BTN, hover_color=RED_HOV, text_color="white",
            command=self._confirm
        ).pack(side="right", padx=4, pady=10)

        self.after(100, self._make_modal)

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _confirm(self):
        self.confirmed = True
        self.grab_release()
        self.destroy()

    def _cancel(self):
        self.confirmed = False
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Profile folder helper
# ---------------------------------------------------------------------------

def sync_modlist_with_mods_folder(modlist_path: Path, mods_dir: Path) -> None:
    """
    Sync modlist_path against mods_dir:
      - Prepend any mod folders not yet in modlist as disabled entries.
      - Remove any non-separator entries whose folder no longer exists.
    Skips MO2 separator dummy folders (_separator suffix).
    Creates modlist_path if it does not exist.
    """
    if not mods_dir.is_dir():
        if not modlist_path.exists():
            modlist_path.touch()
        return

    on_disk: set[str] = {
        d.name for d in mods_dir.iterdir()
        if d.is_dir() and not d.name.endswith("_separator")
    }

    # Parse existing modlist lines, dropping entries whose folder is gone
    existing_lines: list[str] = []
    existing_names: set[str] = set()
    if modlist_path.exists():
        for line in modlist_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped[0] in ("+", "-", "*"):
                name = stripped[1:]
                # Keep separators always; only keep mods that exist on disk
                if name.endswith("_separator") or name in on_disk:
                    existing_lines.append(stripped)
                    existing_names.add(name)
            else:
                existing_lines.append(stripped)

    new_mods = sorted(on_disk - existing_names)
    new_lines = [f"-{name}" for name in new_mods]

    all_lines = new_lines + existing_lines
    modlist_path.write_text("\n".join(all_lines) + ("\n" if all_lines else ""), encoding="utf-8")


def _create_profile_structure(game: BaseGame) -> None:
    """
    Create the standard profile folder structure for a game if it doesn't exist.

    Profiles/<game.name>/
      mods/           — staging area for installed mods
      overwrite/      — MO2-compatible catch-all for game/tool-generated files
      profiles/
        Profile 1/
          modlist.txt
          plugins.txt
    """
    # get_profile_root() returns the directory that contains mods/, profiles/, etc.
    # - Default: Profiles/<game>/ (mods/ is a subfolder)
    # - Custom staging: the staging path itself is the root
    game_profile_root = game.get_profile_root()
    mods_dir = game.get_mod_staging_path()

    # mods/        — staging area for installed mods
    mods_dir.mkdir(parents=True, exist_ok=True)

    # overwrite/   — MO2-compatible catch-all for files written by the game/tools
    (game_profile_root / "overwrite").mkdir(parents=True, exist_ok=True)

    # Root_Folder/ — files here are deployed to the game's root directory
    (game_profile_root / "Root_Folder").mkdir(parents=True, exist_ok=True)

    # Applications/ — exe files (and shortcuts) to run via Proton
    (game_profile_root / "Applications").mkdir(parents=True, exist_ok=True)

    # profiles/default/  — default profile with empty mod/plugin lists
    profile_dir = game_profile_root / "profiles" / "default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "plugins.txt").touch()
    sync_modlist_with_mods_folder(profile_dir / "modlist.txt", mods_dir)
