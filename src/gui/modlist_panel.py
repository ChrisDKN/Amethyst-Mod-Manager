"""
Mod list panel: canvas-based virtual list, toolbar, filters, Nexus update/endorsed.
Used by App. Imports theme, game_helpers, dialogs, install_mod.
"""

import json
import os
import shutil
import subprocess
import threading
import tkinter as tk
import tkinter.messagebox
import tkinter.ttk as ttk
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace

import customtkinter as ctk
from PIL import Image as PilImage, ImageTk

from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_HOVER_ROW,
    BG_PANEL,
    BG_ROW,
    BG_ROW_ALT,
    BG_SEP,
    BG_SELECT,
    BORDER,
    FONT_SMALL,
    FONT_NORMAL,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_SEP,
    plugin_separator,
    plugin_mod,
    conflict_separator,
    conflict_higher,
    conflict_lower,
    _ICONS_DIR,
    load_icon as _load_icon,
)
from gui.game_helpers import (
    _GAMES,
    _load_games,
    _profiles_for_game,
    _create_profile,
    _save_last_game,
    _load_last_game,
    _handle_missing_profile_root,
    _vanilla_plugins_for_game,
)
from gui.dialogs import (
    _RenameDialog,
    _SeparatorNameDialog,
    _ModNameDialog,
    _OverwritesDialog,
    _PriorityDialog,
)
from gui.install_mod import install_mod_from_archive
from gui.add_game_dialog import AddGameDialog, sync_modlist_with_mods_folder
from gui.modlist_filters_dialog import ModlistFiltersDialog
from gui.backup_restore_dialog import BackupRestoreDialog
from gui.nexus_settings_dialog import NexusSettingsDialog

from Utils.filemap import (
    build_filemap,
    CONFLICT_NONE,
    CONFLICT_WINS,
    CONFLICT_LOSES,
    CONFLICT_PARTIAL,
    CONFLICT_FULL,
    OVERWRITE_NAME,
    ROOT_FOLDER_NAME,
)
from Utils.deploy import deploy_root_folder, restore_root_folder, LinkMode
from Utils.modlist import (
    ModEntry,
    read_modlist,
    write_modlist,
    prepend_mod,
    ensure_mod_preserving_position,
)
from Utils.plugin_parser import check_missing_masters
from Utils.profile_backup import create_backup
from Nexus.nexus_api import NexusAPI, NexusAPIError, NexusModRequirement
from Nexus.nexus_meta import build_meta_from_download, write_meta, read_meta
from Nexus.nexus_update_checker import check_for_updates
from Nexus.nexus_requirements import check_missing_requirements
import webbrowser


