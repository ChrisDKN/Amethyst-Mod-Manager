import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import webbrowser
import tkinter as tk
import tkinter.messagebox
import tkinter.ttk as ttk
import zipfile
from pathlib import Path
import customtkinter as ctk
import py7zr
from datetime import datetime

from PIL import Image as PilImage, ImageTk

from gui.fomod_dialog import FomodDialog
from gui.add_game_dialog import AddGameDialog, sync_modlist_with_mods_folder
from gui.nexus_settings_dialog import NexusSettingsDialog
from gui.downloads_panel import DownloadsPanel
from gui.tracked_mods_panel import TrackedModsPanel
from gui.endorsed_mods_panel import EndorsedModsPanel
from gui.browse_mods_panel import BrowseModsPanel
from Games.base_game import BaseGame
from Utils.fomod_installer import resolve_files
from Utils.fomod_parser import detect_fomod, parse_module_config
from Utils.game_loader import discover_games
from Utils.filemap import (build_filemap, CONFLICT_NONE, CONFLICT_WINS,
                           CONFLICT_LOSES, CONFLICT_PARTIAL, CONFLICT_FULL,
                           OVERWRITE_NAME, ROOT_FOLDER_NAME)
from Utils.deploy import deploy_root_folder, restore_root_folder, LinkMode
from Utils.modlist import ModEntry, read_modlist, write_modlist, prepend_mod
from Utils.plugins import (
    PluginEntry, read_plugins, write_plugins, append_plugin,
    read_loadorder, write_loadorder,
    sync_plugins_from_filemap, prune_plugins_from_filemap,
)
from Utils.plugin_parser import check_missing_masters
from LOOT.loot_sorter import sort_plugins as loot_sort, is_available as loot_available
from Utils.config_paths import get_exe_args_path, get_fomod_selections_path, get_profiles_dir
from Nexus.nexus_api import NexusAPI, NexusAPIError, load_api_key, save_api_key, clear_api_key
from Nexus.nxm_handler import NxmLink, NxmHandler, NxmIPC
from Nexus.nexus_download import NexusDownloader
from Nexus.nexus_meta import build_meta_from_download, write_meta, read_meta, scan_installed_mods, resolve_nexus_meta_for_archive
from Nexus.nexus_update_checker import check_for_updates
from Nexus.nexus_requirements import check_missing_requirements

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# ---------------------------------------------------------------------------
# Color palette (MO2-inspired dark theme)
# ---------------------------------------------------------------------------
BG_DEEP    = "#1a1a1a"
BG_PANEL   = "#252526"
BG_HEADER  = "#2a2a2b"
BG_ROW     = "#2d2d2d"
BG_ROW_ALT = "#303030"
BG_SEP     = "#383838"
BG_HOVER   = "#094771"
BG_SELECT  = "#0f5fa3"
BG_HOVER_ROW = "#3d3d3d"
ACCENT     = "#0078d4"
ACCENT_HOV = "#1084d8"
TEXT_MAIN  = "#d4d4d4"
TEXT_DIM   = "#858585"
TEXT_SEP   = "#b0b0b0"
BORDER     = "#444444"

#Highlight Colours:
plugin_separator = "#A45500"
plugin_mod = "#A45500"
conflict_separator = "#5A5A5A"
conflict_higher = "#108d00"
conflict_lower = "#9a0e0e"

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
FONT_NORMAL = ("Segoe UI", 12)
FONT_BOLD   = ("Segoe UI", 12, "bold")
FONT_SMALL  = ("Segoe UI", 10)
FONT_MONO   = ("Courier New", 14)
FONT_SEP    = ("Segoe UI", 11, "bold")
FONT_HEADER = ("Segoe UI", 11, "bold")

# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------
_ICONS_DIR = Path(__file__).parent / "icons"

def _load_icon(name: str, size: tuple[int, int] = (16, 16)) -> ctk.CTkImage | None:
    path = _ICONS_DIR / name
    if not path.is_file():
        return None
    img = PilImage.open(path).convert("RGBA")
    return ctk.CTkImage(light_image=img, dark_image=img, size=size)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
PROFILES = ["Default"]

MODS: list[dict] = []

# Game handlers — populated once at startup
_GAMES: dict[str, BaseGame] = {}


def _vanilla_plugins_for_game(game) -> dict[str, str]:
    """Return vanilla plugin names from the game's data dir.

    Returns a dict mapping ``lowercase_name -> original_cased_name`` so
    that ``name.lower() in result`` works like the old set, but callers
    can also retrieve the original filename for display.
    """
    game_path = game.get_game_path()
    if not game_path:
        return {}
    data_dir = game_path / "Data"
    core_dir = game_path / "Data_Core"
    scan_dir = core_dir if core_dir.is_dir() else data_dir
    if not scan_dir.is_dir():
        return {}
    exts = {e.lower() for e in game.plugin_extensions}
    return {
        entry.name.lower(): entry.name
        for entry in scan_dir.iterdir()
        if entry.is_file() and entry.suffix.lower() in exts
    }


def _load_games() -> list[str]:
    """Discover game handlers and return sorted display names for configured games only."""
    global _GAMES
    _GAMES = discover_games()
    names = sorted(name for name, game in _GAMES.items() if game.is_configured())
    return names if names else ["No games configured"]


def _profiles_for_game(game_name: str) -> list[str]:
    """Return sorted profile folder names for the given game, 'default' first."""
    game = _GAMES.get(game_name)
    if game is not None:
        profiles_dir = game.get_profile_root() / "profiles"
    else:
        profiles_dir = get_profiles_dir() / game_name / "profiles"
    if not profiles_dir.is_dir():
        return ["default"]
    names = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir())
    # Ensure 'default' is always first if present
    if "default" in names:
        names.remove("default")
        names.insert(0, "default")
    return names if names else ["default"]


def _create_profile(game_name: str, profile_name: str) -> Path:
    """Create a new profile folder, copying modlist.txt from default."""
    game = _GAMES.get(game_name)
    if game is not None:
        profiles_root = game.get_profile_root()
    else:
        profiles_root = get_profiles_dir() / game_name
    profile_dir = profiles_root / "profiles" / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    plugins = profile_dir / "plugins.txt"
    if not plugins.exists():
        plugins.touch()
    modlist = profile_dir / "modlist.txt"
    if not modlist.exists():
        default_modlist = profiles_root / "profiles" / "default" / "modlist.txt"
        if default_modlist.exists():
            shutil.copy2(default_modlist, modlist)
        else:
            modlist.touch()
    return profile_dir


# ---------------------------------------------------------------------------
# ModRow
# ---------------------------------------------------------------------------
# ModListPanel  — canvas-based virtual list (fast for 1000+ mods)
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
    HEADERS = ["", "Mod Name", "Flags", "Conflicts", "Priority"]
    # x-start of each logical column (checkbox, name, flags, conflicts, priority)
    # Computed dynamically in _layout_columns(); defaults here.
    _COL_X  = [4, 32, 0, 0, 0]   # patched in _layout_columns

    def __init__(self, parent, log_fn=None):
        super().__init__(parent, fg_color=BG_PANEL, corner_radius=0)
        self._log = log_fn or (lambda msg: None)

        self._entries:  list[ModEntry] = []
        self._sel_idx:  int = -1          # anchor of the current selection
        self._sel_set:  set[int] = set()  # all selected entry indices
        self._hover_idx: int = -1         # entry index under the mouse cursor
        self._highlighted_mod: str | None = None  # mod highlighted by plugin panel selection
        self._modlist_path: Path | None = None
        self._strip_prefixes:    set[str] = set()
        self._install_extensions: set[str] = set()
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

        # Set of mod names that have a Nexus update available
        self._update_mods: set[str] = set()

        # Set of mod names that have missing Nexus requirements
        self._missing_reqs: set[str] = set()
        # Map mod name → list of missing requirement names (for tooltips / context menu)
        self._missing_reqs_detail: dict[str, list[str]] = {}

        # Set of mod names the user has endorsed on Nexus
        self._endorsed_mods: set[str] = set()

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
        self._visible_indices: list[int] = []  # entry indices matching current filter

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
        profile_dir = game.get_profile_root() / "profiles" / profile
        self._modlist_path = profile_dir / "modlist.txt"
        self._strip_prefixes    = game.mod_folder_strip_prefixes
        self._install_extensions = getattr(game, "mod_install_extensions", set())
        self._reload()

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
        # col 4: priority   64px  (+ 14px scrollbar gap)
        right_cols = 50 + 90 + 64 + 14
        name_w = max(80, canvas_w - 28 - right_cols)
        self._COL_X = [
            4,                          # checkbox
            32,                         # name left edge
            32 + name_w,                # flags
            32 + name_w + 50,           # conflicts
            32 + name_w + 50 + 90,      # priority
        ]
        self._canvas_w = canvas_w
        self._name_col_right = 32 + name_w - 4

    def _update_header(self, canvas_w: int):
        for lbl in self._header_labels:
            lbl.destroy()
        self._header_labels.clear()

        titles  = ["", "Mod Name", "Flags", "Conflicts", "Priority"]
        x_pos   = self._COL_X
        anchors = ["center", "w", "center", "center", "center"]
        widths  = [28, self._name_col_right - 32, 50, 90, 64]
        for i, (title, x, anc, w) in enumerate(zip(titles, x_pos, anchors, widths)):
            lbl = tk.Label(
                self._header, text=title, anchor=anc,
                font=("Segoe UI", 11, "bold"), fg=TEXT_SEP,
                bg=BG_HEADER, bd=0
            )
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
        self._scan_update_flags()
        self._scan_missing_reqs_flags()
        self._scan_endorsed_flags()
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
                    tri = "▶" if entry.name in self._collapsed_seps else "▼"
                    self._canvas.create_text(10, y_mid, text=tri, anchor="center",
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
            if entry.name in self._missing_reqs and self._icon_warning:
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

            self._canvas.create_text(
                self._COL_X[4] + 32, y_mid,
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
        """Return entry indices that match the current filter and are not hidden by a collapsed separator."""
        if self._filter_text:
            return [i for i, e in enumerate(self._entries)
                    if self._filter_text in e.name.lower()]
        if not self._collapsed_seps:
            return list(range(len(self._entries)))
        result: list[int] = []
        skip = False
        for i, entry in enumerate(self._entries):
            if entry.is_separator:
                skip = False
                result.append(i)
                if entry.name in self._collapsed_seps:
                    skip = True
            elif not skip:
                result.append(i)
        return result

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
                self._log(f"Selected: {label}")
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
        self._log(f"Selected: {self._entries[idx].name}")
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
                self._log(f"Selected: {self._entries[clicked].name}")
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

        def _on_press(event):
            if not _alive[0]:
                return
            wx, wy = popup.winfo_rootx(), popup.winfo_rooty()
            ww, wh = popup.winfo_width(), popup.winfo_height()
            if not (wx <= event.x_root <= wx + ww and wy <= event.y_root <= wy + wh):
                _dismiss()
        popup.bind_all("<ButtonPress-1>", _on_press)
        popup.bind_all("<ButtonPress-3>", _on_press)

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
        """Log the missing requirements for a mod."""
        if not dep_names:
            self._log(f"{mod_name}: No missing requirements recorded.")
            return
        self._log(f"{mod_name} is missing {len(dep_names)} requirement(s):")
        for name in dep_names:
            self._log(f"  • {name}")

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
                api.endorse_mod(domain, meta.mod_id, meta.version)
                def _done():
                    log_fn(f"Nexus: Endorsed '{mod_name}' ({meta.mod_id}).")
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
                app.after(0, _done)
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
                api.abstain_mod(domain, meta.mod_id, meta.version)
                def _done():
                    log_fn(f"Nexus: Abstained from '{mod_name}' ({meta.mod_id}).")
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
                app.after(0, _done)
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
                    _install_mod_from_archive(
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

        def _worker():
            try:
                count, conflict_map, overrides, overridden_by = build_filemap(
                    modlist_path, staging, output,
                    strip_prefixes=strip_prefixes,
                    allowed_extensions=install_extensions or None,
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
# ---------------------------------------------------------------------------
class PluginPanel(ctk.CTkFrame):
    """Right panel: tabview with Plugins, Archives, Data, Saves, Downloads, Tracked."""

    PLUGIN_HEADERS = ["", "Plugin Name", "Flags", "🔒", "Index"]
    ROW_H = 26

    def __init__(self, parent, log_fn=None, get_filemap_path=None):
        super().__init__(parent, fg_color=BG_PANEL, corner_radius=0)
        self._log = log_fn or (lambda msg: None)
        self._get_filemap_path = get_filemap_path or (lambda: None)

        # Current game (set by caller when game changes)
        self._game = None

        # Plugin system state
        self._plugins_path: Path | None = None
        self._plugin_extensions: list[str] = []
        self._plugin_entries: list[PluginEntry] = []
        self._sel_idx: int = -1
        self._psel_set: set[int] = set()  # all selected plugin indices
        self._phover_idx: int = -1        # plugin row index under the mouse cursor
        self._plugin_mod_map: dict[str, str] = {}  # plugin name → staging mod folder name
        self._on_plugin_selected_cb = None  # callable(mod_name: str | None)
        self._on_mod_selected_cb = None     # callable() — notify mod panel a plugin was selected

        # Missing masters detection
        self._missing_masters: dict[str, list[str]] = {}
        self._staging_root: Path | None = None
        self._data_dir: Path | None = None

        # Warning icon for missing masters (canvas-compatible PhotoImage)
        self._warning_icon: ImageTk.PhotoImage | None = None
        _warn_path = _ICONS_DIR / "warning.png"
        if _warn_path.is_file():
            _img = PilImage.open(_warn_path).convert("RGBA").resize((16, 16), PilImage.LANCZOS)
            self._warning_icon = ImageTk.PhotoImage(_img)

        # Tooltip state
        self._tooltip_win: tk.Toplevel | None = None

        # Canvas column x-positions (patched in _layout_plugin_cols)
        self._pcol_x = [4, 32, 0, 0, 0]  # checkbox, name, flags, lock, index

        # Drag state
        self._drag_idx: int = -1
        self._drag_start_y: int = 0
        self._drag_moved: bool = False
        self._drag_slot: int = -1

        # Vanilla plugins (locked — cannot be disabled by the user)
        self._vanilla_plugins: dict[str, str] = {}  # lowercase -> original name

        # User-locked plugins: plugin name (original case) → bool
        self._plugin_locks: dict[str, bool] = {}

        # Checkbutton widgets (one per entry, placed as canvas windows)
        self._pcheck_vars: list[tk.BooleanVar] = []
        self._pcheck_buttons: list[tk.Checkbutton] = []

        # Lock checkbutton widgets (one per entry, placed as canvas windows)
        self._plock_vars: list[tk.BooleanVar] = []
        self._plock_buttons: list[tk.Checkbutton] = []

        # Canvas dimensions
        self._pcanvas_w: int = 400

        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Executable toolbar
        exe_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=42)
        exe_bar.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 0))
        exe_bar.grid_propagate(False)

        self._exe_var = tk.StringVar(value="")
        # Stores full Path objects in display-name order, parallel to dropdown values
        self._exe_paths: list[Path] = []
        self._exe_menu = ctk.CTkOptionMenu(
            exe_bar, values=["(no executables)"], variable=self._exe_var,
            width=200, font=FONT_SMALL,
            fg_color=BG_PANEL, button_color=ACCENT, button_hover_color=ACCENT_HOV,
            dropdown_fg_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_exe_selected,
        )
        self._exe_menu.pack(side="left", padx=(8, 4), pady=6)

        ctk.CTkButton(
            exe_bar, text="▶ Run EXE", width=90, height=28, font=FONT_SMALL,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_run_exe,
        ).pack(side="left", padx=4, pady=6)

        self._exe_args_var = tk.StringVar(value="")

        ctk.CTkButton(
            exe_bar, text="⚙ Configure", width=100, height=28, font=FONT_SMALL,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_configure_exe,
        ).pack(side="left", padx=4, pady=6)

        ctk.CTkButton(
            exe_bar, text="↺ Refresh", width=80, height=28, font=FONT_SMALL,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self.refresh_exe_list,
        ).pack(side="left", padx=4, pady=6)

        self._tabs = ctk.CTkTabview(
            self, fg_color=BG_PANEL, corner_radius=4,
            segmented_button_fg_color=BG_HEADER,
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOV,
            segmented_button_unselected_color=BG_HEADER,
            segmented_button_unselected_hover_color=BG_HOVER,
            text_color=TEXT_MAIN,
        )
        self._tabs.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

        for name in ("Plugins", "Archives", "Data", "Saves", "Downloads", "Tracked", "Endorsed", "Browse"):
            self._tabs.add(name)

        self._build_plugins_tab()
        self._build_data_tab()
        self._build_downloads_tab()
        self._build_tracked_tab()
        self._build_endorsed_tab()
        self._build_browse_tab()

        for name in ("Archives", "Saves"):
            tab = self._tabs.tab(name)
            tab.grid_rowconfigure(0, weight=1)
            tab.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                tab, text=f"[ {name} — Coming Soon ]",
                font=FONT_NORMAL, text_color=TEXT_DIM
            ).grid(row=0, column=0)

    # ------------------------------------------------------------------
    # Executable toolbar — scan / run
    # ------------------------------------------------------------------

    def refresh_exe_list(self):
        """Scan for .exe files and populate the dropdown."""
        exes: list[Path] = []

        if self._game is not None:
            staging = (
                self._game.get_mod_staging_path()
                if hasattr(self._game, "get_mod_staging_path") else None
            )

            # 1. Scan filemap for .exe files — resolve from the mods staging folder
            if staging is not None and staging.is_dir():
                filemap_path = staging.parent / "filemap.txt"
                if filemap_path.is_file():
                    try:
                        for line in filemap_path.read_text(encoding="utf-8").splitlines():
                            line = line.strip()
                            if not line or "\t" not in line:
                                continue
                            rel_path, mod_name = line.split("\t", 1)
                            if not rel_path.lower().endswith(".exe"):
                                continue
                            mod_dir = staging / mod_name
                            candidate = mod_dir / rel_path
                            if candidate.is_file():
                                exes.append(candidate)
                    except OSError:
                        pass

            # 2. Scan Profiles/<game>/Applications/ for .exe files (recursive)
            if staging is not None:
                apps_dir = staging.parent / "Applications"
                if apps_dir.is_dir():
                    for entry in apps_dir.rglob("*.exe"):
                        if entry.is_file():
                            exes.append(entry)

        if not exes:
            self._exe_paths = []
            self._exe_menu.configure(values=["(no executables)"])
            self._exe_var.set("(no executables)")
            return

        # Sort: Applications/ entries first, then filemap entries, alphabetical within each
        apps_dir_root = None
        if self._game and hasattr(self._game, "get_mod_staging_path"):
            staging = self._game.get_mod_staging_path()
            apps_dir_root = staging.parent / "Applications"

        def _sort_key(p: Path):
            in_apps = apps_dir_root is not None and p.is_relative_to(apps_dir_root)
            return (0 if in_apps else 1, p.name.lower())

        exes.sort(key=_sort_key)

        # Auto-populate exe_args.json with default prefixes for known tools
        if self._game is not None:
            try:
                from Utils.exe_args_builder import build_default_exe_args
                build_default_exe_args(exes, self._game, log_fn=self._log)
            except Exception:
                pass

        self._exe_paths = exes
        labels = [p.name for p in exes]
        self._exe_menu.configure(values=labels)
        self._exe_var.set(labels[0])
        self._on_exe_selected(labels[0])

    def _on_exe_selected(self, name: str):
        """Called when the user selects an exe from the dropdown. Loads saved args if present."""
        idx = self._exe_var_index()
        if idx < 0 or not self._exe_paths:
            self._exe_args_var.set("")
            return
        exe_path = self._exe_paths[idx]
        self._exe_args_var.set(self._load_exe_args(exe_path.name))

    _EXE_ARGS_FILE = get_exe_args_path()

    def _load_exe_args(self, exe_name: str) -> str:
        """Load saved args for an exe from Utils/exe_args.json."""
        try:
            import json as _json
            data = _json.loads(self._EXE_ARGS_FILE.read_text(encoding="utf-8"))
            return data.get(exe_name, "")
        except (OSError, ValueError):
            return ""

    def _on_configure_exe(self):
        """Open the Configure dialog for the selected exe."""
        idx = self._exe_var_index()
        if idx < 0 or not self._exe_paths:
            self._log("Configure: no executable selected.")
            return
        exe_path = self._exe_paths[idx]
        game = self._game
        if game is None:
            self._log("Configure: no game selected.")
            return
        saved_args = self._load_exe_args(exe_path.name)
        dialog = _ExeConfigDialog(
            self.winfo_toplevel(),
            exe_path=exe_path,
            game=game,
            saved_args=saved_args,
        )
        self.winfo_toplevel().wait_window(dialog)
        if dialog.result is not None:
            self._exe_args_var.set(dialog.result)

    def _exe_var_index(self) -> int:
        """Return the index of the currently selected exe in _exe_paths."""
        name = self._exe_var.get()
        for i, p in enumerate(self._exe_paths):
            if p.name == name:
                return i
        return -1

    def _on_run_exe(self):
        """Launch the selected exe in the game's Proton prefix."""
        from Utils.steam_finder import find_proton_for_game

        idx = self._exe_var_index()
        if idx < 0 or not self._exe_paths:
            self._log("Run EXE: no executable selected.")
            return

        exe_path = self._exe_paths[idx]
        if not exe_path.is_file():
            self._log(f"Run EXE: file not found: {exe_path}")
            return

        game = self._game
        if game is None:
            self._log("Run EXE: no game selected.")
            return

        prefix_path = (
            game.get_prefix_path()
            if hasattr(game, "get_prefix_path") else None
        )
        if prefix_path is None or not prefix_path.is_dir():
            self._log("Run EXE: Proton prefix not configured for this game.")
            return

        # The Proton script expects STEAM_COMPAT_DATA_PATH to be the parent of pfx/
        compat_data = prefix_path.parent

        steam_id = getattr(game, "steam_id", "")
        if not steam_id:
            self._log("Run EXE: game has no Steam ID — cannot determine Proton version.")
            return
        proton_script = find_proton_for_game(steam_id)
        if proton_script is None:
            self._log(
                f"Run EXE: could not find the Proton version assigned to app {steam_id}. "
                "Check that Steam has run the game at least once."
            )
            return

        # Determine Steam root from proton script path
        # Proton is at: <steam_root>/steamapps/common/<ProtonName>/proton
        steam_root = proton_script.parent.parent.parent.parent

        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)

        import shlex
        extra_args = shlex.split(self._exe_args_var.get())

        self._log(f"Run EXE: launching {exe_path.name} via {proton_script.parent.name} ...")

        def _worker():
            try:
                subprocess.Popen(
                    ["python3", str(proton_script), "run", str(exe_path)] + extra_args,
                    env=env,
                    cwd=exe_path.parent,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                self.after(0, lambda err=e: self._log(f"Run EXE error: {err}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _build_data_tab(self):
        tab = self._tabs.tab("Data")
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        toolbar = tk.Frame(tab, bg=BG_HEADER, height=28)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)
        tk.Button(
            toolbar, text="↺ Refresh",
            bg=BG_HEADER, fg=TEXT_MAIN, activebackground=BG_HOVER,
            relief="flat", font=("Segoe UI", 10),
            bd=0, cursor="hand2",
            command=self._refresh_data_tab,
        ).pack(side="left", padx=8, pady=2)

        self._data_search_var = tk.StringVar()
        self._data_search_var.trace_add("write", self._on_data_search_changed)
        search_entry = tk.Entry(
            toolbar, textvariable=self._data_search_var,
            bg=BG_DEEP, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=("Segoe UI", 10), width=30,
        )
        search_entry.pack(side="right", padx=8, pady=3)
        search_entry.bind("<Escape>", lambda e: self._data_search_var.set(""))
        tk.Label(
            toolbar, text="Search:", bg=BG_HEADER, fg=TEXT_DIM,
            font=("Segoe UI", 10),
        ).pack(side="right")

        tree_frame = tk.Frame(tab, bg=BG_DEEP, bd=0)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        style = ttk.Style()
        style.configure("DataTree.Treeview",
                        background=BG_DEEP, foreground=TEXT_MAIN,
                        fieldbackground=BG_DEEP, rowheight=22,
                        font=("Segoe UI", 10))
        style.configure("DataTree.Treeview.Heading",
                        background=BG_HEADER, foreground=TEXT_SEP,
                        font=("Segoe UI", 10, "bold"), relief="flat")
        style.map("DataTree.Treeview",
                  background=[("selected", BG_SELECT)],
                  foreground=[("selected", TEXT_MAIN)])

        self._data_tree = ttk.Treeview(
            tree_frame,
            columns=("mod",),
            displaycolumns=("mod",),
            style="DataTree.Treeview",
            selectmode="browse",
        )
        self._data_tree.heading("#0",  text="Path",        anchor="w")
        self._data_tree.heading("mod", text="Winning Mod", anchor="w")
        self._data_tree.column("#0",  minwidth=200, stretch=True)
        self._data_tree.column("mod", minwidth=160, width=200, stretch=False)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self._data_tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal",
                            command=self._data_tree.xview)
        self._data_tree.configure(yscrollcommand=vsb.set,
                                   xscrollcommand=hsb.set)
        self._data_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.grid_rowconfigure(1, weight=0)

        self._data_tree.bind("<Button-4>",
            lambda e: self._data_tree.yview_scroll(-3, "units"))
        self._data_tree.bind("<Button-5>",
            lambda e: self._data_tree.yview_scroll( 3, "units"))

    def _refresh_data_tab(self):
        """Reload the Data tab tree from filemap.txt."""
        self._data_tree.delete(*self._data_tree.get_children())
        self._data_filemap_entries = []
        filemap_path_str = self._get_filemap_path()
        if filemap_path_str is None:
            self._data_tree.insert("", "end",
                text="(no filemap.txt — load a game first)", values=("",))
            return
        filemap_path = Path(filemap_path_str)
        if not filemap_path.is_file():
            self._data_tree.insert("", "end",
                text="(filemap.txt not found)", values=("",))
            return
        self._data_filemap_entries = self._parse_filemap(filemap_path)
        self._build_data_tree_from_entries(self._data_filemap_entries)

    @staticmethod
    def _parse_filemap(filemap_path: Path):
        """Parse filemap.txt and return a list of (rel_path, mod_name) tuples."""
        entries = []
        with filemap_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if "\t" not in line:
                    continue
                rel_path, mod_name = line.split("\t", 1)
                entries.append((rel_path, mod_name))
        return entries

    def _build_data_tree_from_entries(self, entries):
        """Build the tree hierarchy from a list of (rel_path, mod_name) entries."""
        self._data_tree.delete(*self._data_tree.get_children())

        tree_dict: dict = {}
        for rel_path, mod_name in entries:
            parts = rel_path.replace("\\", "/").split("/")
            node = tree_dict
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node.setdefault("__files__", []).append((parts[-1], mod_name))

        self._data_tree.tag_configure("folder", foreground="#56b6c2")
        self._data_tree.tag_configure("file",   foreground=TEXT_MAIN)

        def insert_node(parent_id, name, subtree):
            node_id = self._data_tree.insert(
                parent_id, "end",
                text=f"  {name}", values=("",),
                open=False, tags=("folder",),
            )
            for child in sorted(k for k in subtree if k != "__files__"):
                insert_node(node_id, child, subtree[child])
            for fname, mod in sorted(subtree.get("__files__", [])):
                self._data_tree.insert(
                    node_id, "end",
                    text=fname, values=(mod,), tags=("file",),
                )

        for top in sorted(k for k in tree_dict if k != "__files__"):
            insert_node("", top, tree_dict[top])
        for fname, mod in sorted(tree_dict.get("__files__", [])):
            self._data_tree.insert("", "end",
                text=fname, values=(mod,), tags=("file",))

    def _on_data_search_changed(self, *_):
        """Filter the Data tree based on the search query."""
        query = self._data_search_var.get().casefold()
        if not hasattr(self, "_data_filemap_entries") or not self._data_filemap_entries:
            return
        if not query:
            self._build_data_tree_from_entries(self._data_filemap_entries)
            return
        filtered = [
            (rel_path, mod_name)
            for rel_path, mod_name in self._data_filemap_entries
            if query in rel_path.casefold() or query in mod_name.casefold()
        ]
        self._build_data_tree_from_entries(filtered)
        # Expand all nodes so filtered results are visible
        for item in self._data_tree.get_children():
            self._expand_all(item)

    def _expand_all(self, item):
        """Recursively expand a treeview item and all its children."""
        self._data_tree.item(item, open=True)
        for child in self._data_tree.get_children(item):
            self._expand_all(child)

    def _build_downloads_tab(self):
        tab = self._tabs.tab("Downloads")
        self._downloads_panel = DownloadsPanel(
            tab,
            log_fn=self._log,
            install_fn=self._install_from_downloads,
        )

    def _build_tracked_tab(self):
        tab = self._tabs.tab("Tracked")

        def _get_api():
            app = self.winfo_toplevel()
            return getattr(app, "_nexus_api", None)

        def _get_game_domain():
            app = self.winfo_toplevel()
            topbar = getattr(app, "_topbar", None)
            if topbar is None:
                return ""
            game = _GAMES.get(topbar._game_var.get())
            if game is None or not game.is_configured():
                return ""
            return game.nexus_game_domain

        self._tracked_panel = TrackedModsPanel(
            tab,
            log_fn=self._log,
            get_api=_get_api,
            get_game_domain=_get_game_domain,
            install_fn=self._install_from_tracked,
        )

    def _install_from_tracked(self, entry):
        """Download and install a mod from the Tracked Mods panel.

        For premium users: finds the latest MAIN file, downloads it directly,
        and triggers the standard install flow.
        For free users: opens the mod's files page in the browser so they can
        click "Download with Mod Manager".
        """
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        if api is None:
            self._log("Tracked Mods: Set your Nexus API key first.")
            return

        topbar = getattr(app, "_topbar", None)
        game = _GAMES.get(topbar._game_var.get()) if topbar else None
        if game is None or not game.is_configured():
            self._log("Tracked Mods: No configured game selected.")
            return

        domain = entry.domain_name
        mod_id = entry.mod_id
        mod_name = entry.name or f"Mod {mod_id}"

        self._log(f"Tracked Mods: Installing '{mod_name}'...")

        mod_panel = getattr(app, "_mod_panel", None)
        if mod_panel:
            mod_panel.show_download_progress(f"Installing: {mod_name}")
        log_fn = self._log

        def _worker():
            downloader = getattr(app, "_nexus_downloader", None)
            if downloader is None:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    log_fn("Tracked Mods: Downloader not initialised."),
                ))
                return

            # Check if the user is premium
            is_premium = False
            try:
                user = api.validate()
                is_premium = user.is_premium
            except Exception:
                pass

            if not is_premium:
                files_url = f"https://www.nexusmods.com/{domain}/mods/{mod_id}?tab=files"
                def _fallback():
                    if mod_panel:
                        mod_panel.hide_download_progress()
                    webbrowser.open(files_url)
                    log_fn("Tracked Mods: Premium required for direct download.")
                    log_fn("Tracked Mods: Opened files page — click \"Download with Mod Manager\" there.")
                app.after(0, _fallback)
                return

            # Premium user — find the latest MAIN file and download directly
            mod_info = None
            file_info = None
            try:
                mod_info = api.get_mod(domain, mod_id)
                files_resp = api.get_mod_files(domain, mod_id)
                main_files = [f for f in files_resp.files
                              if f.category_name == "MAIN"]
                if main_files:
                    file_info = max(main_files,
                                    key=lambda f: f.uploaded_timestamp)
                elif files_resp.files:
                    file_info = max(files_resp.files,
                                    key=lambda f: f.uploaded_timestamp)
            except Exception as exc:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    log_fn(f"Tracked Mods: Could not fetch file list — {exc}"),
                ))
                return

            if file_info is None:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    log_fn(f"Tracked Mods: No files found for '{mod_name}'."),
                ))
                return

            result = downloader.download_file(
                game_domain=domain,
                mod_id=mod_id,
                file_id=file_info.file_id,
                progress_cb=lambda cur, total: app.after(
                    0, lambda c=cur, t=total: (
                        mod_panel.update_download_progress(c, t)
                        if mod_panel else None
                    )
                ),
            )

            if result.success and result.file_path:
                def _install():
                    if mod_panel:
                        mod_panel.hide_download_progress()
                    log_fn(f"Tracked Mods: Installing '{mod_name}'...")
                    _install_mod_from_archive(
                        str(result.file_path), app, log_fn, game, mod_panel)
                    # Write Nexus metadata
                    try:
                        meta = build_meta_from_download(
                            game_domain=domain,
                            mod_id=mod_id,
                            file_id=file_info.file_id,
                            archive_name=result.file_name,
                            mod_info=mod_info,
                            file_info=file_info,
                        )
                        raw_stem = os.path.splitext(
                            os.path.basename(str(result.file_path)))[0]
                        if raw_stem.endswith(".tar"):
                            raw_stem = os.path.splitext(raw_stem)[0]
                        suggestions = _suggest_mod_names(raw_stem)
                        folder_name = suggestions[0] if suggestions else raw_stem
                        meta_path = (game.get_mod_staging_path()
                                     / folder_name / "meta.ini")
                        if meta_path.parent.is_dir():
                            write_meta(meta_path, meta)
                            log_fn(f"Tracked Mods: Saved metadata "
                                   f"(mod {meta.mod_id}, v{meta.version})")
                    except Exception as exc:
                        log_fn(f"Tracked Mods: Warning — could not save "
                               f"metadata: {exc}")
                app.after(0, _install)
            else:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    log_fn(f"Tracked Mods: Download failed — {result.error}"),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _build_endorsed_tab(self):
        tab = self._tabs.tab("Endorsed")

        def _get_api():
            app = self.winfo_toplevel()
            return getattr(app, "_nexus_api", None)

        def _get_game_domain():
            app = self.winfo_toplevel()
            topbar = getattr(app, "_topbar", None)
            if topbar is None:
                return ""
            game = _GAMES.get(topbar._game_var.get())
            if game is None or not game.is_configured():
                return ""
            return game.nexus_game_domain

        self._endorsed_panel = EndorsedModsPanel(
            tab,
            log_fn=self._log,
            get_api=_get_api,
            get_game_domain=_get_game_domain,
            install_fn=self._install_from_endorsed,
        )

    def _install_from_endorsed(self, entry):
        """Download and install a mod from the Endorsed Mods panel.

        For premium users: finds the latest MAIN file, downloads it directly,
        and triggers the standard install flow.
        For free users: opens the mod's files page in the browser so they can
        click "Download with Mod Manager".
        """
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        if api is None:
            self._log("Endorsed Mods: Set your Nexus API key first.")
            return

        topbar = getattr(app, "_topbar", None)
        game = _GAMES.get(topbar._game_var.get()) if topbar else None
        if game is None or not game.is_configured():
            self._log("Endorsed Mods: No configured game selected.")
            return

        domain = entry.domain_name
        mod_id = entry.mod_id
        mod_name = entry.name or f"Mod {mod_id}"

        self._log(f"Endorsed Mods: Installing '{mod_name}'...")

        mod_panel = getattr(app, "_mod_panel", None)
        if mod_panel:
            mod_panel.show_download_progress(f"Installing: {mod_name}")
        log_fn = self._log

        def _worker():
            downloader = getattr(app, "_nexus_downloader", None)
            if downloader is None:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    log_fn("Endorsed Mods: Downloader not initialised."),
                ))
                return

            # Check if the user is premium
            is_premium = False
            try:
                user = api.validate()
                is_premium = user.is_premium
            except Exception:
                pass

            if not is_premium:
                files_url = f"https://www.nexusmods.com/{domain}/mods/{mod_id}?tab=files"
                def _fallback():
                    if mod_panel:
                        mod_panel.hide_download_progress()
                    webbrowser.open(files_url)
                    log_fn("Endorsed Mods: Premium required for direct download.")
                    log_fn("Endorsed Mods: Opened files page — click \"Download with Mod Manager\" there.")
                app.after(0, _fallback)
                return

            # Premium user — find the latest MAIN file and download directly
            mod_info = None
            file_info = None
            try:
                mod_info = api.get_mod(domain, mod_id)
                files_resp = api.get_mod_files(domain, mod_id)
                main_files = [f for f in files_resp.files
                              if f.category_name == "MAIN"]
                if main_files:
                    file_info = max(main_files,
                                    key=lambda f: f.uploaded_timestamp)
                elif files_resp.files:
                    file_info = max(files_resp.files,
                                    key=lambda f: f.uploaded_timestamp)
            except Exception as exc:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    log_fn(f"Endorsed Mods: Could not fetch file list — {exc}"),
                ))
                return

            if file_info is None:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    log_fn(f"Endorsed Mods: No files found for '{mod_name}'."),
                ))
                return

            result = downloader.download_file(
                game_domain=domain,
                mod_id=mod_id,
                file_id=file_info.file_id,
                progress_cb=lambda cur, total: app.after(
                    0, lambda c=cur, t=total: (
                        mod_panel.update_download_progress(c, t)
                        if mod_panel else None
                    )
                ),
            )

            if result.success and result.file_path:
                def _install():
                    if mod_panel:
                        mod_panel.hide_download_progress()
                    log_fn(f"Endorsed Mods: Installing '{mod_name}'...")
                    _install_mod_from_archive(
                        str(result.file_path), app, log_fn, game, mod_panel)
                    # Write Nexus metadata
                    try:
                        meta = build_meta_from_download(
                            game_domain=domain,
                            mod_id=mod_id,
                            file_id=file_info.file_id,
                            archive_name=result.file_name,
                            mod_info=mod_info,
                            file_info=file_info,
                        )
                        raw_stem = os.path.splitext(
                            os.path.basename(str(result.file_path)))[0]
                        if raw_stem.endswith(".tar"):
                            raw_stem = os.path.splitext(raw_stem)[0]
                        suggestions = _suggest_mod_names(raw_stem)
                        folder_name = suggestions[0] if suggestions else raw_stem
                        meta_path = (game.get_mod_staging_path()
                                     / folder_name / "meta.ini")
                        if meta_path.parent.is_dir():
                            write_meta(meta_path, meta)
                            log_fn(f"Endorsed Mods: Saved metadata "
                                   f"(mod {meta.mod_id}, v{meta.version})")
                    except Exception as exc:
                        log_fn(f"Endorsed Mods: Warning — could not save "
                               f"metadata: {exc}")
                app.after(0, _install)
            else:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    log_fn(f"Endorsed Mods: Download failed — {result.error}"),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _build_browse_tab(self):
        tab = self._tabs.tab("Browse")

        def _get_api():
            app = self.winfo_toplevel()
            return getattr(app, "_nexus_api", None)

        def _get_game_domain():
            app = self.winfo_toplevel()
            topbar = getattr(app, "_topbar", None)
            if topbar is None:
                return ""
            game = _GAMES.get(topbar._game_var.get())
            if game is None or not game.is_configured():
                return ""
            return game.nexus_game_domain

        self._browse_panel = BrowseModsPanel(
            tab,
            log_fn=self._log,
            get_api=_get_api,
            get_game_domain=_get_game_domain,
            install_fn=self._install_from_browse,
        )

    def _install_from_browse(self, entry):
        """Download and install a mod from the Browse panel."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        if api is None:
            self._log("Browse: Set your Nexus API key first.")
            return

        topbar = getattr(app, "_topbar", None)
        game = _GAMES.get(topbar._game_var.get()) if topbar else None
        if game is None or not game.is_configured():
            self._log("Browse: No configured game selected.")
            return

        domain = entry.domain_name
        mod_id = entry.mod_id
        mod_name = entry.name or f"Mod {mod_id}"

        self._log(f"Browse: Installing '{mod_name}'...")

        mod_panel = getattr(app, "_mod_panel", None)
        if mod_panel:
            mod_panel.show_download_progress(f"Installing: {mod_name}")
        log_fn = self._log

        def _worker():
            downloader = getattr(app, "_nexus_downloader", None)
            if downloader is None:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    log_fn("Browse: Downloader not initialised."),
                ))
                return

            is_premium = False
            try:
                user = api.validate()
                is_premium = user.is_premium
            except Exception:
                pass

            if not is_premium:
                files_url = f"https://www.nexusmods.com/{domain}/mods/{mod_id}?tab=files"
                def _fallback():
                    if mod_panel:
                        mod_panel.hide_download_progress()
                    webbrowser.open(files_url)
                    log_fn("Browse: Premium required for direct download.")
                    log_fn('Browse: Opened files page — click "Download with Mod Manager" there.')
                app.after(0, _fallback)
                return

            mod_info = None
            file_info = None
            try:
                mod_info = api.get_mod(domain, mod_id)
                files_resp = api.get_mod_files(domain, mod_id)
                main_files = [f for f in files_resp.files
                              if f.category_name == "MAIN"]
                if main_files:
                    file_info = max(main_files,
                                    key=lambda f: f.uploaded_timestamp)
                elif files_resp.files:
                    file_info = max(files_resp.files,
                                    key=lambda f: f.uploaded_timestamp)
            except Exception as exc:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    log_fn(f"Browse: Could not fetch file list — {exc}"),
                ))
                return

            if file_info is None:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    log_fn(f"Browse: No files found for '{mod_name}'."),
                ))
                return

            result = downloader.download_file(
                game_domain=domain,
                mod_id=mod_id,
                file_id=file_info.file_id,
                progress_cb=lambda cur, total: app.after(
                    0, lambda c=cur, t=total: (
                        mod_panel.update_download_progress(c, t)
                        if mod_panel else None
                    )
                ),
            )

            if result.success and result.file_path:
                def _install():
                    if mod_panel:
                        mod_panel.hide_download_progress()
                    log_fn(f"Browse: Installing '{mod_name}'...")
                    _install_mod_from_archive(
                        str(result.file_path), app, log_fn, game, mod_panel)
                    try:
                        meta = build_meta_from_download(
                            game_domain=domain,
                            mod_id=mod_id,
                            file_id=file_info.file_id,
                            archive_name=result.file_name,
                            mod_info=mod_info,
                            file_info=file_info,
                        )
                        raw_stem = os.path.splitext(
                            os.path.basename(str(result.file_path)))[0]
                        if raw_stem.endswith(".tar"):
                            raw_stem = os.path.splitext(raw_stem)[0]
                        suggestions = _suggest_mod_names(raw_stem)
                        folder_name = suggestions[0] if suggestions else raw_stem
                        meta_path = (game.get_mod_staging_path()
                                     / folder_name / "meta.ini")
                        if meta_path.parent.is_dir():
                            write_meta(meta_path, meta)
                            log_fn(f"Browse: Saved metadata "
                                   f"(mod {meta.mod_id}, v{meta.version})")
                    except Exception as exc:
                        log_fn(f"Browse: Warning — could not save "
                               f"metadata: {exc}")
                app.after(0, _install)
            else:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    log_fn(f"Browse: Download failed — {result.error}"),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _install_from_downloads(self, archive_path: str):
        """Trigger the standard install-mod flow for an archive from Downloads."""
        app = self.winfo_toplevel()
        topbar = app._topbar
        game = _GAMES.get(topbar._game_var.get())
        if game is None or not game.is_configured():
            self._log("No configured game selected — use + to set the game path first.")
            return
        self._log(f"Installing: {os.path.basename(archive_path)}")
        mod_panel = getattr(app, "_mod_panel", None)
        _install_mod_from_archive(archive_path, app, self._log, game, mod_panel)

    def _build_plugins_tab(self):
        tab = self._tabs.tab("Plugins")
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        self._pheader = ctk.CTkFrame(tab, fg_color=BG_HEADER, corner_radius=0, height=28)
        self._pheader.grid(row=0, column=0, sticky="ew")
        self._pheader.grid_propagate(False)
        self._pheader_labels: list[ctk.CTkLabel] = []

        canvas_frame = tk.Frame(tab, bg=BG_DEEP, bd=0, highlightthickness=0)
        canvas_frame.grid(row=1, column=0, sticky="nsew")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)

        self._pcanvas = tk.Canvas(canvas_frame, bg=BG_DEEP, bd=0,
                                  highlightthickness=0, yscrollincrement=1, takefocus=0)
        self._pvsb = tk.Scrollbar(canvas_frame, orient="vertical",
                                  command=self._pcanvas.yview,
                                  bg=BG_SEP, troughcolor=BG_DEEP,
                                  activebackground=ACCENT,
                                  highlightthickness=0, bd=0)
        self._pcanvas.configure(yscrollcommand=self._pvsb.set)
        self._pcanvas.grid(row=0, column=0, sticky="nsew")
        self._pvsb.grid(row=0, column=1, sticky="ns")

        self._pcanvas.bind("<Configure>",       self._on_pcanvas_resize)
        self._pcanvas.bind("<Button-4>",        self._on_pscroll_up)
        self._pcanvas.bind("<Button-5>",        self._on_pscroll_down)
        self._pcanvas.bind("<MouseWheel>",      self._on_pmousewheel)
        self._pvsb.bind("<B1-Motion>",          lambda e: self._predraw())
        self._pcanvas.bind("<ButtonPress-1>",   self._on_pmouse_press)
        self._pcanvas.bind("<B1-Motion>",       self._on_pmouse_drag)
        self._pcanvas.bind("<ButtonRelease-1>", self._on_pmouse_release)
        self._pcanvas.bind("<Motion>",          self._on_pmouse_motion)
        self._pcanvas.bind("<Leave>",           self._on_pmouse_leave)
        self._pcanvas.bind("<ButtonRelease-3>", self._on_plugin_right_click)

        toolbar = ctk.CTkFrame(tab, fg_color=BG_PANEL, corner_radius=0, height=36)
        toolbar.grid(row=2, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        ctk.CTkButton(
            toolbar, text="Sort Plugins", width=110, height=26,
            fg_color="#2e6b30", hover_color="#3a8a3d",
            text_color=TEXT_MAIN, font=FONT_SMALL,
            command=self._sort_plugins_loot,
        ).pack(side="left", padx=8, pady=5)

    # ------------------------------------------------------------------
    # LOOT sorting
    # ------------------------------------------------------------------

    def _sort_plugins_loot(self):
        """Sort current plugin list using libloot's masterlist rules."""
        if not loot_available():
            self._log("LOOT library not available — cannot sort.")
            return

        if not self._plugins_path or not self._plugin_entries:
            self._log("No plugins loaded to sort.")
            return

        # Get current game from the top bar
        app = self.winfo_toplevel()
        topbar = app._topbar
        game_name = topbar._game_var.get()

        game = _GAMES.get(game_name)
        if not game or not game.is_configured():
            self._log(f"Game '{game_name}' is not configured.")
            return

        if not game.loot_sort_enabled:
            self._log(f"LOOT sorting is not supported for '{game_name}'.")
            return

        game_path = game.get_game_path()
        staging_root = game.get_mod_staging_path()

        # Ensure vanilla plugins are present in the in-memory list before
        # sorting (they are never written to plugins.txt).
        existing_lower = {e.name.lower() for e in self._plugin_entries}
        _ext_order = {".esm": 0, ".esp": 1, ".esl": 2}
        vanilla_added = [
            PluginEntry(name=orig, enabled=True)
            for low, orig in sorted(
                self._vanilla_plugins.items(),
                key=lambda kv: (_ext_order.get(Path(kv[0]).suffix, 9), kv[0]),
            )
            if low not in existing_lower
        ]
        if vanilla_added:
            self._plugin_entries = vanilla_added + self._plugin_entries
            self._log(f"Added {len(vanilla_added)} vanilla plugin(s) for sort.")

        # Separate locked plugins (stay in place) from those LOOT will sort
        locked_indices: dict[int, PluginEntry] = {}
        unlocked_entries: list[PluginEntry] = []
        for i, e in enumerate(self._plugin_entries):
            if self._plugin_locks.get(e.name, False):
                locked_indices[i] = e
            else:
                unlocked_entries.append(e)

        if locked_indices:
            locked_names = [e.name for e in locked_indices.values()]
            self._log(f"Skipping {len(locked_indices)} locked plugin(s): "
                      + ", ".join(locked_names))

        # Build inputs from non-locked entries only
        plugin_names = [e.name for e in unlocked_entries]
        enabled_set = {e.name for e in unlocked_entries if e.enabled}

        try:
            result = loot_sort(
                plugin_names=plugin_names,
                enabled_set=enabled_set,
                game_name=game_name,
                game_path=game_path,
                staging_root=staging_root,
                log_fn=self._log,
                game_type_attr=game.loot_game_type,
                game_id=game.game_id,
                masterlist_url=game.loot_masterlist_url,
            )
        except RuntimeError as e:
            self._log(f"LOOT sort failed: {e}")
            return

        for w in result.warnings:
            self._log(f"Warning: {w}")

        if result.moved_count == 0 and not locked_indices:
            self._log("Load order is already sorted.")
            return

        # Re-interleave: place locked plugins back at their original indices,
        # filling remaining slots with the LOOT-sorted unlocked plugins.
        name_to_enabled = {e.name: e.enabled for e in self._plugin_entries}
        sorted_unlocked = iter(
            PluginEntry(name=n, enabled=name_to_enabled.get(n, True))
            for n in result.sorted_names
        )
        total = len(self._plugin_entries)
        new_entries: list[PluginEntry] = []
        for i in range(total):
            if i in locked_indices:
                new_entries.append(locked_indices[i])
            else:
                new_entries.append(next(sorted_unlocked))

        self._plugin_entries = new_entries
        # Write mod plugins to plugins.txt, full order to loadorder.txt
        write_plugins(self._plugins_path, [
            e for e in new_entries
            if e.name.lower() not in self._vanilla_plugins
        ])
        write_loadorder(
            self._plugins_path.parent / "loadorder.txt", new_entries,
        )
        self._refresh_plugins_tab()
        self._log(f"Sorted — {result.moved_count} plugin(s) changed position.")

    # ------------------------------------------------------------------
    # Plugin column layout
    # ------------------------------------------------------------------

    def _layout_plugin_cols(self, w: int):
        """Compute column x positions given the canvas width."""
        # col 0: checkbox   28px
        # col 1: name       fills
        # col 2: flags      40px
        # col 3: lock       28px
        # col 4: index      50px + 14px scrollbar gap
        idx_w = 50 + 14
        lock_w = 28
        flags_w = 40
        flags_x = max(80, w - idx_w - lock_w - flags_w)
        self._pcol_x = [4, 32, flags_x, flags_x + flags_w, flags_x + flags_w + lock_w]

    def _update_plugin_header(self, w: int):
        """Rebuild header labels to match current column positions."""
        for lbl in self._pheader_labels:
            lbl.destroy()
        self._pheader_labels.clear()

        col_x = self._pcol_x
        titles = self.PLUGIN_HEADERS
        widths = [col_x[1] - col_x[0],
                  col_x[2] - col_x[1],
                  col_x[3] - col_x[2],
                  col_x[4] - col_x[3],
                  w - col_x[4]]

        for i, (title, cw) in enumerate(zip(titles, widths)):
            anchor = "w" if i == 1 else "center"
            lbl = tk.Label(
                self._pheader, text=title, anchor=anchor,
                font=("Segoe UI", 11, "bold"), fg=TEXT_SEP, bg=BG_HEADER,
            )
            lbl.place(x=col_x[i], y=0, width=cw, height=28)
            self._pheader_labels.append(lbl)

    # ------------------------------------------------------------------
    # Plugin lock persistence
    # ------------------------------------------------------------------

    def _plugin_locks_path(self) -> Path | None:
        if self._plugins_path is None:
            return None
        return self._plugins_path.parent / "plugin_locks.json"

    def _load_plugin_locks(self) -> None:
        path = self._plugin_locks_path()
        if path and path.is_file():
            try:
                self._plugin_locks = json.loads(path.read_text(encoding="utf-8"))
                return
            except Exception:
                pass
        self._plugin_locks = {}

    def _save_plugin_locks(self) -> None:
        path = self._plugin_locks_path()
        if path is None:
            return
        path.write_text(json.dumps(self._plugin_locks, indent=2), encoding="utf-8")

    def _on_plugin_lock_toggle(self, idx: int) -> None:
        """Handle lock checkbox toggle for a plugin."""
        if 0 <= idx < len(self._plugin_entries):
            name = self._plugin_entries[idx].name
            locked = self._plock_vars[idx].get()
            if locked:
                self._plugin_locks[name] = True
            else:
                self._plugin_locks.pop(name, None)
            self._save_plugin_locks()
            self._predraw()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def load_plugins(self, plugins_path: Path, plugin_extensions: list[str]) -> None:
        """Load plugins.txt for the given path and extension list."""
        self._plugins_path = plugins_path
        self._plugin_extensions = plugin_extensions
        self._refresh_plugins_tab()

    def clear_plugin_selection(self):
        """Clear the plugin list selection, e.g. when a mod is selected."""
        if self._sel_idx >= 0 or self._psel_set:
            self._sel_idx = -1
            self._psel_set = set()
            self._predraw()

    # ------------------------------------------------------------------
    # Plugins tab refresh (canvas-based)
    # ------------------------------------------------------------------

    def _refresh_plugins_tab(self) -> None:
        """Reload plugin entries from plugins.txt and redraw."""
        self._sel_idx = -1
        self._psel_set = set()
        self._drag_idx = -1

        # Destroy old checkbutton widgets
        for cb in self._pcheck_buttons:
            cb.destroy()
        self._pcheck_buttons.clear()
        self._pcheck_vars.clear()

        for cb in self._plock_buttons:
            cb.destroy()
        self._plock_buttons.clear()
        self._plock_vars.clear()

        if self._plugins_path is None or not self._plugin_extensions:
            self._plugin_entries = []
            self._predraw()
            return

        self._load_plugin_locks()
        mod_entries = read_plugins(self._plugins_path)
        mod_map = {e.name.lower(): e for e in mod_entries}

        # loadorder.txt records the full order (vanilla + mod plugins)
        # so vanilla plugins keep the positions LOOT assigned them.
        loadorder_path = self._plugins_path.parent / "loadorder.txt"
        saved_order = read_loadorder(loadorder_path)

        if saved_order:
            # Rebuild entries in saved order, pulling enabled state from
            # plugins.txt for mod plugins; vanilla plugins are always enabled.
            ordered: list[PluginEntry] = []
            seen: set[str] = set()
            for name in saved_order:
                low = name.lower()
                if low in seen:
                    continue
                seen.add(low)
                if low in mod_map:
                    ordered.append(mod_map[low])
                elif low in self._vanilla_plugins:
                    ordered.append(PluginEntry(
                        name=self._vanilla_plugins[low], enabled=True,
                    ))
                # else: plugin removed since last sort — skip it

            # Append any mod plugins not yet in loadorder (newly installed)
            for e in mod_entries:
                if e.name.lower() not in seen:
                    ordered.append(e)
                    seen.add(e.name.lower())

            # Append any vanilla plugins not yet in loadorder (newly detected)
            _ext_order = {".esm": 0, ".esp": 1, ".esl": 2}
            for low, orig in sorted(
                self._vanilla_plugins.items(),
                key=lambda kv: (_ext_order.get(Path(kv[0]).suffix, 9), kv[0]),
            ):
                if low not in seen:
                    ordered.append(PluginEntry(name=orig, enabled=True))
                    seen.add(low)

            self._plugin_entries = ordered
        else:
            # No loadorder.txt yet — fall back to prepending vanilla plugins
            existing_lower = {e.name.lower() for e in mod_entries}
            _ext_order = {".esm": 0, ".esp": 1, ".esl": 2}
            vanilla_prepend = [
                PluginEntry(name=original, enabled=True)
                for lower, original in sorted(
                    self._vanilla_plugins.items(),
                    key=lambda kv: (_ext_order.get(Path(kv[0]).suffix, 9), kv[0]),
                )
                if lower not in existing_lower
            ]
            self._plugin_entries = vanilla_prepend + mod_entries

        # Build checkbutton widgets for each entry
        for i, entry in enumerate(self._plugin_entries):
            is_vanilla = entry.name.lower() in self._vanilla_plugins
            var = tk.BooleanVar(value=entry.enabled)
            cb = tk.Checkbutton(
                self._pcanvas, variable=var,
                bg=BG_ROW, activebackground=BG_ROW, selectcolor=BG_DEEP,
                fg=ACCENT, indicatoron=True,
                bd=0, highlightthickness=0,
                state="disabled" if is_vanilla else "normal",
                command=lambda idx=i: self._on_plugin_toggle(idx),
            )
            self._pcheck_vars.append(var)
            self._pcheck_buttons.append(cb)

            lock_var = tk.BooleanVar(value=bool(self._plugin_locks.get(entry.name, False)))
            lock_cb = tk.Checkbutton(
                self._pcanvas, variable=lock_var,
                bg=BG_ROW, activebackground=BG_ROW, selectcolor=BG_DEEP,
                fg=TEXT_MAIN, indicatoron=True,
                bd=0, highlightthickness=0,
                command=lambda idx=i: self._on_plugin_lock_toggle(idx),
            )
            self._plock_vars.append(lock_var)
            self._plock_buttons.append(lock_cb)

        self._check_all_masters()
        self._predraw()

    def _save_plugins(self) -> None:
        """Write current plugin entries to plugins.txt and loadorder.txt.

        plugins.txt — mod plugins only (vanilla excluded, the game strips them).
        loadorder.txt — full order including vanilla, so their LOOT-sorted
        positions are preserved across refreshes.
        """
        if self._plugins_path is None:
            return
        all_entries: list[PluginEntry] = []
        mod_entries: list[PluginEntry] = []
        for i, entry in enumerate(self._plugin_entries):
            enabled = self._pcheck_vars[i].get() if i < len(self._pcheck_vars) else entry.enabled
            e = PluginEntry(name=entry.name, enabled=enabled)
            all_entries.append(e)
            if entry.name.lower() not in self._vanilla_plugins:
                mod_entries.append(e)
        write_plugins(self._plugins_path, mod_entries)
        write_loadorder(self._plugins_path.parent / "loadorder.txt", all_entries)

    def _on_plugin_toggle(self, idx: int) -> None:
        """Handle checkbox toggle for a plugin."""
        if 0 <= idx < len(self._plugin_entries):
            if self._plugin_entries[idx].name.lower() in self._vanilla_plugins:
                return
            self._plugin_entries[idx].enabled = self._pcheck_vars[idx].get()
            self._save_plugins()
            self._check_all_masters()
            self._predraw()

    # ------------------------------------------------------------------
    # Canvas drawing
    # ------------------------------------------------------------------

    def _predraw(self):
        """Full redraw of the plugin canvas."""
        self._pcanvas.delete("all")

        cw = self._pcanvas_w
        entries = self._plugin_entries
        dragging = self._drag_idx >= 0 and self._drag_moved

        # Park all checkbuttons off-screen to avoid hide→show flicker
        for cb in self._pcheck_buttons:
            cb.place(x=-9999, y=-9999)
        for cb in self._plock_buttons:
            cb.place(x=-9999, y=-9999)

        canvas_top = int(self._pcanvas.canvasy(0))
        canvas_bottom = canvas_top + self._pcanvas.winfo_height()

        total_h = len(entries) * self.ROW_H

        for row, entry in enumerate(entries):
            y_top = row * self.ROW_H
            y_bot = y_top + self.ROW_H
            # Skip rows outside viewport (virtualisation)
            if y_bot < canvas_top or y_top > canvas_bottom:
                continue

            y_mid = y_top + self.ROW_H // 2

            is_sel = (row in self._psel_set) or (row == self._drag_idx and self._drag_moved)
            if is_sel:
                bg = BG_SELECT
            elif row == self._phover_idx:
                bg = BG_HOVER_ROW
            else:
                bg = BG_ROW if row % 2 == 0 else BG_ROW_ALT

            self._pcanvas.create_rectangle(0, y_top, cw, y_bot, fill=bg, outline="")

            # Checkbutton (only when not dragging)
            if not dragging and row < len(self._pcheck_buttons):
                cb = self._pcheck_buttons[row]
                cb.configure(bg=bg, activebackground=bg)
                widget_y = y_top - canvas_top
                cb.place(x=self._pcol_x[0], y=widget_y,
                         width=24, height=self.ROW_H)

            # Plugin name
            name_color = TEXT_DIM if not entry.enabled else TEXT_MAIN
            self._pcanvas.create_text(
                self._pcol_x[1], y_mid,
                text=entry.name, anchor="w", fill=name_color,
                font=("Segoe UI", 11),
            )

            # Flags — warning icon for missing masters
            if entry.name in self._missing_masters and self._warning_icon:
                flags_mid_x = (self._pcol_x[2] + self._pcol_x[3]) // 2
                self._pcanvas.create_image(
                    flags_mid_x, y_mid,
                    image=self._warning_icon, anchor="center",
                )

            # Lock checkbox
            if not dragging and row < len(self._plock_buttons):
                lock_cb = self._plock_buttons[row]
                lock_cb.configure(bg=bg, activebackground=bg)
                widget_y = y_top - canvas_top
                lock_cb.place(x=self._pcol_x[3], y=widget_y,
                              width=24, height=self.ROW_H)

            # Index
            self._pcanvas.create_text(
                self._pcol_x[4] + 25, y_mid,
                text=f"{row:03d}", anchor="center", fill=TEXT_DIM,
                font=("Segoe UI", 10),
            )

        self._pcanvas.configure(scrollregion=(
            0, 0, cw, max(total_h, self._pcanvas.winfo_height())
        ))

    # ------------------------------------------------------------------
    # Missing masters detection
    # ------------------------------------------------------------------

    def _check_all_masters(self) -> None:
        """Build plugin_paths dict and check all plugins for missing masters."""
        self._missing_masters = {}
        self._plugin_mod_map = {}
        if not self._plugin_entries or not self._plugin_extensions:
            return

        exts_lower = {ext.lower() for ext in self._plugin_extensions}
        plugin_paths: dict[str, Path] = {}

        # 1. Map plugins from filemap.txt → staging mods
        filemap_path_str = self._get_filemap_path()
        if filemap_path_str and self._staging_root:
            filemap_path = Path(filemap_path_str)
            if filemap_path.is_file():
                with filemap_path.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.rstrip("\n")
                        if "\t" not in line:
                            continue
                        rel_path, mod_name = line.split("\t", 1)
                        rel_path = rel_path.replace("\\", "/")
                        if "/" in rel_path:
                            continue
                        if Path(rel_path).suffix.lower() in exts_lower:
                            plugin_paths[rel_path.lower()] = (
                                self._staging_root / mod_name / rel_path
                            )
                            # Map plugin filename → mod folder name
                            self._plugin_mod_map[rel_path] = mod_name

        # 2. Also map vanilla plugins from the game Data dir
        if self._data_dir and self._data_dir.is_dir():
            vanilla_dir = self._data_dir.parent / (self._data_dir.name + "_Core")
            scan_dir = vanilla_dir if vanilla_dir.is_dir() else self._data_dir
            for entry in scan_dir.iterdir():
                if entry.is_file() and entry.suffix.lower() in exts_lower:
                    plugin_paths.setdefault(entry.name.lower(), entry)

        plugin_names = [e.name for e in self._plugin_entries if e.enabled]
        self._missing_masters = check_missing_masters(plugin_names, plugin_paths)

    # ------------------------------------------------------------------
    # Tooltip for missing masters
    # ------------------------------------------------------------------

    def _show_tooltip(self, x: int, y: int, text: str) -> None:
        """Show a tooltip window near the given screen coordinates."""
        self._hide_tooltip()
        tw = tk.Toplevel(self)
        tw.wm_overrideredirect(True)
        tw.configure(bg="#1a1a2e")
        lbl = tk.Label(
            tw, text=text, justify="left",
            bg="#1a1a2e", fg="#ff6b6b",
            font=("Segoe UI", 10), padx=8, pady=4,
            wraplength=350,
        )
        lbl.pack()
        tw.update_idletasks()
        tip_w = tw.winfo_reqwidth()
        # Always place to the left of the cursor (flags column is at the right edge)
        tip_x = x - tip_w - 4
        tw.wm_geometry(f"+{tip_x}+{y + 8}")
        self._tooltip_win = tw

    def _hide_tooltip(self) -> None:
        if self._tooltip_win:
            self._tooltip_win.destroy()
            self._tooltip_win = None

    def _on_pmouse_motion(self, event) -> None:
        """Show tooltip when hovering over a warning icon in the Flags column, and update hover highlight."""
        canvas_y = int(self._pcanvas.canvasy(event.y))
        row = canvas_y // self.ROW_H
        if row < 0 or row >= len(self._plugin_entries):
            self._hide_tooltip()
            if self._phover_idx != -1:
                self._phover_idx = -1
                self._predraw()
            return

        # Update hover highlight
        if row != self._phover_idx:
            self._phover_idx = row
            self._predraw()

        # Check if cursor is in the Flags column
        x = event.x
        if len(self._pcol_x) >= 5 and self._pcol_x[2] <= x < self._pcol_x[3]:
            entry = self._plugin_entries[row]
            missing = self._missing_masters.get(entry.name)
            if missing:
                screen_x = event.x_root
                screen_y = event.y_root
                text = "Missing masters:\n" + "\n".join(f"  - {m}" for m in missing)
                # Only recreate if not already showing for this row
                if self._tooltip_win is None:
                    self._show_tooltip(screen_x, screen_y, text)
                return

        self._hide_tooltip()

    def _on_pmouse_leave(self, event) -> None:
        self._hide_tooltip()
        if self._phover_idx != -1:
            self._phover_idx = -1
            self._predraw()

    # ------------------------------------------------------------------
    # Scroll events
    # ------------------------------------------------------------------

    def _on_pcanvas_resize(self, event):
        self._pcanvas_w = event.width
        self._layout_plugin_cols(event.width)
        self._update_plugin_header(event.width)
        self._predraw()

    def _on_pscroll_up(self, _event):
        self._pcanvas.yview("scroll", -50, "units")
        self._predraw()

    def _on_pscroll_down(self, _event):
        self._pcanvas.yview("scroll", 50, "units")
        self._predraw()

    def _on_pmousewheel(self, event):
        self._pcanvas.yview("scroll", -50 if event.delta > 0 else 50, "units")
        self._predraw()

    # ------------------------------------------------------------------
    # Mouse events (select + drag)
    # ------------------------------------------------------------------

    def _pevent_canvas_y(self, event) -> int:
        return int(self._pcanvas.canvasy(event.y))

    def _pcanvas_y_to_index(self, canvas_y: int) -> int:
        if not self._plugin_entries:
            return 0
        row = int(canvas_y // self.ROW_H)
        return max(0, min(row, len(self._plugin_entries) - 1))

    def _is_plugin_locked(self, idx: int) -> bool:
        """Return True if the plugin at idx is vanilla or user-locked (immovable)."""
        if 0 <= idx < len(self._plugin_entries):
            entry = self._plugin_entries[idx]
            if entry.name.lower() in self._vanilla_plugins:
                return True
            return bool(self._plugin_locks.get(entry.name, False))
        return False

    def _on_pmouse_press(self, event):
        if not self._plugin_entries:
            return
        cy = self._pevent_canvas_y(event)
        idx = self._pcanvas_y_to_index(cy)
        shift = bool(event.state & 0x1)

        # Shift+click: extend selection from anchor
        if shift and self._sel_idx >= 0:
            lo, hi = sorted((self._sel_idx, idx))
            self._psel_set = set(range(lo, hi + 1))
            self._predraw()
            return

        # If clicking inside an existing multi-selection, preserve it so the
        # user can drag the whole group — collapse to single only on release.
        # Don't initiate drag if the clicked entry is locked.
        if idx in self._psel_set and len(self._psel_set) > 1:
            if not self._is_plugin_locked(idx):
                self._drag_idx = idx
                self._drag_start_y = cy
                self._drag_moved = False
                self._drag_slot = -1
            return

        self._sel_idx = idx
        self._psel_set = {idx}
        # Only allow drag start if not locked
        if not self._is_plugin_locked(idx):
            self._drag_idx = idx
            self._drag_start_y = cy
        else:
            self._drag_idx = -1
            self._drag_start_y = 0
        self._drag_moved = False
        self._drag_slot = -1
        self._predraw()
        plugin_name = self._plugin_entries[idx].name
        self._log(f"Selected plugin: {plugin_name}")
        if self._on_mod_selected_cb is not None:
            self._on_mod_selected_cb()
        if self._on_plugin_selected_cb is not None:
            mod_name = self._plugin_mod_map.get(plugin_name)
            self._on_plugin_selected_cb(mod_name)

    def _on_pmouse_drag(self, event):
        if self._drag_idx < 0 or not self._plugin_entries:
            return

        # Auto-scroll near edges
        h = self._pcanvas.winfo_height()
        if event.y < 40:
            self._pcanvas.yview("scroll", -1, "units")
        elif event.y > h - 40:
            self._pcanvas.yview("scroll", 1, "units")

        cy = self._pevent_canvas_y(event)
        n = len(self._plugin_entries)

        # Multi-selection drag: move all selected (non-locked) entries as a group
        if len(self._psel_set) > 1 and self._drag_idx in self._psel_set:
            # Exclude locked entries from the movable set
            sorted_sel = sorted(
                i for i in self._psel_set if not self._is_plugin_locked(i)
            )
            if not sorted_sel:
                return
            blk_size = len(sorted_sel)
            slot = max(0, min(int(cy // self.ROW_H), n - blk_size))

            if slot == self._drag_slot:
                self._predraw()
                return
            self._drag_slot = slot
            self._drag_moved = True

            # Extract the selected entries (highest index first to avoid shift issues)
            extracted = []
            for i in sorted(sorted_sel, reverse=True):
                extracted.insert(0, (
                    self._plugin_entries.pop(i),
                    self._pcheck_vars.pop(i),
                    self._pcheck_buttons.pop(i),
                    self._plock_vars.pop(i),
                    self._plock_buttons.pop(i),
                ))

            insert_at = max(0, min(slot, len(self._plugin_entries)))
            for j, (entry, var, cb, lvar, lcb) in enumerate(extracted):
                self._plugin_entries.insert(insert_at + j, entry)
                self._pcheck_vars.insert(insert_at + j, var)
                self._pcheck_buttons.insert(insert_at + j, cb)
                self._plock_vars.insert(insert_at + j, lvar)
                self._plock_buttons.insert(insert_at + j, lcb)

            self._drag_idx = insert_at
            self._sel_idx = insert_at
            self._psel_set = set(range(insert_at, insert_at + blk_size))
        else:
            slot = max(0, min(int(cy // self.ROW_H), n - 1))

            if slot == self._drag_slot:
                return
            self._drag_slot = slot
            self._drag_moved = True

            entry = self._plugin_entries.pop(self._drag_idx)
            var = self._pcheck_vars.pop(self._drag_idx)
            cb = self._pcheck_buttons.pop(self._drag_idx)
            lvar = self._plock_vars.pop(self._drag_idx)
            lcb = self._plock_buttons.pop(self._drag_idx)

            insert_at = max(0, min(slot, len(self._plugin_entries)))
            self._plugin_entries.insert(insert_at, entry)
            self._pcheck_vars.insert(insert_at, var)
            self._pcheck_buttons.insert(insert_at, cb)
            self._plock_vars.insert(insert_at, lvar)
            self._plock_buttons.insert(insert_at, lcb)

            self._drag_idx = insert_at
            self._sel_idx = insert_at
            self._psel_set = {insert_at}

        # Rebind toggle commands to match new indices
        for i, cb2 in enumerate(self._pcheck_buttons):
            cb2.configure(command=lambda idx=i: self._on_plugin_toggle(idx))
        for i, lcb2 in enumerate(self._plock_buttons):
            lcb2.configure(command=lambda idx=i: self._on_plugin_lock_toggle(idx))

        self._predraw()

    def _on_plugin_right_click(self, event):
        """Show context menu for plugin panel."""
        if not self._plugin_entries:
            return
        cy = self._pevent_canvas_y(event)
        idx = self._pcanvas_y_to_index(cy)

        # If right-clicking outside current selection, select the clicked item
        if idx not in self._psel_set:
            self._sel_idx = idx
            self._psel_set = {idx}
            self._predraw()

        # Collect toggleable plugins in selection (non-vanilla)
        toggleable = [
            i for i in sorted(self._psel_set)
            if 0 <= i < len(self._plugin_entries)
            and self._plugin_entries[i].name.lower() not in self._vanilla_plugins
        ]
        if not toggleable:
            return

        self._show_plugin_context_menu(event.x_root, event.y_root, toggleable)

    def _show_plugin_context_menu(self, x: int, y: int, toggleable: list[int]):
        """Custom popup context menu for the plugin panel."""
        popup = tk.Toplevel(self._pcanvas)
        popup.wm_overrideredirect(True)
        popup.wm_geometry(f"+{x}+{y}")
        popup.configure(bg=BORDER)

        _alive = [True]

        def _dismiss(_event=None):
            if _alive[0]:
                _alive[0] = False
                popup.destroy()

        def _pick(cmd):
            if _alive[0]:
                _alive[0] = False
                popup.destroy()
                cmd()

        inner = tk.Frame(popup, bg=BG_PANEL, bd=0)
        inner.pack(padx=1, pady=1)

        count = len(toggleable)
        items = []
        if count == 1:
            items.append(("Enable plugin",
                           lambda idxs=toggleable: self._enable_selected_plugins(idxs)))
            items.append(("Disable plugin",
                           lambda idxs=toggleable: self._disable_selected_plugins(idxs)))
        else:
            items.append((f"Enable selected ({count})",
                           lambda idxs=toggleable: self._enable_selected_plugins(idxs)))
            items.append((f"Disable selected ({count})",
                           lambda idxs=toggleable: self._disable_selected_plugins(idxs)))

        for label, cmd in items:
            btn = tk.Label(
                inner, text=label, anchor="w",
                bg=BG_PANEL, fg=TEXT_MAIN,
                font=("Segoe UI", 11),
                padx=12, pady=5, cursor="hand2",
            )
            btn.pack(fill="x")
            btn.bind("<ButtonRelease-1>", lambda _e, c=cmd: _pick(c))
            btn.bind("<Enter>", lambda _e, b=btn: b.configure(bg=BG_SELECT))
            btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=BG_PANEL))

        popup.update_idletasks()
        popup.bind("<Escape>", _dismiss)

        def _on_press(event):
            if not _alive[0]:
                return
            wx, wy = popup.winfo_rootx(), popup.winfo_rooty()
            ww, wh = popup.winfo_width(), popup.winfo_height()
            if not (wx <= event.x_root <= wx + ww and wy <= event.y_root <= wy + wh):
                _dismiss()
        popup.bind_all("<ButtonPress-1>", _on_press)
        popup.bind_all("<ButtonPress-3>", _on_press)

    def _enable_selected_plugins(self, indices: list[int]):
        """Enable all plugins at the given indices."""
        for i in indices:
            if 0 <= i < len(self._plugin_entries):
                self._plugin_entries[i].enabled = True
                if i < len(self._pcheck_vars):
                    self._pcheck_vars[i].set(True)
        self._save_plugins()
        self._check_all_masters()
        self._predraw()

    def _disable_selected_plugins(self, indices: list[int]):
        """Disable all plugins at the given indices."""
        for i in indices:
            if 0 <= i < len(self._plugin_entries):
                self._plugin_entries[i].enabled = False
                if i < len(self._pcheck_vars):
                    self._pcheck_vars[i].set(False)
        self._save_plugins()
        self._check_all_masters()
        self._predraw()

    def _on_pmouse_release(self, event):
        if self._drag_idx >= 0 and self._drag_moved:
            self._save_plugins()
        elif self._drag_idx >= 0 and not self._drag_moved and len(self._psel_set) > 1:
            # Click (no drag) inside multi-selection — collapse to the clicked item
            cy = self._pevent_canvas_y(event)
            clicked = self._pcanvas_y_to_index(cy)
            if clicked in self._psel_set:
                self._sel_idx = clicked
                self._psel_set = {clicked}
                self._log(f"Selected plugin: {self._plugin_entries[clicked].name}")
        self._drag_idx = -1
        self._drag_moved = False
        self._drag_slot = -1
        self._predraw()


# ---------------------------------------------------------------------------
# Game picker dialog (used by the + button to select an unconfigured game)
# ---------------------------------------------------------------------------
class _GamePickerDialog(ctk.CTkToplevel):
    _ROW_H   = 36   # px per radio button row
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
        for i, name in enumerate(game_names):
            ctk.CTkRadioButton(
                scroll, text=name, variable=self._var, value=name,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
            ).grid(row=i, column=0, sticky="w", padx=12, pady=4)

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

        # Size: fit content up to _MAX_H, centre on parent
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
# TopBar
# ---------------------------------------------------------------------------
class TopBar(ctk.CTkFrame):
    def __init__(self, parent, log_fn=None):
        super().__init__(parent, fg_color=BG_PANEL, corner_radius=0, height=46)
        self.grid_propagate(False)
        self._log = log_fn or (lambda msg: None)

        # Bottom separator line
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="bottom", fill="x"
        )

        # Left: Game label, + button, dropdown
        game_names = _load_games()
        self._game_var = tk.StringVar(value=game_names[0])

        ctk.CTkLabel(
            self, text="Game:", font=FONT_BOLD, text_color=TEXT_MAIN
        ).pack(side="left", padx=(12, 4))

        ctk.CTkButton(
            self, text="+", width=30, height=30, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_add_game
        ).pack(side="left", padx=(0, 4))

        self._game_menu = ctk.CTkOptionMenu(
            self, values=game_names, variable=self._game_var,
            width=180, font=FONT_NORMAL,
            fg_color=BG_HEADER, button_color=ACCENT, button_hover_color=ACCENT_HOV,
            dropdown_fg_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_game_change
        )
        self._game_menu.pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            self, text="⚙", width=30, height=30, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_settings
        ).pack(side="left", padx=(0, 16))

        # Profile
        ctk.CTkLabel(
            self, text="Profile:", font=FONT_BOLD, text_color=TEXT_MAIN
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            self, text="+", width=30, height=30, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_add_profile
        ).pack(side="left", padx=(0, 2))

        ctk.CTkButton(
            self, text="−", width=30, height=30, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_remove_profile
        ).pack(side="left", padx=(0, 4))

        initial_game_name = game_names[0]
        profile_names = _profiles_for_game(initial_game_name)
        self._profile_var = tk.StringVar(value=profile_names[0])
        self._profile_menu = ctk.CTkOptionMenu(
            self, values=profile_names, variable=self._profile_var,
            width=160, font=FONT_NORMAL,
            fg_color=BG_HEADER, button_color=ACCENT, button_hover_color=ACCENT_HOV,
            dropdown_fg_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_profile_change
        )
        self._profile_menu.pack(side="left", padx=(0, 16))

        # Install Mod button
        ctk.CTkButton(
            self, text="+ Install Mod", width=130, height=32, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_install_mod
        ).pack(side="left", padx=8)

        # Deploy button
        self._deploy_btn = ctk.CTkButton(
            self, text="▶ Deploy", width=100, height=32, font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._on_deploy
        )
        self._deploy_btn.pack(side="left", padx=(0, 8))

        # Restore button
        self._restore_btn = ctk.CTkButton(
            self, text="↩ Restore", width=100, height=32, font=FONT_BOLD,
            fg_color="#8b1a1a", hover_color="#b22222", text_color="white",
            command=self._on_restore
        )
        self._restore_btn.pack(side="left", padx=(0, 8))

        # Proton tools button
        _proton_icon = _load_icon("proton.png", size=(18, 18))
        self._proton_btn = ctk.CTkButton(
            self, text="Proton", width=100, height=32, font=FONT_BOLD,
            image=_proton_icon, compound="left",
            fg_color="#7b2d8b", hover_color="#9a3aae", text_color="white",
            command=self._on_proton_tools
        )
        self._proton_btn.pack(side="left", padx=(0, 8))

        # Nexus Mods settings button
        _nexus_icon = _load_icon("nexus.png", size=(18, 18))
        ctk.CTkButton(
            self, text="Nexus", width=80, height=32, font=FONT_BOLD,
            image=_nexus_icon, compound="left",
            fg_color="#da8e35", hover_color="#e5a04a", text_color="white",
            command=self._on_nexus_settings
        ).pack(side="left", padx=(0, 4))

        # Check for Nexus mod updates button
        self._update_btn = ctk.CTkButton(
            self, text="Check Updates", width=120, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_check_updates
        )
        self._update_btn.pack(side="left", padx=(0, 8))

    def _on_nexus_settings(self):
        """Open the Nexus Mods settings dialog."""
        app = self.winfo_toplevel()
        def _key_changed():
            app._init_nexus_api()
            self._log("Nexus API key updated.")
        dialog = NexusSettingsDialog(app, on_key_changed=_key_changed)
        app.wait_window(dialog)

    def _on_check_updates(self):
        """Check all installed Nexus mods for updates and missing requirements."""
        app = self.winfo_toplevel()
        if app._nexus_api is None:
            self._log("Nexus: Set your API key first (Nexus button).")
            return
        game = _GAMES.get(self._game_var.get())
        if game is None or not game.is_configured():
            self._log("No configured game selected.")
            return

        staging = game.get_mod_staging_path()
        self._update_btn.configure(text="Checking...", state="disabled")
        log_fn = self._log

        def _worker():
            try:
                # Phase 1: Check for updates
                results = check_for_updates(
                    app._nexus_api, staging,
                    game_domain=game.nexus_game_domain,
                    progress_cb=lambda m: app.after(0, lambda msg=m: log_fn(msg)),
                )
                # Phase 2: Check for missing requirements
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
                    # Refresh flags column so icons appear / disappear
                    if hasattr(app, "_mod_panel"):
                        app._mod_panel._scan_update_flags()
                        app._mod_panel._scan_missing_reqs_flags()
                        app._mod_panel._scan_endorsed_flags()
                        app._mod_panel._redraw()
                app.after(0, _done)
            except Exception as exc:
                app.after(0, lambda: (
                    self._update_btn.configure(text="Check Updates", state="normal"),
                    log_fn(f"Nexus: Check failed — {exc}"),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_proton_tools(self):
        game = _GAMES.get(self._game_var.get())
        if game is None or not game.is_configured():
            self._log("Proton Tools: no configured game selected.")
            return
        dlg = _ProtonToolsDialog(self.winfo_toplevel(), game, self._log)
        self.winfo_toplevel().wait_window(dlg)

    def _on_profile_change(self, value: str):
        self._log(f"Profile: {value}")
        self._reload_mod_panel()

    def _on_game_change(self, value: str):
        game = _GAMES.get(value)
        if game and game.is_configured():
            self._log(f"Game: {value} — {game.get_game_path()}")
        else:
            self._log(f"Game: {value} — not configured (click + to set path)")
        # Refresh profile dropdown for the new game
        profiles = _profiles_for_game(value)
        self._profile_menu.configure(values=profiles)
        self._profile_var.set(profiles[0])
        self._reload_mod_panel()

    def _reload_mod_panel(self):
        """Tell the mod panel and plugin panel to load the current game + profile."""
        app = self.winfo_toplevel()
        if not hasattr(app, "_mod_panel"):
            return
        game = _GAMES.get(self._game_var.get())
        if game and game.is_configured():
            # Update plugin panel paths BEFORE load_game, because load_game
            # triggers _rebuild_filemap → _on_filemap_rebuilt which reads
            # _plugins_path. If we update after, the old game's path is used.
            # Also clear _plugin_entries immediately so any pending save callbacks
            # cannot write the old game's plugins to the new game's file.
            if hasattr(app, "_plugin_panel"):
                plugins_path = (
                    game.get_profile_root()
                    / "profiles" / self._profile_var.get() / "plugins.txt"
                )
                app._plugin_panel._plugin_entries = []
                app._plugin_panel._plugins_path = plugins_path
                app._plugin_panel._plugin_extensions = game.plugin_extensions
                app._plugin_panel._vanilla_plugins = _vanilla_plugins_for_game(game)
                app._plugin_panel._staging_root = game.get_mod_staging_path()
                data_path = game.get_mod_data_path() if hasattr(game, 'get_mod_data_path') else None
                app._plugin_panel._data_dir = data_path
                app._plugin_panel._game = game
            app._mod_panel.load_game(game, self._profile_var.get())
            # load_game already triggered _on_filemap_rebuilt which refreshed
            # the plugins tab, so just ensure state is consistent.
            if hasattr(app, "_plugin_panel"):
                app._plugin_panel._refresh_plugins_tab()
                app._plugin_panel.refresh_exe_list()

    def _on_add_profile(self):
        game_name = self._game_var.get()
        if game_name not in _GAMES:
            self._log("No game selected.")
            return
        dialog = _ProfileNameDialog(self.winfo_toplevel())
        self.winfo_toplevel().wait_window(dialog)
        if dialog.result is None:
            return
        name = dialog.result
        # Reject names that clash with 'default' or already exist
        existing = _profiles_for_game(game_name)
        if name in existing:
            self._log(f"Profile '{name}' already exists.")
            return
        _create_profile(game_name, name)
        self._log(f"Profile '{name}' created.")
        profiles = _profiles_for_game(game_name)
        self._profile_menu.configure(values=profiles)
        self._profile_var.set(name)
        self._reload_mod_panel()

    def _on_remove_profile(self):
        game_name = self._game_var.get()
        profile = self._profile_var.get()
        if profile == "default":
            self._log("Cannot remove the default profile.")
            return
        confirmed = tk.messagebox.askyesno(
            "Remove Profile",
            f"Remove profile '{profile}'?\n\nThis will delete modlist.txt and plugins.txt for this profile.",
            parent=self.winfo_toplevel(),
        )
        if not confirmed:
            return
        game = _GAMES.get(game_name)
        if game is not None:
            profile_dir = game.get_profile_root() / "profiles" / profile
        else:
            profile_dir = get_profiles_dir() / game_name / "profiles" / profile
        if profile_dir.is_dir():
            shutil.rmtree(profile_dir)
        self._log(f"Profile '{profile}' removed.")
        profiles = _profiles_for_game(game_name)
        self._profile_menu.configure(values=profiles)
        self._profile_var.set(profiles[0])
        self._reload_mod_panel()

    def _on_add_game(self):
        all_names = sorted(_GAMES.keys())
        if not all_names:
            self._log("No game handlers discovered.")
            return
        picker = _GamePickerDialog(self.winfo_toplevel(), all_names)
        self.winfo_toplevel().wait_window(picker)
        if picker.result is None:
            return
        game = _GAMES.get(picker.result)
        if game is None:
            return
        dialog = AddGameDialog(self.winfo_toplevel(), game)
        self.winfo_toplevel().wait_window(dialog)
        if dialog.result is not None:
            self._log(f"Game path set: {dialog.result}")
            configured = sorted(n for n, g in _GAMES.items() if g.is_configured())
            self._game_menu.configure(values=configured or ["No games configured"])
            if picker.result in configured:
                self._game_var.set(picker.result)
                self._reload_mod_panel()

    def _on_settings(self):
        game_name = self._game_var.get()
        game = _GAMES.get(game_name)
        if game is None:
            self._log("No game selected.")
            return
        dialog = AddGameDialog(self.winfo_toplevel(), game)
        self.winfo_toplevel().wait_window(dialog)
        if getattr(dialog, "removed", False):
            self._log(f"Removed instance: {game_name}")
            # Re-load the game handler so it picks up the missing config
            game.load_paths()
            configured = sorted(n for n, g in _GAMES.items() if g.is_configured())
            self._game_menu.configure(values=configured or ["No games configured"])
            if configured:
                self._game_var.set(configured[0])
                self._on_game_change(configured[0])
            else:
                self._game_var.set("No games configured")
                self._on_game_change("No games configured")
        elif dialog.result is not None:
            self._log(f"Game path updated: {dialog.result}")
            self._reload_mod_panel()

    def _set_deploy_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self._deploy_btn.configure(state=state)
        self._restore_btn.configure(state=state)

    def _on_deploy(self):
        game = _GAMES.get(self._game_var.get())
        if game is None or not game.is_configured():
            self._log("Deploy: no configured game selected.")
            return
        if not hasattr(game, "deploy"):
            self._log(f"Deploy: '{game.name}' does not support deployment.")
            return

        app = self.winfo_toplevel()
        root_folder_enabled = (
            app._mod_panel._root_folder_enabled
            if hasattr(app, "_mod_panel") else True
        )
        root_folder_dir = game.get_mod_staging_path().parent / "Root_Folder"
        game_root = game.get_game_path()
        profile = self._profile_var.get()

        status_bar = self.winfo_toplevel()._status

        def _worker():
            # Thread-safe log: schedule UI update on the main thread.
            def _tlog(msg):
                self.after(0, lambda m=msg: self._log(m))

            def _progress(done: int, total: int):
                self.after(0, lambda d=done, t=total: status_bar.set_progress(d, t))

            try:
                if hasattr(game, "restore"):
                    try:
                        game.restore(log_fn=_tlog)
                    except RuntimeError:
                        pass
                if root_folder_dir.is_dir() and game_root:
                    restore_root_folder(root_folder_dir, game_root, log_fn=_tlog)

                deploy_mode = game.get_deploy_mode() if hasattr(game, "get_deploy_mode") else LinkMode.HARDLINK
                game.deploy(log_fn=_tlog, profile=profile, progress_fn=_progress,
                            mode=deploy_mode)

                rf_allowed = getattr(game, "root_folder_deploy_enabled", True)
                if rf_allowed and root_folder_enabled and root_folder_dir.is_dir() and game_root:
                    _tlog("Root Folder: transferring files to game root ...")
                    deploy_root_folder(root_folder_dir, game_root,
                                       mode=deploy_mode, log_fn=_tlog)
            except Exception as e:
                self.after(0, lambda err=e: self._log(f"Deploy error: {err}"))
            finally:
                self.after(0, lambda: self._set_deploy_buttons_enabled(True))
                self.after(1500, status_bar.clear_progress)

        self._set_deploy_buttons_enabled(False)
        threading.Thread(target=_worker, daemon=True).start()

    def _on_restore(self):
        game = _GAMES.get(self._game_var.get())
        if game is None or not game.is_configured():
            self._log("Restore: no configured game selected.")
            return

        root_folder_dir = game.get_mod_staging_path().parent / "Root_Folder"
        game_root = game.get_game_path()

        def _worker():
            def _tlog(msg):
                self.after(0, lambda m=msg: self._log(m))

            try:
                if hasattr(game, "restore"):
                    game.restore(log_fn=_tlog)
                else:
                    _tlog(f"Restore: '{game.name}' does not support restore.")
                if root_folder_dir.is_dir() and game_root:
                    restore_root_folder(root_folder_dir, game_root, log_fn=_tlog)
            except Exception as e:
                self.after(0, lambda err=e: self._log(f"Restore error: {err}"))
            finally:
                self.after(0, lambda: self._set_deploy_buttons_enabled(True))

        self._set_deploy_buttons_enabled(False)
        threading.Thread(target=_worker, daemon=True).start()

    def _on_install_mod(self):
        path = _pick_file_zenity("Select Mod Archive")
        if not path:
            return
        game = _GAMES.get(self._game_var.get())
        if game is None or not game.is_configured():
            self._log("No configured game selected — use + to set the game path first.")
            return
        self._log(f"Installing: {os.path.basename(path)}")
        app = self.winfo_toplevel()
        mod_panel = getattr(app, "_mod_panel", None)
        _install_mod_from_archive(path, app, self._log, game, mod_panel)


# ---------------------------------------------------------------------------
# Install logic
# ---------------------------------------------------------------------------

def _suggest_mod_names(filename_stem: str) -> list[str]:
    """
    Given a raw filename stem (no extension), return a list of name candidates
    from most-clean to least-clean.

    Nexus Mods format:  ModName-nexusid-version-timestamp
    e.g. "All in one (all game versions)-32444-11-1770897704"
      → ["All in one (all game versions)", "All in one (all game versions)-32444-11-1770897704"]

    The algorithm strips trailing dash-separated segments that are purely
    numeric, stopping when it hits a non-numeric segment.
    """
    # Strip all trailing numeric dash-segments at once (Nexus: name-id-ver-timestamp)
    clean = re.sub(r"(-\d+)+$", "", filename_stem).strip()

    result = []
    if clean and clean != filename_stem:
        result.append(clean)
    result.append(filename_stem)
    return result


class NameModDialog(ctk.CTkToplevel):
    """
    Modal dialog that lets the user pick/edit the mod name before installing.
    Shows a dropdown of suggested names and an editable entry.

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

        # Editable entry pre-filled with cleanest suggestion
        self._entry_var = tk.StringVar(value=suggestions[0] if suggestions else "")
        entry = ctk.CTkEntry(
            self, textvariable=self._entry_var,
            font=FONT_NORMAL, fg_color=BG_PANEL, text_color=TEXT_MAIN,
            border_color=BORDER
        )
        entry.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 4))
        entry.bind("<Return>", lambda _e: self._on_ok())

        # Dropdown of suggestions (only shown if more than one)
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

        # Button bar
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

        # Resize to fit content and center on parent window
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
        # Only cancel if focus left the dialog entirely (not just moved between child widgets)
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


class _ProtonToolsDialog(ctk.CTkToplevel):
    """Modal dialog with Proton-related tools for the selected game."""

    def __init__(self, parent, game, log_fn):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Proton Tools")
        self.geometry("340x200")
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

    # ------------------------------------------------------------------

    def _get_proton_env(self):
        """Return (proton_script, env) or log an error and return (None, None)."""
        from Utils.steam_finder import find_proton_for_game

        prefix_path = self._game.get_prefix_path()
        if prefix_path is None or not prefix_path.is_dir():
            self._log("Proton Tools: prefix not configured for this game.")
            return None, None

        steam_id = getattr(self._game, "steam_id", "")
        if not steam_id:
            self._log("Proton Tools: game has no Steam ID — cannot determine Proton version.")
            return None, None

        proton_script = find_proton_for_game(steam_id)
        if proton_script is None:
            self._log(
                f"Proton Tools: could not find Proton version for app {steam_id}. "
                "Check that Steam has run the game at least once."
            )
            return None, None

        compat_data = prefix_path.parent
        steam_root = proton_script.parent.parent.parent.parent

        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)

        return proton_script, env

    def _close_and_run(self, fn):
        """Release the modal grab, destroy the dialog, then call *fn*."""
        log = self._log                # prevent reference to destroyed self
        parent = self.master           # keep a live widget for .after()
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        # Schedule fn on the next event-loop tick so the dialog is fully gone
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

    def _run_protontricks(self):
        steam_id = getattr(self._game, "steam_id", "")
        if not steam_id:
            self._log("Proton Tools: game has no Steam ID — cannot run protontricks.")
            return

        # Check for native binary first, then Flatpak
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
            # Pick an exe using zenity (runs in main thread — zenity is a
            # separate process so it won't freeze the GUI on most WMs)
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


def _to_wine_path(linux_path: "Path | str") -> str:
    r"""Convert a Linux absolute path to a Proton/Wine Z:\ path."""
    return "Z:" + str(linux_path).replace("/", "\\")


class _ExeConfigDialog(ctk.CTkToplevel):
    """Modal dialog for configuring command-line arguments for a Windows exe.

    Builds a structured argument string from:
      - A game-root flag + the game's install directory (as a Wine path)
      - An output flag + a selected mod folder (as a Wine path)
    The assembled string is shown in an editable text box and saved to
    Profiles/<game>/Applications/<exe_stem>.json.
    """

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

        # Compute base paths
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
        # List of (display_name, actual_path) for every selectable output folder
        self._mod_entries: list[tuple[str, "Path"]] = self._load_mod_entries()
        self._filtered_entries: list[tuple[str, "Path"]] = list(self._mod_entries)
        self._radio_buttons: list[ctk.CTkRadioButton] = []

        self._build()
        self._load_saved()

        # Wire up auto-assembly after initial load
        self._game_flag_var.trace_add("write", self._assemble)
        self._output_flag_var.trace_add("write", self._assemble)
        self._mod_var.trace_add("write", self._assemble)
        self._search_var.trace_add("write", self._on_search_changed)

        self.after(80, self._make_modal)

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _load_mod_entries(self) -> "list[tuple[str, Path]]":
        entries: list[tuple[str, Path]] = []
        # Overwrite folder first (if it exists)
        if self._overwrite_path and self._overwrite_path.is_dir():
            entries.append(("overwrite", self._overwrite_path))
        # All mod folders, sorted alphabetically
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

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)  # mod list row expands

        # ── Section 1: Game path arg ──────────────────────────────────
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

        # ── Section 2: Output arg ──────────────────────────────────────
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

        # Mod list (scrollable)
        self._mod_scroll = ctk.CTkScrollableFrame(
            self, fg_color=BG_PANEL, corner_radius=6,
        )
        self._mod_scroll.grid(row=2, column=0, sticky="nsew", padx=12, pady=4)
        self._mod_scroll.grid_columnconfigure(0, weight=1)
        self._rebuild_mod_list()

        # ── Section 3: Final argument ──────────────────────────────────
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

        # ── Button bar ────────────────────────────────────────────────
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

    # ------------------------------------------------------------------
    # Mod list filtering
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Argument assembly
    # ------------------------------------------------------------------

    def _assemble(self, *_):
        """Build the final argument string from the current field values."""
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

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def _parse_saved_args(self, args: str):
        """Parse a saved argument string and populate flag / mod fields.

        Scans the string for quoted Wine paths that match the game path or
        any known mod folder, and extracts the flag prefix preceding each
        quoted path.
        """
        import re

        # Find all  flag"path"  or  flag"path"  segments
        # Pattern: non-whitespace flag chars immediately before a quoted string
        segments = re.findall(r'(\S+?)"([^"]+)"', args)

        game_wine = _to_wine_path(self._game_path).rstrip("\\") if self._game_path else None

        for flag, quoted_path in segments:
            normalised = quoted_path.rstrip("\\")

            # Check if this segment matches the game path (or a sub-path of it)
            if game_wine and (normalised == game_wine
                              or normalised.startswith(game_wine + "\\")):
                self._game_flag_var.set(flag)
                continue

            # Check if it matches any known mod / overwrite folder
            matched = False
            for name, path in self._mod_entries:
                mod_wine = _to_wine_path(path).rstrip("\\")
                if normalised == mod_wine or normalised.startswith(mod_wine + "\\"):
                    self._output_flag_var.set(flag)
                    self._mod_var.set(name)
                    matched = True
                    break

            # Fallback: extract the last path component and match by name.
            # Handles cases where the folder doesn't exist on disk yet or
            # the path was built from a different staging root.
            if not matched:
                tail = normalised.rsplit("\\", 1)[-1] if "\\" in normalised else ""
                if tail:
                    self._output_flag_var.set(flag)
                    # Select the mod if it's in the list, otherwise just
                    # set the variable so it's visible in the final text.
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
        import json as _json
        try:
            data = _json.loads(self._EXE_ARGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[self._exe_path.name] = final
        try:
            self._EXE_ARGS_FILE.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass  # Non-fatal; args still returned to caller
        self.result = final
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


def _pick_file_zenity(title: str) -> str:
    """Open a native GTK file picker via zenity. Returns the chosen path or ''."""
    try:
        result = subprocess.run(
            [
                "zenity", "--file-selection",
                f"--title={title}",
                "--file-filter=Mod Archives (*.zip, *.7z, *.tar.gz, *.tar) | *.zip *.7z *.tar.gz *.tar",
                "--file-filter=All files | *",
            ],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    return ""


class _ReplaceModDialog(ctk.CTkToplevel):
    """
    Modal dialog shown when installing a mod whose name already exists.
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
    game's required folders.  Shows a live-updating folder tree preview as the
    user types a prefix path.

    result: ("prefix", path_str) | ("as_is", None) | None (cancelled)
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
        self.grid_rowconfigure(4, weight=1)   # tree row expands

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

        # Tree preview
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

    # ------------------------------------------------------------------
    # Live tree
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

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
        self._file_list = file_list  # [(src_rel, dst_rel, is_folder), ...]
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

        # Scrollable frame for checkboxes
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

        # Select all / none helpers
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

        # Size and centre
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


def _check_mod_top_level(file_list: list[tuple[str, str, bool]],
                         required: set[str]) -> bool:
    """Return True if at least one file's top-level folder matches a required name."""
    for _, dst_rel, _ in file_list:
        top = dst_rel.replace("\\", "/").split("/")[0].lower()
        if top in required:
            return True
    return False


def _install_mod_from_archive(archive_path: str, parent_window, log_fn,
                              game, mod_panel=None) -> None:
    """
    Extract archive to a temp directory, detect FOMOD, run the wizard if
    present, then copy the resolved files into the game's mod staging area.
    Supports .zip, .7z, and .tar.* formats.
    """
    ext = archive_path.lower()
    raw_stem = os.path.splitext(os.path.basename(archive_path))[0]
    # Strip inner extension for .tar.gz etc.
    if raw_stem.endswith(".tar"):
        raw_stem = os.path.splitext(raw_stem)[0]

    # --- Determine mod name (cleanest Nexus-stripped candidate) ---
    suggestions = _suggest_mod_names(raw_stem)
    mod_name = suggestions[0] if suggestions else raw_stem

    extract_dir = tempfile.mkdtemp(prefix="modmgr_")

    try:
        # --- Extract ---
        if ext.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as z:
                z.extractall(extract_dir)
        elif ext.endswith(".7z"):
            try:
                with py7zr.SevenZipFile(archive_path, "r") as z:
                    z.extractall(extract_dir)
            except Exception as e7:
                log_fn(f"py7zr failed ({e7}), retrying with libarchive…")
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                import libarchive
                prev_cwd = os.getcwd()
                try:
                    os.chdir(extract_dir)
                    libarchive.extract_file(archive_path)
                finally:
                    os.chdir(prev_cwd)
        elif any(ext.endswith(s) for s in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar")):
            with tarfile.open(archive_path, "r:*") as t:
                t.extractall(extract_dir)
        elif ext.endswith(".rar"):
            try:
                import rarfile
                with rarfile.RarFile(archive_path, "r") as r:
                    r.extractall(extract_dir)
            except (ImportError, Exception) as e_rar:
                log_fn(f"rarfile failed ({e_rar}), trying libarchive…")
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                import libarchive
                prev_cwd = os.getcwd()
                try:
                    os.chdir(extract_dir)
                    libarchive.extract_file(archive_path)
                finally:
                    os.chdir(prev_cwd)
        else:
            log_fn(f"Unsupported archive format: {os.path.basename(archive_path)}")
            log_fn("Supported formats: .zip, .7z, .rar, .tar.gz")
            return

        # --- Resolve file list ---
        fomod_result = detect_fomod(extract_dir)
        if fomod_result:
            mod_root, config_path = fomod_result
            log_fn("FOMOD installer detected — opening wizard...")
            config = parse_module_config(config_path)

            # Build set of installed/active plugin filenames for dependency checks.
            # Lowercased for case-insensitive matching (FOMOD is Windows-native).
            installed_files: set[str] = set()
            if mod_panel is not None and mod_panel._modlist_path is not None:
                plugins_path = mod_panel._modlist_path.parent / "plugins.txt"
                if plugins_path.is_file():
                    for entry in read_plugins(plugins_path):
                        if entry.enabled:
                            installed_files.add(entry.name.lower())

            # Load saved FOMOD selections from a previous install
            saved_selections = None
            game_name = getattr(game, "name", "")
            if game_name:
                sel_path = get_fomod_selections_path(game_name, mod_name)
                if sel_path.is_file():
                    try:
                        with open(sel_path, "r", encoding="utf-8") as f:
                            saved_selections = json.load(f)
                        log_fn("Restored previous FOMOD selections.")
                    except Exception:
                        saved_selections = None

            dialog = FomodDialog(parent_window, config, mod_root,
                                 installed_files=installed_files,
                                 saved_selections=saved_selections)
            parent_window.wait_window(dialog)
            if dialog.result is None:
                log_fn("FOMOD install cancelled.")
                return

            # Save FOMOD selections for future reinstalls
            if game_name:
                sel_path = get_fomod_selections_path(game_name, mod_name)
                try:
                    with open(sel_path, "w", encoding="utf-8") as f:
                        json.dump(dialog.result, f, indent=2)
                except Exception:
                    pass

            file_list = resolve_files(config, dialog.result, installed_files)
            log_fn(f"FOMOD complete — {len(file_list)} file(s) to install.")
        else:
            # Direct install: copy everything from the archive root
            mod_root = extract_dir
            file_list = _resolve_direct_files(extract_dir)
            log_fn(f"Direct install — {len(file_list)} file(s) to install.")

        # --- Check for existing mod folder ---
        dest_root = game.get_mod_staging_path() / mod_name
        replace_selected_only = False
        replace_all = False
        if dest_root.exists():
            replace_dialog = _ReplaceModDialog(parent_window, mod_name)
            parent_window.wait_window(replace_dialog)
            if replace_dialog.result == "cancel":
                log_fn(f"Install cancelled — '{mod_name}' already exists.")
                return
            if replace_dialog.result == "selected":
                replace_selected_only = True
            elif replace_dialog.result == "all":
                replace_all = True

        # --- If replacing selected files only, show picker now ---
        if replace_selected_only:
            sel_dialog = _SelectFilesDialog(parent_window, file_list)
            parent_window.wait_window(sel_dialog)
            if sel_dialog.result is None:
                log_fn("Install cancelled — no files selected.")
                return
            chosen = sel_dialog.result  # set of dst_rel strings
            file_list = [(s, d, f) for s, d, f in file_list if d in chosen]
            log_fn(f"Replace selected: {len(file_list)} file(s) chosen.")

        # --- Apply automatic install prefix (e.g. "mods" for Witcher 3) ---
        install_prefix = getattr(game, "mod_install_prefix", "")
        if install_prefix:
            install_prefix = install_prefix.strip().strip("/").replace("\\", "/")
            prefix_parts = install_prefix.lower().split("/")
            new_file_list = []
            for s, d, f in file_list:
                d_parts = d.replace("\\", "/").split("/")
                d_parts_lower = [p.lower() for p in d_parts]
                # Find how many trailing segments of the prefix match the leading segments of d.
                # e.g. prefix="BepInEx/plugins", d="plugins/foo.dll" → 1 match ("plugins")
                #       so we prepend "BepInEx" only.
                match_len = 0
                for i in range(len(prefix_parts), 0, -1):
                    if d_parts_lower[:i] == prefix_parts[-i:]:
                        match_len = i
                        break
                missing = "/".join(install_prefix.split("/")[:len(prefix_parts) - match_len])
                if missing:
                    new_file_list.append((s, f"{missing}/{d}", f))
                else:
                    new_file_list.append((s, d, f))
            file_list = new_file_list
            log_fn(f"Auto-prefixed mod files under '{install_prefix}/' (where needed).")

        # --- Check mod structure (games with required top-level folders) ---
        required = getattr(game, "mod_required_top_level_folders", set())
        if required and not _check_mod_top_level(file_list, required):
            dlg = _SetPrefixDialog(parent_window, required, file_list)
            parent_window.wait_window(dlg)
            if dlg.result is None:
                log_fn("Install cancelled — mod structure not mapped.")
                return
            action, prefix = dlg.result
            if action == "prefix" and prefix:
                prefix = prefix.strip().strip("/").replace("\\", "/")
                file_list = [(s, f"{prefix}/{d}", f) for s, d, f in file_list]
                log_fn(f"Remapped mod files under '{prefix}/'.")

        # --- Copy into staging area ---
        dest_root = game.get_mod_staging_path() / mod_name
        if replace_all and dest_root.exists():
            shutil.rmtree(dest_root)
            log_fn(f"Cleared existing mod folder for clean reinstall.")
        _copy_file_list(file_list, mod_root, dest_root, log_fn)
        log_fn(f"Installed '{mod_name}' → {dest_root}")

        # --- Scan newly installed mod for plugin files and append to plugins.txt ---
        plugin_exts = getattr(game, "plugin_extensions", [])
        if plugin_exts and mod_panel is not None and mod_panel._modlist_path is not None:
            plugins_path = mod_panel._modlist_path.parent / "plugins.txt"
            exts_lower = {ext.lower() for ext in plugin_exts}
            added = 0
            if dest_root.is_dir():
                for entry in dest_root.iterdir():
                    if entry.is_file() and entry.suffix.lower() in exts_lower:
                        append_plugin(plugins_path, entry.name, enabled=True)
                        added += 1
            if added:
                log_fn(f"plugins.txt: added {added} plugin(s) from '{mod_name}'.")

        # --- Add to modlist.txt (top = highest priority) ---
        if mod_panel is not None and mod_panel._modlist_path is not None:
            modlist_path = mod_panel._modlist_path
        else:
            profile_dir = game.get_profile_root() / "profiles" / "default"
            modlist_path = profile_dir / "modlist.txt"
        prepend_mod(modlist_path, mod_name, enabled=True)
        log_fn(f"Added '{mod_name}' to modlist.")

        # --- Auto-detect Nexus metadata (filename parsing + MD5 lookup) ---
        # Only for manual installs (NXM installs handle metadata separately).
        # Run in a background thread so it doesn't block the UI.
        meta_path = dest_root / "meta.ini"
        _archive = Path(archive_path)
        _game_domain = getattr(game, "nexus_game_domain", "")
        if _game_domain and _archive.is_file():
            def _detect_meta():
                try:
                    # Get the app's Nexus API instance (may be None)
                    import tkinter as _tk
                    app = None
                    try:
                        for w in parent_window.winfo_children():
                            pass
                        app = parent_window.winfo_toplevel()
                    except Exception:
                        pass
                    api = getattr(app, "_nexus_api", None) if app else None

                    meta = resolve_nexus_meta_for_archive(
                        _archive, _game_domain,
                        api=api,
                        log_fn=lambda m: (
                            app.after(0, lambda msg=m: log_fn(msg))
                            if app else None
                        ),
                    )
                    if meta:
                        write_meta(meta_path, meta)
                        msg = f"Nexus: Saved metadata for '{mod_name}' (mod {meta.mod_id})"
                        if app:
                            app.after(0, lambda: log_fn(msg))
                except Exception:
                    pass  # non-critical — don't break the install
            threading.Thread(target=_detect_meta, daemon=True).start()

        # --- Refresh the mod panel ---
        if mod_panel is not None:
            mod_panel.reload_after_install()

    except Exception as e:
        import traceback
        log_fn(f"Install error: {e}")
        log_fn(traceback.format_exc())
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def _resolve_direct_files(extract_dir: str) -> list[tuple[str, str, bool]]:
    """
    For a non-FOMOD archive, return every file as a (src, dst, is_folder)
    tuple where src and dst are both relative to the archive root.
    """
    result = []
    root = Path(extract_dir)
    for entry in root.rglob("*"):
        if entry.is_file():
            rel = str(entry.relative_to(root))
            result.append((rel, rel, False))
    return result


def _copy_file_list(file_list: list[tuple[str, str, bool]],
                    src_root: str, dest_root: Path, log_fn) -> None:
    """
    Copy each (source, destination, is_folder) entry from src_root into dest_root.
    source and destination are relative paths from the FOMOD XML (may have
    Windows backslashes — already normalized by fomod_parser properties).
    """
    copied = 0
    for src_rel, dst_rel, is_folder in file_list:
        src = Path(src_root) / src_rel
        dst = dest_root / dst_rel

        if is_folder:
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
                copied += 1
        else:
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    dst.chmod(0o644)
                    dst.unlink()
                shutil.copy2(src, dst)
                copied += 1

    log_fn(f"Copied {copied} item(s) to staging area.")


# ---------------------------------------------------------------------------
# StatusBar
# ---------------------------------------------------------------------------
class StatusBar(ctk.CTkFrame):
    _COLLAPSED_H = 22   # height when log is hidden (just the label bar)
    _EXPANDED_H  = 100  # height when log is visible

    def __init__(self, parent):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0,
                         height=self._COLLAPSED_H)
        self.grid_propagate(False)

        self._visible = False  # hidden by default

        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )

        label_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=20)
        label_bar.pack(side="top", fill="x")
        ctk.CTkLabel(
            label_bar, text="Log", font=FONT_SMALL, text_color=TEXT_DIM
        ).pack(side="left", padx=8)

        self._toggle_btn = ctk.CTkButton(
            label_bar, text="▲ Show", width=70, height=16,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_DIM, font=FONT_SMALL,
            command=self._toggle_log,
        )
        self._toggle_btn.pack(side="right", padx=6, pady=2)

        # Progress bar + label (hidden until a deploy is in progress)
        self._progress_label = ctk.CTkLabel(
            label_bar, text="", font=FONT_SMALL, text_color=TEXT_DIM, width=120, anchor="e"
        )
        self._progress_bar = ctk.CTkProgressBar(
            label_bar, width=180, height=10,
            fg_color=BG_HEADER, progress_color="#7aa2f7", corner_radius=4
        )
        self._progress_bar.set(0)
        self._progress_visible = False

        self._textbox = ctk.CTkTextbox(
            self, font=FONT_MONO, fg_color=BG_DEEP,
            text_color=TEXT_MAIN, state="disabled",
            wrap="none", corner_radius=0
        )
        # Start hidden — don't pack the textbox yet

    def _toggle_log(self):
        self._visible = not self._visible
        if self._visible:
            self._textbox.pack(fill="both", expand=True)
            self.configure(height=self._EXPANDED_H)
            self._toggle_btn.configure(text="▼ Hide")
        else:
            self._textbox.pack_forget()
            self.configure(height=self._COLLAPSED_H)
            self._toggle_btn.configure(text="▲ Show")

    def set_progress(self, done: int, total: int) -> None:
        """Show / update the progress bar.  Call from main thread only."""
        if not self._progress_visible:
            # Pack bar first (rightmost after toggle btn), then label to its left.
            self._progress_bar.pack(side="right", padx=(0, 8))
            self._progress_label.pack(side="right", padx=(0, 4))
            self._progress_visible = True
        frac = done / total if total > 0 else 0
        self._progress_bar.set(frac)
        self._progress_label.configure(text=f"{done} / {total}")

    def clear_progress(self) -> None:
        """Hide the progress bar when the operation finishes."""
        if self._progress_visible:
            self._progress_bar.pack_forget()
            self._progress_label.pack_forget()
            self._progress_visible = False
        self._progress_bar.set(0)
        self._progress_label.configure(text="")

    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._textbox.configure(state="normal")
        self._textbox.insert("end", f"[{timestamp}]  {message}\n")
        self._textbox.see("end")
        self._textbox.configure(state="disabled")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=BG_DEEP)
        self.title("Amethyst Mod Manager")
        self.geometry("1400x820")
        self.minsize(900, 600)
        self._nexus_api: NexusAPI | None = None
        self._nexus_downloader: NexusDownloader | None = None
        self._init_nexus_api()
        self._build_layout()
        self._startup_log()
        # Process --nxm argument if the app was launched via protocol handler
        self._handle_nxm_argv()

    # -- Nexus API init -----------------------------------------------------

    def _init_nexus_api(self):
        """Load saved API key and initialise the Nexus client (if key exists)."""
        key = load_api_key()
        if key:
            self._nexus_api = NexusAPI(api_key=key)
            self._nexus_downloader = NexusDownloader(self._nexus_api)
        else:
            self._nexus_api = None
            self._nexus_downloader = None

    # -- NXM protocol handling ----------------------------------------------

    def _handle_nxm_argv(self):
        """Check sys.argv for --nxm <url> and kick off a download."""
        import sys
        if "--nxm" not in sys.argv:
            return
        try:
            idx = sys.argv.index("--nxm")
            nxm_url = sys.argv[idx + 1]
        except (IndexError, ValueError):
            return
        self.after(500, lambda: self._process_nxm_link(nxm_url))

    def _start_nxm_ipc(self):
        """Start the IPC server so running instance can receive NXM links."""
        def _on_nxm(url: str):
            self.after(0, lambda: self._receive_nxm(url))
        NxmIPC.start_server(_on_nxm)

    def _receive_nxm(self, nxm_url: str):
        """Handle an NXM link delivered via IPC from a second instance."""
        self._status.log(f"Nexus: Received link from browser.")
        # Raise the window so the user sees what's happening
        self.deiconify()
        self.lift()
        self.focus_force()
        self._process_nxm_link(nxm_url)

    def _process_nxm_link(self, nxm_url: str):
        """Download a mod from an nxm:// link and install it."""
        log = self._status.log

        if self._nexus_api is None or self._nexus_downloader is None:
            log("Nexus: No API key configured — cannot download.")
            log("Open the Nexus button in the toolbar to set your API key.")
            from tkinter import messagebox
            messagebox.showwarning(
                "Nexus API Key Required",
                "You need to set your Nexus Mods API key before downloading.\n\n"
                "Click the \"Nexus\" button in the toolbar to enter your key.\n\n"
                "Get your key from:\nnexusmods.com → Settings → API Keys",
                parent=self,
            )
            return

        try:
            link = NxmLink.parse(nxm_url)
        except ValueError as exc:
            log(f"Nexus: Bad nxm:// URL — {exc}")
            return

        log(f"Nexus: Downloading mod {link.mod_id} file {link.file_id} "
            f"from {link.game_domain}...")

        # Show download progress bar on the mod panel
        mod_panel = getattr(self, "_mod_panel", None)
        if mod_panel:
            mod_panel.show_download_progress("Downloading...")

        # Try to auto-select the matching game
        matched_game = None
        for name, game in _GAMES.items():
            if game.nexus_game_domain == link.game_domain and game.is_configured():
                matched_game = (name, game)
                break

        if matched_game:
            current = self._topbar._game_var.get()
            if current != matched_game[0]:
                self._topbar._game_var.set(matched_game[0])
                self._topbar._on_game_change(matched_game[0])
                log(f"Nexus: Switched to game '{matched_game[0]}'")

        def _worker():
            # Fetch mod + file info in parallel with the download for metadata
            mod_info = None
            file_info = None
            try:
                mod_info = self._nexus_api.get_mod(link.game_domain, link.mod_id)
                # Update the progress bar label with the actual mod name
                if mod_panel and mod_info:
                    self.after(0, lambda: mod_panel.show_download_progress(
                        f"Downloading: {mod_info.name}"))
                files_resp = self._nexus_api.get_mod_files(link.game_domain, link.mod_id)
                for f in files_resp.files:
                    if f.file_id == link.file_id:
                        file_info = f
                        break
            except Exception as exc:
                log_fn = lambda m=str(exc): self.after(0, lambda: log(
                    f"Nexus: Could not fetch mod info ({m}) — metadata will be partial."))
                log_fn()

            result = self._nexus_downloader.download_from_nxm(
                link,
                progress_cb=lambda cur, total: self.after(
                    0, lambda c=cur, t=total: (
                        mod_panel.update_download_progress(c, t)
                        if mod_panel else None
                    )
                ),
            )
            if result.success and result.file_path:
                self.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    self._nxm_install(
                        result, matched_game, mod_info=mod_info, file_info=file_info),
                ))
            else:
                self.after(0, lambda: (
                    mod_panel.hide_download_progress() if mod_panel else None,
                    log(f"Nexus: Download failed — {result.error}"),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _nxm_install(self, result, matched_game, mod_info=None, file_info=None):
        """Install a downloaded NXM file into the current game."""
        log = self._status.log
        game_name = self._topbar._game_var.get()
        game = _GAMES.get(game_name)
        if game is None or not game.is_configured():
            log(f"Nexus: Downloaded {result.file_name} to {result.file_path}")
            log("No configured game selected — install manually from Downloads tab.")
            if hasattr(self, "_plugin_panel"):
                dl_panel = getattr(self._plugin_panel, "_downloads_panel", None)
                if dl_panel:
                    dl_panel.refresh()
            return

        log(f"Nexus: Installing {result.file_name}...")
        mod_panel = getattr(self, "_mod_panel", None)
        _install_mod_from_archive(str(result.file_path), self, log, game, mod_panel)

        # Write Nexus metadata to the installed mod's meta.ini
        try:
            meta = build_meta_from_download(
                game_domain=result.game_domain,
                mod_id=result.mod_id,
                file_id=result.file_id,
                archive_name=result.file_name,
                mod_info=mod_info,
                file_info=file_info,
            )
            # Determine the mod folder name (same logic as _install_mod_from_archive)
            raw_stem = os.path.splitext(os.path.basename(str(result.file_path)))[0]
            if raw_stem.endswith(".tar"):
                raw_stem = os.path.splitext(raw_stem)[0]
            suggestions = _suggest_mod_names(raw_stem)
            folder_name = suggestions[0] if suggestions else raw_stem
            meta_path = game.get_mod_staging_path() / folder_name / "meta.ini"
            if meta_path.parent.is_dir():
                write_meta(meta_path, meta)
                log(f"Nexus: Saved metadata (mod {meta.mod_id}, v{meta.version})")
        except Exception as exc:
            log(f"Nexus: Warning — could not save metadata: {exc}")

    def _build_layout(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)
        self.grid_columnconfigure(0, weight=1)

        # Build status bar first so log_fn is available immediately
        self._status = StatusBar(self)
        self._status.grid(row=2, column=0, sticky="ew")

        log = self._status.log

        self._topbar = TopBar(self, log_fn=log)
        self._topbar.grid(row=0, column=0, sticky="ew", pady=(4, 0))

        main = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        main.grid(row=1, column=0, sticky="nsew")
        main.grid_columnconfigure(0, weight=3)
        main.grid_columnconfigure(1, weight=0)
        main.grid_columnconfigure(2, weight=2)
        main.grid_rowconfigure(0, weight=1)

        self._mod_panel = ModListPanel(main, log_fn=log)
        self._mod_panel.grid(row=0, column=0, sticky="nsew")

        ctk.CTkFrame(main, fg_color=BORDER, width=1, corner_radius=0).grid(
            row=0, column=1, sticky="ns"
        )

        self._plugin_panel = PluginPanel(
            main, log_fn=log,
            get_filemap_path=lambda: (
                str(self._mod_panel._modlist_path.parent.parent.parent / "filemap.txt")
                if self._mod_panel._modlist_path else None
            ),
        )
        self._plugin_panel.grid(row=0, column=2, sticky="nsew")

        def _on_filemap_rebuilt():
            # 1. Sync plugins.txt from the updated filemap
            filemap_path_str = (
                str(self._mod_panel._modlist_path.parent.parent.parent / "filemap.txt")
                if self._mod_panel._modlist_path else None
            )
            if (filemap_path_str
                    and self._plugin_panel._plugins_path is not None
                    and self._plugin_panel._plugin_extensions):
                game = _GAMES.get(self._topbar._game_var.get())
                if game and game.is_configured():
                    self._plugin_panel._vanilla_plugins = _vanilla_plugins_for_game(game)
                    self._plugin_panel._staging_root = (
                        self._mod_panel._modlist_path.parent.parent.parent / "mods"
                    )
                data_dir = (
                    game.get_mod_data_path()
                    if game and game.is_configured() and hasattr(game, 'get_mod_data_path')
                    else None
                )
                self._plugin_panel._data_dir = data_dir
                removed = prune_plugins_from_filemap(
                    Path(filemap_path_str),
                    self._plugin_panel._plugins_path,
                    self._plugin_panel._plugin_extensions,
                    data_dir=data_dir,
                )
                if removed:
                    self._status.log(f"plugins.txt: removed {removed} plugin(s).")
                added = sync_plugins_from_filemap(
                    Path(filemap_path_str),
                    self._plugin_panel._plugins_path,
                    self._plugin_panel._plugin_extensions,
                )
                if added:
                    self._status.log(f"plugins.txt: added {added} new plugin(s).")
            # 2. Refresh Data tab
            self._plugin_panel._refresh_data_tab()
            # 3. Reload Plugins tab from updated plugins.txt
            if (self._plugin_panel._plugins_path is not None
                    and self._plugin_panel._plugin_extensions):
                self._plugin_panel._refresh_plugins_tab()

        self._mod_panel._on_filemap_rebuilt = _on_filemap_rebuilt

        # Wire plugin selection → mod highlight cross-panel (and mutual deselection)
        self._plugin_panel._on_plugin_selected_cb = self._mod_panel.set_highlighted_mod
        self._plugin_panel._on_mod_selected_cb = self._mod_panel.clear_selection  # plugin selected → clear mod selection
        def _on_mod_selected():
            self._plugin_panel.clear_plugin_selection()
            self._mod_panel.set_highlighted_mod(None)
        self._mod_panel._on_mod_selected_cb = _on_mod_selected  # mod selected → clear plugin selection + highlight

        # Load initial game + profile — set plugin paths BEFORE load_game
        # because load_game triggers filemap rebuild which reads _plugins_path.
        initial_game = _GAMES.get(self._topbar._game_var.get())
        if initial_game and initial_game.is_configured():
            profile = self._topbar._profile_var.get()
            plugins_path = (
                initial_game.get_profile_root()
                / "profiles" / profile / "plugins.txt"
            )
            self._plugin_panel._plugins_path = plugins_path
            self._plugin_panel._plugin_extensions = initial_game.plugin_extensions
            self._plugin_panel._vanilla_plugins = _vanilla_plugins_for_game(initial_game)
            self._plugin_panel._staging_root = initial_game.get_mod_staging_path()
            data_path = initial_game.get_mod_data_path() if hasattr(initial_game, 'get_mod_data_path') else None
            self._plugin_panel._data_dir = data_path
            self._plugin_panel._game = initial_game
            self._mod_panel.load_game(initial_game, profile)
            self._plugin_panel.refresh_exe_list()

    def _startup_log(self):
        configured = sum(1 for g in _GAMES.values() if g.is_configured())
        total = len(_GAMES)
        self._status.log(f"Mod Manager ready. {configured}/{total} games configured.")
        self._status.log("Linux mode active. Using CustomTkinter UI framework.")
        if self._nexus_api is not None:
            self._status.log("Nexus Mods API key loaded.")
        if NxmHandler.is_registered():
            self._status.log("NXM protocol handler registered.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    # Register as nxm:// handler on first run (idempotent)
    NxmHandler.register()

    # Single-instance: if --nxm was passed and another instance is running,
    # hand off the link and exit immediately.
    if "--nxm" in sys.argv:
        try:
            idx = sys.argv.index("--nxm")
            nxm_url = sys.argv[idx + 1]
        except (IndexError, ValueError):
            nxm_url = None

        if nxm_url and NxmIPC.send_to_running(nxm_url):
            # Link delivered to the running instance — nothing more to do.
            sys.exit(0)
        # Otherwise no instance is running; continue and open the app.

    app = App()
    app._start_nxm_ipc()          # listen for NXM links from future instances
    app.protocol("WM_DELETE_WINDOW", lambda: (NxmIPC.shutdown(), app.destroy()))
    app.mainloop()