# ---------------------------------------------------------------------------
# ModListPanel
# ---------------------------------------------------------------------------
class ModListPanel(ctk.CTkFrame):
    """
    Left panel: column header, canvas-based mod list, toolbar.

    Rows are drawn as canvas items rather than individual CTk widgets.
    One tk.Checkbutton per visible row is placed as a canvas window —
    all other columns are drawn as canvas text items.  This gives smooth
    scrolling and instant load for large mod lists.
    """

    ROW_H   = 26
    HEADERS = ["", "Mod Name", "Flags", "Conflicts", "Installed", "Priority"]
    # x-start of each logical column (checkbox, name, flags, conflicts, installed, priority)
    # Computed dynamically in _layout_columns(); defaults here.
    _COL_X  = [4, 32, 0, 0, 0, 0]   # patched in _layout_columns

    def __init__(self, parent, log_fn=None):
        super().__init__(parent, fg_color=BG_PANEL, corner_radius=0)
        self._log = log_fn or (lambda msg: None)

        self._game = None
        self._entries:  list[ModEntry] = []
        self._sel_idx:  int = -1          # anchor of the current selection
        self._sel_set:  set[int] = set()  # all selected entry indices
        self._hover_idx: int = -1         # entry index under the mouse cursor
        self._highlighted_mod: str | None = None  # mod highlighted by plugin panel selection
        self._modlist_path: Path | None = None
        self._strip_prefixes:    set[str] = set()
        self._mod_strip_prefixes: dict[str, list[str]] = {}  # mod name -> top-level folders to ignore
        self._install_extensions: set[str] = set()
        self._root_deploy_folders: set[str] = set()
        self._root_folder_enabled: bool = True
        self._conflict_map:  dict[str, int]      = {}  # mod_name → CONFLICT_* constant

        # Conflict icons (canvas-compatible PhotoImage)
        self._icon_plus: ImageTk.PhotoImage | None = None
        self._icon_minus: ImageTk.PhotoImage | None = None
        self._icon_cross: ImageTk.PhotoImage | None = None
        _plus_path = _ICONS_DIR / "plus.png"
        _minus_path = _ICONS_DIR / "minus.png"
        _cross_path = _ICONS_DIR / "cross.png"
        if _plus_path.is_file():
            self._icon_plus = ImageTk.PhotoImage(
                PilImage.open(_plus_path).convert("RGBA").resize((14, 14), PilImage.LANCZOS))
        if _minus_path.is_file():
            self._icon_minus = ImageTk.PhotoImage(
                PilImage.open(_minus_path).convert("RGBA").resize((14, 14), PilImage.LANCZOS))
        if _cross_path.is_file():
            self._icon_cross = ImageTk.PhotoImage(
                PilImage.open(_cross_path).convert("RGBA").resize((14, 14), PilImage.LANCZOS))

        # Update-available icon
        self._icon_update: ImageTk.PhotoImage | None = None
        _update_path = _ICONS_DIR / "update.png"
        if _update_path.is_file():
            self._icon_update = ImageTk.PhotoImage(
                PilImage.open(_update_path).convert("RGBA").resize((14, 14), PilImage.LANCZOS))

        # Missing-requirements warning icon
        self._icon_warning: ImageTk.PhotoImage | None = None
        _warning_path = _ICONS_DIR / "warning.png"
        if _warning_path.is_file():
            self._icon_warning = ImageTk.PhotoImage(
                PilImage.open(_warning_path).convert("RGBA").resize((14, 14), PilImage.LANCZOS))

        # Endorsed mod tick icon
        self._icon_endorsed: ImageTk.PhotoImage | None = None
        _tick_path = _ICONS_DIR / "tick.png"
        if _tick_path.is_file():
            self._icon_endorsed = ImageTk.PhotoImage(
                PilImage.open(_tick_path).convert("RGBA").resize((14, 14), PilImage.LANCZOS))

        # Separator collapse/expand arrows (right = collapsed, arrow = expanded)
        self._icon_sep_right: ImageTk.PhotoImage | None = None
        self._icon_sep_arrow: ImageTk.PhotoImage | None = None
        _right_path = _ICONS_DIR / "right.png"
        _arrow_path = _ICONS_DIR / "arrow.png"
        if _right_path.is_file():
            self._icon_sep_right = ImageTk.PhotoImage(
                PilImage.open(_right_path).convert("RGBA").resize((14, 14), PilImage.LANCZOS))
        if _arrow_path.is_file():
            self._icon_sep_arrow = ImageTk.PhotoImage(
                PilImage.open(_arrow_path).convert("RGBA").resize((14, 14), PilImage.LANCZOS))

        # Set of mod names that have a Nexus update available
        self._update_mods: set[str] = set()

        # Set of mod names that have missing Nexus requirements
        self._missing_reqs: set[str] = set()
        # Map mod name → list of missing requirement names (for tooltips / context menu)
        self._missing_reqs_detail: dict[str, list[str]] = {}
        # Mod names for which the user chose "Ignore requirements" (flag hidden, per profile)
        self._ignored_missing_reqs: set[str] = set()

        # Set of mod names the user has endorsed on Nexus
        self._endorsed_mods: set[str] = set()

        # Map mod name → install date display string
        self._install_dates: dict[str, str] = {}

        self._overrides:     dict[str, set[str]] = {}  # mod beats these mods
        self._overridden_by: dict[str, set[str]] = {}  # these mods beat this mod
        self._on_filemap_rebuilt: callable | None = None  # called after each filemap rebuild
        self._on_mod_selected_cb: callable | None = None  # called when a mod is selected
        self._filemap_pending: bool = False   # True while a background rebuild is running
        self._filemap_dirty:   bool = False   # True if another rebuild was requested while one was running

        # Drag state
        self._drag_idx:      int = -1      # entry index being dragged (stays fixed during drag)
        self._drag_origin_idx: int = -1    # original index when drag began (same as _drag_idx for now)
        self._drag_start_y:  int = 0
        self._drag_moved:    bool = False
        self._drag_is_block: bool = False   # True when dragging a separator+its mods
        self._drag_block:    list  = []     # snapshot of (entry, cb, var) at mousedown
        self._drag_cursor_y: int  = 0      # raw widget-space Y during drag (for ghost)
        self._drag_slot:     int  = -1     # last computed insertion slot (in vis-without-drag space)
        self._drag_target_slot: int = -1   # same as _drag_slot, kept for clarity in release handler
        self._drag_pending:  bool = False  # waiting for hold delay before drag activates
        self._drag_after_id: str | None = None  # after() id for drag-start timer

        # Separator lock state: sep_name → bool (True = locked, block drag disabled)
        self._sep_locks: dict[str, bool] = {}

        # Collapsed separators: set of sep names whose mods are hidden
        self._collapsed_seps: set[str] = set()

        # Search/filter
        self._filter_text: str = ""
        self._filter_show_disabled: bool = False
        self._filter_show_enabled: bool = False
        self._filter_hide_separators: bool = False
        self._filter_conflict_winning: bool = False
        self._filter_conflict_losing: bool = False
        self._filter_conflict_partial: bool = False
        self._filter_conflict_full: bool = False
        self._filter_missing_reqs: bool = False
        self._visible_indices: list[int] = []  # entry indices matching current filter

        # Column sorting (visual only — never touches modlist.txt)
        # _sort_column: None or one of "name", "installed", "flags", "conflicts", "priority"
        self._sort_column: str | None = None
        self._sort_ascending: bool = True

        # Checkbutton widgets reused per-row (canvas windows)
        self._check_vars:    list[tk.BooleanVar] = []
        self._check_buttons: list[tk.Checkbutton] = []

        # Lock checkboxes for separator rows: sep_name → (BooleanVar, Checkbutton)
        self._lock_widgets: dict[str, tuple[tk.BooleanVar, tk.Checkbutton]] = {}

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._build_header()
        self._build_canvas()
        self._build_toolbar()
        self._build_search_bar()
        self._build_download_bar()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_game(self, game, profile: str = "default") -> None:
        if game is None:
            self._game = None
            self._modlist_path = None
            self._ignored_missing_reqs = set()
            self._reload()
            if hasattr(self, "_restore_backup_btn"):
                self._restore_backup_btn.configure(state="disabled")
            return
        self._game = game
        profile_dir = game.get_profile_root() / "profiles" / profile
        self._modlist_path = profile_dir / "modlist.txt"
        self._strip_prefixes    = game.mod_folder_strip_prefixes
        self._install_extensions = getattr(game, "mod_install_extensions", set())
        self._root_deploy_folders = getattr(game, "mod_root_deploy_folders", set())
        # Load ignored missing-requirements list (one mod name per line)
        ignored_path = profile_dir / "ignored_missing_requirements.txt"
        self._ignored_missing_reqs = set()
        if ignored_path.is_file():
            try:
                self._ignored_missing_reqs = {
                    line.strip() for line in ignored_path.read_text().splitlines()
                    if line.strip()
                }
            except OSError:
                pass
        self._reload()
        if hasattr(self, "_restore_backup_btn"):
            self._restore_backup_btn.configure(state="normal")

    def reload_after_install(self):
        self._reload()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build_header(self):
        self._header = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=28)
        self._header.grid(row=0, column=0, sticky="ew")
        self._header.grid_propagate(False)
        # Header labels placed after canvas is built (we need its width)
        self._header_labels: list[ctk.CTkLabel] = []

    def _build_canvas(self):
        frame = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(frame, bg=BG_DEEP, bd=0, highlightthickness=0,
                                 yscrollincrement=1, takefocus=0)
        self._vsb = tk.Scrollbar(frame, orient="vertical",
                                 command=self._canvas.yview,
                                 bg=BG_SEP, troughcolor=BG_DEEP,
                                 activebackground=ACCENT,
                                 highlightthickness=0, bd=0)
        self._canvas.configure(yscrollcommand=self._vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._vsb.grid(row=0, column=1, sticky="ns")

        self._canvas_w = 600   # updated on first <Configure>
        self._canvas.bind("<Configure>",      self._on_canvas_resize)
        self._canvas.bind("<Button-4>",       self._on_scroll_up)
        self._canvas.bind("<Button-5>",       self._on_scroll_down)
        self._vsb.bind("<B1-Motion>",         lambda e: self._redraw())
        self._canvas.bind("<MouseWheel>",     self._on_mousewheel)
        self._canvas.bind("<ButtonPress-1>",  self._on_mouse_press)
        self._canvas.bind("<B1-Motion>",      self._on_mouse_drag)
        self._canvas.bind("<ButtonRelease-1>",self._on_mouse_release)
        self._canvas.bind("<ButtonRelease-3>", self._on_right_click)
        self._canvas.bind("<Motion>",         self._on_mouse_motion)
        self._canvas.bind("<Leave>",          self._on_mouse_leave)

    def _build_toolbar(self):
        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)

        ctk.CTkButton(
            bar, text="Move Up", width=90, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=FONT_SMALL,
            command=self._move_up
        ).pack(side="left", padx=(8, 4), pady=5)

        ctk.CTkButton(
            bar, text="Move Down", width=90, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=FONT_SMALL,
            command=self._move_down
        ).pack(side="left", padx=4, pady=5)

        # Expand/Collapse all separators toggle
        self._expand_collapse_all_btn = ctk.CTkButton(
            bar, text="Expand all", width=90, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=FONT_SMALL,
            command=self._toggle_all_separators
        )
        self._expand_collapse_all_btn.pack(side="left", padx=4, pady=5)
        self._update_expand_collapse_all_btn()

        # Check for Nexus mod updates button
        self._update_btn = ctk.CTkButton(
            bar, text="Check Updates", width=110, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=FONT_SMALL,
            command=self._on_check_updates
        )
        self._update_btn.pack(side="left", padx=4, pady=5)

        ctk.CTkButton(
            bar, text="Filters", width=80, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=FONT_SMALL,
            command=self._on_open_filters
        ).pack(side="left", padx=4, pady=5)

        self._restore_backup_btn = ctk.CTkButton(
            bar, text="Restore backup", width=110, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=FONT_SMALL,
            command=self._on_restore_backup,
            state="disabled",
        )
        self._restore_backup_btn.pack(side="left", padx=4, pady=5)

        # Refresh button (icon only)
        refresh_icon = _load_icon("refresh.png", size=(16, 16))
        ctk.CTkButton(
            bar, text="" if refresh_icon else "↺", image=refresh_icon,
            width=30, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=FONT_SMALL,
            command=self._reload
        ).pack(side="left", padx=4, pady=5)

        # Fixed-width clip frame prevents the label from resizing the toolbar
        info_clip = tk.Frame(bar, bg=BG_PANEL, width=300, height=26)
        info_clip.pack(side="left", padx=8)
        info_clip.pack_propagate(False)
        self._info_label = ctk.CTkLabel(
            info_clip, text="", font=FONT_SMALL, text_color=TEXT_DIM, anchor="w"
        )
        self._info_label.pack(fill="both", expand=True)

    def _build_search_bar(self):
        bar = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0, height=32)
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_propagate(False)

        tk.Label(bar, text="🔍", bg=BG_DEEP, fg=TEXT_DIM,
                 font=("Segoe UI", 11)).pack(side="left", padx=(8, 2), pady=4)

        self._search_entry = tk.Entry(
            bar,
            bg=BG_PANEL, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=("Segoe UI", 11),
            bd=0, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self._search_entry.pack(side="left", fill="x", expand=True, padx=(2, 8), pady=4)

        # KeyRelease fires after the character is committed to the widget
        self._search_entry.bind("<KeyRelease>", self._on_search_change)
        self._search_entry.bind("<Escape>", self._on_search_clear)
        self._search_entry.bind("<Control-a>", lambda e: (
            self._search_entry.select_range(0, "end"),
            self._search_entry.icursor("end"),
            "break"
        )[-1])

    def _build_download_bar(self):
        """Nexus download progress bar — hidden by default."""
        self._dl_bar = ctk.CTkFrame(self, fg_color=BG_DEEP, corner_radius=0, height=36)
        # Don't grid it yet — shown only during downloads
        self._dl_bar.grid_propagate(False)

        self._dl_label = ctk.CTkLabel(
            self._dl_bar, text="", font=FONT_SMALL, text_color=TEXT_MAIN, anchor="w",
        )
        self._dl_label.pack(side="left", padx=(8, 6), pady=4)

        self._dl_progress = ctk.CTkProgressBar(
            self._dl_bar, width=200, height=14,
            fg_color=BG_HEADER, progress_color=ACCENT,
            corner_radius=4,
        )
        self._dl_progress.set(0)
        self._dl_progress.pack(side="left", fill="x", expand=True, padx=(0, 6), pady=4)

        self._dl_pct = ctk.CTkLabel(
            self._dl_bar, text="0%", font=FONT_SMALL, text_color=TEXT_DIM,
            width=48, anchor="e",
        )
        self._dl_pct.pack(side="right", padx=(0, 8), pady=4)

    def show_download_progress(self, label: str = "Downloading..."):
        """Show the download progress bar."""
        self._dl_label.configure(text=label)
        self._dl_progress.set(0)
        self._dl_pct.configure(text="0%")
        self._dl_bar.grid(row=4, column=0, sticky="ew")

    def update_download_progress(self, current: int, total: int, label: str = ""):
        """Update the download progress bar."""
        if total > 0:
            frac = min(current / total, 1.0)
            self._dl_progress.set(frac)
            pct = int(frac * 100)
            cur_mb = current / (1024 * 1024)
            tot_mb = total / (1024 * 1024)
            self._dl_pct.configure(text=f"{pct}%")
            if label:
                self._dl_label.configure(text=label)
            else:
                self._dl_label.configure(text=f"Downloading: {cur_mb:.1f} / {tot_mb:.1f} MB")

    def hide_download_progress(self):
        """Hide the download progress bar."""
        self._dl_bar.grid_forget()

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    def _layout_columns(self, canvas_w: int):
        """Compute column x positions given the current canvas width."""
        # col 0: checkbox   28px
        # col 1: name       fills
        # col 2: flags      50px
        # col 3: conflicts  90px
        # col 4: installed  68px
        # col 5: priority   64px  (+ 14px scrollbar gap)
        right_cols = 50 + 90 + 68 + 64 + 14
        name_w = max(80, canvas_w - 28 - right_cols)
        self._COL_X = [
            4,                               # checkbox
            32,                              # name left edge
            32 + name_w,                     # flags
            32 + name_w + 50,                # conflicts
            32 + name_w + 50 + 90,           # installed
            32 + name_w + 50 + 90 + 68,      # priority
        ]
        self._canvas_w = canvas_w
        self._name_col_right = 32 + name_w - 4

    # Map header index → sort key name (index 0 is checkbox, not sortable)
    _HEADER_SORT_KEYS = {1: "name", 2: "flags", 3: "conflicts", 4: "installed", 5: "priority"}

    def _update_header(self, canvas_w: int):
        for lbl in self._header_labels:
            lbl.destroy()
        self._header_labels.clear()

        titles  = ["", "Mod Name", "Flags", "Conflicts", "Installed", "Priority"]
        x_pos   = self._COL_X
        anchors = ["center", "w", "center", "center", "center", "center"]
        widths  = [28, self._name_col_right - 32, 50, 90, 68, 64]
        for i, (title, x, anc, w) in enumerate(zip(titles, x_pos, anchors, widths)):
            sort_key = self._HEADER_SORT_KEYS.get(i)
            # Show sort arrow on the active column
            display = title
            if sort_key and sort_key == self._sort_column:
                arrow = " ▲" if self._sort_ascending else " ▼"
                display = title + arrow
            lbl = tk.Label(
                self._header, text=display, anchor=anc,
                font=("Segoe UI", 11, "bold"),
                fg=ACCENT if sort_key == self._sort_column else TEXT_SEP,
                bg=BG_HEADER, bd=0,
                cursor="hand2" if sort_key else "",
            )
            if sort_key:
                lbl.bind("<Button-1>", lambda e, k=sort_key: self._on_header_click(k))
            lbl.place(x=x, y=0, height=28, width=w)
            self._header_labels.append(lbl)

    # ------------------------------------------------------------------
    # Load / reload
    # ------------------------------------------------------------------

    def _locks_path(self) -> Path | None:
        if self._modlist_path is None:
            return None
        return self._modlist_path.parent / "separator_locks.json"

    def _load_sep_locks(self) -> None:
        path = self._locks_path()
        if path and path.is_file():
            try:
                self._sep_locks = json.loads(path.read_text(encoding="utf-8"))
                return
            except Exception:
                pass
        self._sep_locks = {}

    def _save_sep_locks(self) -> None:
        path = self._locks_path()
        if path is None:
            return
        path.write_text(json.dumps(self._sep_locks, indent=2), encoding="utf-8")

    def _root_folder_state_path(self) -> Path | None:
        if self._modlist_path is None:
            return None
        return self._modlist_path.parent / "root_folder_state.json"

    def _mod_strip_prefixes_path(self) -> Path | None:
        if self._modlist_path is None:
            return None
        return self._modlist_path.parent / "mod_strip_prefixes.json"

    def _load_mod_strip_prefixes(self) -> None:
        path = self._mod_strip_prefixes_path()
        if path and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._mod_strip_prefixes = {
                        k: v if isinstance(v, list) else []
                        for k, v in data.items() if isinstance(k, str)
                    }
                    return
            except Exception:
                pass
        self._mod_strip_prefixes = {}

    def _save_mod_strip_prefixes(self) -> None:
        path = self._mod_strip_prefixes_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._mod_strip_prefixes, indent=2), encoding="utf-8")

    def _load_root_folder_state(self) -> None:
        path = self._root_folder_state_path()
        if path and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._root_folder_enabled = bool(data.get("enabled", True))
                return
            except Exception:
                pass
        self._root_folder_enabled = True

    def _save_root_folder_state(self) -> None:
        path = self._root_folder_state_path()
        if path is None:
            return
        path.write_text(
            json.dumps({"enabled": self._root_folder_enabled}, indent=2),
            encoding="utf-8"
        )

    def _collapsed_path(self) -> Path | None:
        if self._modlist_path is None:
            return None
        return self._modlist_path.parent / "collapsed_seps.json"

    def _load_collapsed(self) -> None:
        path = self._collapsed_path()
        if path and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._collapsed_seps = set(data) if isinstance(data, list) else set()
                return
            except Exception:
                pass
        self._collapsed_seps = set()

    def _save_collapsed(self) -> None:
        path = self._collapsed_path()
        if path is None:
            return
        path.write_text(json.dumps(sorted(self._collapsed_seps), indent=2),
                        encoding="utf-8")

    def _reload(self):
        self._sel_idx = -1
        self._sel_set = set()
        self._drag_idx = -1
        # Clear visual sort on reload so the list matches modlist.txt order
        self._sort_column = None
        self._sort_ascending = True
        # Destroy stale lock widgets before rebuilding
        for _, cb in self._lock_widgets.values():
            cb.destroy()
        self._lock_widgets.clear()
        if self._modlist_path is None:
            self._entries = []
        else:
            # Sync any mods in the mods folder not yet in modlist.txt
            mods_dir = self._modlist_path.parent.parent.parent / "mods"
            sync_modlist_with_mods_folder(self._modlist_path, mods_dir)
            self._load_root_folder_state()
            self._load_mod_strip_prefixes()
            self._entries = read_modlist(self._modlist_path)
            # Prepend synthetic Overwrite row — always first (highest priority),
            # never saved to modlist.txt.
            self._entries.insert(0, ModEntry(
                name=OVERWRITE_NAME, enabled=True, locked=True, is_separator=True
            ))
            # Append synthetic Root_Folder row at the bottom (lowest priority)
            # if the folder exists.
            root_folder_dir = self._modlist_path.parent.parent.parent / "Root_Folder"
            if root_folder_dir.is_dir():
                self._entries.append(ModEntry(
                    name=ROOT_FOLDER_NAME,
                    enabled=self._root_folder_enabled,
                    locked=True, is_separator=True
                ))
        self._load_sep_locks()
        self._load_collapsed()
        self._update_expand_collapse_all_btn()
        self._scan_update_flags()
        self._scan_missing_reqs_flags()
        self._scan_endorsed_flags()
        self._scan_install_dates()
        self._rebuild_check_widgets()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _scan_update_flags(self):
        """Scan meta.ini files to build the set of mods with updates available."""
        self._update_mods.clear()
        if self._modlist_path is None:
            return
        mods_dir = self._modlist_path.parent.parent.parent / "mods"
        if not mods_dir.is_dir():
            return
        for entry in self._entries:
            if entry.is_separator:
                continue
            meta_path = mods_dir / entry.name / "meta.ini"
            if not meta_path.is_file():
                continue
            try:
                meta = read_meta(meta_path)
                if meta.has_update:
                    self._update_mods.add(entry.name)
            except Exception:
                pass

    def _scan_missing_reqs_flags(self):
        """Scan meta.ini files to build the set of mods with missing requirements."""
        self._missing_reqs.clear()
        self._missing_reqs_detail.clear()
        if self._modlist_path is None:
            return
        mods_dir = self._modlist_path.parent.parent.parent / "mods"
        if not mods_dir.is_dir():
            return
        for entry in self._entries:
            if entry.is_separator:
                continue
            meta_path = mods_dir / entry.name / "meta.ini"
            if not meta_path.is_file():
                continue
            try:
                meta = read_meta(meta_path)
                if meta.missing_requirements:
                    self._missing_reqs.add(entry.name)
                    # Parse "modId:name;modId:name" into readable names
                    names = []
                    for pair in meta.missing_requirements.split(";"):
                        parts = pair.split(":", 1)
                        if len(parts) == 2:
                            names.append(parts[1])
                        elif parts[0]:
                            names.append(parts[0])
                    self._missing_reqs_detail[entry.name] = names
            except Exception:
                pass

    def _save_ignored_missing_reqs(self) -> None:
        """Persist _ignored_missing_reqs to profile's ignored_missing_requirements.txt."""
        if self._modlist_path is None:
            return
        profile_dir = self._modlist_path.parent
        path = profile_dir / "ignored_missing_requirements.txt"
        try:
            if self._ignored_missing_reqs:
                path.write_text("\n".join(sorted(self._ignored_missing_reqs)) + "\n")
            elif path.is_file():
                path.unlink()
        except OSError:
            pass

    def _scan_endorsed_flags(self):
        """Scan meta.ini files to build the set of endorsed mods."""
        self._endorsed_mods.clear()
        if self._modlist_path is None:
            return
        mods_dir = self._modlist_path.parent.parent.parent / "mods"
        if not mods_dir.is_dir():
            return
        for entry in self._entries:
            if entry.is_separator:
                continue
            meta_path = mods_dir / entry.name / "meta.ini"
            if not meta_path.is_file():
                continue
            try:
                meta = read_meta(meta_path)
                if meta.endorsed:
                    self._endorsed_mods.add(entry.name)
            except Exception:
                pass

    def _scan_install_dates(self):
        """Scan meta.ini files to build the install date display strings per mod."""
        self._install_dates.clear()
        if self._modlist_path is None:
            return
        mods_dir = self._modlist_path.parent.parent.parent / "mods"
        if not mods_dir.is_dir():
            return
        today = datetime.now().date()
        for entry in self._entries:
            if entry.is_separator:
                continue
            meta_path = mods_dir / entry.name / "meta.ini"
            if not meta_path.is_file():
                continue
            try:
                meta = read_meta(meta_path)
                if meta.installed:
                    dt = datetime.fromisoformat(meta.installed)
                    if dt.date() == today:
                        self._install_dates[entry.name] = dt.strftime("%-I:%M %p")
                    else:
                        self._install_dates[entry.name] = dt.strftime("%-m/%-d/%y")
            except Exception:
                pass

    def _rebuild_check_widgets(self):
        """Destroy old Checkbutton widgets and create one per non-separator entry."""
        # Clear vars first so any variable-trace callbacks triggered by destroy()
        # see an empty list and do not call _save_modlist with stale data.
        old_buttons = list(self._check_buttons)
        self._check_buttons.clear()
        self._check_vars.clear()
        for cb in old_buttons:
            if cb is not None:
                cb.destroy()

        for i, entry in enumerate(self._entries):
            if entry.is_separator:
                # Placeholder so indices stay aligned with self._entries
                self._check_vars.append(None)
                self._check_buttons.append(None)
                continue
            var = tk.BooleanVar(value=entry.enabled)
            state = "disabled" if entry.locked else "normal"
            cb = tk.Checkbutton(
                self._canvas,
                variable=var,
                bg=BG_ROW if i % 2 == 0 else BG_ROW_ALT,
                activebackground=BG_HOVER,
                selectcolor=BG_DEEP,
                fg=ACCENT,
                bd=0, highlightthickness=0,
                command=lambda idx=i: self._on_toggle(idx),
                state=state,
            )
            self._check_vars.append(var)
            self._check_buttons.append(cb)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _redraw(self):
        """Full redraw of all canvas items."""
        self._canvas.delete("all")

        cw = self._canvas_w
        dragging = self._drag_idx >= 0 and self._drag_moved

        # Move all widgets off-screen instead of hiding them.
        # place_forget() causes a hide→show flicker; parking at y=-9999
        # keeps the widget alive and invisible without triggering a redraw flash.
        for cb in self._check_buttons:
            if cb is not None:
                cb.place(x=-9999, y=-9999)
        for _var, cb in self._lock_widgets.values():
            cb.place(x=-9999, y=-9999)

        canvas_top    = int(self._canvas.canvasy(0))
        canvas_bottom = canvas_top + self._canvas.winfo_height()

        # Compute which entries are visible under the current filter
        self._visible_indices = self._compute_visible_indices()

        # Pre-compute which entry indices are part of the active drag
        drag_indices: set[int] = set()
        if dragging:
            if self._drag_is_block and self._drag_block:
                drag_indices = set(range(self._drag_idx,
                                         self._drag_idx + len(self._drag_block)))
            else:
                drag_indices = {self._drag_idx}

        # During drag, exclude the dragged entries from the rendered list so they
        # don't leave a gap — the ghost overlay shows them at the cursor instead.
        if dragging and drag_indices:
            vis = [i for i in self._visible_indices if i not in drag_indices]
        else:
            vis = self._visible_indices

        total_h = len(vis) * self.ROW_H

        # Pre-compute priorities from the full (unfiltered) list
        priorities: dict[int, int] = {}
        mod_count = sum(1 for e in self._entries if not e.is_separator)
        p = mod_count - 1
        for idx, entry in enumerate(self._entries):
            if not entry.is_separator:
                priorities[idx] = p
                p -= 1

        _DOT_COLORS = {
            CONFLICT_WINS:    "#98c379",
            CONFLICT_LOSES:   "#e06c75",
            CONFLICT_PARTIAL: "#e5c07b",
            CONFLICT_FULL:    "#cccccc",
        }

        sel_entry = (self._entries[self._sel_idx]
                     if 0 <= self._sel_idx < len(self._entries) else None)

        # Pre-compute which separator indices should be highlighted for conflict context
        conflict_sep_indices: set[int] = set()
        if sel_entry and not sel_entry.is_separator:
            sel_name = sel_entry.name
            conflict_mods = (self._overrides.get(sel_name, set())
                             | self._overridden_by.get(sel_name, set()))
            for cm in conflict_mods:
                si = self._sep_idx_for_mod(cm)
                if si >= 0:
                    conflict_sep_indices.add(si)
        elif sel_entry and sel_entry.name == OVERWRITE_NAME:
            for cm in self._overrides.get(OVERWRITE_NAME, set()):
                si = self._sep_idx_for_mod(cm)
                if si >= 0:
                    conflict_sep_indices.add(si)

        # Pre-compute which separator index contains the plugin-highlighted mod
        highlighted_sep_idx: int = -1
        if self._highlighted_mod:
            highlighted_sep_idx = self._sep_idx_for_mod(self._highlighted_mod)

        for row, i in enumerate(vis):
            entry = self._entries[i]
            y_top = row * self.ROW_H
            y_bot = y_top + self.ROW_H
            # Skip rows outside viewport (virtualisation)
            if y_bot < canvas_top or y_top > canvas_bottom:
                continue

            y_mid = y_top + self.ROW_H // 2

            if entry.is_separator:
                is_overwrite    = (entry.name == OVERWRITE_NAME)
                is_root_folder  = (entry.name == ROOT_FOLDER_NAME)
                is_synthetic    = is_overwrite or is_root_folder
                is_sel_row = (i in self._sel_set)
                if is_overwrite:
                    base_bg = "#1e2a1e"
                    txt_col = "#6dbf6d"
                elif is_root_folder:
                    base_bg = "#1e1e2e" if entry.enabled else BG_SEP
                    txt_col = "#7aa2f7" if entry.enabled else TEXT_DIM
                else:
                    base_bg = BG_SEP
                    txt_col = TEXT_SEP
                if is_sel_row:
                    row_bg = BG_SELECT
                elif not is_synthetic and i in conflict_sep_indices:
                    row_bg = conflict_separator 
                elif not is_synthetic and i == highlighted_sep_idx:
                    row_bg = plugin_separator 
                else:
                    row_bg = base_bg
                self._canvas.create_rectangle(0, y_top, cw, y_bot,
                                              fill=row_bg, outline="")
                # Draw collapse toggle triangle on real separators only
                if not is_synthetic:
                    if entry.name in self._collapsed_seps:
                        if self._icon_sep_right:
                            self._canvas.create_image(10, y_mid, image=self._icon_sep_right, anchor="center")
                        else:
                            self._canvas.create_text(10, y_mid, text="▶", anchor="center",
                                                     fill=TEXT_DIM, font=("Segoe UI", 9))
                    else:
                        if self._icon_sep_arrow:
                            self._canvas.create_image(10, y_mid, image=self._icon_sep_arrow, anchor="center")
                        else:
                            self._canvas.create_text(10, y_mid, text="▼", anchor="center",
                                                     fill=TEXT_DIM, font=("Segoe UI", 9))
                if is_overwrite:
                    label = "Overwrite"
                elif is_root_folder:
                    label = "Root Folder"
                else:
                    label = entry.display_name
                # Synthetic rows: no lock widget on right; root folder gets left-side checkbox
                lock_w = 28 if not is_synthetic else 0
                right_edge = cw - lock_w - 8
                left_edge = 32 if is_root_folder else (20 if not is_synthetic else 8)
                # Always center text at the true canvas midpoint so all separator types align
                mid_x = cw // 2
                text_pad = 6
                self._canvas.create_line(left_edge, y_mid, mid_x - len(label) * 4 - text_pad,
                                         y_mid, fill=BORDER, width=1)
                self._canvas.create_line(mid_x + len(label) * 4 + text_pad, y_mid,
                                         right_edge, y_mid, fill=BORDER, width=1)
                self._canvas.create_text(
                    mid_x, y_mid, text=label, anchor="center",
                    fill=txt_col, font=("Segoe UI", 10, "bold"),
                )
                if is_overwrite and self._overrides.get(OVERWRITE_NAME):
                    cx = self._COL_X[3] + 45
                    if self._icon_minus and self._icon_plus:
                        self._canvas.create_image(cx - 8, y_mid, image=self._icon_minus, anchor="center")
                        self._canvas.create_image(cx + 8, y_mid, image=self._icon_plus, anchor="center")
                    else:
                        self._canvas.create_text(
                            cx, y_mid, text="●", anchor="center",
                            fill="#e5c07b", font=("Segoe UI", 10),
                        )
                # Enable/disable checkbox for Root Folder row (left side, like regular mods)
                if is_root_folder:
                    rf_key = ROOT_FOLDER_NAME
                    if rf_key not in self._lock_widgets:
                        var = tk.BooleanVar(value=entry.enabled)
                        cb = tk.Checkbutton(
                            self._canvas,
                            variable=var,
                            bg=base_bg, activebackground=base_bg,
                            selectcolor=BG_DEEP,
                            fg=ACCENT,
                            bd=1, highlightthickness=0,
                            command=self._on_root_folder_toggle,
                        )
                        self._lock_widgets[rf_key] = (var, cb)
                    else:
                        var, cb = self._lock_widgets[rf_key]
                        var.set(entry.enabled)
                        cb.configure(bg=base_bg, activebackground=base_bg)
                    widget_y = y_top - canvas_top
                    cb.place(x=self._COL_X[0], y=widget_y,
                             width=24, height=self.ROW_H)
                # Lock checkbox for real separators
                elif not is_synthetic:
                    sname = entry.name
                    if sname not in self._lock_widgets:
                        var = tk.BooleanVar(value=self._sep_locks.get(sname, False))
                        cb = tk.Checkbutton(
                            self._canvas,
                            variable=var, text="🔒",
                            bg=row_bg, activebackground=row_bg,
                            selectcolor=BG_DEEP, fg=TEXT_SEP,
                            font=("Segoe UI", 9),
                            bd=0, highlightthickness=0,
                            command=lambda n=sname: self._on_sep_lock_toggle(n),
                        )
                        self._lock_widgets[sname] = (var, cb)
                    else:
                        var, cb = self._lock_widgets[sname]
                        cb.configure(bg=row_bg, activebackground=row_bg)
                    widget_y = y_top - canvas_top
                    cb.place(x=cw - lock_w - 8, y=widget_y,
                             width=lock_w, height=self.ROW_H)
                continue

            is_sel = (i in self._sel_set) or (i == self._drag_idx)
            if is_sel:
                bg = BG_SELECT
            elif entry.name == self._highlighted_mod:
                bg = plugin_mod
            elif i == self._hover_idx:
                bg = BG_HOVER_ROW
            elif sel_entry and (not sel_entry.is_separator
                                or sel_entry.name == OVERWRITE_NAME):
                sel_name = sel_entry.name
                if entry.name in self._overrides.get(sel_name, set()):
                    bg = conflict_higher
                elif entry.name in self._overridden_by.get(sel_name, set()):
                    bg = conflict_lower
                else:
                    bg = BG_ROW if row % 2 == 0 else BG_ROW_ALT
            else:
                bg = BG_ROW if row % 2 == 0 else BG_ROW_ALT

            self._canvas.create_rectangle(0, y_top, cw, y_bot, fill=bg, outline="")

            # Only place checkbutton widgets when not dragging (avoids flicker)
            if not dragging:
                cb = self._check_buttons[i]
                cb.configure(bg=bg, activebackground=bg)
                widget_y = y_top - canvas_top
                cb.place(x=self._COL_X[0], y=widget_y,
                         width=24, height=self.ROW_H)

            name_color = TEXT_DIM if not entry.enabled else TEXT_MAIN
            self._canvas.create_text(
                self._COL_X[1], y_mid,
                text=entry.name, anchor="w", fill=name_color,
                font=("Segoe UI", 11),
            )

            # Flags column: warning (highest) > locked star > update > endorsed tick (lowest)
            flag_x = self._COL_X[2] + 10
            if (entry.name in self._missing_reqs and entry.name not in self._ignored_missing_reqs
                    and self._icon_warning):
                self._canvas.create_image(flag_x, y_mid, image=self._icon_warning, anchor="center")
            elif entry.locked:
                self._canvas.create_text(
                    flag_x, y_mid,
                    text="★", anchor="center", fill="#e5c07b",
                    font=("Segoe UI", 11),
                )
                flag_x += 18
                if entry.name in self._update_mods and self._icon_update:
                    self._canvas.create_image(flag_x, y_mid, image=self._icon_update, anchor="center")
                elif entry.name in self._endorsed_mods and self._icon_endorsed:
                    self._canvas.create_image(flag_x, y_mid, image=self._icon_endorsed, anchor="center")
            elif entry.name in self._update_mods and self._icon_update:
                self._canvas.create_image(flag_x, y_mid, image=self._icon_update, anchor="center")
            elif entry.name in self._endorsed_mods and self._icon_endorsed:
                self._canvas.create_image(flag_x, y_mid, image=self._icon_endorsed, anchor="center")

            conflict = self._conflict_map.get(entry.name, CONFLICT_NONE)
            cx = self._COL_X[3] + 45
            if conflict == CONFLICT_WINS and self._icon_plus:
                self._canvas.create_image(cx, y_mid, image=self._icon_plus, anchor="center")
            elif conflict == CONFLICT_LOSES and self._icon_minus:
                self._canvas.create_image(cx, y_mid, image=self._icon_minus, anchor="center")
            elif conflict == CONFLICT_PARTIAL and self._icon_minus and self._icon_plus:
                self._canvas.create_image(cx - 8, y_mid, image=self._icon_minus, anchor="center")
                self._canvas.create_image(cx + 8, y_mid, image=self._icon_plus, anchor="center")
            elif conflict == CONFLICT_FULL and self._icon_cross:
                self._canvas.create_image(cx, y_mid, image=self._icon_cross, anchor="center")
            elif conflict in (CONFLICT_WINS, CONFLICT_LOSES, CONFLICT_PARTIAL, CONFLICT_FULL):
                dot_color = _DOT_COLORS.get(conflict)
                if dot_color:
                    self._canvas.create_text(
                        cx, y_mid, text="●", anchor="center",
                        fill=dot_color, font=("Segoe UI", 10),
                    )

            install_text = self._install_dates.get(entry.name, "")
            if install_text:
                self._canvas.create_text(
                    self._COL_X[4] + 34, y_mid,
                    text=install_text, anchor="center", fill=TEXT_DIM,
                    font=("Segoe UI", 10),
                )

            self._canvas.create_text(
                self._COL_X[5] + 32, y_mid,
                text=str(priorities.get(i, "")), anchor="center", fill=TEXT_DIM,
                font=("Segoe UI", 10),
            )

        self._canvas.configure(scrollregion=(
            0, 0, cw, max(total_h, self._canvas.winfo_height())
        ))

    def _draw_drag_overlay(self):
        """Draw a drag ghost under the cursor + a blue insertion line at the target slot."""
        self._canvas.delete("drag_overlay")
        if self._drag_idx < 0 or not self._entries:
            return

        cw = self._canvas_w
        gh = self.ROW_H

        # Build the list of entries to show in the ghost.
        # For collapsed separators, only show the separator itself (mods stay hidden).
        if self._drag_is_block and self._drag_block:
            sep_entry = self._drag_block[0][0]
            if sep_entry.is_separator and sep_entry.name in self._collapsed_seps:
                ghost_entries = [sep_entry]
            else:
                ghost_entries = [item[0] for item in self._drag_block]
        else:
            ghost_entries = [self._entries[self._drag_idx]]

        # Draw the ghost centered on the cursor (in widget-space, not canvas-space)
        canvas_top = int(self._canvas.canvasy(0))
        # _drag_cursor_y is widget-space; convert to canvas-space for drawing
        cursor_canvas_y = self._drag_cursor_y + canvas_top
        ghost_top = cursor_canvas_y - gh // 2

        for offset, entry in enumerate(ghost_entries):
            gy_top = ghost_top + offset * gh
            gy_mid = gy_top + gh // 2
            is_sep = entry.is_separator
            bg = BG_SEP if is_sep else BG_SELECT
            outline = ACCENT if offset == 0 else BORDER
            self._canvas.create_rectangle(
                2, gy_top, cw - 2, gy_top + gh,
                fill=bg, outline=outline, width=1, tags="drag_overlay",
            )
            self._canvas.create_text(
                self._COL_X[1], gy_mid,
                text=entry.display_name, anchor="w",
                fill=TEXT_SEP if is_sep else TEXT_MAIN,
                font=("Segoe UI", 10, "bold") if is_sep else ("Segoe UI", 11),
                tags="drag_overlay",
            )

        # Blue insertion line showing where the item will land when released.
        # _drag_slot is an index into the vis-without-drag list.
        slot = self._drag_target_slot
        blk_size = len(self._drag_block) if self._drag_is_block else 1
        vis = self._visible_indices
        drag_set = set(range(self._drag_idx, self._drag_idx + blk_size))
        vis_without_drag = [i for i in vis if i not in drag_set]

        if slot >= len(vis_without_drag):
            # Inserting after the last rendered row
            line_y = len(vis_without_drag) * gh
        else:
            # Find the rendered row index of that entry in the full vis list
            target_entry_idx = vis_without_drag[slot]
            # Count how many vis entries come before it (some may be the drag entries)
            line_row = sum(1 for v in vis if v < target_entry_idx and v not in drag_set)
            line_y = line_row * gh

        self._canvas.create_line(
            0, line_y, cw, line_y,
            fill=ACCENT, width=2, tags="drag_overlay",
        )

    def _on_search_change(self, _event=None):
        # Ignore key events that fire after focus has left the search entry
        if self.focus_get() is not self._search_entry:
            return
        self._filter_text = self._search_entry.get().lower()
        self._sel_idx = -1
        self._redraw()

    def _on_search_clear(self, _event=None):
        self._search_entry.delete(0, "end")
        self._on_search_change()

    def _compute_visible_indices(self) -> list[int]:
        """Return entry indices that match the current filter, collapsed state, and column sort."""
        # Step 1: basic visibility (filter or collapse)
        if self._filter_text:
            base = [i for i, e in enumerate(self._entries)
                    if self._filter_text in e.name.lower()]
        elif not self._collapsed_seps:
            base = list(range(len(self._entries)))
        else:
            base = []
            skip = False
            for i, entry in enumerate(self._entries):
                if entry.is_separator:
                    skip = False
                    base.append(i)
                    if entry.name in self._collapsed_seps:
                        skip = True
                elif not skip:
                    base.append(i)

        # Step 2: hide separators filter
        if self._filter_hide_separators:
            base = [i for i in base if not self._entries[i].is_separator]

        # Step 3: enabled/disabled filter
        # When showing only disabled (or only enabled), keep separators only if their
        # block has at least one matching mod; otherwise the separator is hidden.
        if self._filter_show_disabled and not self._filter_show_enabled:
            result = []
            for i in base:
                entry = self._entries[i]
                if entry.is_separator:
                    if self._sep_block_has_disabled(i):
                        result.append(i)
                elif not entry.enabled:
                    result.append(i)
            base = result
        elif self._filter_show_enabled and not self._filter_show_disabled:
            result = []
            for i in base:
                entry = self._entries[i]
                if entry.is_separator:
                    if self._sep_block_has_enabled(i):
                        result.append(i)
                elif entry.enabled:
                    result.append(i)
            base = result
        # if both or neither: no enabled-state filter

        # Step 4: conflict type filter
        # When filtering by conflict type, keep separators only if their block has at least one matching mod.
        if (self._filter_conflict_winning or self._filter_conflict_losing
                or self._filter_conflict_partial or self._filter_conflict_full):
            allowed = set()
            if self._filter_conflict_winning:
                allowed.add(CONFLICT_WINS)
            if self._filter_conflict_losing:
                allowed.add(CONFLICT_LOSES)
            if self._filter_conflict_partial:
                allowed.add(CONFLICT_PARTIAL)
            if self._filter_conflict_full:
                allowed.add(CONFLICT_FULL)
            result = []
            for i in base:
                entry = self._entries[i]
                if entry.is_separator:
                    if self._sep_block_has_conflict_in(i, allowed):
                        result.append(i)
                elif self._conflict_map.get(entry.name, CONFLICT_NONE) in allowed:
                    result.append(i)
            base = result

        # Step 4b: missing requirements filter (show only mods with missing reqs, not ignored)
        if self._filter_missing_reqs:
            result = []
            for i in base:
                entry = self._entries[i]
                if entry.is_separator:
                    if self._sep_block_has_missing_reqs(i):
                        result.append(i)
                elif (entry.name in self._missing_reqs
                      and entry.name not in self._ignored_missing_reqs):
                    result.append(i)
            base = result

        # Step 5: apply column sort (visual only)
        if self._sort_column is not None:
            base = self._apply_column_sort(base)
        return base

    # ------------------------------------------------------------------
    # Column sorting helpers (visual only — never touches modlist.txt)
    # ------------------------------------------------------------------

    def _on_header_click(self, sort_key: str):
        """Handle a click on a sortable column header."""
        if self._sort_column == sort_key:
            # Same column clicked again — toggle direction, or clear on third click
            if not self._sort_ascending:
                # Already descending → clear sort
                self._sort_column = None
                self._sort_ascending = True
            else:
                self._sort_ascending = False
        else:
            self._sort_column = sort_key
            self._sort_ascending = True
        self._update_header(self._canvas_w)
        self._redraw()

    def _apply_column_sort(self, indices: list[int]) -> list[int]:
        """Sort visible indices by the active column. Separators stay in place;
        only mod rows within each separator group are reordered."""
        if not self._sort_column:
            return indices

        # Split indices into groups: each group starts with a separator (or the
        # implicit top-level group if the first entries aren't under a separator).
        groups: list[list[int]] = []
        current_sep: int | None = None
        current_mods: list[int] = []
        for idx in indices:
            entry = self._entries[idx]
            if entry.is_separator:
                # Flush previous group
                if current_sep is not None or current_mods:
                    groups.append((current_sep, current_mods))
                current_sep = idx
                current_mods = []
            else:
                current_mods.append(idx)
        # Flush last group
        if current_sep is not None or current_mods:
            groups.append((current_sep, current_mods))

        # Build sort key function
        key_fn = self._sort_key_fn()

        result: list[int] = []
        for sep_idx, mod_indices in groups:
            if sep_idx is not None:
                result.append(sep_idx)
            sorted_mods = sorted(mod_indices, key=key_fn, reverse=not self._sort_ascending)
            result.extend(sorted_mods)
        return result

    def _sort_key_fn(self):
        """Return a key function for sorting entry indices by the active column."""
        col = self._sort_column

        # Pre-compute priorities (same logic as _redraw)
        priorities: dict[int, int] = {}
        mod_count = sum(1 for e in self._entries if not e.is_separator)
        p = mod_count - 1
        for idx, entry in enumerate(self._entries):
            if not entry.is_separator:
                priorities[idx] = p
                p -= 1

        if col == "name":
            return lambda i: self._entries[i].name.lower()
        elif col == "installed":
            def _installed_key(i):
                date_str = self._install_dates.get(self._entries[i].name, "")
                if not date_str:
                    return (1, "")  # mods without date sort last
                return (0, date_str)
            return _installed_key
        elif col == "flags":
            def _flags_key(i):
                name = self._entries[i].name
                # Lower number = flagged (sorts first when ascending)
                has_warning = (name in self._missing_reqs
                              and name not in self._ignored_missing_reqs)
                has_update = name in self._update_mods
                has_endorsed = name in self._endorsed_mods
                is_locked = self._entries[i].locked
                score = 0
                if has_warning:  score |= 8
                if is_locked:    score |= 4
                if has_update:   score |= 2
                if has_endorsed: score |= 1
                return -score  # negate so flagged mods sort first in ascending
            return _flags_key
        elif col == "conflicts":
            # Order: partial (+-), loses (-), wins (+), full (x), none
            _CONFLICT_ORDER = {
                CONFLICT_PARTIAL: 0,
                CONFLICT_LOSES:   1,
                CONFLICT_WINS:    2,
                CONFLICT_FULL:    3,
                CONFLICT_NONE:    4,
            }
            def _conflict_key(i):
                c = self._conflict_map.get(self._entries[i].name, CONFLICT_NONE)
                return _CONFLICT_ORDER.get(c, 4)
            return _conflict_key
        elif col == "priority":
            return lambda i: priorities.get(i, 0)
        else:
            return lambda i: i

    def _on_canvas_resize(self, event):
        self._layout_columns(event.width)
        self._update_header(event.width)
        self._redraw()

    def _on_scroll_up(self, _event):
        self._canvas.yview("scroll", -50, "units")
        self._redraw()

    def _on_scroll_down(self, _event):
        self._canvas.yview("scroll", 50, "units")
        self._redraw()

    def _on_mousewheel(self, event):
        self._canvas.yview("scroll", -50 if event.delta > 0 else 6, "units")
        self._redraw()

    # ------------------------------------------------------------------
    # Hit-testing
    # ------------------------------------------------------------------

    def _canvas_y_to_index(self, canvas_y: int) -> int:
        """Convert a canvas-space y coordinate to a real entry index via visible list."""
        vis = self._visible_indices
        if not vis:
            return 0
        row = int(canvas_y // self.ROW_H)
        row = max(0, min(row, len(vis) - 1))
        return vis[row]

    def _event_canvas_y(self, event) -> int:
        return int(self._canvas.canvasy(event.y))

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    # Milliseconds the user must hold the mouse button before dragging starts
    _DRAG_DELAY_MS = 500

    def _cancel_drag_timer(self):
        """Cancel any pending drag-start timer."""
        if self._drag_after_id is not None:
            self._canvas.after_cancel(self._drag_after_id)
            self._drag_after_id = None
        self._drag_pending = False

    def _on_mouse_press(self, event):
        if not self._entries:
            return
        # Cancel any previous pending drag
        self._cancel_drag_timer()
        cy = self._event_canvas_y(event)
        idx = self._canvas_y_to_index(cy)
        shift = bool(event.state & 0x1)

        if self._entries[idx].is_separator:
            if self._entries[idx].name in (OVERWRITE_NAME, ROOT_FOLDER_NAME):
                # Synthetic rows are selectable (shows conflict highlights) but not draggable
                self._sel_idx = idx
                self._sel_set = {idx}
                self._drag_idx = -1
                self._drag_moved = False
                self._drag_slot  = -1
                self._redraw()
                self._update_info()
                label = "Overwrite" if self._entries[idx].name == OVERWRITE_NAME else "Root Folder"
                if self._on_mod_selected_cb is not None:
                    self._on_mod_selected_cb()
            else:
                # Click on collapse triangle zone (left 22px) — toggle collapse
                if event.x < 22:
                    self._toggle_collapse(self._entries[idx].name)
                    return
                # Shift+click on separator: extend selection range
                if shift and self._sel_idx >= 0:
                    lo, hi = sorted((self._sel_idx, idx))
                    self._sel_set = set(range(lo, hi + 1))
                    self._redraw()
                    return
                self._sel_idx = idx
                self._sel_set = {idx}
                if self._on_mod_selected_cb is not None:
                    self._on_mod_selected_cb()
                # Regular separators — schedule drag activation after hold delay
                if self._sep_locks.get(self._entries[idx].name, False):
                    blk = self._sep_block_range(idx)
                    pending_block = [
                        (self._entries[i], self._check_buttons[i], self._check_vars[i])
                        for i in blk
                    ]
                    is_block = True
                else:
                    pending_block = []
                    is_block = False
                self._drag_pending = True
                self._drag_after_id = self._canvas.after(
                    self._DRAG_DELAY_MS,
                    lambda: self._activate_drag(idx, cy, is_block, pending_block),
                )
                self._redraw()
            return

        # Shift+click: extend selection from anchor to clicked row
        if shift and self._sel_idx >= 0:
            lo, hi = sorted((self._sel_idx, idx))
            self._sel_set = set(range(lo, hi + 1))
            self._redraw()
            self._update_info()
            return

        # If clicking inside an existing multi-selection, preserve it so the
        # user can hold to drag the whole group — only collapse to single on release.
        if idx in self._sel_set and len(self._sel_set) > 1:
            if not self._entries[idx].locked:
                self._drag_pending = True
                self._drag_after_id = self._canvas.after(
                    self._DRAG_DELAY_MS,
                    lambda: self._activate_drag(idx, cy, False, []),
                )
            return

        self._sel_idx = idx
        self._sel_set = {idx}
        if self._on_mod_selected_cb is not None:
            self._on_mod_selected_cb()
        self._redraw()
        self._update_info()
        if self._entries[idx].locked:
            # * entries are selectable but not draggable
            self._drag_idx = -1
            self._drag_moved = False
            self._drag_slot  = -1
            return
        # Schedule drag activation after hold delay
        self._drag_pending = True
        self._drag_after_id = self._canvas.after(
            self._DRAG_DELAY_MS,
            lambda: self._activate_drag(idx, cy, False, []),
        )

    def _activate_drag(self, idx: int, start_y: int, is_block: bool, block: list):
        """Called after the hold delay — officially begin the drag."""
        self._drag_after_id = None
        self._drag_pending = False

        # If multiple items are selected and the dragged item is in the selection,
        # treat the whole selection as the drag block (sorted by entry index).
        if len(self._sel_set) > 1 and idx in self._sel_set and not is_block:
            sorted_sel = sorted(self._sel_set)
            block = [
                (self._entries[i], self._check_buttons[i], self._check_vars[i])
                for i in sorted_sel
            ]
            # Anchor the drag at the first selected index
            idx = sorted_sel[0]
            is_block = True

        self._drag_idx = idx
        self._drag_origin_idx = idx
        self._drag_start_y = start_y
        self._drag_moved = False
        self._drag_slot  = -1
        self._drag_target_slot = -1
        self._drag_is_block = is_block
        self._drag_block = block

    def _sep_block_range(self, sep_idx: int) -> range:
        """Return the range of indices [sep_idx, end) belonging to this separator block.
        The block is the separator plus every non-separator entry below it
        until the next separator (or end of list)."""
        end = sep_idx + 1
        while end < len(self._entries) and not self._entries[end].is_separator:
            end += 1
        return range(sep_idx, end)

    def _sep_block_has_disabled(self, sep_idx: int) -> bool:
        """True if this separator's block contains at least one disabled mod."""
        for i in self._sep_block_range(sep_idx):
            if not self._entries[i].is_separator and not self._entries[i].enabled:
                return True
        return False

    def _sep_block_has_enabled(self, sep_idx: int) -> bool:
        """True if this separator's block contains at least one enabled mod."""
        for i in self._sep_block_range(sep_idx):
            if not self._entries[i].is_separator and self._entries[i].enabled:
                return True
        return False

    def _sep_block_has_conflict_in(self, sep_idx: int, allowed: set) -> bool:
        """True if this separator's block contains at least one mod whose conflict status is in allowed."""
        for i in self._sep_block_range(sep_idx):
            if not self._entries[i].is_separator:
                if self._conflict_map.get(self._entries[i].name, CONFLICT_NONE) in allowed:
                    return True
        return False

    def _sep_block_has_missing_reqs(self, sep_idx: int) -> bool:
        """True if this separator's block contains at least one mod with missing requirements (not ignored)."""
        for i in self._sep_block_range(sep_idx):
            if not self._entries[i].is_separator:
                name = self._entries[i].name
                if name in self._missing_reqs and name not in self._ignored_missing_reqs:
                    return True
        return False

    def _on_mouse_drag(self, event):
        if self._drag_idx < 0 or not self._entries:
            return

        # Track cursor position for ghost rendering
        self._drag_cursor_y = event.y

        # Auto-scroll near edges
        h = self._canvas.winfo_height()
        if event.y < 40:
            self._canvas.yview("scroll", -1, "units")
        elif event.y > h - 40:
            self._canvas.yview("scroll",  1, "units")

        cy = self._event_canvas_y(event)
        blk_size = len(self._drag_block) if self._drag_is_block else 1

        # Compute visible indices; for a collapsed separator drag the hidden
        # mods are already excluded from this list so we only subtract the
        # *visible* portion of the dragged block (usually just 1 — the separator).
        vis = self._compute_visible_indices()
        drag_set = set(range(self._drag_idx, self._drag_idx + blk_size))
        drag_vis_count = sum(1 for i in drag_set if i in set(vis))
        n_rendered = len(vis) - drag_vis_count

        # Which slot in the rendered list (without dragged items) is the cursor over?
        slot = max(0, min(int(cy // self.ROW_H), n_rendered))

        self._drag_moved = True
        self._drag_slot = slot
        self._drag_target_slot = slot

        # Redraw with ghost at cursor and insertion line at target slot
        self._redraw()
        self._draw_drag_overlay()

    def _on_mouse_release(self, event):
        # Cancel pending drag timer if the user released before the hold delay
        was_pending = self._drag_pending
        self._cancel_drag_timer()
        if self._drag_idx >= 0 and self._drag_moved:
            # Commit the deferred move now that the user released the mouse.
            slot = self._drag_target_slot
            blk_size = len(self._drag_block) if self._drag_is_block else 1
            vis = self._compute_visible_indices()
            drag_set = set(range(self._drag_idx, self._drag_idx + blk_size))
            vis_without_drag = [i for i in vis if i not in drag_set]

            if self._drag_is_block:
                del self._entries[self._drag_idx:self._drag_idx + blk_size]
                del self._check_buttons[self._drag_idx:self._drag_idx + blk_size]
                del self._check_vars[self._drag_idx:self._drag_idx + blk_size]

                if slot >= len(vis_without_drag):
                    insert_at = len(self._entries)
                else:
                    target_orig = vis_without_drag[slot]
                    insert_at = target_orig - sum(1 for d in drag_set if d < target_orig)
                insert_at = max(1, min(insert_at, len(self._entries)))
                if (self._entries and self._entries[-1].name == ROOT_FOLDER_NAME
                        and insert_at > len(self._entries) - 1):
                    insert_at = len(self._entries) - 1

                for j, (entry, cb, var) in enumerate(self._drag_block):
                    self._entries.insert(insert_at + j, entry)
                    self._check_buttons.insert(insert_at + j, cb)
                    self._check_vars.insert(insert_at + j, var)
                self._drag_idx = insert_at
            else:
                entry = self._entries.pop(self._drag_idx)
                cb    = self._check_buttons.pop(self._drag_idx)
                var   = self._check_vars.pop(self._drag_idx)

                if slot >= len(vis_without_drag):
                    insert_at = len(self._entries)
                else:
                    target_orig = vis_without_drag[slot]
                    insert_at = target_orig - (1 if self._drag_idx < target_orig else 0)
                insert_at = max(0, min(insert_at, len(self._entries)))
                if (self._entries and self._entries[-1].name == ROOT_FOLDER_NAME
                        and insert_at > len(self._entries) - 1):
                    insert_at = len(self._entries) - 1

                self._entries.insert(insert_at, entry)
                self._check_buttons.insert(insert_at, cb)
                self._check_vars.insert(insert_at, var)
                self._drag_idx = insert_at
                self._sel_idx  = insert_at

            for i, cb2 in enumerate(self._check_buttons):
                if cb2 is not None:
                    cb2.configure(command=lambda idx=i: self._on_toggle(idx))
            self._save_modlist()
            self._rebuild_filemap()
        elif was_pending and self._drag_idx < 0:
            # Click (no drag) inside a multi-selection — collapse to the clicked item
            cy = self._event_canvas_y(event)
            clicked = self._canvas_y_to_index(cy)
            if clicked in self._sel_set:
                self._sel_idx = clicked
                self._sel_set = {clicked}
                self._update_info()
        self._drag_idx = -1
        self._drag_origin_idx = -1
        self._drag_moved = False
        self._drag_slot  = -1
        self._drag_target_slot = -1
        self._drag_is_block = False
        self._redraw()
        self._update_info()

    def _on_mouse_motion(self, event):
        """Update hover highlight as the mouse moves over the modlist."""
        if not self._entries or self._drag_idx >= 0:
            return
        cy = self._event_canvas_y(event)
        vis = self._visible_indices
        row = cy // self.ROW_H
        new_hover = vis[row] if 0 <= row < len(vis) else -1
        if new_hover != self._hover_idx:
            self._hover_idx = new_hover
            self._redraw()

    def _on_mouse_leave(self, event):
        """Clear hover highlight when mouse leaves the canvas."""
        if self._hover_idx != -1:
            self._hover_idx = -1
            self._redraw()

    def _on_right_click(self, event):
        if not self._entries:
            return
        cy = self._event_canvas_y(event)
        idx = self._canvas_y_to_index(cy)
        entry = self._entries[idx]
        is_sep = entry.is_separator

        # If right-clicking outside the current selection, collapse to clicked item
        if idx not in self._sel_set:
            self._sel_idx = idx
            self._sel_set = {idx}
            self._redraw()

        # Find .ini files in this mod's staging folder (only for non-separators)
        ini_files: list[Path] = []
        mod_folder: Path | None = None
        if self._modlist_path is not None:
            staging_root = self._modlist_path.parent.parent.parent / "mods"
            if not is_sep:
                mod_dir = staging_root / entry.name
                mod_folder = mod_dir
                if mod_dir.is_dir():
                    ini_files = [p for p in sorted(mod_dir.rglob("*.ini"))
                                 if p.name.lower() != "meta.ini"]
            elif entry.name == OVERWRITE_NAME:
                mod_folder = staging_root.parent / "overwrite"
            elif entry.name == ROOT_FOLDER_NAME:
                mod_folder = staging_root.parent / "Root_Folder"

        self._show_context_menu(event.x_root, event.y_root, idx, is_sep, ini_files,
                                mod_folder=mod_folder)

    def _show_context_menu(self, x: int, y: int, idx: int, is_separator: bool,
                           ini_files: list[Path] | None = None,
                           mod_folder: Path | None = None):
        """Custom popup menu — grab_set captures all clicks; outside clicks dismiss it."""
        popup = tk.Toplevel(self._canvas)
        popup.wm_overrideredirect(True)
        popup.wm_geometry(f"+{x}+{y}")
        popup.configure(bg=BORDER)

        _alive = [True]
        _active_sub = [None]  # tracks the currently open submenu Toplevel

        def _close_active_sub():
            if _active_sub[0] is not None:
                try:
                    _active_sub[0].destroy()
                except tk.TclError:
                    pass
                _active_sub[0] = None

        def _dismiss(_event=None):
            if _alive[0]:
                _alive[0] = False
                _close_active_sub()
                popup.destroy()

        def _pick(cmd):
            if _alive[0]:
                _alive[0] = False
                _close_active_sub()
                popup.destroy()
                cmd()

        inner = tk.Frame(popup, bg=BG_PANEL, bd=0)
        inner.pack(padx=1, pady=1)

        is_overwrite   = self._entries[idx].name == OVERWRITE_NAME
        is_root_folder = self._entries[idx].name == ROOT_FOLDER_NAME
        is_synthetic   = is_overwrite or is_root_folder
        # items: list of (label, callback, is_submenu)
        items = [
            ("Add separator above", lambda: self._add_separator(idx, above=True), False),
            ("Add separator below", lambda: self._add_separator(idx, above=False), False),
        ]
        if self._modlist_path is not None and not is_synthetic:
            items.append(("Create empty mod below", lambda: self._create_empty_mod(idx), False))
        if is_separator and not is_synthetic:
            items.append(("Rename separator", lambda: self._rename_separator(idx), False))
            items.append(("Remove separator", lambda: self._remove_separator(idx), False))
        elif not is_separator and not self._entries[idx].locked:
            items.append(("Rename mod", lambda: self._rename_mod(idx), False))
            items.append(("Set priority…", lambda i=idx: self._set_priority(i), False))
            items.append(("Remove mod", lambda: self._remove_mod(idx), False))
            # Move to separator — collect separator names now so they're stable
            sep_names = [e.name for e in self._entries
                         if e.is_separator and e.name != OVERWRITE_NAME
                         and e.name != ROOT_FOLDER_NAME]
            if sep_names:
                items.append(("Move to separator ▶",
                               lambda sn=sep_names: self._show_separator_picker(
                                   idx, sn, parent_dismiss=_dismiss,
                                   parent_popup=popup), True))
            # INI files submenu
            if ini_files:
                items.append(("INI files ▶",
                               lambda inis=ini_files: self._show_ini_picker(
                                   inis, parent_dismiss=_dismiss,
                                   parent_popup=popup), True))
            # Deployment paths — which top-level folders to ignore
            if mod_folder is not None:
                mod_name_cap = self._entries[idx].name
                items.append(("Set deployment paths…",
                               lambda m=mod_name_cap, p=mod_folder: self._show_mod_strip_dialog(m, p), False))

        if mod_folder is not None:
            items.append(("Open folder", lambda p=mod_folder: self._open_folder(p), False))

        if not is_separator:
            conflict_status = self._conflict_map.get(self._entries[idx].name, CONFLICT_NONE)
            if conflict_status != CONFLICT_NONE:
                name_capture = self._entries[idx].name
                items.append(("Show Conflicts",
                               lambda n=name_capture: self._show_overwrites_dialog(n), False))

        # Nexus options: Open on Nexus / Update Mod
        if not is_separator and not is_synthetic and self._modlist_path is not None:
            mod_name_capture = self._entries[idx].name
            staging_root = self._modlist_path.parent.parent.parent / "mods"
            meta_path = staging_root / mod_name_capture / "meta.ini"
            if meta_path.is_file():
                try:
                    _ctx_meta = read_meta(meta_path)
                    if _ctx_meta.mod_id > 0:
                        # Prefer the current game's known domain over
                        # whatever MO2 stored in meta.ini
                        app = self.winfo_toplevel()
                        _cur_game = _GAMES.get(getattr(
                            getattr(app, "_topbar", None), "_game_var", tk.StringVar()).get(), None)
                        _domain = (
                            _cur_game.nexus_game_domain
                            if _cur_game and _cur_game.nexus_game_domain
                            else _ctx_meta.nexus_page_url.split("/mods/")[0].rsplit("/", 1)[-1]
                            if "/mods/" in _ctx_meta.nexus_page_url
                            else _ctx_meta.game_domain
                        )
                        nexus_url = f"https://www.nexusmods.com/{_domain}/mods/{_ctx_meta.mod_id}"
                        items.append(("Open on Nexus",
                                       lambda u=nexus_url: self._open_nexus_page(u), False))
                        # Endorse / Abstain based on current endorsement status
                        if _ctx_meta.endorsed:
                            items.append(("Abstain from Endorsement",
                                           lambda n=mod_name_capture, d=_domain, m=_ctx_meta:
                                               self._abstain_nexus_mod(n, d, m), False))
                        else:
                            items.append(("Endorse Mod",
                                           lambda n=mod_name_capture, d=_domain, m=_ctx_meta:
                                               self._endorse_nexus_mod(n, d, m), False))
                except Exception:
                    pass
            if mod_name_capture in self._update_mods:
                items.append(("Update Mod",
                               lambda n=mod_name_capture: self._update_nexus_mod(n), False))
            if mod_name_capture in self._missing_reqs:
                dep_names = self._missing_reqs_detail.get(mod_name_capture, [])
                items.append(("Missing Requirements",
                               lambda n=mod_name_capture, d=dep_names: self._show_missing_reqs(n, d), False))

        # Multi-selection options: enable/disable/remove selected mods
        if len(self._sel_set) > 1:
            # Collect toggleable mods in selection (non-separator, non-locked, non-synthetic)
            toggleable = [
                i for i in sorted(self._sel_set)
                if 0 <= i < len(self._entries)
                and not self._entries[i].is_separator
                and not self._entries[i].locked
                and self._entries[i].name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
            ]
            if toggleable:
                count = len(toggleable)
                items.append((f"Enable selected ({count})",
                               lambda idxs=toggleable: self._enable_selected_mods(idxs), False))
                items.append((f"Disable selected ({count})",
                               lambda idxs=toggleable: self._disable_selected_mods(idxs), False))
                if self._modlist_path is not None:
                    items.append((f"Remove selected ({count})",
                                   lambda idxs=toggleable: self._remove_selected_mods(idxs), False))

        for label, cmd, is_submenu in items:
            btn = tk.Label(
                inner, text=label, anchor="w",
                bg=BG_PANEL, fg=TEXT_MAIN,
                font=("Segoe UI", 11),
                padx=12, pady=5, cursor="hand2",
            )
            btn.pack(fill="x")
            if is_submenu:
                def _open_sub(_e, b=btn, c=cmd):
                    _close_active_sub()
                    b.configure(bg=BG_SELECT)
                    _active_sub[0] = c()
                def _leave_sub(_e, b=btn):
                    b.configure(bg=BG_PANEL)
                    # Small delay so the user can move to the submenu
                    def _check_close():
                        if _active_sub[0] is None:
                            return
                        try:
                            px, py = popup.winfo_pointerxy()
                            # Check if pointer is over the submenu
                            sx = _active_sub[0].winfo_rootx()
                            sy = _active_sub[0].winfo_rooty()
                            sw = _active_sub[0].winfo_width()
                            sh = _active_sub[0].winfo_height()
                            if sx <= px <= sx + sw and sy <= py <= sy + sh:
                                return
                            # Check if pointer is over the parent popup
                            wx = popup.winfo_rootx()
                            wy = popup.winfo_rooty()
                            ww = popup.winfo_width()
                            wh = popup.winfo_height()
                            if wx <= px <= wx + ww and wy <= py <= wy + wh:
                                return
                            _close_active_sub()
                        except tk.TclError:
                            pass
                    popup.after(150, _check_close)
                btn.bind("<Enter>", _open_sub)
                btn.bind("<Leave>", _leave_sub)
            else:
                def _enter_normal(_e, b=btn):
                    _close_active_sub()
                    b.configure(bg=BG_SELECT)
                btn.bind("<ButtonRelease-1>", lambda _e, c=cmd: _pick(c))
                btn.bind("<Enter>", _enter_normal)
                btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=BG_PANEL))

        popup.update_idletasks()

        # Reposition if the popup would go off-screen.
        # Use the main app window's bottom edge as the limit — this is
        # more reliable than winfo_screenheight() on Steam Deck / Wayland /
        # gamescope where the reported screen size may not match usable area.
        pw = popup.winfo_reqwidth()
        ph = popup.winfo_reqheight()
        _app_toplevel = self.winfo_toplevel()
        app_bottom = _app_toplevel.winfo_rooty() + _app_toplevel.winfo_height()
        app_right  = _app_toplevel.winfo_rootx() + _app_toplevel.winfo_width()
        nx = x if x + pw <= app_right else max(0, x - pw)
        ny = y if y + ph <= app_bottom else max(0, y - ph)
        popup.wm_geometry(f"+{nx}+{ny}")

        # Dismiss when the application loses focus (e.g. Alt-Tab)
        def _on_focus_out(event):
            # Only dismiss if focus left the popup itself
            try:
                focus_w = popup.focus_get()
                if focus_w is None:
                    _dismiss()
            except (tk.TclError, KeyError):
                _dismiss()
        popup.bind("<FocusOut>", _on_focus_out)

        # Also watch the top-level app window
        _app_toplevel = self.winfo_toplevel()
        def _on_app_focus_out(_event):
            if _alive[0]:
                _dismiss()
        _app_toplevel.bind("<FocusOut>", _on_app_focus_out, add="+")
        # Unbind when popup closes to avoid leaking bindings
        _orig_dismiss = _dismiss
        def _dismiss_and_unbind(_event=None):
            try:
                _app_toplevel.unbind("<FocusOut>")
            except (tk.TclError, KeyError):
                pass
            _orig_dismiss(_event)
        _dismiss = _dismiss_and_unbind

        popup.bind("<Escape>", _dismiss)

        def _on_press(event):
            if not _alive[0]:
                return
            ex, ey = event.x_root, event.y_root
            # Check if click is inside the parent popup
            wx, wy = popup.winfo_rootx(), popup.winfo_rooty()
            ww, wh = popup.winfo_width(), popup.winfo_height()
            if wx <= ex <= wx + ww and wy <= ey <= wy + wh:
                return
            # Check if click is inside the active submenu
            if _active_sub[0] is not None:
                try:
                    sx, sy = _active_sub[0].winfo_rootx(), _active_sub[0].winfo_rooty()
                    sw, sh = _active_sub[0].winfo_width(), _active_sub[0].winfo_height()
                    if sx <= ex <= sx + sw and sy <= ey <= sy + sh:
                        return
                except tk.TclError:
                    pass
            _dismiss()
        popup.bind_all("<ButtonPress-1>", _on_press)
        popup.bind_all("<ButtonPress-3>", _on_press)

    def _on_root_folder_toggle(self) -> None:
        if ROOT_FOLDER_NAME in self._lock_widgets:
            self._root_folder_enabled = self._lock_widgets[ROOT_FOLDER_NAME][0].get()
            self._save_root_folder_state()
            # Update the synthetic entry's enabled state in-place
            for entry in self._entries:
                if entry.name == ROOT_FOLDER_NAME:
                    entry.enabled = self._root_folder_enabled
                    break
            self._redraw()

    def _on_sep_lock_toggle(self, sep_name: str) -> None:
        if sep_name in self._lock_widgets:
            locked = self._lock_widgets[sep_name][0].get()
            self._sep_locks[sep_name] = locked
            self._save_sep_locks()

    def _toggle_collapse(self, sep_name: str) -> None:
        if sep_name in self._collapsed_seps:
            self._collapsed_seps.discard(sep_name)
        else:
            self._collapsed_seps.add(sep_name)
        self._save_collapsed()
        self._update_expand_collapse_all_btn()
        self._redraw()

    def _toggleable_separator_names(self) -> list[str]:
        """Separator names that can be collapsed (excludes Overwrite and Root Folder)."""
        return [e.name for e in self._entries
                if e.is_separator and e.name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)]

    def _update_expand_collapse_all_btn(self) -> None:
        if not getattr(self, "_expand_collapse_all_btn", None):
            return
        sep_names = self._toggleable_separator_names()
        if not sep_names:
            self._expand_collapse_all_btn.configure(text="Expand all")
            return
        any_collapsed = any(s in self._collapsed_seps for s in sep_names)
        self._expand_collapse_all_btn.configure(
            text="Expand all" if any_collapsed else "Collapse all"
        )

    def _toggle_all_separators(self) -> None:
        sep_names = self._toggleable_separator_names()
        if not sep_names:
            return
        sep_set = set(sep_names)
        if all(s in self._collapsed_seps for s in sep_names):
            self._collapsed_seps -= sep_set
        else:
            self._collapsed_seps |= sep_set
        self._save_collapsed()
        self._update_expand_collapse_all_btn()
        self._redraw()

    def _remove_separator(self, idx: int):
        if 0 <= idx < len(self._entries) and self._entries[idx].is_separator:
            sname = self._entries[idx].name
            self._entries.pop(idx)
            self._check_vars.pop(idx)
            self._check_buttons.pop(idx)
            # Clean up lock widget for this separator
            if sname in self._lock_widgets:
                self._lock_widgets[sname][1].destroy()
                del self._lock_widgets[sname]
            self._sep_locks.pop(sname, None)
            self._save_sep_locks()
            self._collapsed_seps.discard(sname)
            self._save_collapsed()
            self._update_expand_collapse_all_btn()
            if self._sel_idx == idx:
                self._sel_idx = -1
            elif self._sel_idx > idx:
                self._sel_idx -= 1
            self._save_modlist()
            self._rebuild_filemap()
            self._redraw()
            self._update_info()

    def _remove_mod(self, idx: int):
        if not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        if entry.is_separator:
            return
        confirmed = tk.messagebox.askyesno(
            "Remove Mod",
            f"Remove '{entry.name}'?\n\nThis will delete the mod folder and cannot be undone.",
            parent=self.winfo_toplevel(),
        )
        if not confirmed:
            return
        # Delete the mod folder from staging
        if self._modlist_path is not None:
            # Staging path is <profiles_root>/<game>/mods/<mod_name>
            staging = self._modlist_path.parent.parent.parent / "mods" / entry.name
            if staging.is_dir():
                shutil.rmtree(staging)
        # Remove from lists
        self._entries.pop(idx)
        cb = self._check_buttons.pop(idx)
        self._check_vars.pop(idx)
        if cb is not None:
            cb.destroy()
        if self._sel_idx == idx:
            self._sel_idx = -1
        elif self._sel_idx > idx:
            self._sel_idx -= 1
        # Fix toggle callbacks for shifted rows
        for i, cb2 in enumerate(self._check_buttons):
            if cb2 is not None:
                cb2.configure(command=lambda i=i: self._on_toggle(i))
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _enable_selected_mods(self, indices: list[int]):
        """Enable all mods at the given indices."""
        for i in indices:
            if 0 <= i < len(self._entries):
                self._entries[i].enabled = True
                if i < len(self._check_vars) and self._check_vars[i] is not None:
                    self._check_vars[i].set(True)
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _disable_selected_mods(self, indices: list[int]):
        """Disable all mods at the given indices."""
        for i in indices:
            if 0 <= i < len(self._entries):
                self._entries[i].enabled = False
                if i < len(self._check_vars) and self._check_vars[i] is not None:
                    self._check_vars[i].set(False)
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _remove_selected_mods(self, indices: list[int]):
        """Remove multiple mods at once (with confirmation)."""
        names = [self._entries[i].name for i in indices
                 if 0 <= i < len(self._entries)]
        if not names:
            return
        confirmed = tk.messagebox.askyesno(
            "Remove Mods",
            f"Remove {len(names)} selected mod(s)?\n\nThis will delete the mod folders and cannot be undone.",
            parent=self.winfo_toplevel(),
        )
        if not confirmed:
            return
        staging_root = None
        if self._modlist_path is not None:
            staging_root = self._modlist_path.parent.parent.parent / "mods"
        # Remove from highest index first to avoid shifting
        for i in sorted(indices, reverse=True):
            if not (0 <= i < len(self._entries)):
                continue
            entry = self._entries[i]
            if entry.is_separator:
                continue
            # Delete the mod folder from staging
            if staging_root is not None:
                staging = staging_root / entry.name
                if staging.is_dir():
                    shutil.rmtree(staging)
            self._entries.pop(i)
            cb = self._check_buttons.pop(i)
            self._check_vars.pop(i)
            if cb is not None:
                cb.destroy()
        self._sel_idx = -1
        self._sel_set = set()
        # Fix toggle callbacks for shifted rows
        for i, cb2 in enumerate(self._check_buttons):
            if cb2 is not None:
                cb2.configure(command=lambda i=i: self._on_toggle(i))
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _rename_mod(self, idx: int):
        if not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        if entry.is_separator:
            return
        top = self.winfo_toplevel()
        dlg = _RenameDialog(top, entry.name)
        top.wait_window(dlg)
        new_name = dlg.result
        if not new_name or new_name == entry.name:
            return
        # Rename staging folder on disk
        if self._modlist_path is not None:
            staging_root = self._modlist_path.parent.parent.parent / "mods"
            old_folder = staging_root / entry.name
            new_folder = staging_root / new_name
            if old_folder.is_dir():
                if new_folder.exists():
                    tk.messagebox.showerror(
                        "Rename Failed",
                        f"A mod named '{new_name}' already exists.",
                        parent=top,
                    )
                    return
                old_folder.rename(new_folder)
        # Update entry in memory
        entry.name = new_name
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _rename_separator(self, idx: int):
        if not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        if not entry.is_separator:
            return
        top = self.winfo_toplevel()
        dlg = _RenameDialog(top, entry.display_name)
        top.wait_window(dlg)
        new_display = dlg.result
        if not new_display:
            return
        new_name = new_display + "_separator"
        if new_name == entry.name:
            return
        # Update collapse/lock tracking keys
        old_name = entry.name
        if old_name in self._collapsed_seps:
            self._collapsed_seps.discard(old_name)
            self._collapsed_seps.add(new_name)
            self._save_collapsed()
            self._update_expand_collapse_all_btn()
        if old_name in self._sep_locks:
            self._sep_locks[new_name] = self._sep_locks.pop(old_name)
            self._save_sep_locks()
        if old_name in self._lock_widgets:
            self._lock_widgets[new_name] = self._lock_widgets.pop(old_name)
        entry.name = new_name
        self._save_modlist()
        self._redraw()

    def _show_separator_picker(self, mod_idx: int, sep_names: list[str],
                               parent_dismiss=None,
                               parent_popup=None) -> tk.Toplevel:
        """Show a second popup listing all separators; clicking one moves the mod below it.
        Returns the popup widget so the caller can manage its lifecycle."""
        popup = tk.Toplevel(self._canvas)
        popup.wm_overrideredirect(True)
        popup.configure(bg=BORDER)
        cx, cy = popup.winfo_pointerxy()

        _alive = [True]

        def _dismiss(_event=None):
            if _alive[0]:
                _alive[0] = False
                popup.destroy()

        def _pick(sep_name: str):
            if _alive[0]:
                _alive[0] = False
                popup.destroy()
                if parent_dismiss:
                    parent_dismiss()
                self._move_to_separator(mod_idx, sep_name)

        # Build display names
        displays = [
            name[:-len("_separator")] if name.endswith("_separator") else name
            for name in sep_names
        ]

        ROW_H      = 30   # px per item
        MAX_ROWS   = 20   # cap before scrollbar kicks in
        FONT       = ("Segoe UI", 11)
        PAD_X      = 24   # left+right padding around text

        # Measure width needed for the longest name
        tmp = tk.Label(popup, font=FONT, text="")
        tmp.update_idletasks()
        import tkinter.font as tkfont
        fnt = tkfont.Font(font=FONT)
        max_text_w = max((fnt.measure(d) for d in displays), default=100)
        tmp.destroy()
        popup_w = max_text_w + PAD_X * 2

        needs_scroll = len(sep_names) > MAX_ROWS
        visible_rows = min(len(sep_names), MAX_ROWS)
        popup_h      = visible_rows * ROW_H

        # Outer border frame
        outer = tk.Frame(popup, bg=BORDER, bd=0)
        outer.pack(padx=1, pady=1)

        if needs_scroll:
            # Canvas + scrollbar for long lists
            canvas = tk.Canvas(outer, bg=BG_PANEL, bd=0, highlightthickness=0,
                               width=popup_w, height=popup_h)
            vsb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=vsb.set)
            canvas.pack(side="left", fill="both", expand=True)
            vsb.pack(side="right", fill="y")
            inner = tk.Frame(canvas, bg=BG_PANEL, bd=0)
            canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")

            def _on_inner_resize(e):
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.itemconfigure(canvas_window, width=canvas.winfo_width())
            inner.bind("<Configure>", _on_inner_resize)
            canvas.bind("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
            canvas.bind("<Button-5>", lambda e: canvas.yview_scroll( 3, "units"))
        else:
            inner = tk.Frame(outer, bg=BG_PANEL, bd=0, width=popup_w)
            inner.pack(fill="both", expand=True)

        for name, display in zip(sep_names, displays):
            btn = tk.Label(
                inner, text=display, anchor="w",
                bg=BG_PANEL, fg=TEXT_MAIN,
                font=FONT,
                padx=12, pady=5, cursor="hand2",
                width=0,
            )
            btn.pack(fill="x")
            btn.bind("<ButtonRelease-1>", lambda _e, n=name: _pick(n))
            btn.bind("<Enter>", lambda _e, b=btn: b.configure(bg=BG_SELECT))
            btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=BG_PANEL))

        popup.update_idletasks()
        pw = popup.winfo_reqwidth()
        ph = popup.winfo_reqheight()
        _app_tl = self.winfo_toplevel()
        app_right  = _app_tl.winfo_rootx() + _app_tl.winfo_width()
        app_bottom = _app_tl.winfo_rooty() + _app_tl.winfo_height()
        if parent_popup is not None:
            # Position to the right of the parent menu
            px = parent_popup.winfo_rootx() + parent_popup.winfo_width()
            py = cy - ph // 2  # vertically centre on the cursor
        else:
            px = cx
            py = cy
        # Clamp to app window bounds
        px = min(px, app_right - pw)
        py = min(py, app_bottom - ph)
        px = max(px, 0)
        py = max(py, 0)
        popup.wm_geometry(f"+{px}+{py}")

        return popup

    def _show_ini_picker(self, ini_files: list[Path],
                         parent_dismiss=None,
                         parent_popup=None) -> tk.Toplevel:
        """Show a submenu listing all INI files; clicking one opens it.
        Returns the popup widget so the caller can manage its lifecycle."""
        popup = tk.Toplevel(self._canvas)
        popup.wm_overrideredirect(True)
        popup.configure(bg=BORDER)
        cx, cy = popup.winfo_pointerxy()

        _alive = [True]

        def _dismiss(_event=None):
            if _alive[0]:
                _alive[0] = False
                popup.destroy()

        def _pick(ini_path: Path):
            if _alive[0]:
                _alive[0] = False
                popup.destroy()
                if parent_dismiss:
                    parent_dismiss()
                self._open_ini(ini_path)

        displays = [f"Open {p.name}" for p in ini_files]

        ROW_H    = 30
        MAX_ROWS = 20
        FONT     = ("Segoe UI", 11)
        PAD_X    = 24

        tmp = tk.Label(popup, font=FONT, text="")
        tmp.update_idletasks()
        import tkinter.font as tkfont
        fnt = tkfont.Font(font=FONT)
        max_text_w = max((fnt.measure(d) for d in displays), default=100)
        tmp.destroy()
        popup_w = max_text_w + PAD_X * 2

        needs_scroll = len(ini_files) > MAX_ROWS
        visible_rows = min(len(ini_files), MAX_ROWS)
        popup_h      = visible_rows * ROW_H

        outer = tk.Frame(popup, bg=BORDER, bd=0)
        outer.pack(padx=1, pady=1)

        if needs_scroll:
            canvas = tk.Canvas(outer, bg=BG_PANEL, bd=0, highlightthickness=0,
                               width=popup_w, height=popup_h)
            vsb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=vsb.set)
            canvas.pack(side="left", fill="both", expand=True)
            vsb.pack(side="right", fill="y")
            inner = tk.Frame(canvas, bg=BG_PANEL, bd=0)
            canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")

            def _on_inner_resize(e):
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.itemconfigure(canvas_window, width=canvas.winfo_width())
            inner.bind("<Configure>", _on_inner_resize)
            canvas.bind("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
            canvas.bind("<Button-5>", lambda e: canvas.yview_scroll( 3, "units"))
        else:
            inner = tk.Frame(outer, bg=BG_PANEL, bd=0, width=popup_w)
            inner.pack(fill="both", expand=True)

        for ini_path, display in zip(ini_files, displays):
            btn = tk.Label(
                inner, text=display, anchor="w",
                bg=BG_PANEL, fg=TEXT_MAIN,
                font=FONT,
                padx=12, pady=5, cursor="hand2",
                width=0,
            )
            btn.pack(fill="x")
            btn.bind("<ButtonRelease-1>", lambda _e, p=ini_path: _pick(p))
            btn.bind("<Enter>", lambda _e, b=btn: b.configure(bg=BG_SELECT))
            btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=BG_PANEL))

        popup.update_idletasks()
        pw = popup.winfo_reqwidth()
        ph = popup.winfo_reqheight()
        _app_tl = self.winfo_toplevel()
        app_right  = _app_tl.winfo_rootx() + _app_tl.winfo_width()
        app_bottom = _app_tl.winfo_rooty() + _app_tl.winfo_height()
        if parent_popup is not None:
            px = parent_popup.winfo_rootx() + parent_popup.winfo_width()
            py = cy - ph // 2
        else:
            px = cx
            py = cy
        # Clamp to app window bounds
        px = min(px, app_right - pw)
        py = min(py, app_bottom - ph)
        px = max(px, 0)
        py = max(py, 0)
        popup.wm_geometry(f"+{px}+{py}")

        return popup

    def _show_mod_strip_dialog(self, mod_name: str, mod_folder: Path) -> None:
        """Open a dialog to set which folders (at any depth) to ignore during deployment.
        Checked folders are stripped so their contents deploy one level up."""
        if not mod_folder.is_dir():
            return

        win = tk.Toplevel(self.winfo_toplevel())
        win.title(f"Deployment paths — {mod_name}")
        win.configure(bg=BG_PANEL, highlightthickness=0,
                      highlightbackground=BG_PANEL, highlightcolor=BG_PANEL)
        win.transient(self.winfo_toplevel())
        win.resizable(True, True)
        # Single content frame with no border so no white edge from WM
        content = tk.Frame(win, bg=BG_PANEL, bd=0, highlightthickness=0)
        content.pack(fill="both", expand=True)

        msg = tk.Label(
            content, text="Select folders to ignore during deployment (at any depth).\n"
                          "Their contents will be deployed one level up:",
            bg=BG_PANEL, fg=TEXT_MAIN, font=FONT_SMALL,
            justify="left",
        )
        msg.pack(anchor="w", padx=12, pady=(12, 8))

        self._load_mod_strip_prefixes()
        current = self._mod_strip_prefixes.get(mod_name, [])
        # Support both formats: full paths (e.g. "Tree", "Meshes/Architecture") and legacy segment names only
        use_path_format = any("/" in p for p in current)
        current_set = {p.lower() for p in current} if use_path_format else {s.lower() for s in current}
        vars_map: dict[str, tk.BooleanVar] = {}  # rel_path -> var
        scroll_h = 320
        _scrollbar_bg = "#383838"
        list_frame = tk.Frame(content, bg=_scrollbar_bg, bd=0, highlightthickness=0)
        list_frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        _tree_bg = "#1a1a1a"
        _tree_style = "ModStrip.Treeview"
        _heading_style = "ModStrip.Treeview.Heading"
        style = ttk.Style()
        style.configure(_tree_style,
                        background=_tree_bg, foreground=TEXT_MAIN,
                        fieldbackground=_tree_bg, rowheight=22,
                        font=("Segoe UI", 10),
                        bordercolor=BG_ROW, borderwidth=1,
                        focuscolor=_tree_bg)
        style.configure(_heading_style,
                        background=BG_HEADER, foreground=TEXT_SEP,
                        font=("Segoe UI", 10), borderwidth=0)
        style.map(_tree_style,
                  background=[("selected", BG_SELECT), ("focus", _tree_bg)],
                  foreground=[("selected", TEXT_MAIN)])

        tree = ttk.Treeview(
            list_frame,
            columns=("check",),
            show="tree headings",
            style=_tree_style,
            selectmode="browse",
            height=scroll_h // 22,
        )
        tree.heading("#0", text="Folder", anchor="w")
        tree.heading("check", text="", anchor="w")
        tree.column("#0", minwidth=200, stretch=True)
        tree.column("check", width=28, stretch=False)

        vsb = tk.Scrollbar(
            list_frame, orient="vertical", command=tree.yview,
            bg=_scrollbar_bg, troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
        )
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        def _iid(rel_path: str) -> str:
            return rel_path.replace("/", "\u241f")

        def _rel(iid: str) -> str:
            return iid.replace("\u241f", "/")

        def _scroll_canvas(evt):
            if getattr(evt, "delta", 0) > 0:
                tree.yview_scroll(-3, "units")
            else:
                tree.yview_scroll(3, "units")
        tree.bind("<Button-4>", lambda e: tree.yview_scroll(-3, "units"))
        tree.bind("<Button-5>", lambda e: tree.yview_scroll(3, "units"))
        tree.bind("<MouseWheel>", _scroll_canvas)
        list_frame.bind("<Button-4>", lambda e: tree.yview_scroll(-3, "units"))
        list_frame.bind("<Button-5>", lambda e: tree.yview_scroll(3, "units"))
        list_frame.bind("<MouseWheel>", _scroll_canvas)
        content.bind("<MouseWheel>", _scroll_canvas)
        content.bind("<Button-4>", lambda e: tree.yview_scroll(-3, "units"))
        content.bind("<Button-5>", lambda e: tree.yview_scroll(3, "units"))
        win.bind("<MouseWheel>", _scroll_canvas)
        win.bind("<Button-4>", lambda e: tree.yview_scroll(-3, "units"))
        win.bind("<Button-5>", lambda e: tree.yview_scroll(3, "units"))

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
                vars_map[rel] = var
                check_char = "\u2611" if var.get() else "\u2610"  # ☑ / ☐
                iid = _iid(rel)
                tree.insert(parent_iid, "end", iid=iid, text=name, values=(check_char,),
                            open=False)
                _scan(rel, iid, depth + 1)

        _scan("", "", 0)

        def _on_toggle(evt):
            region = tree.identify_region(evt.x, evt.y)
            if region == "tree":
                return
            item = tree.identify_row(evt.y)
            if not item:
                return
            rel = _rel(item)
            if rel not in vars_map:
                return
            var = vars_map[rel]
            var.set(not var.get())
            tree.set(item, "check", "\u2611" if var.get() else "\u2610")

        tree.bind("<ButtonRelease-1>", _on_toggle)

        if not vars_map:
            tree.insert("", "end", iid="__none__", text="(No folders found in this mod.)", values=("",))
            vars_map["__none__"] = tk.BooleanVar(value=False)

        def _ok():
            chosen = [
                rel_path for rel_path, v in vars_map.items()
                if rel_path != "__none__" and v.get()
            ]
            self._mod_strip_prefixes[mod_name] = chosen
            self._save_mod_strip_prefixes()
            self._rebuild_filemap()
            self._redraw()
            win.destroy()

        def _cancel():
            win.destroy()

        def _clear_all():
            for rel_path, v in vars_map.items():
                if rel_path == "__none__":
                    continue
                v.set(False)
                try:
                    tree.set(_iid(rel_path), "check", "\u2610")
                except tk.TclError:
                    pass

        def _mkbtn(parent, text, cmd, bg, **kwargs):
            opts = dict(
                font=FONT_SMALL, relief="flat", overrelief="flat",
                padx=16, pady=4, cursor="hand2",
                highlightthickness=0, highlightbackground=bg, highlightcolor=bg,
                borderwidth=0, activebackground=bg, activeforeground=TEXT_MAIN,
            )
            opts.update(kwargs)
            return tk.Button(parent, text=text, command=cmd, bg=bg, fg=TEXT_MAIN, **opts)

        btn_frame = tk.Frame(content, bg=BG_ROW, bd=0, highlightthickness=0)
        btn_frame.pack(fill="x", padx=12, pady=(0, 12))
        _mkbtn(btn_frame, "OK", _ok, ACCENT).pack(side="right", padx=(8, 0))
        _mkbtn(btn_frame, "Cancel", _cancel, BG_ROW).pack(side="right")
        _mkbtn(btn_frame, "Clear all", _clear_all, BG_ROW).pack(side="right")

        win.update_idletasks()
        w, h = 430, 480
        win.geometry(f"{w}x{h}")
        win.minsize(360, 220)
        win.maxsize(0, h)  # cap height so scrollbar is used; 0 = no width cap
        # Center on the main window (or on screen if main window size not yet available)
        app = self.winfo_toplevel()
        ax = app.winfo_rootx()
        ay = app.winfo_rooty()
        aw = app.winfo_width()
        ah = app.winfo_height()
        if aw <= 1 or ah <= 1:
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            wx = max(0, (sw - w) // 2)
            wy = max(0, (sh - h) // 2)
        else:
            wx = ax + max(0, (aw - w) // 2)
            wy = ay + max(0, (ah - h) // 2)
        win.geometry(f"+{wx}+{wy}")

    def _move_to_separator(self, mod_idx: int, sep_name: str):
        """Move the mod at mod_idx to directly below the named separator."""
        if not (0 <= mod_idx < len(self._entries)):
            return
        # Find the separator's current index
        sep_idx = next(
            (i for i, e in enumerate(self._entries)
             if e.is_separator and e.name == sep_name),
            None,
        )
        if sep_idx is None:
            return

        # Pull the mod out
        entry = self._entries.pop(mod_idx)
        cb    = self._check_buttons.pop(mod_idx)
        var   = self._check_vars.pop(mod_idx)

        # Recalculate sep_idx after removal
        if mod_idx < sep_idx:
            sep_idx -= 1

        # Insert directly below the separator
        dest = sep_idx + 1
        self._entries.insert(dest, entry)
        self._check_buttons.insert(dest, cb)
        self._check_vars.insert(dest, var)

        # Fix toggle callbacks for all rows
        for i, cb2 in enumerate(self._check_buttons):
            if cb2 is not None:
                cb2.configure(command=lambda i=i: self._on_toggle(i))

        self._sel_idx = dest
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

        # Scroll the destination row into view
        self._canvas.yview_moveto(dest * self.ROW_H /
                                   max(len(self._entries) * self.ROW_H,
                                       self._canvas.winfo_height()))

    def _open_ini(self, path: Path):
        """Open an .ini file in the user's default text editor via xdg-open."""
        try:
            subprocess.Popen(["xdg-open", str(path)])
            self._log(f"Opened: {path.name}")
        except Exception as e:
            self._log(f"Could not open {path.name}: {e}")

    def _open_folder(self, path: Path) -> None:
        """Open a directory in the system file manager via xdg-open."""
        if not path.is_dir():
            self._log(f"Folder not found: {path}")
            return
        try:
            subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            self._log(f"Could not open folder: {e}")

    def _open_nexus_page(self, url: str) -> None:
        """Open a Nexus Mods page in the default browser."""
        if url:
            webbrowser.open(url)
            self._log(f"Nexus: Opened {url}")

    def _show_missing_reqs(self, mod_name: str, dep_names: list[str]) -> None:
        """Open a CTk window listing missing requirements in browse-tab style, with View/Install and Ignore checkbox."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        if api is None:
            self._log("Nexus: Set your API key first.")
            return
        if self._modlist_path is None:
            self._log("No profile loaded.")
            return
        topbar = getattr(app, "_topbar", None)
        game = _GAMES.get(topbar._game_var.get()) if topbar else None
        domain = (game.nexus_game_domain if game and game.is_configured() else "") or ""

        staging_root = self._modlist_path.parent.parent.parent / "mods"
        meta_path = staging_root / mod_name / "meta.ini"
        if not meta_path.is_file():
            self._log(f"{mod_name}: No meta.ini found.")
            return
        try:
            meta = read_meta(meta_path)
        except Exception:
            self._log(f"{mod_name}: Could not read meta.ini.")
            return
        if meta.mod_id <= 0:
            self._log(f"{mod_name}: No Nexus mod ID.")
            return
        if not domain and "/mods/" in meta.nexus_page_url:
            domain = meta.nexus_page_url.split("/mods/")[0].rsplit("/", 1)[-1]
        if not domain:
            self._log("Could not determine game domain.")
            return

        # Parse missing mod IDs from meta.ini
        missing_ids: set[int] = set()
        for pair in (meta.missing_requirements or "").split(";"):
            part = pair.split(":", 1)[0].strip()
            if part:
                try:
                    missing_ids.add(int(part))
                except ValueError:
                    pass

        win = ctk.CTkToplevel(app)
        win.title(f"Missing requirements — {mod_name}")
        win.geometry("640x400")
        win.minsize(400, 300)
        win.configure(fg_color=BG_PANEL)

        # Header
        header = ctk.CTkFrame(win, fg_color=BG_HEADER, corner_radius=0, height=36)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(
            header, text=f"Missing requirements for: {mod_name}",
            font=FONT_SMALL, text_color=TEXT_MAIN,
        ).pack(side="left", padx=10, pady=6)

        # Status (Loading… or error)
        status_var = tk.StringVar(value="Loading…")
        status_lbl = ctk.CTkLabel(
            win, textvariable=status_var,
            font=FONT_SMALL, text_color=TEXT_DIM,
        )
        status_lbl.pack(pady=20)

        # Scrollable list area (canvas + scrollbar) — built after fetch
        list_frame = tk.Frame(win, bg=BG_DEEP)
        list_frame.pack(fill="both", expand=True, padx=4, pady=4)
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)
        canvas = tk.Canvas(
            list_frame, bg=BG_DEEP, bd=0, highlightthickness=0,
            yscrollincrement=1, takefocus=0,
        )
        vsb = tk.Scrollbar(list_frame, orient="vertical", command=canvas.yview,
                           bg=BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
                           highlightthickness=0, bd=0)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        ROW_H = 56
        BTN_W = 70
        VIEW_W = 56
        NAME_PAD = 10

        def _on_wheel(e):
            if getattr(e, "delta", 0):
                canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
            return "break"

        canvas.bind("<MouseWheel>", _on_wheel)

        # Footer: Ignore checkbox + Close
        footer = ctk.CTkFrame(win, fg_color=BG_HEADER, corner_radius=0, height=44)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        ignore_var = tk.BooleanVar(value=mod_name in self._ignored_missing_reqs)
        ctk.CTkCheckBox(
            footer, text="Ignore requirements",
            variable=ignore_var,
            font=FONT_SMALL, text_color=TEXT_MAIN,
            checkbox_width=18, checkbox_height=18,
        ).pack(side="left", padx=12, pady=10)
        def _on_close():
            if ignore_var.get():
                self._ignored_missing_reqs.add(mod_name)
            else:
                self._ignored_missing_reqs.discard(mod_name)
            self._save_ignored_missing_reqs()
            self._redraw()
            win.destroy()
        ctk.CTkButton(
            footer, text="Close", width=80, height=28,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            command=_on_close,
        ).pack(side="right", padx=12, pady=8)

        # Resolve app that has _install_from_browse (may be parent of toplevel, not toplevel itself)
        _app = app
        for _ in range(5):
            if hasattr(_app, "_install_from_browse"):
                break
            _app = getattr(_app, "master", None) or getattr(_app, "parent", None)
            if _app is None:
                break
        install_from_browse = getattr(_app, "_install_from_browse", None) if _app else None

        def _mod_url(req: NexusModRequirement) -> str:
            return req.url or f"https://www.nexusmods.com/{domain or req.game_domain or ''}/mods/{req.mod_id}"

        def _on_install(req: NexusModRequirement):
            if install_from_browse is not None:
                entry = SimpleNamespace(
                    mod_id=req.mod_id,
                    domain_name=domain or req.game_domain or "",
                    name=req.mod_name or f"Mod {req.mod_id}",
                )
                install_from_browse(entry)
            else:
                # No install callback (e.g. wrong widget hierarchy) or user not premium: open mod in browser
                webbrowser.open(_mod_url(req))
                self._log(f"Nexus: Opened {req.mod_name} in browser.")

        def _populate(missing_list: list[NexusModRequirement]) -> None:
            status_lbl.pack_forget()
            canvas_w = [600]

            def _on_resize(ev):
                canvas_w[0] = max(ev.width, 200)
                _repaint()

            list_frame.bind("<Configure>", _on_resize)
            row_bounds: list[tuple[int, int]] = []
            view_btns: list[tk.Button] = []
            install_btns: list[tk.Button] = []

            def _repaint():
                canvas.delete("all")
                row_bounds.clear()
                cw = canvas_w[0]
                btn_left = cw - 2 * BTN_W - 16
                name_max_px = max(btn_left - NAME_PAD - 8, 20)
                y = 0
                for i, req in enumerate(missing_list):
                    y_top = y
                    notes = (req.notes or "").strip() or "No notes"
                    title = req.mod_name + (" (External)" if req.is_external else "")
                    # Measure wrapped notes height
                    line_h = 16
                    lines = 1
                    w = name_max_px
                    for chunk in notes.replace("\n", " ").split():
                        pass  # simplified: one line for notes
                    desc_h = min(line_h * 2, 32)
                    row_h = max(ROW_H, 24 + desc_h + 12)
                    y_bot = y_top + row_h
                    row_bounds.append((y_top, y_bot))
                    bg = BG_ROW_ALT if i % 2 else BG_ROW
                    canvas.create_rectangle(0, y_top, cw, y_bot, fill=bg, outline="")
                    canvas.create_text(
                        NAME_PAD, y_top + 12,
                        text=title[:80] + ("…" if len(title) > 80 else ""),
                        anchor="w", font=("Segoe UI", 11), fill=TEXT_MAIN,
                    )
                    canvas.create_text(
                        NAME_PAD, y_top + 30,
                        text=notes[:120] + ("…" if len(notes) > 120 else ""),
                        anchor="nw", width=name_max_px,
                        font=("Segoe UI", 10), fill=TEXT_DIM,
                    )
                    y = y_bot
                total_h = max(y, 1)
                canvas.configure(scrollregion=(0, 0, cw, total_h))

                # Buttons: create or reuse
                while len(view_btns) < len(missing_list):
                    idx = len(view_btns)
                    req = missing_list[idx]
                    url = req.url or f"https://www.nexusmods.com/{domain or req.game_domain}/mods/{req.mod_id}"
                    vb = tk.Button(
                        canvas, text="View",
                        bg=ACCENT, fg="#ffffff", activebackground=ACCENT_HOV,
                        relief="flat", font=("Segoe UI", 10), bd=0,
                        highlightthickness=0, cursor="hand2",
                        command=lambda u=url: webbrowser.open(u),
                    )
                    ib = tk.Button(
                        canvas, text="Install",
                        bg="#2d7a2d", fg="#ffffff", activebackground="#3a9e3a",
                        relief="flat", font=("Segoe UI", 10), bd=0,
                        highlightthickness=0, cursor="hand2",
                        command=lambda r=req: _on_install(r),
                    )
                    view_btns.append(vb)
                    install_btns.append(ib)
                for idx in range(len(missing_list)):
                    y_top, y_bot = row_bounds[idx]
                    cy = y_top + (y_bot - y_top) // 2
                    vx = cw - BTN_W - 4 - BTN_W - 4
                    ix = cw - BTN_W - 4
                    canvas.create_window(vx, cy, window=view_btns[idx], width=VIEW_W, height=28, tags="btns")
                    canvas.create_window(ix, cy, window=install_btns[idx], width=BTN_W, height=28, tags="btns")

            _repaint()

        def _fetch_done(missing_list: list[NexusModRequirement] | None, err: str | None) -> None:
            def _run():
                if err:
                    status_var.set(err)
                    return
                if not missing_list:
                    status_var.set("No missing requirements (list is empty).")
                    return
                _populate(missing_list)
            app.after(0, _run)

        def _worker():
            err = None
            missing_list: list[NexusModRequirement] = []
            try:
                all_reqs = api.get_mod_requirements(domain, meta.mod_id)
                for r in all_reqs:
                    if r.mod_id in missing_ids:
                        missing_list.append(r)
            except Exception as e:
                err = f"Could not load requirements: {e}"
            _fetch_done(missing_list, err)

        threading.Thread(target=_worker, daemon=True).start()

    def _endorse_nexus_mod(self, mod_name: str, domain: str, meta) -> None:
        """Endorse a mod on Nexus Mods in a background thread."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        if api is None:
            self._log("Nexus: Set your API key first.")
            return
        log_fn = self._log

        def _worker():
            try:
                result = api.endorse_mod(domain, meta.mod_id, meta.version)
                def _done(res):
                    log_fn(f"Nexus: Endorsed '{mod_name}' ({meta.mod_id}).")
                    if res is not None:
                        body = json.dumps(res, indent=None)
                        log_fn(f"  Response: {body[:500]}{'...' if len(body) > 500 else ''}")
                    # Update meta.ini
                    try:
                        if self._modlist_path is not None:
                            staging_root = self._modlist_path.parent.parent.parent / "mods"
                            meta_path = staging_root / mod_name / "meta.ini"
                            if meta_path.is_file():
                                m = read_meta(meta_path)
                                m.endorsed = True
                                write_meta(meta_path, m)
                    except Exception:
                        pass
                    self._endorsed_mods.add(mod_name)
                    self._redraw()
                app.after(0, lambda: _done(result))
            except Exception as exc:
                app.after(0, lambda: log_fn(f"Nexus: Endorse failed — {exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _abstain_nexus_mod(self, mod_name: str, domain: str, meta) -> None:
        """Abstain from endorsing a mod on Nexus Mods in a background thread."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        if api is None:
            self._log("Nexus: Set your API key first.")
            return
        log_fn = self._log

        def _worker():
            try:
                result = api.abstain_mod(domain, meta.mod_id, meta.version)
                def _done(res):
                    log_fn(f"Nexus: Abstained from '{mod_name}' ({meta.mod_id}).")
                    if res is not None:
                        body = json.dumps(res, indent=None)
                        log_fn(f"  Response: {body[:500]}{'...' if len(body) > 500 else ''}")
                    # Update meta.ini
                    try:
                        if self._modlist_path is not None:
                            staging_root = self._modlist_path.parent.parent.parent / "mods"
                            meta_path = staging_root / mod_name / "meta.ini"
                            if meta_path.is_file():
                                m = read_meta(meta_path)
                                m.endorsed = False
                                write_meta(meta_path, m)
                    except Exception:
                        pass
                    self._endorsed_mods.discard(mod_name)
                    self._redraw()
                app.after(0, lambda: _done(result))
            except Exception as exc:
                app.after(0, lambda: log_fn(f"Nexus: Abstain failed — {exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _update_nexus_mod(self, mod_name: str) -> None:
        """Download the latest version of a mod from Nexus and install it."""
        app = self.winfo_toplevel()
        if getattr(app, "_nexus_api", None) is None:
            self._log("Nexus: Set your API key first (Nexus button).")
            return
        if self._modlist_path is None:
            return
        staging_root = self._modlist_path.parent.parent.parent / "mods"
        meta_path = staging_root / mod_name / "meta.ini"
        if not meta_path.is_file():
            self._log(f"Nexus: No metadata for {mod_name}")
            return
        try:
            meta = read_meta(meta_path)
        except Exception as exc:
            self._log(f"Nexus: Could not read metadata — {exc}")
            return
        if meta.latest_file_id <= 0:
            self._log(f"Nexus: No update info for {mod_name} — run Check Updates first.")
            return

        game_name = app._topbar._game_var.get()
        game = _GAMES.get(game_name)
        if game is None or not game.is_configured():
            self._log("Nexus: No configured game selected.")
            return
        game_domain = game.nexus_game_domain or meta.game_domain

        self._log(f"Nexus: Updating {mod_name}...")
        self.show_download_progress(f"Updating: {mod_name}")
        log_fn = self._log
        mod_panel = self

        def _worker():
            api = app._nexus_api
            downloader = app._nexus_downloader

            # Check if the user is premium
            is_premium = False
            try:
                user = api.validate()
                is_premium = user.is_premium
            except Exception:
                pass

            if not is_premium:
                # Free user — open the mod's files page in the browser
                files_url = f"https://www.nexusmods.com/{game_domain}/mods/{meta.mod_id}?tab=files"
                def _fallback():
                    mod_panel.hide_download_progress()
                    webbrowser.open(files_url)
                    log_fn(f"Nexus: Premium required for direct download.")
                    log_fn(f"Nexus: Opened files page — click \"Download with Mod Manager\" there.")
                app.after(0, _fallback)
                return

            # Premium user — direct download
            mod_info = None
            file_info = None
            try:
                mod_info = api.get_mod(game_domain, meta.mod_id)
                files_resp = api.get_mod_files(game_domain, meta.mod_id)
                for f in files_resp.files:
                    if f.file_id == meta.latest_file_id:
                        file_info = f
                        break
            except Exception:
                pass

            result = downloader.download_file(
                game_domain=game_domain,
                mod_id=meta.mod_id,
                file_id=meta.latest_file_id,
                progress_cb=lambda cur, total: app.after(
                    0, lambda c=cur, t=total: mod_panel.update_download_progress(c, t)
                ),
            )

            if result.success and result.file_path:
                def _install():
                    mod_panel.hide_download_progress()
                    log_fn(f"Nexus: Installing update for {mod_name}...")
                    install_mod_from_archive(
                        str(result.file_path), app, log_fn, game, mod_panel)
                    # Update metadata
                    try:
                        new_meta = build_meta_from_download(
                            game_domain=game_domain,
                            mod_id=meta.mod_id,
                            file_id=meta.latest_file_id,
                            archive_name=result.file_name,
                            mod_info=mod_info,
                            file_info=file_info,
                        )
                        new_meta.has_update = False
                        # Write to the original mod folder (user may have renamed)
                        write_meta(meta_path, new_meta)
                    except Exception as exc:
                        log_fn(f"Nexus: Warning — could not update metadata: {exc}")
                    # Refresh update flags
                    mod_panel._scan_update_flags()
                    mod_panel._redraw()
                    log_fn(f"Nexus: {mod_name} updated successfully.")
                app.after(0, _install)
            else:
                def _fail():
                    mod_panel.hide_download_progress()
                    log_fn(f"Nexus: Update download failed — {result.error}")
                app.after(0, _fail)

        threading.Thread(target=_worker, daemon=True).start()

    def _show_overwrites_dialog(self, mod_name: str) -> None:
        """Open the conflict detail dialog for a mod."""
        if self._modlist_path is None:
            return
        filemap_path = self._modlist_path.parent.parent.parent / "filemap.txt"
        staging_root = self._modlist_path.parent.parent.parent / "mods"

        # Build winner map: lowercase_rel -> (original_rel, winning_mod)
        winning_map: dict[str, tuple[str, str]] = {}
        if filemap_path.is_file():
            with filemap_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if "\t" not in line:
                        continue
                    rel_path, winner = line.split("\t", 1)
                    winning_map[rel_path.lower()] = (rel_path, winner)

        # Walk this mod's staging folder to get its file set
        my_staging = staging_root / mod_name
        my_files: dict[str, str] = {}   # lowercase_rel -> original_rel
        if my_staging.is_dir():
            for dirpath, _, fnames in os.walk(my_staging):
                for fname in fnames:
                    if fname.lower() == "meta.ini":
                        continue
                    full = os.path.join(dirpath, fname)
                    rel = os.path.relpath(full, my_staging).replace("\\", "/")
                    my_files[rel.lower()] = rel

        # Classify each file
        files_i_win:  list[tuple[str, str]] = []   # (path, beaten mods str)
        files_i_lose: list[tuple[str, str]] = []   # (path, winner mod)

        for rel_lower, orig_rel in sorted(my_files.items()):
            if rel_lower in winning_map:
                orig, winner = winning_map[rel_lower]
                if winner == mod_name:
                    files_i_win.append((orig, ""))
                else:
                    files_i_lose.append((orig, winner))
            else:
                files_i_lose.append((orig_rel, "(no winner — disabled?)"))

        # Annotate wins: find which specific mods are beaten per file
        beaten_mods = self._overrides.get(mod_name, set())
        rel_to_losers: dict[str, list[str]] = {}
        for loser_mod in beaten_mods:
            loser_staging = staging_root / loser_mod
            if not loser_staging.is_dir():
                continue
            for dirpath, _, fnames in os.walk(loser_staging):
                for fname in fnames:
                    if fname.lower() == "meta.ini":
                        continue
                    full = os.path.join(dirpath, fname)
                    rel = os.path.relpath(full, loser_staging).replace("\\", "/").lower()
                    if rel in my_files:
                        rel_to_losers.setdefault(rel, []).append(loser_mod)

        files_i_win_final: list[tuple[str, str]] = [
            (orig, ", ".join(rel_to_losers.get(orig.lower(), [])))
            for orig, _ in files_i_win
        ]

        _OverwritesDialog(
            self.winfo_toplevel(),
            mod_name=mod_name,
            files_win=files_i_win_final,
            files_lose=files_i_lose,
        )

    def _add_separator(self, ref_idx: int, above: bool):
        """Prompt for a separator name and insert it above or below ref_idx."""
        dialog = _SeparatorNameDialog(self.winfo_toplevel())
        self.winfo_toplevel().wait_window(dialog)
        if dialog.result is None:
            return
        sep_name = dialog.result.strip() + "_separator"
        insert_at = ref_idx if above else ref_idx + 1
        entry = ModEntry(name=sep_name, enabled=True, locked=True, is_separator=True)
        self._entries.insert(insert_at, entry)
        # Keep check_vars / check_buttons aligned (None for separators)
        self._check_vars.insert(insert_at, None)
        self._check_buttons.insert(insert_at, None)
        # Fix toggle callbacks for rows that shifted
        for i, cb in enumerate(self._check_buttons):
            if cb is not None:
                cb.configure(command=lambda idx=i: self._on_toggle(idx))
        if self._sel_idx >= insert_at:
            self._sel_idx += 1
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _create_empty_mod(self, ref_idx: int):
        """Prompt for a mod name, create an empty staging folder, and insert a new mod entry below ref_idx."""
        if self._modlist_path is None:
            return
        dialog = _ModNameDialog(self.winfo_toplevel())
        self.winfo_toplevel().wait_window(dialog)
        if dialog.result is None:
            return
        mod_name = dialog.result.strip()
        if not mod_name:
            return
        # Check for name collision
        existing = {e.name for e in self._entries}
        if mod_name in existing:
            tk.messagebox.showerror(
                "Name Conflict",
                f"A mod or separator named '{mod_name}' already exists.",
                parent=self.winfo_toplevel(),
            )
            return
        # Create the staging folder
        staging = self._modlist_path.parent.parent.parent / "mods" / mod_name
        staging.mkdir(parents=True, exist_ok=True)
        # Write a minimal meta.ini so MO2 recognizes the folder
        (staging / "meta.ini").write_text("[General]\n", encoding="utf-8")
        insert_at = ref_idx + 1
        entry = ModEntry(name=mod_name, enabled=True, locked=False, is_separator=False)
        self._entries.insert(insert_at, entry)
        # Create checkbox widgets for the new mod
        var = tk.BooleanVar(value=True)
        cb = tk.Checkbutton(
            self._canvas, variable=var,
            bg=BG_ROW, activebackground=BG_ROW, selectcolor=BG_DEEP,
            fg=TEXT_MAIN, indicatoron=True,
            bd=0, highlightthickness=0,
            command=lambda idx=insert_at: self._on_toggle(idx),
        )
        self._check_vars.insert(insert_at, var)
        self._check_buttons.insert(insert_at, cb)
        # Fix toggle callbacks for all rows
        for i, cb2 in enumerate(self._check_buttons):
            if cb2 is not None:
                cb2.configure(command=lambda idx=i: self._on_toggle(idx))
        if self._sel_idx >= insert_at:
            self._sel_idx += 1
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()
        self._log(f"Created empty mod: {mod_name}")

    # ------------------------------------------------------------------
    # Toggle
    # ------------------------------------------------------------------

    def _on_toggle(self, idx: int):
        if not self._check_vars or not self._entries:
            return
        if 0 <= idx < len(self._entries) and idx < len(self._check_vars):
            var = self._check_vars[idx]
            if var is None:
                return
            self._entries[idx].enabled = var.get()
            self._save_modlist()
            self._rebuild_filemap()
            self._redraw()
            self._update_info()

    # ------------------------------------------------------------------
    # Move Up / Down buttons
    # ------------------------------------------------------------------

    def _on_check_updates(self):
        """Check all installed Nexus mods for updates and missing requirements."""
        app = self.winfo_toplevel()
        if app._nexus_api is None:
            self._log("Nexus: Set your API key first (Nexus button).")
            return
        game = self._game
        if game is None or not game.is_configured():
            self._log("No configured game selected.")
            return

        staging = game.get_mod_staging_path()
        self._update_btn.configure(text="Checking...", state="disabled")
        log_fn = self._log

        def _worker():
            try:
                results = check_for_updates(
                    app._nexus_api, staging,
                    game_domain=game.nexus_game_domain,
                    progress_cb=lambda m: app.after(0, lambda msg=m: log_fn(msg)),
                )
                app.after(0, lambda: log_fn("Nexus: Checking mod requirements..."))
                missing = check_missing_requirements(
                    app._nexus_api, staging,
                    game_domain=game.nexus_game_domain,
                    progress_cb=lambda m: app.after(0, lambda msg=m: log_fn(msg)),
                )
                def _done():
                    self._update_btn.configure(text="Check Updates", state="normal")
                    if results:
                        log_fn(f"Nexus: {len(results)} update(s) available!")
                        for u in results:
                            log_fn(f"  ↑ {u.mod_name}: {u.installed_version} → {u.latest_version}")
                    else:
                        log_fn("Nexus: All mods are up to date.")
                    if missing:
                        log_fn(f"Nexus: {len(missing)} mod(s) have missing requirements!")
                        for m in missing:
                            names = ", ".join(r.mod_name for r in m.missing[:3])
                            suffix = f" (+{len(m.missing) - 3} more)" if len(m.missing) > 3 else ""
                            log_fn(f"  ⚠ {m.mod_name}: needs {names}{suffix}")
                    else:
                        log_fn("Nexus: All mod requirements satisfied.")
                    self._scan_update_flags()
                    self._scan_missing_reqs_flags()
                    self._scan_endorsed_flags()
                    self._redraw()
                app.after(0, _done)
            except Exception as exc:
                app.after(0, lambda: (
                    self._update_btn.configure(text="Check Updates", state="normal"),
                    log_fn(f"Nexus: Check failed — {exc}"),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_open_filters(self):
        """Open the modlist filters dialog."""
        state = {
            "filter_show_disabled": self._filter_show_disabled,
            "filter_show_enabled": self._filter_show_enabled,
            "filter_hide_separators": self._filter_hide_separators,
            "filter_winning": self._filter_conflict_winning,
            "filter_losing": self._filter_conflict_losing,
            "filter_partial": self._filter_conflict_partial,
            "filter_full": self._filter_conflict_full,
            "filter_missing_reqs": self._filter_missing_reqs,
        }
        ModlistFiltersDialog(
            self.winfo_toplevel(),
            initial_state=state,
            on_apply=self._apply_modlist_filters,
        )

    def _on_restore_backup(self):
        """Open the backup restore dialog for the current profile."""
        if not self._modlist_path or not self._modlist_path.parent.is_dir():
            return
        app = self.winfo_toplevel()
        profile_dir = self._modlist_path.parent
        profile_name = getattr(
            getattr(app, "_topbar", None),
            "_profile_var",
            None,
        )
        profile_name = profile_name.get() if profile_name is not None else "default"
        dlg = BackupRestoreDialog(
            app,
            profile_dir,
            profile_name=profile_name,
            on_restored=lambda: app._topbar._reload_mod_panel(),
        )
        app.wait_window(dlg)

    def _apply_modlist_filters(self, state: dict):
        """Apply filter state from the filters dialog and redraw."""
        self._filter_show_disabled = state.get("filter_show_disabled", False)
        self._filter_show_enabled = state.get("filter_show_enabled", False)
        self._filter_hide_separators = state.get("filter_hide_separators", False)
        self._filter_conflict_winning = state.get("filter_winning", False)
        self._filter_conflict_losing = state.get("filter_losing", False)
        self._filter_conflict_partial = state.get("filter_partial", False)
        self._filter_conflict_full = state.get("filter_full", False)
        self._filter_missing_reqs = state.get("filter_missing_reqs", False)
        self._visible_indices = self._compute_visible_indices()
        self._redraw()

    def _move_up(self):
        indices = sorted(self._sel_set) if self._sel_set else (
            [self._sel_idx] if self._sel_idx >= 0 else []
        )
        if not indices or indices[0] <= 0:
            return
        if any(self._entries[i].locked for i in indices):
            return
        for i in indices:
            self._entries[i], self._entries[i - 1] = self._entries[i - 1], self._entries[i]
            self._check_vars[i], self._check_vars[i - 1] = self._check_vars[i - 1], self._check_vars[i]
            self._check_buttons[i], self._check_buttons[i - 1] = self._check_buttons[i - 1], self._check_buttons[i]
        for j, cb in enumerate(self._check_buttons):
            if cb is not None:
                cb.configure(command=lambda idx=j: self._on_toggle(idx))
        self._sel_set = {i - 1 for i in indices}
        self._sel_idx = self._sel_idx - 1 if self._sel_idx >= 0 else -1
        self._redraw()
        self._update_info()
        self._save_modlist()
        self._rebuild_filemap()
        label = self._entries[indices[0] - 1].name if len(indices) == 1 else f"{len(indices)} items"
        self._log(f"Moved '{label}' up")

    def _move_down(self):
        indices = sorted(self._sel_set, reverse=True) if self._sel_set else (
            [self._sel_idx] if self._sel_idx >= 0 else []
        )
        if not indices or indices[0] >= len(self._entries) - 1:
            return
        if any(self._entries[i].locked for i in indices):
            return
        for i in indices:
            self._entries[i], self._entries[i + 1] = self._entries[i + 1], self._entries[i]
            self._check_vars[i], self._check_vars[i + 1] = self._check_vars[i + 1], self._check_vars[i]
            self._check_buttons[i], self._check_buttons[i + 1] = self._check_buttons[i + 1], self._check_buttons[i]
        for j, cb in enumerate(self._check_buttons):
            if cb is not None:
                cb.configure(command=lambda idx=j: self._on_toggle(idx))
        self._sel_set = {i + 1 for i in indices}
        self._sel_idx = self._sel_idx + 1 if self._sel_idx >= 0 else -1
        self._redraw()
        self._update_info()
        self._save_modlist()
        self._rebuild_filemap()
        sorted_fwd = sorted(indices)
        label = self._entries[sorted_fwd[0] + 1].name if len(indices) == 1 else f"{len(indices)} items"
        self._log(f"Moved '{label}' down")

    def _set_priority(self, idx: int):
        """Prompt for a target position and move the mod there.

        Priority: 0 = bottom (lowest), highest number = top. So e.g. with 200 mods,
        entering 0 puts the mod at the bottom; entering 199 or 470 puts it at the top.
        """
        if not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        if entry.is_separator or entry.name in (OVERWRITE_NAME, ROOT_FOLDER_NAME):
            return
        if entry.locked:
            return

        mod_indices = [
            i for i, e in enumerate(self._entries)
            if not e.is_separator and e.name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
        ]
        total_mods = len(mod_indices)
        if total_mods <= 1:
            return

        top = self.winfo_toplevel()
        dlg = _PriorityDialog(top, entry.name, total_mods)
        top.wait_window(dlg)
        value = dlg.result
        if value is None:
            return

        # 0 = bottom (rank total_mods-1), highest = top (rank 0)
        target_rank = total_mods - 1 - min(value, total_mods - 1)

        try:
            current_rank = mod_indices.index(idx)
        except ValueError:
            return

        if target_rank == current_rank:
            return

        target_idx = mod_indices[target_rank]
        from_idx = idx
        to_idx = target_idx
        if from_idx < to_idx:
            to_idx -= 1

        moved_entry = self._entries.pop(from_idx)
        moved_cb = self._check_buttons.pop(from_idx)
        moved_var = self._check_vars.pop(from_idx)

        self._entries.insert(to_idx, moved_entry)
        self._check_buttons.insert(to_idx, moved_cb)
        self._check_vars.insert(to_idx, moved_var)

        for i, cb in enumerate(self._check_buttons):
            if cb is not None:
                cb.configure(command=lambda idx=i: self._on_toggle(idx))

        self._sel_idx = to_idx
        self._sel_set = {to_idx}
        self._visible_indices = self._compute_visible_indices()
        self._redraw()
        self._update_info()
        self._save_modlist()
        self._rebuild_filemap()
        self._log(f"Set priority for '{moved_entry.name}' to position {value}")

    # ------------------------------------------------------------------
    # Persist + info
    # ------------------------------------------------------------------

    def _rebuild_filemap(self):
        """Kick off a background filemap rebuild. Safe to call from the main thread."""
        if self._modlist_path is None:
            return
        if self._filemap_pending:
            # A rebuild is already running; mark dirty so we re-run when it finishes.
            self._filemap_dirty = True
            return
        self._filemap_pending = True
        self._filemap_dirty = False

        modlist_path      = self._modlist_path
        staging           = modlist_path.parent.parent.parent / "mods"
        output            = modlist_path.parent.parent.parent / "filemap.txt"
        strip_prefixes    = self._strip_prefixes
        install_extensions = self._install_extensions
        root_deploy_folders = self._root_deploy_folders

        def _worker():
            try:
                count, conflict_map, overrides, overridden_by = build_filemap(
                    modlist_path, staging, output,
                    strip_prefixes=strip_prefixes,
                    per_mod_strip_prefixes=self._mod_strip_prefixes,
                    allowed_extensions=install_extensions or None,
                    root_deploy_folders=root_deploy_folders or None,
                )
                self.after(0, lambda: _done(count, conflict_map, overrides, overridden_by, None))
            except Exception as exc:
                self.after(0, lambda: _done(0, {}, {}, {}, exc))

        def _done(count, conflict_map, overrides, overridden_by, exc):
            self._filemap_pending = False
            if exc is not None:
                self._conflict_map = {}
                self._overrides = {}
                self._overridden_by = {}
                self._log(f"Filemap error: {exc}")
            else:
                self._conflict_map  = conflict_map
                self._overrides     = overrides
                self._overridden_by = overridden_by
                self._log(f"Filemap updated: {count} file(s).")
            self._redraw()
            if self._on_filemap_rebuilt:
                self._on_filemap_rebuilt()
            # If something changed while we were running, rebuild again.
            if self._filemap_dirty:
                self._rebuild_filemap()

        threading.Thread(target=_worker, daemon=True).start()

    def _save_modlist(self):
        if self._modlist_path is None:
            return
        # Exclude synthetic rows — they are never persisted
        entries = [e for e in self._entries
                   if e.name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)]
        write_modlist(self._modlist_path, entries)

    def _update_info(self):
        mods    = [e for e in self._entries if not e.is_separator]
        enabled = sum(1 for e in mods if e.enabled)
        total   = len(mods)
        sel_entry = self._entries[self._sel_idx] if 0 <= self._sel_idx < len(self._entries) else None
        sel = (f" | Selected: {sel_entry.name}"
               if sel_entry and not sel_entry.is_separator else "")
        self._info_label.configure(text=f"{enabled}/{total} mods active{sel}")

    def set_highlighted_mod(self, mod_name: str | None):
        """Highlight the given mod (by name) in the modlist, e.g. when a plugin is selected."""
        if mod_name != self._highlighted_mod:
            self._highlighted_mod = mod_name
            self._redraw()

    def clear_selection(self):
        """Clear the mod list selection, e.g. when a plugin is selected."""
        if self._sel_idx >= 0 or self._sel_set:
            self._sel_idx = -1
            self._sel_set = set()
            self._redraw()

    def _sep_idx_for_mod(self, mod_name: str) -> int:
        """Return the index of the separator immediately above mod_name in _entries, or -1."""
        result = -1
        for i, e in enumerate(self._entries):
            if e.is_separator:
                result = i
            elif e.name == mod_name:
                return result
        return -1


# ---------------------------------------------------------------------------
# PluginPanel