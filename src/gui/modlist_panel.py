"""
Mod list panel: canvas-based virtual list, toolbar, filters, Nexus update/endorsed.
Used by App. Imports theme, game_helpers, dialogs, install_mod.
"""

import json
import os
import shutil
import subprocess
import threading

from Utils.xdg import xdg_open, open_url
import tkinter as tk
import tkinter.font as tkfont
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
import gui.theme as _theme
from gui.theme import scaled
from gui.ctk_components import CTkAlert, CTkPopupMenu, CTkProgressPopup
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
    _DisablePluginsDialog,
    _ReplaceModDialog,
    ask_yes_no,
    show_error,
)
from gui.install_mod import install_mod_from_archive, _show_mod_notification
from gui.add_game_dialog import AddGameDialog, sync_modlist_with_mods_folder
from gui.modlist_filters_dialog import ModlistFiltersDialog
from gui.backup_restore_dialog import BackupRestoreDialog

from Utils.filemap import (
    build_filemap,
    read_mod_index,
    rebuild_mod_index,
    remove_from_mod_index,
    rename_in_mod_index,
    fix_flat_staging_folders,
    CONFLICT_NONE,
    CONFLICT_WINS,
    CONFLICT_LOSES,
    CONFLICT_PARTIAL,
    CONFLICT_FULL,
    OVERWRITE_NAME,
    ROOT_FOLDER_NAME,
)
from Utils.deploy import deploy_root_folder, restore_root_folder, LinkMode, load_per_mod_strip_prefixes, undeploy_mod_files, restore_custom_deploy_backup_for_path
from Utils.modlist import (
    ModEntry,
    read_modlist,
    write_modlist,
    prepend_mod,
    ensure_mod_preserving_position,
)
from Utils.plugin_parser import check_missing_masters
from Utils.plugins import (
    read_plugins, write_plugins, PluginEntry,
    read_loadorder, write_loadorder,
)
from Utils.profile_backup import create_backup
from Utils.profile_state import (
    read_profile_state,
    read_collapsed_seps,
    read_separator_locks,
    read_separator_colors,
    read_separator_deploy_paths,
    read_root_folder_state,
    read_mod_strip_prefixes,
    read_disabled_plugins,
    read_excluded_mod_files,
    read_ignored_missing_requirements,
    write_collapsed_seps,
    write_separator_locks,
    write_separator_colors,
    write_separator_deploy_paths,
    write_root_folder_state,
    write_mod_strip_prefixes,
    write_disabled_plugins,
    write_excluded_mod_files,
    write_ignored_missing_requirements,
)
from Nexus.nexus_api import NexusAPI, NexusAPIError, NexusModRequirement
from gui.collections_dialog import CollectionsDialog
from gui.workshop_dialog import WorkshopDialog
from gui.nexus_browser_overlay import NexusBrowserOverlay
from gui.changelog_overlay import ChangelogOverlay
from gui.mod_files_overlay import ModFilesOverlay
from Nexus.nexus_meta import build_meta_from_download, ensure_installed_stamp, read_meta, write_meta
from Nexus.nexus_download import delete_archive_and_sidecar
from Utils.config_paths import get_download_cache_dir
from Utils.ui_config import load_column_widths, save_column_widths, load_column_order, save_column_order, load_normalize_folder_case, load_sort_state, save_sort_state, load_column_hidden, save_column_hidden
from Nexus.nexus_update_checker import check_for_updates


from gui.text_utils import truncate_text as _truncate_text_for_width, clear_truncate_cache as _clear_truncate_cache


def _scan_meta_flags_impl(entries: list, mods_dir: Path) -> dict:
    """Pure scan over meta.ini; returns dict of results. Safe to run in thread."""
    update_mods: set[str] = set()
    missing_reqs: set[str] = set()
    missing_reqs_detail: dict[str, list[str]] = {}
    endorsed_mods: set[str] = set()
    install_dates: dict[str, str] = {}
    install_datetimes: dict[str, datetime] = {}
    category_names: dict[str, str] = {}
    mod_versions: dict[str, str] = {}
    fomod_mods: set[str] = set()
    today = datetime.now().date()
    for entry in entries:
        if entry.is_separator:
            continue
        meta_path = mods_dir / entry.name / "meta.ini"
        if not meta_path.is_file():
            continue
        try:
            meta = read_meta(meta_path)
            if not meta.installed and ensure_installed_stamp(meta_path):
                meta = read_meta(meta_path)
            if meta.has_update and not meta.ignore_update:
                update_mods.add(entry.name)
            if meta.missing_requirements:
                missing_reqs.add(entry.name)
                names = []
                for pair in meta.missing_requirements.split(";"):
                    parts = pair.split(":", 1)
                    if len(parts) == 2:
                        names.append(parts[1])
                    elif parts[0]:
                        names.append(parts[0])
                missing_reqs_detail[entry.name] = names
            if meta.endorsed:
                endorsed_mods.add(entry.name)
            if meta.installed:
                dt = datetime.fromisoformat(meta.installed)
                if dt.date() == today:
                    install_dates[entry.name] = dt.strftime("%-I:%M %p")
                else:
                    install_dates[entry.name] = dt.strftime("%-m/%-d/%y")
                install_datetimes[entry.name] = dt
            if meta.category_name:
                category_names[entry.name] = meta.category_name
            if meta.version:
                mod_versions[entry.name] = meta.version
            if meta.is_fomod:
                fomod_mods.add(entry.name)
        except Exception:
            pass
    return {
        "update_mods": update_mods,
        "missing_reqs": missing_reqs,
        "missing_reqs_detail": missing_reqs_detail,
        "endorsed_mods": endorsed_mods,
        "install_dates": install_dates,
        "install_datetimes": install_datetimes,
        "category_names": category_names,
        "mod_versions": mod_versions,
        "fomod_mods": fomod_mods,
    }


# ---------------------------------------------------------------------------
# ModListPanel
# ---------------------------------------------------------------------------
class ModListPanel(ctk.CTkFrame):
    """
    Left panel: column header, canvas-based mod list, toolbar.

    Rows are drawn as canvas items rather than individual CTk widgets.
    One tk.Checkbutton per visible row is embedded via canvas.create_window
    (not place), so it composites properly with the canvas and avoids opaque
    Checkbutton backgrounds.  All other columns are drawn as canvas text items.
    This gives smooth scrolling and instant load for large mod lists.
    """

    ROW_H   = scaled(26)
    HEADERS = ["", "Mod Name", "Flags", "Conflicts", "Installed", "Priority"]
    # x-start of each logical column (checkbox, name, flags, conflicts, installed, priority)
    # Computed dynamically in _layout_columns(); defaults here.
    _COL_X  = [4, 32, 0, 0, 0, 0, 0, 0]   # patched in _layout_columns

    def __init__(self, parent, log_fn=None, call_threadsafe_fn=None):
        super().__init__(parent, fg_color=BG_PANEL, corner_radius=0)
        self._log = log_fn or (lambda msg: None)
        self._call_threadsafe = call_threadsafe_fn

        self._game = None
        self.__profile_state: dict = {}   # cached profile_state.json for current profile load
        self._entries:  list[ModEntry] = []
        self._sel_idx:  int = -1          # anchor of the current selection
        self._sel_set:  set[int] = set()  # all selected entry indices
        self._hover_idx: int = -1         # entry index under the mouse cursor
        self._highlighted_mod: str | None = None  # mod highlighted by plugin panel selection
        self._modlist_path: Path | None = None
        self._staging_root: Path | None = None
        self._filemap_path: Path | None = None
        self._strip_prefixes:    set[str] = set()
        self._mod_strip_prefixes: dict[str, list[str]] = {}  # mod name -> top-level folders to ignore
        self._install_extensions: set[str] = set()
        self._root_deploy_folders: set[str] = set()
        self._staging_requires_subdir: bool = False
        self._normalize_folder_case: bool = True
        self._filemap_exclude_dirs: frozenset[str] = frozenset({"fomod"})
        self._root_folder_enabled: bool = True
        self._conflict_map:  dict[str, int]      = {}  # mod_name → CONFLICT_* constant

        # Conflict icons (canvas-compatible PhotoImage)
        self._icon_plus: ImageTk.PhotoImage | None = None
        self._icon_minus: ImageTk.PhotoImage | None = None
        self._icon_cross: ImageTk.PhotoImage | None = None
        _plus_path = _ICONS_DIR / "plus.png"
        _minus_path = _ICONS_DIR / "minus.png"
        _cross_path = _ICONS_DIR / "cross.png"
        _icon_sz = scaled(14)
        if _plus_path.is_file():
            self._icon_plus = ImageTk.PhotoImage(
                PilImage.open(_plus_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))
        if _minus_path.is_file():
            self._icon_minus = ImageTk.PhotoImage(
                PilImage.open(_minus_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))
        if _cross_path.is_file():
            self._icon_cross = ImageTk.PhotoImage(
                PilImage.open(_cross_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Update-available icon
        self._icon_update: ImageTk.PhotoImage | None = None
        _update_path = _ICONS_DIR / "update.png"
        if _update_path.is_file():
            self._icon_update = ImageTk.PhotoImage(
                PilImage.open(_update_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Missing-requirements warning icon
        self._icon_warning: ImageTk.PhotoImage | None = None
        _warning_path = _ICONS_DIR / "warning.png"
        if _warning_path.is_file():
            self._icon_warning = ImageTk.PhotoImage(
                PilImage.open(_warning_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Pre-RTX mod info icon
        self._icon_info: ImageTk.PhotoImage | None = None
        _info_path = _ICONS_DIR / "info.png"
        if _info_path.is_file():
            self._icon_info = ImageTk.PhotoImage(
                PilImage.open(_info_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Endorsed mod tick icon
        self._icon_endorsed: ImageTk.PhotoImage | None = None
        _tick_path = _ICONS_DIR / "tick.png"
        if _tick_path.is_file():
            self._icon_endorsed = ImageTk.PhotoImage(
                PilImage.open(_tick_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Disabled files icon
        self._icon_disabled_files: ImageTk.PhotoImage | None = None
        _eye2_path = _ICONS_DIR / "eye2_white.png"
        if _eye2_path.is_file():
            self._icon_disabled_files = ImageTk.PhotoImage(
                PilImage.open(_eye2_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Separator collapse/expand arrows (right = collapsed, arrow = expanded)
        self._icon_sep_right: ImageTk.PhotoImage | None = None
        self._icon_sep_arrow: ImageTk.PhotoImage | None = None
        _right_path = _ICONS_DIR / "right.png"
        _arrow_path = _ICONS_DIR / "arrow.png"
        if _right_path.is_file():
            self._icon_sep_right = ImageTk.PhotoImage(
                PilImage.open(_right_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))
        if _arrow_path.is_file():
            self._icon_sep_arrow = ImageTk.PhotoImage(
                PilImage.open(_arrow_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Separator lock icon
        self._icon_lock: ImageTk.PhotoImage | None = None
        _lock_path = _ICONS_DIR / "lock.png"
        if _lock_path.is_file():
            self._icon_lock = ImageTk.PhotoImage(
                PilImage.open(_lock_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Set of mod names that have a Nexus update available
        self._update_mods: set[str] = set()

        # Set of mod names that have missing Nexus requirements
        self._missing_reqs: set[str] = set()
        # Map mod name → list of missing requirement names (for tooltips / context menu)
        self._missing_reqs_detail: dict[str, list[str]] = {}
        # Mod names for which the user chose "Ignore requirements" (flag hidden, per profile)
        self._ignored_missing_reqs: set[str] = set()

        # Tooltip state (missing requirements hover)
        self._tooltip_win: tk.Toplevel | None = None
        self._tooltip_text: str = ""

        # Set of mod names the user has endorsed on Nexus
        self._endorsed_mods: set[str] = set()

        # Set of mod names that contain pre-RTX (natives/x64) files
        self._prertx_mods: set[str] = set()

        # Map mod name → install date display string
        self._install_dates: dict[str, str] = {}
        self._category_names: dict[str, str] = {}
        self._mod_versions: dict[str, str] = {}
        self._fomod_mods: set[str] = set()
        # Map mod name → install datetime for sorting (parallel to _install_dates)
        self._install_datetimes: dict[str, datetime] = {}

        self._overrides:     dict[str, set[str]] = {}  # mod beats these mods
        self._overridden_by: dict[str, set[str]] = {}  # these mods beat this mod
        self._on_filemap_rebuilt: callable | None = None  # called after each filemap rebuild
        self._on_mod_selected_cb: callable | None = None  # called when a mod is selected
        self._filemap_pending: bool = False   # True while a background rebuild is running
        self._filemap_dirty:   bool = False   # True if another rebuild was requested while one was running
        self._filemap_after_id: str | None = None  # after() handle for debounce timer
        self._filemap_rescan_index: bool = False  # True if next rebuild should regenerate modindex.bin first
        self._redraw_after_id: str | None = None  # after_idle handle for scroll-debounce
        self._canvas_resize_after_id: str | None = None  # after() handle for resize-debounce
        self._marker_strip_after_id: str | None = None   # after() handle for marker-strip debounce

        # Drag state
        self._drag_idx:      int = -1      # entry index being dragged (updated in real-time)
        self._drag_origin_idx: int = -1    # original index when drag began
        self._drag_start_y:  int = 0
        self._drag_moved:    bool = False
        self._drag_is_block: bool = False   # True when dragging a separator+its mods
        self._drag_block:    list  = []     # snapshot of (entry, var) at mousedown
        self._drag_sel_indices: list[int] = []  # actual entry indices for sparse multi-select drag
        self._drag_slot:     int  = -1     # last computed insertion slot (in vis-without-drag space)
        self._drag_start_slot: int = 0     # slot when drag began (for delta-based movement)

        self._drag_entries_snapshot: list | None = None  # snapshot for column-sort drag
        self._drag_vars_snapshot: list | None = None
        self._drag_reordered_snapshot: list | None = None  # reordered state for inverted drag
        self._drag_reordered_vars: list | None = None
        self._drag_saved_sort_column: str | None = None
        self._drag_saved_sort_ascending: bool | None = None

        self._drag_pending:  bool = False  # waiting for click-vs-drag disambiguation
        self._drag_after_id: str | None = None  # after() id for drag-start timer
        self._drag_scroll_after: str | None = None  # after() id for auto-scroll repeat
        self._drag_last_event_y: int = 0  # last widget-space Y from mouse drag

        # Separator lock state: sep_name → bool (True = locked, block drag disabled)
        self._sep_locks: dict[str, bool] = {}

        # Separator custom background colors: sep_name → hex color string
        self._sep_colors: dict[str, str] = {}

        # Separator custom deployment directories: sep_name → path string (empty = default)
        self._sep_deploy_paths: dict[str, dict] = {}  # {sep_name: {"path": str, "raw": bool}}

        # Separator settings overlay widget (in-panel frame placed over the list)
        self._sep_settings_overlay: tk.Frame | None = None

        # Collapsed separators: set of sep names whose mods are hidden
        self._collapsed_seps: set[str] = set()

        # Bundle groups: bundle_name → list of entry indices (computed on reload)
        self._bundle_groups: dict[str, list[int]] = {}

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
        self._filter_has_disabled_plugins: bool = False
        self._filter_has_plugins: bool = False
        self._filter_has_disabled_files: bool = False
        self._filter_has_updates: bool = False
        self._filter_fomod_only: bool = False
        self._filter_categories: frozenset[str] = frozenset()  # when non-empty, show only these categories
        self._disabled_plugins_map: dict[str, list[str]] = {}  # mod_name → [plugin, ...]
        self._excluded_mod_files_map: dict[str, list[str]] = {}  # mod_name → [rel_key, ...]
        self._visible_indices: list[int] = []  # entry indices matching current filter
        self._vis_dirty: bool = True           # True when _visible_indices needs recomputing
        self._priorities: dict[int, int] = {}  # entry index → priority number (cached)
        self._sep_block_cache: dict[int, range] = {}  # sep_idx → range (cached)

        # Column sorting (visual only — never touches modlist.txt)
        # _sort_column: None or one of "name", "installed", "flags", "conflicts", "priority"
        self._sort_column, self._sort_ascending = load_sort_state()

        # Column resize overrides: col index (1–6) → width in px
        # When set, _layout_columns uses this instead of auto-calculated width.
        self._col_w_override: dict[int, int] = load_column_widths()
        self._col_drag_col: int | None = None   # which col boundary is being dragged
        self._col_drag_start_x: int = 0
        self._col_drag_start_w: int = 0

        # Column reorder: list of data col indices (2-5) in left→right display order
        self._col_order: list[int] = load_column_order()
        # Hidden columns: set of data col indices (2..7). Col 1 (name) is never hideable.
        self._col_hidden: set[int] = load_column_hidden()
        # _col_pos: data col index → _COL_X/W slot index (computed in _layout_columns)
        self._col_pos: dict[int, int] = {2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
        # Header label drag-to-reorder state
        self._hdr_drag_col: int | None = None   # data col index being dragged
        self._hdr_drag_start_x: int = 0
        self._hdr_drag_moved: bool = False
        self._hdr_drag_ghost: tk.Label | None = None  # floating ghost label

        # Per-entry logical state (parallel to _entries, aligned by index)
        self._check_vars:    list[tk.BooleanVar | None] = []

        # Lock canvas items for separator rows: sep_name → canvas item id
        # Pure canvas rect + checkmark — scroll in sync automatically.
        self._lock_cb_rects: dict[str, int] = {}
        self._lock_cb_marks: dict[str, int] = {}

        # Virtual-list pool — pre-allocated canvas items + widgets for visible rows.
        # Only _pool_size slots are ever created; they are reconfigured on every draw.
        self._pool_size: int = 60
        self._pool_data_idx: list[int] = []          # slot → entry index (-1 = unused)
        self._pool_bg: list[int] = []                # rectangle canvas item ids
        self._pool_name: list[int] = []              # text canvas item ids (mod name / sep label)
        self._pool_flag_icon: list[int] = []         # image canvas item ids (flags column, slot 1)
        self._pool_flag_icon2: list[int] = []        # image canvas item ids (flags column, slot 2)
        self._pool_flag_icon3: list[int] = []        # image canvas item ids (flags column, slot 3)
        self._pool_flag_star: list[int] = []         # text canvas item ids (lock star in flags column)
        self._pool_conflict_icon1: list[int] = []    # image canvas item ids (conflict col left)
        self._pool_conflict_icon2: list[int] = []    # image canvas item ids (conflict col right)
        self._pool_category_text: list[int] = []     # text canvas item ids (category)
        self._pool_install_text: list[int] = []      # text canvas item ids (install date)
        self._pool_priority_text: list[int] = []     # text canvas item ids (priority)
        self._pool_version_text: list[int] = []      # text canvas item ids (version)
        self._pool_sep_icon: list[int] = []          # image canvas item ids (collapse arrow)
        self._pool_sep_line_l: list[int] = []        # line canvas item ids (separator left line)
        self._pool_sep_line_r: list[int] = []        # line canvas item ids (separator right line)
        self._pool_sep_badge: list[int] = []         # text canvas item ids (custom deploy badge)
        self._pool_check_vars: list[tk.BooleanVar] = []
        self._pool_cb_rect: list[int] = []   # canvas rectangle ids for checkbox outline
        self._pool_cb_mark: list[int] = []   # canvas text ids for checkmark
        # Pool canvas_w cached for pool creation after canvas exists
        self._canvas_w: int = 600
        self._context_menu: CTkPopupMenu | None = None

        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=0, minsize=0)  # filter side panel
        self.grid_columnconfigure(1, weight=1)              # main content

        self._build_new_profile_bar()
        self._build_header()
        self._build_canvas()
        self._build_toolbar()
        self._build_search_bar()
        self._build_download_bar()
        self._build_filter_side_panel()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_game(self, game, profile: str = "default") -> None:
        # Hide any open new-profile bar when switching game/profile
        if hasattr(self, "_new_profile_bar"):
            self.hide_new_profile_bar()
        _collections_was_open = getattr(self, "_collections_panel", None) is not None
        if game is None:
            self._game = None
            self._modlist_path = None
            self._ignored_missing_reqs = set()
            self._reload()
            self._close_collections()
            if hasattr(self, "_restore_backup_btn"):
                self._restore_backup_btn.configure(state="disabled")
            return
        self._game = game
        profile_dir = game.get_profile_root() / "profiles" / profile
        self._modlist_path = profile_dir / "modlist.txt"
        self._staging_root = game.get_effective_mod_staging_path()
        self._filemap_path = self._staging_root.parent / "filemap.txt"
        self._strip_prefixes    = game.mod_folder_strip_prefixes | getattr(game, "mod_folder_strip_prefixes_post", set())
        self._install_extensions = getattr(game, "mod_install_extensions", set())
        self._root_deploy_folders = getattr(game, "mod_root_deploy_folders", set())
        self._staging_requires_subdir = getattr(game, "mod_staging_requires_subdir", False)
        self._normalize_folder_case = getattr(game, "normalize_folder_case", True) and load_normalize_folder_case()
        self._conflict_ignore_filenames = getattr(game, "conflict_ignore_filenames", set())
        self._filemap_exclude_dirs = getattr(game, "filemap_exclude_dirs", frozenset({"fomod"}))
        # Load profile_state.json once; individual loaders pull from it
        self.__profile_state = read_profile_state(profile_dir)
        self._ignored_missing_reqs = read_ignored_missing_requirements(profile_dir, self.__profile_state)
        self._reload()
        if hasattr(self, "_restore_backup_btn"):
            self._restore_backup_btn.configure(state="normal")
        # If the collections panel was open, re-open it for the new game
        if _collections_was_open:
            self._on_collections()

    def reload_after_install(self):
        self._reload()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build_new_profile_bar(self):
        """Inline bar (row 0) shown when the user clicks '+' to create a profile."""
        bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=scaled(40))
        bar.grid(row=0, column=1, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_remove()  # hidden by default
        self._new_profile_bar = bar

        ctk.CTkLabel(
            bar, text="New profile:", font=_theme.FONT_NORMAL,
            text_color=TEXT_MAIN,
        ).pack(side="left", padx=(8, 4), pady=6)

        self._new_profile_var = tk.StringVar()
        self._new_profile_entry = ctk.CTkEntry(
            bar, textvariable=self._new_profile_var, font=_theme.FONT_NORMAL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER,
            width=180, height=26,
        )
        self._new_profile_entry.pack(side="left", padx=(0, 8), pady=6)
        self._new_profile_entry.bind("<Return>", lambda _e: self._on_new_profile_create())
        self._new_profile_entry.bind("<Escape>", lambda _e: self.hide_new_profile_bar())

        self._new_profile_specific_mods_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            bar,
            text="Use Profile Specific Mods",
            variable=self._new_profile_specific_mods_var,
            font=_theme.FONT_NORMAL,
            text_color=TEXT_MAIN,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            border_color=BORDER,
            checkmark_color="white",
            width=22, height=22,
        ).pack(side="left", padx=(0, 12), pady=6)

        ctk.CTkButton(
            bar, text="Create", width=72, height=26, font=_theme.FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_new_profile_create,
        ).pack(side="left", padx=(0, 4), pady=6)

        ctk.CTkButton(
            bar, text="Cancel", width=72, height=26, font=_theme.FONT_NORMAL,
            fg_color=BG_HOVER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self.hide_new_profile_bar,
        ).pack(side="left", padx=(0, 8), pady=6)

    # ------------------------------------------------------------------
    # New-profile bar public API
    # ------------------------------------------------------------------

    def show_new_profile_bar(self, on_create_fn):
        """Reveal the inline new-profile bar and focus the name entry.

        *on_create_fn(name: str, profile_specific_mods: bool)* is called when
        the user confirms; it can do validation and profile creation.
        """
        self._new_profile_create_fn = on_create_fn
        self._new_profile_var.set("")
        self._new_profile_specific_mods_var.set(False)
        self._new_profile_bar.grid()
        self._new_profile_entry.focus_set()

    def hide_new_profile_bar(self):
        """Hide the inline new-profile bar."""
        self._new_profile_bar.grid_remove()
        self._new_profile_create_fn = None

    def _on_new_profile_create(self):
        name = self._new_profile_var.get().strip()
        if not name:
            return
        fn = getattr(self, "_new_profile_create_fn", None)
        self.hide_new_profile_bar()
        if fn:
            fn(name, self._new_profile_specific_mods_var.get())

    def _build_header(self):
        # Use design size 28: CTk applies its own widget scaling, so scaled() would double-scale
        self._header = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=28)
        self._header.grid(row=1, column=1, sticky="ew")
        self._header.grid_propagate(False)
        # Header labels placed after canvas is built (we need its width)
        self._header_labels: list[ctk.CTkLabel] = []
        # Grey divider lines between columns — drag events bound directly to these
        self._header_dividers: list[tk.Frame] = []
        # Column visibility menu button (created lazily in _update_header)
        self._col_menu_btn: tk.Label | None = None
        self._col_menu_popup: CTkPopupMenu | None = None

    def _build_canvas(self):
        frame = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0)
        frame.grid(row=2, column=1, sticky="nsew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(frame, bg=BG_DEEP, bd=0, highlightthickness=0,
                                 yscrollincrement=1, takefocus=0)
        self._marker_strip = tk.Canvas(frame, bg=BG_DEEP, bd=0, highlightthickness=0,
                                       width=4, takefocus=0)
        self._vsb = tk.Scrollbar(frame, orient="vertical",
                                 command=self._canvas.yview,
                                 bg=BG_SEP, troughcolor=BG_DEEP,
                                 activebackground=ACCENT,
                                 highlightthickness=0, bd=0)
        self._canvas.configure(yscrollcommand=self._vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._marker_strip.grid(row=0, column=1, sticky="ns")
        self._vsb.grid(row=0, column=2, sticky="ns")
        self._marker_strip.bind("<Configure>", self._on_marker_strip_resize)

        self._canvas_w = 600   # updated on first <Configure>
        self._layout_columns(600)  # init before first _redraw (avoids IndexError on _COL_X[6])
        self._canvas.bind("<Configure>",      self._on_canvas_resize)
        self._canvas.bind("<Button-4>",       self._on_scroll_up)
        self._canvas.bind("<Button-5>",       self._on_scroll_down)
        self._vsb.bind("<B1-Motion>",         lambda e: self._schedule_redraw())
        self._canvas.bind("<MouseWheel>",     self._on_mousewheel)
        self._canvas.bind("<ButtonPress-1>",         self._on_mouse_press)
        self._canvas.bind("<Control-ButtonPress-1>", self._on_mouse_press)
        self._canvas.bind("<B1-Motion>",             self._on_mouse_drag)
        self._canvas.bind("<ButtonRelease-1>",       self._on_mouse_release)
        self._canvas.bind("<ButtonRelease-3>", self._on_right_click)
        self._canvas.bind("<ButtonRelease-2>", self._on_middle_click)
        self._canvas.bind("<Motion>",         self._on_mouse_motion)
        self._canvas.bind("<Leave>",          self._on_mouse_leave)

        self._create_pool()

    def _build_toolbar(self):
        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=scaled(36))
        bar.grid(row=3, column=1, sticky="ew")
        bar.grid_propagate(False)
        self._toolbar_bar = bar

        # Expand/Collapse all separators toggle
        self._expand_collapse_all_btn = ctk.CTkButton(
            bar, text="Expand all", width=90, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._toggle_all_separators
        )
        self._expand_collapse_all_btn.pack(side="left", padx=4, pady=5)
        self._update_expand_collapse_all_btn()

        # Enable/Disable all mods toggle
        self._enable_disable_all_btn = ctk.CTkButton(
            bar, text="Enable all", width=90, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._toggle_all_mods_enabled
        )
        self._enable_disable_all_btn.pack(side="left", padx=4, pady=5)
        self._update_enable_disable_all_btn()

        # Check for Nexus mod updates button
        self._update_btn = ctk.CTkButton(
            bar, text="Check Updates", width=110, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._on_check_updates
        )
        self._update_btn.pack(side="left", padx=4, pady=5)

        self._filter_btn = ctk.CTkButton(
            bar, text="Filters", width=64, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._on_open_filters
        )
        self._filter_btn.pack(side="left", padx=4, pady=5)

        self._restore_backup_btn = ctk.CTkButton(
            bar, text="Restore backup", width=110, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._on_restore_backup,
            state="disabled",
        )
        self._restore_backup_btn.pack(side="left", padx=4, pady=5)

        # Refresh button (icon only)
        refresh_icon = _load_icon("refresh.png", size=(16, 16))
        ctk.CTkButton(
            bar, text="Refresh Modlist" if refresh_icon else "↺", image=refresh_icon,
            width=30, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._reload
        ).pack(side="left", padx=4, pady=5)

        ctk.CTkButton(
            bar, text="Generate Separators", width=140, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._generate_separators
        ).pack(side="left", padx=4, pady=5)

        self._status_bar = None  # set via set_status_bar() after construction

    def _build_search_bar(self):
        bar = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0, height=scaled(32))
        bar.grid(row=4, column=1, sticky="ew")
        bar.grid_propagate(False)

        tk.Label(bar, text="🔍", bg=BG_DEEP, fg=TEXT_DIM,
                 font=(_theme.FONT_FAMILY, _theme.FS11)).pack(side="left", padx=(8, 2), pady=4)

        self._search_entry = tk.Entry(
            bar,
            bg=BG_PANEL, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=(_theme.FONT_FAMILY, _theme.FS11),
            bd=0, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self._search_entry.pack(side="left", fill="x", expand=True, padx=(2, 2), pady=4)

        self._search_clear_btn = ctk.CTkButton(
            bar, text="✕", width=scaled(32), height=scaled(24),
            fg_color="#b33a3a", hover_color="#c94848", text_color="white",
            font=_theme.FONT_HEADER, cursor="hand2",
            command=self._on_search_clear,
        )
        self._search_clear_btn.pack(side="left", padx=(0, 8), pady=4)
        self._search_clear_btn.pack_forget()  # hidden until there is text

        # KeyRelease fires after the character is committed to the widget
        self._search_entry.bind("<KeyRelease>", self._on_search_change)
        self._search_entry.bind("<Escape>", self._on_search_clear)
        self._search_entry.bind("<Control-a>", lambda e: (
            self._search_entry.select_range(0, "end"),
            self._search_entry.icursor("end"),
            "break"
        )[-1])

    # ------------------------------------------------------------------
    # Download progress popups (one per concurrent download, stacked)
    # ------------------------------------------------------------------
    # Each active download is tracked as a _DlSlot: (popup, cancel_event, bind_id).
    # Popups stack upward from the bottom-right corner.

    class _DlSlot:
        __slots__ = ("popup", "cancel", "bind_id")
        def __init__(self, popup: "CTkProgressPopup", cancel: threading.Event, bind_id: str | None):
            self.popup = popup
            self.cancel = cancel
            self.bind_id = bind_id

    def _build_download_bar(self):
        """Initialise the download-popup slot list."""
        self._dl_slots: list["ModListPanel._DlSlot"] = []
        self._dl_cancel_locked: bool = False

    def _build_filter_side_panel(self):
        """Build the inline filter side panel (column 0, initially hidden)."""
        self._filter_panel_open = False

        # 300 was too narrow at 1.25x–1.5x scale (labels got truncated)
        # CTk scales frame width; use unscaled design value
        panel = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0,
                             width=380)
        panel.grid(row=0, column=0, rowspan=5, sticky="nsew")
        panel.grid_propagate(False)
        panel.grid_remove()  # hidden by default
        self._filter_side_panel = panel

        # ── Header row ──────────────────────────────────────────────
        header = tk.Frame(panel, bg=BG_HEADER, height=scaled(36))
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        tk.Label(
            header, text="Filters", bg=BG_HEADER, fg=TEXT_MAIN,
            font=_theme.FONT_BOLD, anchor="w",
        ).pack(side="left", padx=10, pady=6)

        # Close (×) button
        close_btn = tk.Label(
            header, text="×", bg=BG_HEADER, fg=TEXT_DIM,
            font=(_theme.FONT_FAMILY, 16, "bold"), cursor="hand2",
        )
        close_btn.pack(side="right", padx=8)
        close_btn.bind("<Button-1>", lambda _e: self._close_filter_side_panel())
        close_btn.bind("<Enter>",    lambda _e: close_btn.configure(fg=TEXT_MAIN))
        close_btn.bind("<Leave>",    lambda _e: close_btn.configure(fg=TEXT_DIM))

        # Clear all button
        clear_btn = tk.Label(
            header, text="Clear all", bg=BG_HEADER, fg=TEXT_DIM,
            font=_theme.FONT_SMALL, cursor="hand2",
        )
        clear_btn.pack(side="right", padx=(0, 4))
        clear_btn.bind("<Button-1>", lambda _e: self._clear_all_filters())
        clear_btn.bind("<Enter>",    lambda _e: clear_btn.configure(fg=TEXT_MAIN))
        clear_btn.bind("<Leave>",    lambda _e: clear_btn.configure(fg=TEXT_DIM))

        # Separator
        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x")

        # ── Scrollable checkbox area ─────────────────────────────────
        scroll_frame = ctk.CTkScrollableFrame(
            panel, fg_color="transparent", corner_radius=0,
        )
        scroll_frame.pack(fill="both", expand=True, padx=8, pady=6)

        opts = [
            ("filter_show_disabled",       "Show only disabled mods"),
            ("filter_show_enabled",        "Show only enabled mods"),
            ("filter_hide_separators",     "Hide separators"),
            ("filter_winning",             "Show only winning conflicts"),
            ("filter_losing",              "Show only losing conflicts"),
            ("filter_partial",             "Show only winning & losing conflicts"),
            ("filter_full",                "Show only fully conflicted mods"),
            ("filter_missing_reqs",        "Show only missing requirements"),
            ("filter_has_disabled_plugins","Show only mods with disabled plugins"),
            ("filter_has_plugins",         "Show only mods with plugins"),
            ("filter_has_disabled_files",  "Show mods with disabled files"),
            ("filter_has_updates",         "Show only mods with updates"),
            ("filter_fomod_only",          "Show only FOMOD mods"),
        ]

        self._fsp_vars: dict[str, tk.BooleanVar] = {}
        for key, label in opts:
            var = tk.BooleanVar(value=False)
            self._fsp_vars[key] = var
            ctk.CTkCheckBox(
                scroll_frame,
                text=label,
                variable=var,
                font=_theme.FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                border_color=BORDER,
                checkmark_color="white",
                command=self._on_filter_panel_change,
            ).pack(anchor="w", fill="x", pady=3)

        # Category filter section
        ctk.CTkLabel(
            scroll_frame, text="", height=8, fg_color="transparent",
        ).pack(anchor="w")
        ctk.CTkLabel(
            scroll_frame, text="Show only categories:",
            font=_theme.FONT_SMALL, text_color=TEXT_DIM, anchor="w",
        ).pack(anchor="w")
        self._fsp_category_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        self._fsp_category_frame.pack(anchor="w", pady=(2, 0))
        self._fsp_category_vars: dict[str, tk.BooleanVar] = {}

        self._filter_scroll_frame = scroll_frame
        self._bind_filter_panel_scroll()

    def _bind_filter_panel_scroll(self) -> None:
        """Bind mouse wheel to the filter panel's scroll frame (Linux Button-4/5, Windows MouseWheel)."""
        scroll_frame = getattr(self, "_filter_scroll_frame", None)
        if not scroll_frame or not hasattr(scroll_frame, "_parent_canvas"):
            return

        def _on_wheel(evt):
            num = getattr(evt, "num", None)
            delta = getattr(evt, "delta", 0) or 0
            if num == 4 or delta > 0:
                scroll_frame._parent_canvas.yview_scroll(-3, "units")
            elif num == 5 or delta < 0:
                scroll_frame._parent_canvas.yview_scroll(3, "units")

        def _bind_recursive(w):
            w.bind("<MouseWheel>", _on_wheel)
            w.bind("<Button-4>", _on_wheel)
            w.bind("<Button-5>", _on_wheel)
            for child in w.winfo_children():
                _bind_recursive(child)

        _bind_recursive(scroll_frame)

    def _refresh_filter_category_list(self) -> None:
        """Populate category checkboxes from current _category_names. Call when opening filter panel."""
        for w in self._fsp_category_frame.winfo_children():
            w.destroy()
        self._fsp_category_vars.clear()
        categories = sorted(
            set(self._category_names.values()) | {""},
            key=lambda c: ("(Uncategorized)" if c == "" else c).lower(),
        )
        for cat in categories:
            label = "(Uncategorized)" if cat == "" else cat
            var = tk.BooleanVar(value=cat in self._filter_categories)
            self._fsp_category_vars[cat] = var
            ctk.CTkCheckBox(
                self._fsp_category_frame,
                text=label,
                variable=var,
                font=_theme.FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                border_color=BORDER,
                checkmark_color="white",
                command=self._on_filter_panel_change,
            ).pack(anchor="w", pady=2)

        self._bind_filter_panel_scroll()

    def _clear_all_filters(self):
        """Reset all filter checkboxes to unchecked."""
        for v in self._fsp_vars.values():
            v.set(False)
        for v in self._fsp_category_vars.values():
            v.set(False)
        self._apply_modlist_filters({"filter_categories": frozenset()})

    def _on_filter_panel_change(self):
        """Called when any checkbox in the inline filter panel changes."""
        state = {k: v.get() for k, v in self._fsp_vars.items()}
        state["filter_categories"] = frozenset(
            c for c, v in self._fsp_category_vars.items() if v.get()
        )
        self._apply_modlist_filters(state)

    def _reposition_all_dl_popups(self, *_) -> None:
        """Stack all live download popups (CTkToplevel) upward from the bottom-right."""
        root = self.winfo_toplevel()
        try:
            if root.state() != "normal":
                return
        except Exception:
            pass
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        rw, rh = root.winfo_width(), root.winfo_height()
        gap = scaled(8)
        margin = scaled(20)
        y = ry + rh - margin
        for slot in self._dl_slots:
            p = slot.popup
            if not p.winfo_exists():
                continue
            pw, ph = p.winfo_width(), p.winfo_height()
            y -= ph
            x = rx + rw - pw - margin
            p.geometry(f"+{x}+{y}")
            y -= gap

    def get_download_cancel_event(self) -> threading.Event:
        """Create a new download slot with a popup and cancel event.
        Returns the cancel event; pass it to the downloader."""
        root = self.winfo_toplevel()
        cancel = threading.Event()
        popup = CTkProgressPopup(root, title="Downloading", label="Starting...", message="0%",
                                 on_show=self._reposition_all_dl_popups)
        # CTkProgressPopup binds its own update_position to <Configure>, which calls
        # update_idletasks() twice on every event — expensive during scroll. Silence it.
        popup.update_position = lambda *_: None
        popup._configure_bid = root.bind("<Configure>", self._reposition_all_dl_popups, add="+")
        slot = self._DlSlot(popup, cancel, None)
        self._dl_slots.append(slot)
        # Wire this popup's X button to cancel just this slot
        popup.cancel_btn.configure(command=lambda s=slot: self._cancel_dl_slot(s))
        self._reposition_all_dl_popups()
        self.after(100, self._reposition_all_dl_popups)
        return cancel

    def _cancel_dl_slot(self, slot: "_DlSlot") -> None:
        if self._dl_cancel_locked:
            return
        slot.cancel.set()
        self._close_dl_slot(slot, user_cancel=True)

    def _close_dl_slot(self, slot: "_DlSlot", user_cancel: bool = False) -> None:
        bid = getattr(slot.popup, "_configure_bid", None)
        if bid is not None:
            try:
                self.winfo_toplevel().unbind("<Configure>", bid)
            except Exception:
                pass
        if slot.popup.winfo_exists():
            slot.popup.destroy()
        try:
            self._dl_slots.remove(slot)
        except ValueError:
            pass

        if user_cancel and self._dl_slots:
            # Hide surviving popups and defer reposition so that the mouse
            # button is released before any popup appears under the cursor.
            for s in self._dl_slots:
                if s.popup.winfo_exists():
                    s.popup.withdraw()
            self._dl_cancel_locked = True
            self.after(300, self._deferred_reshow)
        else:
            self._reposition_all_dl_popups()

    def _deferred_reshow(self) -> None:
        self._dl_cancel_locked = False
        for s in self._dl_slots:
            if s.popup.winfo_exists():
                s.popup.deiconify()
        self._reposition_all_dl_popups()

    def _slot_for_cancel(self, cancel: threading.Event) -> "_DlSlot | None":
        for slot in self._dl_slots:
            if slot.cancel is cancel:
                return slot
        return None

    def show_download_progress(self, label: str = "Downloading...", cancel: threading.Event | None = None):
        """Update the label on the popup for the given cancel event (or the most recent one)."""
        if cancel is not None:
            slot = self._slot_for_cancel(cancel)
        else:
            slot = self._dl_slots[-1] if self._dl_slots else None
        if slot and slot.popup.winfo_exists():
            slot.popup.update_label(label)
            slot.popup.update_progress(0)
            slot.popup.update_message("0%")

    def update_download_progress(self, current: int, total: int, label: str = "",
                                  cancel: threading.Event | None = None):
        """Update progress on the popup for the given cancel event (or most recent)."""
        if cancel is not None:
            slot = self._slot_for_cancel(cancel)
        else:
            slot = self._dl_slots[-1] if self._dl_slots else None
        if slot is None or not slot.popup.winfo_exists():
            return
        if total > 0:
            frac = min(current / total, 1.0)
            pct = int(frac * 100)
            _GB = 1024 * 1024 * 1024
            if total >= _GB:
                cur_u = current / _GB
                tot_u = total / _GB
                unit = "GB"
            else:
                cur_u = current / (1024 * 1024)
                tot_u = total / (1024 * 1024)
                unit = "MB"
            slot.popup.update_progress(frac)
            slot.popup.update_message(
                label if label else f"{cur_u:.2f} / {tot_u:.2f} {unit}  ({pct}%)"
            )

    def hide_download_progress(self, cancel: threading.Event | None = None):
        """Close the popup for the given cancel event (or most recent).
        If a cancel event is given but its slot is already gone, do nothing."""
        if cancel is not None:
            slot = self._slot_for_cancel(cancel)
        else:
            slot = self._dl_slots[-1] if self._dl_slots else None
        if slot:
            self._close_dl_slot(slot)

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    def _layout_columns(self, canvas_w: int):
        """Compute column x positions given the current canvas width.

        All columns fill the available space. Dragging a boundary grows one
        column while shrinking its neighbour — total width stays constant.
        """
        scale = min(1.4, max(0.5, canvas_w / scaled(700)))
        gap = scaled(10)
        scroll_gap = scaled(14)
        # Extra right-side pad for the column-visibility menu button
        menu_btn_pad = scaled(22)
        x0, x1 = scaled(4), scaled(32)
        hidden = self._col_hidden
        # Number of visible cols among 2..7 (name col 1 is always visible)
        visible_data_cols = [dc for dc in self._col_order if dc not in hidden]
        n_visible = len(visible_data_cols)
        # gaps: between name and col2, between subsequent visible cols, and before scrollbar
        avail = canvas_w - x1 - gap * (n_visible + 1) - scroll_gap - menu_btn_pad

        # Default proportional widths keyed by data-col index (1=name, 2=cat, 3=flags, 4=conf, 5=inst, 6=prio, 7=version)
        data_defaults = {
            1: max(scaled(80), avail - scaled(int((130 + 56 + 95 + 100 + 72 + 90) * scale))),
            2: scaled(int(130 * scale)),
            3: scaled(int(56 * scale)),
            4: scaled(int(95 * scale)),
            5: scaled(int(100 * scale)),
            6: scaled(int(72 * scale)),
            7: scaled(int(90 * scale)),
        }
        data_mins = {1: scaled(120), 2: scaled(95), 3: scaled(70), 4: scaled(95), 5: scaled(95), 6: scaled(80), 7: scaled(80)}

        ov = self._col_w_override
        # Width per visible data col (name col 1 always included)
        visible_dcs = [1] + visible_data_cols
        data_widths = {dc: max(data_mins[dc], ov.get(dc, data_defaults[dc])) for dc in visible_dcs}

        # Scale all widths proportionally to fit available space exactly
        total = sum(data_widths[dc] for dc in visible_dcs)
        if total != avail and avail > 0:
            factor = avail / total
            data_widths = {dc: max(data_mins[dc], int(data_widths[dc] * factor)) for dc in visible_dcs}
            remainder = avail - sum(data_widths.values())
            data_widths[1] += remainder

        # Build slot-ordered widths: slot 0=checkbox, 1=name, then visible cols in order.
        # Hidden cols get slot indices too, but with width=0 and x off-screen, so draw
        # code that references them via _col_pos sees invisible geometry.
        slot_data = [0, 1] + visible_data_cols  # slot → data col
        # widths[0] = name (slot 1), widths[1..n_visible] = visible cols (slots 2..n_visible+1),
        # remaining entries are 0 for dead/hidden slots.
        widths = []
        for i in range(7):
            slot = i + 1  # _COL_W slot index
            if slot < len(slot_data):
                widths.append(data_widths.get(slot_data[slot], 0))
            else:
                widths.append(0)

        # Build _col_pos: data col → slot index in _COL_X/_COL_W
        self._col_pos = {1: 1}
        for k, dc in enumerate(visible_data_cols):
            self._col_pos[dc] = k + 2  # slots 2..(n_visible+1)

        # Build x positions for visible slots
        xs = [x0, x1]
        for i in range(n_visible):
            xs.append(xs[-1] + widths[i] + gap)
        # Pad remaining slots (for hidden cols) with a far off-screen sentinel
        off_x = -99999
        while len(xs) < 8:
            xs.append(off_x)

        # Hidden col slots: assign them slot indices past the visible ones, off-screen.
        for k, dc in enumerate(self._col_order):
            if dc in hidden:
                slot = n_visible + 2  # shared off-screen slot (width 0, x off_x)
                if slot >= 8:
                    slot = 7
                self._col_pos[dc] = slot

        self._COL_X = xs
        self._COL_W = [scaled(28)] + widths
        self._canvas_w = canvas_w
        self._name_col_right = x1 + widths[0] - scaled(4)
        # Right edge of the last visible column (for placing the menu button).
        self._last_col_right = xs[n_visible + 1] if n_visible > 0 else (x1 + widths[0])

    # Static sort key names by data-col index
    _DATA_COL_SORT_KEYS = {1: "name", 2: "category", 3: "flags", 4: "conflicts", 5: "installed", 6: "priority", 7: "version"}
    # Static titles by data-col index
    _DATA_COL_TITLES = {0: "", 1: "Mod Name", 2: "Category", 3: "Flags", 4: "Conflicts", 5: "Installed", 6: "Priority", 7: "Version"}

    def _update_header(self, canvas_w: int):
        try:
            self._header.configure(width=canvas_w)
        except RecursionError:
            return

        # Build slot-ordered metadata using only visible cols.
        visible = [dc for dc in self._col_order if dc not in self._col_hidden]
        slot_data_cols = [0, 1] + visible + [0] * (6 - len(visible))
        titles  = [self._DATA_COL_TITLES.get(slot_data_cols[i], "") for i in range(8)]
        x_pos   = self._COL_X
        anchors = ["center", "w", "center", "center", "center", "center", "center", "center"]
        widths  = self._COL_W
        for i, (title, x, anc, w) in enumerate(zip(titles, x_pos, anchors, widths)):
            dc = slot_data_cols[i]
            # Slots past the last visible col have no real data col → hide any existing label.
            is_dead_slot = (i >= 2 + len(visible))
            if is_dead_slot:
                if i < len(self._header_labels):
                    try:
                        self._header_labels[i].place_forget()
                    except Exception:
                        pass
                continue
            sort_key = self._DATA_COL_SORT_KEYS.get(dc)
            is_movable = dc in (2, 3, 4, 5, 6, 7)
            display = title
            if sort_key and sort_key == self._sort_column:
                arrow = " ▲" if self._sort_ascending else " ▼"
                display = title + arrow
            if i < len(self._header_labels):
                lbl = self._header_labels[i]
                lbl.configure(
                    text=display,
                    fg=ACCENT if sort_key == self._sort_column else TEXT_SEP,
                    cursor="fleur" if is_movable else ("hand2" if sort_key else ""),
                )
                # Rebind events so they reflect the current dc/sort_key for this slot
                lbl.unbind("<Button-1>")
                lbl.unbind("<ButtonPress-1>")
                lbl.unbind("<B1-Motion>")
                lbl.unbind("<ButtonRelease-1>")
                if sort_key and not is_movable:
                    lbl.bind("<Button-1>", lambda e, k=sort_key: self._on_header_click(k))
                if is_movable:
                    lbl.bind("<ButtonPress-1>",  lambda e, d=dc, k=sort_key: self._on_hdr_drag_start(e, d, k))
                    lbl.bind("<B1-Motion>",       self._on_hdr_drag_motion)
                    lbl.bind("<ButtonRelease-1>", self._on_hdr_drag_end)
                lbl.place(x=x, y=0, height=scaled(28), width=w)
            else:
                lbl = tk.Label(
                    self._header, text=display, anchor=anc,
                    font=(_theme.FONT_FAMILY, _theme.FS11, "bold"),
                    fg=ACCENT if sort_key == self._sort_column else TEXT_SEP,
                    bg=BG_HEADER, bd=0,
                    cursor="fleur" if is_movable else ("hand2" if sort_key else ""),
                )
                if sort_key and not is_movable:
                    lbl.bind("<Button-1>", lambda e, k=sort_key: self._on_header_click(k))
                if is_movable:
                    lbl.bind("<ButtonPress-1>",  lambda e, d=dc, k=sort_key: self._on_hdr_drag_start(e, d, k))
                    lbl.bind("<B1-Motion>",       self._on_hdr_drag_motion)
                    lbl.bind("<ButtonRelease-1>", self._on_hdr_drag_end)
                lbl.place(x=x, y=0, height=scaled(28), width=w)
                self._header_labels.append(lbl)

        # Place divider grab handles at each resizable boundary (x2..x6).
        # Wider hit area (8px) with a visible 2px line centered inside.
        # Events are bound directly to dividers so header labels can't intercept.
        boundaries = self._COL_X[2:]  # skip x0 (checkbox) and x1 (name start)
        n_visible_local = len(visible)
        for i, bx in enumerate(boundaries):
            col_idx = i + 1  # _COL_W index of the left column at this boundary
            # Only place dividers for boundaries that sit between visible cols.
            is_visible_boundary = (i < n_visible_local)
            if i < len(self._header_dividers):
                div = self._header_dividers[i]
            else:
                div = tk.Frame(self._header, bg=BG_HEADER, cursor="sb_h_double_arrow",
                               highlightthickness=0, bd=0)
                line = tk.Frame(div, bg="#666666", width=2)
                line.place(relx=0.5, y=4, anchor="n", width=2, height=scaled(20))
                # Bind drag events directly to the divider
                div.bind("<ButtonPress-1>", lambda e, c=col_idx: self._on_divider_drag_start(e, c))
                div.bind("<B1-Motion>", self._on_header_col_drag_motion)
                div.bind("<ButtonRelease-1>", self._on_header_col_drag_end)
                div.bind("<Double-Button-1>", lambda e, c=col_idx: self._on_divider_drag_reset(e, c))
                # Also bind to the inner line so clicks on the visible part work
                line.bind("<ButtonPress-1>", lambda e, c=col_idx: self._on_divider_drag_start(e, c))
                line.bind("<B1-Motion>", self._on_header_col_drag_motion)
                line.bind("<ButtonRelease-1>", self._on_header_col_drag_end)
                line.bind("<Double-Button-1>", lambda e, c=col_idx: self._on_divider_drag_reset(e, c))
                line.configure(cursor="sb_h_double_arrow")
                self._header_dividers.append(div)
            if is_visible_boundary:
                div.place(x=bx - 4, y=0, width=8, height=scaled(28))
                div.lift()  # raise above labels
            else:
                try:
                    div.place_forget()
                except Exception:
                    pass

        # Column visibility menu button (far right of header)
        if not hasattr(self, "_col_menu_btn") or self._col_menu_btn is None:
            btn = tk.Label(
                self._header, text="⋮",
                font=(_theme.FONT_FAMILY, _theme.FS12, "bold"),
                fg=TEXT_SEP, bg=BG_HEADER, bd=0, cursor="hand2",
            )
            btn.bind("<Button-1>", lambda e: self._show_column_menu())
            btn.bind("<Enter>", lambda e: btn.configure(fg=ACCENT))
            btn.bind("<Leave>", lambda e: btn.configure(fg=TEXT_SEP))
            self._col_menu_btn = btn
        btn_w = scaled(20)
        btn_x = max(0, canvas_w - btn_w - scaled(14))
        self._col_menu_btn.place(x=btn_x, y=0, width=btn_w, height=scaled(28))
        self._col_menu_btn.lift()

    # ------------------------------------------------------------------
    # Column resize drag (bound directly to divider widgets)
    # ------------------------------------------------------------------

    # Minimum widths per _COL_W index: 0=checkbox, 1=name, 2=cat, 3=flags, 4=conflicts, 5=installed, 6=priority
    _COL_MIN_W = {1: 120, 2: 95, 3: 70, 4: 95, 5: 95, 6: 80, 7: 80}

    def _slot_to_data_col(self, slot: int) -> int:
        """Convert a _COL_W/X slot index to the data-col index it currently holds."""
        if slot == 1:
            return 1
        visible = [dc for dc in self._col_order if dc not in self._col_hidden]
        idx = slot - 2
        if 0 <= idx < len(visible):
            return visible[idx]
        return slot

    def _on_divider_drag_start(self, event: tk.Event, col: int) -> None:
        """Start a column resize drag. col = _COL_W slot index of the left column."""
        self._col_drag_col = col
        self._col_drag_start_x = event.x_root
        # Snapshot widths and data-col keys for all resizable slots (1..6)
        n_visible = len([dc for dc in self._col_order if dc not in self._col_hidden])
        self._col_drag_max_slot = n_visible + 1  # last live slot (slot 1=name, 2..n_visible+1)
        self._col_drag_snap = {
            slot: (self._slot_to_data_col(slot), self._COL_W[slot])
            for slot in range(1, self._col_drag_max_slot + 1)
        }

    def _on_header_col_drag_motion(self, event: tk.Event) -> None:
        if self._col_drag_col is None:
            return
        col = self._col_drag_col          # divider between slot col and col+1
        delta = event.x_root - self._col_drag_start_x
        snap = self._col_drag_snap

        # Slots to the left of divider and to the right
        max_slot = getattr(self, "_col_drag_max_slot", 7)
        left_slots  = list(range(col, 0, -1))              # [col, col-1, ..., 1]
        right_slots = list(range(col + 1, max_slot + 1))   # only live slots
        def distribute(slots: list[int], budget: int) -> dict[int, int]:
            """Shrink/grow slots greedily. budget>0 grows first slot; budget<0 shrinks in order."""
            new_w: dict[int, int] = {}
            remaining = budget
            for s in slots:
                dc, orig = snap[s]
                mn = scaled(self._COL_MIN_W.get(dc, 30))
                if remaining >= 0:
                    new_w[s] = orig + remaining  # first slot absorbs all growth
                    remaining = 0
                else:
                    can_shrink = orig - mn
                    take = max(remaining, -can_shrink)  # take is <= 0
                    new_w[s] = orig + take
                    remaining -= take
                    if remaining == 0:
                        break
            # slots not touched keep their original width
            for s in slots:
                if s not in new_w:
                    new_w[s] = snap[s][1]
            return new_w

        if delta < 0:
            # Moving left: shrink left cols immediate-left first, grow right col
            left_new  = distribute(left_slots, delta)           # delta < 0 → shrink from immediate-left
            actual    = sum(left_new[s] - snap[s][1] for s in left_slots)
            right_new = distribute(right_slots, -actual)        # grow by what was freed
        else:
            # Moving right: shrink right cols, grow immediate-left col
            right_new = distribute(right_slots, -delta)         # negative → shrink
            actual    = sum(snap[s][1] - right_new[s] for s in right_slots)
            left_new  = distribute(left_slots,  actual)         # grow immediate-left first

        for s, (dc, _) in snap.items():
            w = left_new.get(s, right_new.get(s, snap[s][1]))
            self._col_w_override[dc] = w

        self._layout_columns(self._canvas_w)
        self._update_header(self._canvas_w)
        self._redraw()

    def _on_header_col_drag_end(self, event: tk.Event) -> None:
        self._col_drag_col = None
        save_column_widths(self._col_w_override)

    def _on_divider_drag_reset(self, event: tk.Event, col: int) -> None:
        """Double-click a divider to reset both adjacent columns to auto width."""
        left_dc = self._slot_to_data_col(col)
        right_dc = self._slot_to_data_col(col + 1)
        self._col_w_override.pop(left_dc, None)
        self._col_w_override.pop(right_dc, None)
        save_column_widths(self._col_w_override)
        self._layout_columns(self._canvas_w)
        self._update_header(self._canvas_w)
        self._redraw()

    # ------------------------------------------------------------------
    # Column visibility menu
    # ------------------------------------------------------------------
    def _show_column_menu(self) -> None:
        """Popup menu to toggle column visibility. Persists to amethyst.ini."""
        # Close any previous instance so rapid clicks don't leak popups.
        prev = self._col_menu_popup
        if prev is not None:
            try:
                prev.destroy()
            except Exception:
                pass
            self._col_menu_popup = None
        menu = CTkPopupMenu(self.winfo_toplevel(), width=200, title="")
        self._col_menu_popup = menu
        # Columns in current display order (2..7), name col (1) is never hideable.
        for dc in self._col_order:
            title = self._DATA_COL_TITLES.get(dc, f"Col {dc}")
            visible = dc not in self._col_hidden
            prefix = "☑  " if visible else "☐  "
            menu.add_command(prefix + title, lambda d=dc: self._toggle_column_hidden(d),
                             font=("Cantarell", _theme.FONT_NORMAL[1]))
        try:
            btn = self._col_menu_btn
            x = btn.winfo_rootx()
            y = btn.winfo_rooty() + btn.winfo_height()
            menu.popup(x - 170, y)
        except Exception:
            menu.popup()

    def _toggle_column_hidden(self, dc: int) -> None:
        """Toggle a column's visibility and persist."""
        if dc in self._col_hidden:
            self._col_hidden.discard(dc)
        else:
            # Don't allow hiding every non-name column; keep at least one visible.
            if len(self._col_hidden) + 1 >= len(self._col_order):
                return
            self._col_hidden.add(dc)
        save_column_hidden(self._col_hidden)
        self._layout_columns(self._canvas_w)
        self._update_header(self._canvas_w)
        self._redraw()

    # ------------------------------------------------------------------
    # Column reorder drag (header label drag-to-move)
    # ------------------------------------------------------------------

    def _on_hdr_drag_start(self, event: tk.Event, data_col: int, sort_key: str | None) -> None:
        self._hdr_drag_col = data_col
        self._hdr_drag_sort_key = sort_key
        self._hdr_drag_start_x = event.x_root
        self._hdr_drag_moved = False

    def _on_hdr_drag_motion(self, event: tk.Event) -> None:
        if self._hdr_drag_col is None:
            return
        dx = abs(event.x_root - self._hdr_drag_start_x)
        if dx > 5:
            self._hdr_drag_moved = True
        if not self._hdr_drag_moved:
            return
        # Show a ghost label following the cursor
        x_root, y_root = event.x_root, event.y_root
        if self._hdr_drag_ghost is None:
            dc = self._hdr_drag_col
            title = self._DATA_COL_TITLES.get(dc, "")
            self._hdr_drag_ghost = tk.Label(
                self._header, text=title,
                font=(_theme.FONT_FAMILY, _theme.FS11, "bold"),
                fg=ACCENT, bg="#3a3a5a", relief="solid", bd=1,
                padx=4,
            )
        # Position relative to header widget
        hdr_x = self._header.winfo_rootx()
        hdr_y = self._header.winfo_rooty()
        ghost_x = x_root - hdr_x - 20
        self._hdr_drag_ghost.place(x=ghost_x, y=2, height=scaled(24))
        self._hdr_drag_ghost.lift()
        # Highlight the drop target column
        self._hdr_drag_highlight(event.x_root)

    def _hdr_drag_highlight(self, x_root: int) -> None:
        """Update header label backgrounds to show drop target."""
        hdr_x = self._header.winfo_rootx()
        local_x = x_root - hdr_x
        target_slot = self._hdr_slot_at(local_x)
        visible = [dc for dc in self._col_order if dc not in self._col_hidden]
        slot_data_cols = [0, 1] + visible
        for k, lbl in enumerate(self._header_labels):
            slot = k  # label index = slot
            dc = slot_data_cols[slot] if slot < len(slot_data_cols) else 0
            is_movable = dc in (2, 3, 4, 5, 6, 7)
            if is_movable and slot == target_slot:
                lbl.configure(bg="#3a3a5a")
            else:
                lbl.configure(bg=BG_HEADER)

    def _hdr_slot_at(self, local_x: int) -> int:
        """Return the slot index at header local x, clamped to visible movable range."""
        visible = [dc for dc in self._col_order if dc not in self._col_hidden]
        n_visible = len(visible)
        if n_visible == 0:
            return 2
        max_slot = n_visible + 1  # slots 2..n_visible+1
        for slot in range(max_slot, 1, -1):
            if local_x >= self._COL_X[slot]:
                return slot
        return 2

    def _on_hdr_drag_end(self, event: tk.Event) -> None:
        if self._hdr_drag_col is None:
            return
        dc = self._hdr_drag_col
        moved = self._hdr_drag_moved
        sort_key = getattr(self, "_hdr_drag_sort_key", None)
        # Clean up ghost
        if self._hdr_drag_ghost is not None:
            self._hdr_drag_ghost.destroy()
            self._hdr_drag_ghost = None
        # Reset label backgrounds
        for lbl in self._header_labels:
            lbl.configure(bg=BG_HEADER)
        self._hdr_drag_col = None
        self._hdr_drag_moved = False
        if not moved:
            # Treat as a click — sort if sortable
            if sort_key:
                self._on_header_click(sort_key)
            return
        # Determine drop target slot
        hdr_x = self._header.winfo_rootx()
        local_x = event.x_root - hdr_x
        target_slot = self._hdr_slot_at(local_x)
        # Find source slot
        src_slot = self._col_pos.get(dc, -1)
        visible = [c for c in self._col_order if c not in self._col_hidden]
        n_visible = len(visible)
        if src_slot == target_slot or target_slot < 2 or target_slot > (n_visible + 1):
            return
        # Swap within the visible list, then splice back into _col_order preserving hidden positions
        src_k = src_slot - 2
        tgt_k = target_slot - 2
        if src_k < 0 or src_k >= n_visible or tgt_k < 0 or tgt_k >= n_visible:
            return
        visible[src_k], visible[tgt_k] = visible[tgt_k], visible[src_k]
        new_order: list[int] = []
        vi = 0
        for c in self._col_order:
            if c in self._col_hidden:
                new_order.append(c)
            else:
                new_order.append(visible[vi])
                vi += 1
        order = new_order
        self._col_order = order
        save_column_order(order)
        # Rebuild header labels so bindings use the new order
        for lbl in self._header_labels:
            lbl.destroy()
        self._header_labels.clear()
        self._layout_columns(self._canvas_w)
        self._update_header(self._canvas_w)
        self._redraw()

    # ------------------------------------------------------------------
    # Load / reload
    # ------------------------------------------------------------------

    def _load_sep_locks(self) -> None:
        if self._modlist_path is None:
            self._sep_locks = {}
            return
        self._sep_locks = read_separator_locks(self._modlist_path.parent, self.__profile_state)

    def _save_sep_locks(self) -> None:
        if self._modlist_path is None:
            return
        write_separator_locks(self._modlist_path.parent, self._sep_locks)

    def _load_sep_colors(self) -> None:
        if self._modlist_path is None:
            self._sep_colors = {}
            return
        self._sep_colors = read_separator_colors(self._modlist_path.parent, self.__profile_state)

    def _save_sep_colors(self) -> None:
        if self._modlist_path is None:
            return
        write_separator_colors(self._modlist_path.parent, self._sep_colors)

    def _load_sep_deploy_paths(self) -> None:
        if self._modlist_path is None:
            self._sep_deploy_paths = {}
            return
        self._sep_deploy_paths = read_separator_deploy_paths(self._modlist_path.parent, self.__profile_state)

    def _save_sep_deploy_paths(self) -> None:
        if self._modlist_path is None:
            return
        write_separator_deploy_paths(self._modlist_path.parent, self._sep_deploy_paths)

    def _load_mod_strip_prefixes(self) -> None:
        if self._modlist_path is None:
            self._mod_strip_prefixes = {}
            return
        self._mod_strip_prefixes = read_mod_strip_prefixes(self._modlist_path.parent, self.__profile_state)

    def _save_mod_strip_prefixes(self) -> None:
        if self._modlist_path is None:
            return
        write_mod_strip_prefixes(self._modlist_path.parent, self._mod_strip_prefixes)
        self.__profile_state.pop("mod_strip_prefixes", None)

    def _load_root_folder_state(self) -> None:
        if self._modlist_path is None:
            self._root_folder_enabled = True
            return
        self._root_folder_enabled = read_root_folder_state(self._modlist_path.parent, self.__profile_state)

    def _save_root_folder_state(self) -> None:
        if self._modlist_path is None:
            return
        write_root_folder_state(self._modlist_path.parent, self._root_folder_enabled)

    def _load_collapsed(self) -> None:
        if self._modlist_path is None:
            self._collapsed_seps = set()
            return
        self._collapsed_seps = read_collapsed_seps(self._modlist_path.parent, self.__profile_state)

    def _save_collapsed(self) -> None:
        if self._modlist_path is None:
            return
        write_collapsed_seps(self._modlist_path.parent, self._collapsed_seps)

    def _compute_bundle_groups(self) -> None:
        """Rebuild _bundle_groups from current _entries.

        Maps bundle_name → [entry_idx, ...] in order.  Called after every
        _reload() so the radio-toggle logic and renderer always have fresh data.
        """
        groups: dict[str, list[int]] = {}
        for i, entry in enumerate(self._entries):
            bname = entry.bundle_name
            if bname is not None:
                groups.setdefault(bname, []).append(i)
        self._bundle_groups = groups

    def _is_bundle_separator(self, idx: int) -> bool:
        """True if the entry at *idx* is a separator that owns a bundle block."""
        e = self._entries[idx]
        if not e.is_separator:
            return False
        display = e.display_name  # strips _separator suffix
        return display in self._bundle_groups

    def _clamp_outside_bundle_blocks(self, insert_at: int) -> int:
        """If *insert_at* falls strictly inside a bundle separator block,
        push it just before that block so non-bundle mods can never land
        inside a bundle group."""
        # Walk backwards to find the separator that owns this position.
        for si in range(insert_at - 1, -1, -1):
            e = self._entries[si]
            if e.is_separator:
                if self._is_bundle_separator(si):
                    blk = self._sep_block_range(si)
                    if insert_at < blk.stop:
                        # insert_at is inside the bundle block — move before it
                        return si
                break  # inside a normal separator block — fine
        return insert_at

    def _clamp_insert_at(self, insert_at: int, is_block: bool) -> int:
        """Clamp insert_at so items never cross OW or Root Folder boundaries.

        Works regardless of whether _entries is in natural or inverted order.
        After removing the dragged items, OW and Root are at fixed positions.
        Nothing should be inserted before whichever is first or after whichever
        is last.
        """
        n = len(self._entries)
        # Find OW and Root positions in the current (post-removal) _entries.
        # If the dragged item IS a pinned separator it won't be in _entries,
        # so we also check the drag block for pinned names.
        ow_idx = -1
        rf_idx = -1
        for i, e in enumerate(self._entries):
            if e.name == OVERWRITE_NAME:
                ow_idx = i
            elif e.name == ROOT_FOLDER_NAME:
                rf_idx = i

        # If neither pinned separator is found, no clamping needed.
        if ow_idx < 0 and rf_idx < 0:
            return max(0, min(insert_at, n))

        # When only one pinned separator exists, clamp relative to it.
        if ow_idx < 0 or rf_idx < 0:
            pinned = ow_idx if ow_idx >= 0 else rf_idx
            # Items go after OW or before Root
            if self._entries[pinned].name == OVERWRITE_NAME:
                lo = pinned + 1
                return max(lo, min(insert_at, n))
            else:
                hi = pinned
                return max(0, min(insert_at, hi))

        # Both pinned separators present.
        first_pinned = min(ow_idx, rf_idx)
        last_pinned = max(ow_idx, rf_idx)

        lo = first_pinned + 1
        hi = last_pinned

        return max(lo, min(insert_at, hi))

    def _reload(self):
        self._sel_idx = -1
        self._sel_set = set()
        self._drag_idx = -1
        # Preserve the active column sort across reloads.
        # Remove stale lock canvas items before rebuilding
        c = getattr(self, "_canvas", None)
        if c is not None:
            for item_id in self._lock_cb_rects.values():
                c.delete(item_id)
            for item_id in self._lock_cb_marks.values():
                c.delete(item_id)
        self._lock_cb_rects.clear()
        self._lock_cb_marks.clear()
        if self._modlist_path is None:
            self._entries = []
        else:
            # Sync any mods in the mods folder not yet in modlist.txt
            mods_dir = self._staging_root
            sync_modlist_with_mods_folder(self._modlist_path, mods_dir)
            self._load_root_folder_state()
            self._load_mod_strip_prefixes()
            self._load_sep_deploy_paths()
            self._entries = read_modlist(self._modlist_path)
            # Prepend synthetic Overwrite row — always first (highest priority),
            # never saved to modlist.txt.
            self._entries.insert(0, ModEntry(
                name=OVERWRITE_NAME, enabled=True, locked=True, is_separator=True
            ))
            # Append synthetic Root_Folder row at the bottom (lowest priority)
            # if the folder exists.
            root_folder_dir = (
                self._game.get_effective_root_folder_path()
                if self._game is not None
                else self._modlist_path.parent.parent.parent / "Root_Folder"
            )
            if root_folder_dir.is_dir():
                self._entries.append(ModEntry(
                    name=ROOT_FOLDER_NAME,
                    enabled=self._root_folder_enabled,
                    locked=True, is_separator=True
                ))
        if self._modlist_path is not None:
            self.__profile_state = read_profile_state(self._modlist_path.parent)
            self._disabled_plugins_map = read_disabled_plugins(self._modlist_path.parent, self.__profile_state)
            _exc = read_excluded_mod_files(self._modlist_path.parent, self.__profile_state)
            self._excluded_mod_files_map = _exc or {}
        self._load_sep_locks()
        self._load_sep_colors()
        self._load_collapsed()
        self._update_expand_collapse_all_btn()
        self._update_enable_disable_all_btn()
        # Defer meta scan to background so the window appears sooner
        self._scan_meta_flags_async()
        self._rebuild_check_widgets()  # also calls _compute_bundle_groups()
        # Refresh always rescans all mod folders to rebuild the index from scratch.
        self._filemap_rescan_index = True
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _scan_meta_flags(self):
        """Single pass over meta.ini: update, missing_reqs, endorsed, install_dates (sync)."""
        results = _scan_meta_flags_impl(self._entries, self._staging_root)
        self._apply_meta_results(results)

    def _scan_meta_flags_async(self):
        """Run meta scan in background so the window appears sooner; apply results when done.

        Note: we no longer clear the dictionaries up-front.  The previous scan's
        data stays visible until the new results arrive, avoiding a flash where
        Category / Flags / Installed columns go blank during the async gap.
        """
        if self._modlist_path is None or not self._staging_root.is_dir():
            self._update_mods.clear()
            self._missing_reqs.clear()
            self._missing_reqs_detail.clear()
            self._endorsed_mods.clear()
            self._install_dates.clear()
            self._install_datetimes.clear()
            self._category_names.clear()
            self._mod_versions.clear()
            self._fomod_mods.clear()
            self._vis_dirty = True
            return
        if not self._call_threadsafe:
            self._scan_meta_flags()  # Fallback: sync if no thread-safe callback
            return
        entries = list(self._entries)
        mods_dir = self._staging_root
        modlist_path = self._modlist_path
        call_threadsafe = self._call_threadsafe

        def _worker():
            results = _scan_meta_flags_impl(entries, mods_dir)
            results["_modlist_path"] = modlist_path
            call_threadsafe(lambda: self._apply_meta_results(results))

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_meta_results(self, results: dict):
        """Merge scan results into instance state and redraw (main thread only)."""
        # Staleness check only applies to async scans (which add _modlist_path).
        # Sync scans (e.g. after Check Updates) don't include it; avoid rejecting them.
        if "_modlist_path" in results and results["_modlist_path"] != self._modlist_path:
            return  # Stale: user switched game before scan finished
        self._update_mods = results["update_mods"]
        self._missing_reqs = results["missing_reqs"]
        self._missing_reqs_detail = results["missing_reqs_detail"]
        self._endorsed_mods = results["endorsed_mods"]
        self._install_dates = results["install_dates"]
        self._install_datetimes = results["install_datetimes"]
        self._category_names = results.get("category_names", {})
        self._mod_versions = results.get("mod_versions", {})
        self._fomod_mods = results.get("fomod_mods", set())
        if self._filter_panel_open:
            self._refresh_filter_category_list()
        self._vis_dirty = True
        self._redraw()
        self._update_info()

    def _scan_update_flags(self):
        """Scan meta.ini for update flags. Uses async to avoid blocking UI."""
        self._scan_meta_flags_async()

    def _scan_missing_reqs_flags(self):
        """Scan meta.ini for missing requirements. Uses async to avoid blocking UI."""
        self._scan_meta_flags_async()

    def _scan_endorsed_flags(self):
        """Scan meta.ini for endorsed mods. Uses async to avoid blocking UI."""
        self._scan_meta_flags_async()

    def _scan_install_dates(self):
        """Scan meta.ini for install dates. Uses async to avoid blocking UI."""
        self._scan_meta_flags_async()

    def _save_ignored_missing_reqs(self) -> None:
        """Persist _ignored_missing_reqs to profile_state.json."""
        if self._modlist_path is None:
            return
        write_ignored_missing_requirements(self._modlist_path.parent, self._ignored_missing_reqs)

    def _rebuild_check_widgets(self):
        """Rebuild per-entry BooleanVars (logical state only — visual pool is separate)."""
        self._check_vars.clear()
        self._invalidate_derived_caches()

        for entry in self._entries:
            if entry.is_separator:
                # Placeholder keeps indices aligned with self._entries
                self._check_vars.append(None)
                continue
            var = tk.BooleanVar(value=entry.enabled)
            self._check_vars.append(var)

    def _invalidate_derived_caches(self) -> None:
        """Mark cached data derived from _entries as stale.

        Call whenever _entries is mutated (add/remove/reorder) or filter/sort
        state changes so that _redraw() recomputes priorities and visible indices.
        """
        self._vis_dirty = True
        self._priorities = {}
        self._sep_block_cache: dict[int, range] = {}
        self._compute_bundle_groups()

    # ------------------------------------------------------------------
    # Virtual-list pool
    # ------------------------------------------------------------------

    def _create_pool(self) -> None:
        """Pre-allocate a fixed set of canvas items and checkbutton widgets.

        Called once after the canvas is created.  On every _redraw() call the
        pool items are reconfigured (coords/text/fill/state) rather than deleted
        and recreated.  Items outside the visible viewport are set to
        state='hidden' so Tkinter does not render them.
        """
        c = self._canvas
        for s in range(self._pool_size):
            self._pool_data_idx.append(-1)

            # Background rectangle
            bg_id = c.create_rectangle(0, -200, 0, -200, fill="", outline="", state="hidden")
            # Mod name / separator label text
            name_id = c.create_text(0, -200, text="", anchor="w", fill="",
                                    font=(_theme.FONT_FAMILY, _theme.FS11), state="hidden")
            # Flags column icons (up to 3 icons + lock star text)
            flag_id = c.create_image(0, -200, anchor="center", state="hidden")
            flag2_id = c.create_image(0, -200, anchor="center", state="hidden")
            flag3_id = c.create_image(0, -200, anchor="center", state="hidden")
            flag_star_id = c.create_text(0, -200, text="★", anchor="center", fill="#e5c07b",
                                         font=(_theme.FONT_FAMILY, _theme.FS11), state="hidden")
            # Conflict icons (left slot and right slot)
            conf1_id = c.create_image(0, -200, anchor="center", state="hidden")
            conf2_id = c.create_image(0, -200, anchor="center", state="hidden")
            # Category text
            cat_id = c.create_text(0, -200, text="", anchor="center", fill="",
                                  font=(_theme.FONT_FAMILY, _theme.FS10), state="hidden")
            # Install date text
            inst_id = c.create_text(0, -200, text="", anchor="center", fill="",
                                    font=(_theme.FONT_FAMILY, _theme.FS10), state="hidden")
            # Priority text
            prio_id = c.create_text(0, -200, text="", anchor="center", fill="",
                                    font=(_theme.FONT_FAMILY, _theme.FS10), state="hidden")
            # Version text
            ver_id = c.create_text(0, -200, text="", anchor="center", fill="",
                                   font=(_theme.FONT_FAMILY, _theme.FS10), state="hidden")
            # Separator collapse icon
            sep_icon_id = c.create_image(0, -200, anchor="center", state="hidden")
            # Separator decorative lines (left and right of label)
            sep_line_l = c.create_line(0, -200, 0, -200, fill="", width=1, state="hidden")
            sep_line_r = c.create_line(0, -200, 0, -200, fill="", width=1, state="hidden")
            # Custom deploy path badge (shown right of label on separators with override)
            sep_badge_id = c.create_text(0, -200, text="", anchor="w", fill="",
                                         font=(_theme.FONT_FAMILY, _theme.FS9), state="hidden")

            self._pool_bg.append(bg_id)
            self._pool_name.append(name_id)
            self._pool_flag_icon.append(flag_id)
            self._pool_flag_icon2.append(flag2_id)
            self._pool_flag_icon3.append(flag3_id)
            self._pool_flag_star.append(flag_star_id)
            self._pool_conflict_icon1.append(conf1_id)
            self._pool_conflict_icon2.append(conf2_id)
            self._pool_category_text.append(cat_id)
            self._pool_install_text.append(inst_id)
            self._pool_priority_text.append(prio_id)
            self._pool_version_text.append(ver_id)
            self._pool_sep_icon.append(sep_icon_id)
            self._pool_sep_line_l.append(sep_line_l)
            self._pool_sep_line_r.append(sep_line_r)
            self._pool_sep_badge.append(sep_badge_id)

            # Canvas-drawn checkbox (no tk.Checkbutton) — avoids opaque widget background
            # on Linux. Rect + checkmark text, click handled via tag_bind.
            var = tk.BooleanVar(value=False)
            self._pool_check_vars.append(var)
            cb_tag = f"pool_cb_{s}"
            rect_id = c.create_rectangle(
                0, -200, 0, -200, outline=BORDER, width=1, state="hidden",
                tags=(cb_tag, "pool_cb"),
            )
            mark_id = c.create_text(
                0, -200, text="✓", anchor="center", fill=ACCENT,
                font=(_theme.FONT_FAMILY, _theme.FS12, "bold"), state="hidden",
                tags=(cb_tag, "pool_cb"),
            )
            self._pool_cb_rect.append(rect_id)
            self._pool_cb_mark.append(mark_id)
            def _cb_press(e, slot=s):
                return "break"
            def _cb_release(e, slot=s):
                self._on_pool_check_toggle(slot)
                return "break"
            def _cb_enter(e, slot=s):
                c.config(cursor="hand2")
            def _cb_leave(e, slot=s):
                c.config(cursor="")
            c.tag_bind(cb_tag, "<ButtonPress-1>", _cb_press)
            c.tag_bind(cb_tag, "<ButtonRelease-1>", _cb_release)
            c.tag_bind(cb_tag, "<Enter>", _cb_enter)
            c.tag_bind(cb_tag, "<Leave>", _cb_leave)

    def _on_pool_check_toggle(self, slot: int) -> None:
        """A pooled enable-checkbox was clicked — map back to the entry and toggle."""
        # _on_mouse_press already handled this click via the checkbox hit-test — skip.
        if getattr(self, "_checkbox_click_handled", False):
            self._checkbox_click_handled = False
            return
        entry_idx = self._pool_data_idx[slot] if slot < len(self._pool_data_idx) else -1
        if entry_idx < 0:
            return
        # Bundle variants are toggleable even when locked=True (locked only prevents drag/rename).
        _entry = self._entries[entry_idx] if entry_idx < len(self._entries) else None
        if _entry and _entry.locked and _entry.bundle_name is None:
            return
        # Toggle: flip current value, sync to logical var, persist
        checked = not self._pool_check_vars[slot].get()
        self._pool_check_vars[slot].set(checked)
        if entry_idx < len(self._check_vars) and self._check_vars[entry_idx] is not None:
            self._check_vars[entry_idx].set(checked)
        self._on_toggle(entry_idx)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _redraw(self):
        """Pool-based redraw: reconfigure pre-allocated canvas items for the visible viewport.

        No delete("all") — items outside the viewport are hidden via state="hidden".
        This is the same technique used by the plugin panel for smooth scrolling.
        """
        self._redraw_after_id = None
        c = self._canvas
        cw = self._canvas_w

        # Pre-compute column x/w for each data col (respects reorder)
        _col_pos = self._col_pos
        _CAT_X  = self._COL_X[_col_pos.get(2, 2)]; _CAT_W  = self._COL_W[_col_pos.get(2, 2)]
        _FLAG_X = self._COL_X[_col_pos.get(3, 3)]; _FLAG_W = self._COL_W[_col_pos.get(3, 3)]
        _CONF_X = self._COL_X[_col_pos.get(4, 4)]; _CONF_W = self._COL_W[_col_pos.get(4, 4)]
        _INST_X = self._COL_X[_col_pos.get(5, 5)]; _INST_W = self._COL_W[_col_pos.get(5, 5)]
        _PRIO_X = self._COL_X[_col_pos.get(6, 6)]; _PRIO_W = self._COL_W[_col_pos.get(6, 6)]
        _VER_X  = self._COL_X[_col_pos.get(7, 7)]; _VER_W  = self._COL_W[_col_pos.get(7, 7)]

        # Pre-compute font tuples (avoid re-creating inside the inner loop)
        _FONT_NAME = (_theme.FONT_FAMILY, _theme.FS11)
        _FONT_SEP_BOLD = (_theme.FONT_FAMILY, _theme.FS10, "bold")
        _FONT_SMALL = (_theme.FONT_FAMILY, _theme.FS10)
        _FONT_TINY = (_theme.FONT_FAMILY, _theme.FS9)
        _FONT_CHECK = (_theme.FONT_FAMILY, _theme.FS12, "bold")
        _FONT_RADIO = (_theme.FONT_FAMILY, _theme.FS13, "bold")
        _FONT_STAR = (_theme.FONT_FAMILY, _theme.FS11)

        # Indices currently being dragged — suppress _sel_set highlight for these
        # so the blue box only shows at the current drag position, not the origin.
        _dragging = (set(self._drag_sel_indices) if self._drag_sel_indices
                     else ({self._drag_idx} if self._drag_idx >= 0 else set()))

        canvas_top = int(c.canvasy(0))
        canvas_h = c.winfo_height()

        # Recompute priorities cache only when _entries has changed.
        # Must happen before _compute_visible_indices so priority-column sort works.
        if not self._priorities:
            mod_count = sum(1 for e in self._entries if not e.is_separator)
            p = mod_count - 1
            for idx, entry in enumerate(self._entries):
                if not entry.is_separator:
                    self._priorities[idx] = p
                    p -= 1

        # Recompute visible indices only when something structural changed.
        if self._vis_dirty:
            self._visible_indices = self._compute_visible_indices()
            self._vis_dirty = False

        priorities = self._priorities

        vis = self._visible_indices

        n = len(vis)
        total_h = n * self.ROW_H
        row_h = self.ROW_H

        # Viewport slice: only reconfigure pool slots for visible rows.
        first_row = max(0, canvas_top // row_h)
        last_row  = min(n, (canvas_top + canvas_h) // row_h + 2)
        vis_count = last_row - first_row

        sel_entry = (self._entries[self._sel_idx]
                     if 0 <= self._sel_idx < len(self._entries) else None)

        # Pre-compute separator highlight sets
        conflict_sep_higher: set[int] = set()  # green — wins over selected
        conflict_sep_lower:  set[int] = set()  # red   — loses to selected
        # Mod-level highlight sets (used when a separator is selected and some mods are expanded)
        conflict_mod_higher: set[str] = set()  # mod names that selected-separator mods override
        conflict_mod_lower:  set[str] = set()  # mod names that override selected-separator mods
        # Mod-level conflict sets for all selected non-separator mods (multi-selection support)
        conflict_sel_higher: set[str] = set()  # mod names that any selected mod overrides
        conflict_sel_lower:  set[str] = set()  # mod names that override any selected mod
        if sel_entry and not sel_entry.is_separator:
            # Collect conflicts for every selected non-separator mod
            for sel_i in self._sel_set:
                if sel_i < 0 or sel_i >= len(self._entries):
                    continue
                e = self._entries[sel_i]
                if e.is_separator:
                    continue
                sel_name = e.name
                for cm in self._overrides.get(sel_name, set()):
                    si = self._sep_idx_for_mod(cm)
                    if si >= 0 and self._entries[si].name in self._collapsed_seps:
                        conflict_sep_higher.add(si)
                    else:
                        conflict_sel_higher.add(cm)
                for cm in self._overridden_by.get(sel_name, set()):
                    si = self._sep_idx_for_mod(cm)
                    if si >= 0 and self._entries[si].name in self._collapsed_seps:
                        conflict_sep_lower.add(si)
                    else:
                        conflict_sel_lower.add(cm)
        elif sel_entry and sel_entry.is_separator and sel_entry.name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME):
            # Selected entry is a normal separator — highlight all separators/mods
            # that conflict with any mod inside this separator.
            sel_sep_idx = self._sel_idx
            # Collect mods under this separator
            i = sel_sep_idx + 1
            while i < len(self._entries) and not self._entries[i].is_separator:
                mod_name = self._entries[i].name
                for cm in self._overrides.get(mod_name, set()):
                    si = self._sep_idx_for_mod(cm)
                    if si >= 0:
                        if self._entries[si].name in self._collapsed_seps:
                            conflict_sep_higher.add(si)
                        else:
                            conflict_mod_higher.add(cm)
                    elif cm == OVERWRITE_NAME:
                        ow_idx = next((j for j, e in enumerate(self._entries) if e.name == OVERWRITE_NAME), -1)
                        if ow_idx >= 0:
                            conflict_sep_higher.add(ow_idx)
                for cm in self._overridden_by.get(mod_name, set()):
                    si = self._sep_idx_for_mod(cm)
                    if si >= 0:
                        if self._entries[si].name in self._collapsed_seps:
                            conflict_sep_lower.add(si)
                        else:
                            conflict_mod_lower.add(cm)
                    elif cm == OVERWRITE_NAME:
                        ow_idx = next((j for j, e in enumerate(self._entries) if e.name == OVERWRITE_NAME), -1)
                        if ow_idx >= 0:
                            conflict_sep_lower.add(ow_idx)
                i += 1
        elif sel_entry and sel_entry.name == OVERWRITE_NAME:
            for cm in self._overrides.get(OVERWRITE_NAME, set()):
                si = self._sep_idx_for_mod(cm)
                if si >= 0 and self._entries[si].name in self._collapsed_seps:
                    conflict_sep_higher.add(si)

        # Special case: if Overwrite overrides any selected mod, highlight the Overwrite row green.
        if sel_entry and not sel_entry.is_separator:
            for sel_i in self._sel_set:
                if sel_i < 0 or sel_i >= len(self._entries):
                    continue
                e = self._entries[sel_i]
                if not e.is_separator and OVERWRITE_NAME in self._overridden_by.get(e.name, set()):
                    ow_idx = next((j for j, oe in enumerate(self._entries) if oe.name == OVERWRITE_NAME), -1)
                    if ow_idx >= 0:
                        conflict_sep_higher.add(ow_idx)
                    break

        highlighted_sep_idx: int = -1
        if self._highlighted_mod:
            highlighted_sep_idx = self._sep_idx_for_mod(self._highlighted_mod)

        # Track which lock canvas items were repositioned this frame
        _visited_lock_keys: set[str] = set()

        # Reconfigure pool slots
        for s in range(self._pool_size):
            row = first_row + s
            if s < vis_count and row < n:
                i = vis[row]
                entry = self._entries[i]
                y_top = row * row_h
                y_bot = y_top + row_h
                y_mid = y_top + row_h // 2

                self._pool_data_idx[s] = i

                if entry.is_separator:
                    is_overwrite   = (entry.name == OVERWRITE_NAME)
                    is_root_folder = (entry.name == ROOT_FOLDER_NAME)
                    is_synthetic   = is_overwrite or is_root_folder
                    is_sel_row = (i in self._sel_set and i not in _dragging) or (i == self._drag_idx)

                    custom_color = None
                    if is_overwrite:
                        base_bg = "#1e2a1e"
                        txt_col = "#6dbf6d"
                    elif is_root_folder:
                        base_bg = "#1e1e2e" if entry.enabled else BG_SEP
                        txt_col = "#7aa2f7" if entry.enabled else TEXT_DIM
                    else:
                        custom_color = self._sep_colors.get(entry.name)
                        base_bg = custom_color if custom_color else BG_SEP
                        txt_col = _theme.contrasting_text_color(base_bg) if custom_color else TEXT_SEP

                    if is_sel_row:
                        row_bg = BG_SELECT
                    elif (not is_synthetic or is_overwrite) and i in conflict_sep_higher:
                        row_bg = conflict_higher
                    elif (not is_synthetic or is_overwrite) and i in conflict_sep_lower:
                        row_bg = conflict_lower
                    elif not is_synthetic and i == highlighted_sep_idx:
                        row_bg = plugin_separator
                    elif i == self._hover_idx and self._drag_idx < 0:
                        if custom_color:
                            # Lighten the custom colour slightly on hover
                            r, g, b = int(base_bg[1:3], 16), int(base_bg[3:5], 16), int(base_bg[5:7], 16)
                            r, g, b = min(255, r + 20), min(255, g + 20), min(255, b + 20)
                            row_bg = f"#{r:02x}{g:02x}{b:02x}"
                        else:
                            row_bg = BG_HOVER_ROW
                    else:
                        row_bg = base_bg

                    # Background rectangle
                    c.coords(self._pool_bg[s], 0, y_top, cw, y_bot)
                    c.itemconfigure(self._pool_bg[s], fill=row_bg, outline="", state="normal")

                    # Separator label (pool name item, centred, bold)
                    if is_overwrite:
                        label = "Overwrite"
                    elif is_root_folder:
                        label = "Root Folder"
                    else:
                        label = entry.display_name

                    mid_x     = cw // 2
                    lock_w    = scaled(28) if not is_synthetic else 0
                    _badge_info = self._sep_deploy_paths.get(entry.name, {}) if not is_synthetic else {}
                    has_badge = bool(_badge_info and (
                        (_badge_info.get("path") if isinstance(_badge_info, dict) else _badge_info)
                        or (_badge_info.get("raw") if isinstance(_badge_info, dict) else False)
                    ))
                    left_edge = scaled(32) if is_root_folder else (scaled(20) if not is_synthetic else scaled(8))

                    if has_badge:
                        # Left-aligned layout: label + path badge flowing from left edge
                        label_x = left_edge + scaled(4)
                        c.coords(self._pool_name[s], label_x, y_mid)
                        c.itemconfigure(self._pool_name[s], text=label, anchor="w",
                                        fill=txt_col, font=_FONT_SEP_BOLD, state="normal")
                        # Approximate bold label width at FS10 (~7px per char)
                        badge_x = label_x + len(label) * scaled(7) + scaled(8)
                        _deploy_path = _badge_info.get("path", "") if isinstance(_badge_info, dict) else str(_badge_info)
                        _is_raw_deploy = _badge_info.get("raw", False) if isinstance(_badge_info, dict) else False
                        try:
                            _home = Path.home()
                            _dp = Path(_deploy_path)
                            badge_path = "~/" + str(_dp.relative_to(_home)) if _dp.is_relative_to(_home) else _deploy_path
                        except Exception:
                            badge_path = _deploy_path
                        if _deploy_path and _is_raw_deploy:
                            badge_text = f"⇒ {badge_path}  [raw]"
                        elif _deploy_path:
                            badge_text = f"⇒ {badge_path}"
                        else:
                            badge_text = "[raw deploy]"
                        c.coords(self._pool_sep_badge[s], badge_x, y_mid)
                        c.itemconfigure(self._pool_sep_badge[s], text=badge_text,
                                        fill="#7aa2f7", state="normal")
                        c.itemconfigure(self._pool_sep_line_l[s], state="hidden")
                        c.itemconfigure(self._pool_sep_line_r[s], state="hidden")
                    else:
                        # Centered label with flanking decorative lines (default)
                        c.coords(self._pool_name[s], mid_x, y_mid)
                        c.itemconfigure(self._pool_name[s], text=label, anchor="center",
                                        fill=txt_col, font=_FONT_SEP_BOLD, state="normal")
                        c.itemconfigure(self._pool_sep_badge[s], state="hidden")
                        text_pad     = scaled(6)
                        label_hw     = len(label) * scaled(4) + text_pad
                        right_edge   = cw - lock_w - scaled(8)
                        sep_line_col = txt_col if (custom_color if not is_synthetic else False) else BORDER
                        c.coords(self._pool_sep_line_l[s],
                                 left_edge, y_mid, mid_x - label_hw, y_mid)
                        c.itemconfigure(self._pool_sep_line_l[s], fill=sep_line_col, state="normal")
                        c.coords(self._pool_sep_line_r[s],
                                 mid_x + label_hw, y_mid, right_edge, y_mid)
                        c.itemconfigure(self._pool_sep_line_r[s], fill=sep_line_col, state="normal")

                    # Collapse icon (real separators only)
                    if not is_synthetic:
                        if entry.name in self._collapsed_seps:
                            icon = self._icon_sep_right
                            fallback = "▶"
                        else:
                            icon = self._icon_sep_arrow
                            fallback = "▼"
                        if icon:
                            c.coords(self._pool_sep_icon[s], scaled(10), y_mid)
                            c.itemconfigure(self._pool_sep_icon[s],
                                            image=icon, state="normal")
                        else:
                            # No image — use a text item; reuse pool_name won't work here,
                            # so just hide the image slot and draw nothing extra.
                            c.itemconfigure(self._pool_sep_icon[s], state="hidden")
                    else:
                        c.itemconfigure(self._pool_sep_icon[s], state="hidden")

                    # Overwrite row conflict icons in conflict column — always wins only
                    if is_overwrite and self._overrides.get(OVERWRITE_NAME):
                        cx = _CONF_X + _CONF_W // 2
                        c.itemconfigure(self._pool_conflict_icon1[s], state="hidden")
                        if self._icon_plus:
                            c.coords(self._pool_conflict_icon2[s], cx, y_mid)
                            c.itemconfigure(self._pool_conflict_icon2[s],
                                            image=self._icon_plus, state="normal")
                        else:
                            c.itemconfigure(self._pool_conflict_icon2[s], state="hidden")
                    else:
                        c.itemconfigure(self._pool_conflict_icon1[s], state="hidden")
                        c.itemconfigure(self._pool_conflict_icon2[s], state="hidden")

                    # Hide mod-only items
                    c.itemconfigure(self._pool_flag_icon[s], state="hidden")
                    c.itemconfigure(self._pool_flag_icon2[s], state="hidden")
                    c.itemconfigure(self._pool_flag_icon3[s], state="hidden")
                    c.itemconfigure(self._pool_flag_star[s], state="hidden")
                    c.itemconfigure(self._pool_category_text[s], state="hidden")
                    c.itemconfigure(self._pool_install_text[s], state="hidden")
                    c.itemconfigure(self._pool_priority_text[s], state="hidden")
                    c.itemconfigure(self._pool_version_text[s], state="hidden")

                    # Pool check widget — hidden for separators
                    c.itemconfigure(self._pool_cb_rect[s], state="hidden")
                    c.itemconfigure(self._pool_cb_mark[s], state="hidden")

                    # Canvas-drawn lock / root-folder enable checkbox (no tk widget —
                    # canvas items scroll in sync automatically).
                    if is_root_folder:
                        rf_key = ROOT_FOLDER_NAME
                        _visited_lock_keys.add(rf_key)
                        checked_rf = entry.enabled
                        cb_cx = self._COL_X[0] + scaled(12)
                        cb_size = scaled(14)
                        x1, y1 = cb_cx - cb_size // 2, y_mid - cb_size // 2
                        x2, y2 = cb_cx + cb_size // 2, y_mid + cb_size // 2
                        if rf_key not in self._lock_cb_rects:
                            lk_tag = "lock_cb_root"
                            rect_id = c.create_rectangle(
                                x1, y1, x2, y2,
                                outline=BORDER, width=1,
                                fill=BG_DEEP if checked_rf else base_bg,
                                tags=(lk_tag, "lock_cb"),
                            )
                            mark_id = c.create_text(
                                cb_cx, y_mid, text="✓", anchor="center",
                                fill=ACCENT, font=_FONT_CHECK,
                                state="normal" if checked_rf else "hidden",
                                tags=(lk_tag, "lock_cb"),
                            )
                            self._lock_cb_rects[rf_key] = rect_id
                            self._lock_cb_marks[rf_key] = mark_id
                            c.tag_bind(lk_tag, "<ButtonRelease-1>",
                                       lambda e: self._on_root_folder_toggle())
                            c.tag_bind(lk_tag, "<Enter>",
                                       lambda e: c.config(cursor="hand2"))
                            c.tag_bind(lk_tag, "<Leave>",
                                       lambda e: c.config(cursor=""))
                        else:
                            rect_id = self._lock_cb_rects[rf_key]
                            mark_id = self._lock_cb_marks[rf_key]
                            c.coords(rect_id, x1, y1, x2, y2)
                            c.itemconfigure(rect_id, fill=BG_DEEP if checked_rf else base_bg)
                            c.coords(mark_id, cb_cx, y_mid)
                            c.itemconfigure(mark_id,
                                            state="normal" if checked_rf else "hidden")
                    elif not is_synthetic:
                        sname = entry.name
                        _visited_lock_keys.add(sname)
                        locked_state = self._sep_locks.get(sname, False)
                        lk_x = cw - lock_w - scaled(8) + lock_w // 2
                        cb_size2 = scaled(14)
                        x1, y1 = lk_x - cb_size2 // 2, y_mid - cb_size2 // 2
                        x2, y2 = lk_x + cb_size2 // 2, y_mid + cb_size2 // 2
                        if sname not in self._lock_cb_rects:
                            lk_tag2 = f"lock_cb_{len(self._lock_cb_rects)}"
                            rect2_id = c.create_rectangle(
                                x1, y1, x2, y2,
                                outline=BORDER, width=1,
                                fill=BG_DEEP if locked_state else row_bg,
                                tags=(lk_tag2, "lock_cb"),
                            )
                            if self._icon_lock:
                                mark2_id = c.create_image(
                                    lk_x, y_mid, anchor="center",
                                    image=self._icon_lock,
                                    state="normal" if locked_state else "hidden",
                                    tags=(lk_tag2, "lock_cb"),
                                )
                            else:
                                mark2_id = c.create_text(
                                    lk_x, y_mid, text="🔒", anchor="center",
                                    fill=TEXT_SEP, font=_FONT_TINY,
                                    state="normal" if locked_state else "hidden",
                                    tags=(lk_tag2, "lock_cb"),
                                )
                            self._lock_cb_rects[sname] = rect2_id
                            self._lock_cb_marks[sname] = mark2_id
                            c.tag_bind(lk_tag2, "<ButtonRelease-1>",
                                       lambda e, n=sname: self._on_sep_lock_toggle(n))
                            c.tag_bind(lk_tag2, "<Enter>",
                                       lambda e: c.config(cursor="hand2"))
                            c.tag_bind(lk_tag2, "<Leave>",
                                       lambda e: c.config(cursor=""))
                        else:
                            rect2_id = self._lock_cb_rects[sname]
                            mark2_id = self._lock_cb_marks[sname]
                            c.coords(rect2_id, x1, y1, x2, y2)
                            c.itemconfigure(rect2_id,
                                            fill=BG_DEEP if locked_state else row_bg)
                            c.coords(mark2_id, lk_x, y_mid)
                            c.itemconfigure(mark2_id,
                                            state="normal" if locked_state else "hidden")

                else:
                    # --- Regular mod row ---
                    is_sel = (i in self._sel_set and i not in _dragging) or (i == self._drag_idx)
                    if is_sel:
                        bg = BG_SELECT
                    elif entry.name == self._highlighted_mod:
                        bg = plugin_mod
                    elif i == self._hover_idx and self._drag_idx < 0:
                        bg = BG_HOVER_ROW
                    elif sel_entry and (not sel_entry.is_separator
                                        or sel_entry.name == OVERWRITE_NAME):
                        if entry.name in conflict_sel_higher:
                            bg = conflict_higher
                        elif entry.name in conflict_sel_lower:
                            bg = conflict_lower
                        else:
                            bg = BG_ROW if row % 2 == 0 else BG_ROW_ALT
                    elif entry.name in conflict_mod_higher:
                        bg = conflict_higher
                    elif entry.name in conflict_mod_lower:
                        bg = conflict_lower
                    else:
                        bg = BG_ROW if row % 2 == 0 else BG_ROW_ALT

                    # Background
                    c.coords(self._pool_bg[s], 0, y_top, cw, y_bot)
                    c.itemconfigure(self._pool_bg[s], fill=bg, outline="", state="normal")

                    # Name text (truncate if it would overlap the category column)
                    name_color = TEXT_DIM if not entry.enabled else TEXT_MAIN
                    name_font = _FONT_NAME
                    is_bundle_variant = entry.bundle_name is not None
                    _name_indent = 0
                    name_width = self._COL_W[1] - scaled(4) - _name_indent
                    _display_label = f"{entry.bundle_name} - {entry.variant_name}" if is_bundle_variant else entry.name
                    display_name = _truncate_text_for_width(c, _display_label, name_font, name_width)
                    c.coords(self._pool_name[s], self._COL_X[1] + _name_indent, y_mid)
                    c.itemconfigure(self._pool_name[s], text=display_name, anchor="w",
                                    fill=name_color, font=name_font, state="normal")

                    # Hide separator-only items
                    c.itemconfigure(self._pool_sep_icon[s], state="hidden")
                    c.itemconfigure(self._pool_sep_line_l[s], state="hidden")
                    c.itemconfigure(self._pool_sep_line_r[s], state="hidden")
                    c.itemconfigure(self._pool_sep_badge[s], state="hidden")

                    # Category text (truncate if it would overlap the flags column)
                    cat_text = self._category_names.get(entry.name, "")
                    if cat_text:
                        cat_font = _FONT_SMALL
                        cat_width = _CAT_W - scaled(4)
                        display_cat = _truncate_text_for_width(c, cat_text, cat_font, cat_width)
                        cat_cx = _CAT_X + _CAT_W // 2
                        c.coords(self._pool_category_text[s], cat_cx, y_mid)
                        c.itemconfigure(self._pool_category_text[s],
                                        text=display_cat, anchor="center",
                                        fill=TEXT_DIM, font=cat_font, state="normal")
                    else:
                        c.itemconfigure(self._pool_category_text[s], state="hidden")

                    # Flags column — collect ordered list of icons/items to show side by side.
                    # Each flag is a tuple: ("img", image_obj) or ("star",).
                    _flags: list = []
                    has_missing = (entry.name in self._missing_reqs
                                   and entry.name not in self._ignored_missing_reqs)
                    if has_missing and self._icon_warning:
                        _flags.append(("img", self._icon_warning))
                    if entry.locked:
                        _flags.append(("star",))
                    if entry.name in self._update_mods and self._icon_update:
                        _flags.append(("img", self._icon_update))
                    if entry.name in self._endorsed_mods and self._icon_endorsed:
                        _flags.append(("img", self._icon_endorsed))
                    if entry.name in self._prertx_mods and self._icon_info:
                        _flags.append(("img", self._icon_info))
                    if entry.name in self._excluded_mod_files_map and self._icon_disabled_files:
                        _flags.append(("img", self._icon_disabled_files))

                    # Lay out flags left-aligned inside the flags column (icon spacing = 18px)
                    _FLAG_ICON_SPACING = 18
                    _flag_slots = [
                        (self._pool_flag_icon[s], "img"),
                        (self._pool_flag_icon2[s], "img"),
                        (self._pool_flag_icon3[s], "img"),
                    ]
                    _flag_star_slot = self._pool_flag_star[s]
                    # Centre the group within the column
                    _n_flags = len(_flags)
                    if _n_flags > 0:
                        _group_w = (_n_flags - 1) * _FLAG_ICON_SPACING
                        _fx_start = _FLAG_X + _FLAG_W // 2 - _group_w // 2
                    else:
                        _fx_start = _FLAG_X + _FLAG_W // 2
                    _img_slot_idx = 0
                    _star_placed = False
                    for _fi, _flag in enumerate(_flags):
                        _fx = _fx_start + _fi * _FLAG_ICON_SPACING
                        if _flag[0] == "star":
                            c.coords(_flag_star_slot, _fx, y_mid)
                            c.itemconfigure(_flag_star_slot, state="normal")
                            _star_placed = True
                        else:
                            if _img_slot_idx < len(_flag_slots):
                                _slot_id = _flag_slots[_img_slot_idx][0]
                                c.coords(_slot_id, _fx, y_mid)
                                c.itemconfigure(_slot_id, image=_flag[1], state="normal")
                                _img_slot_idx += 1
                    # Hide unused image slots
                    for _si in range(_img_slot_idx, len(_flag_slots)):
                        c.itemconfigure(_flag_slots[_si][0], state="hidden")
                    if not _star_placed:
                        c.itemconfigure(_flag_star_slot, state="hidden")

                    # Conflict icons
                    if entry.locked:
                        c.itemconfigure(self._pool_conflict_icon1[s], state="hidden")
                        c.itemconfigure(self._pool_conflict_icon2[s], state="hidden")
                    else:
                        conflict = self._conflict_map.get(entry.name, CONFLICT_NONE)
                        cx = _CONF_X + _CONF_W // 2
                        if conflict == CONFLICT_WINS and self._icon_plus:
                            c.coords(self._pool_conflict_icon1[s], cx, y_mid)
                            c.itemconfigure(self._pool_conflict_icon1[s],
                                            image=self._icon_plus, state="normal")
                            c.itemconfigure(self._pool_conflict_icon2[s], state="hidden")
                        elif conflict == CONFLICT_LOSES and self._icon_minus:
                            c.coords(self._pool_conflict_icon1[s], cx, y_mid)
                            c.itemconfigure(self._pool_conflict_icon1[s],
                                            image=self._icon_minus, state="normal")
                            c.itemconfigure(self._pool_conflict_icon2[s], state="hidden")
                        elif conflict == CONFLICT_PARTIAL and self._icon_minus and self._icon_plus:
                            c.coords(self._pool_conflict_icon1[s], cx - scaled(8), y_mid)
                            c.itemconfigure(self._pool_conflict_icon1[s],
                                            image=self._icon_minus, state="normal")
                            c.coords(self._pool_conflict_icon2[s], cx + scaled(8), y_mid)
                            c.itemconfigure(self._pool_conflict_icon2[s],
                                            image=self._icon_plus, state="normal")
                        elif conflict == CONFLICT_FULL and self._icon_cross:
                            c.coords(self._pool_conflict_icon1[s], cx, y_mid)
                            c.itemconfigure(self._pool_conflict_icon1[s],
                                            image=self._icon_cross, state="normal")
                            c.itemconfigure(self._pool_conflict_icon2[s], state="hidden")
                        else:
                            c.itemconfigure(self._pool_conflict_icon1[s], state="hidden")
                            c.itemconfigure(self._pool_conflict_icon2[s], state="hidden")

                    # Install date text
                    install_text = self._install_dates.get(entry.name, "")
                    if install_text:
                        inst_cx = _INST_X + _INST_W // 2
                        c.coords(self._pool_install_text[s], inst_cx, y_mid)
                        c.itemconfigure(self._pool_install_text[s],
                                        text=install_text, anchor="center",
                                        fill=TEXT_DIM, font=_FONT_SMALL, state="normal")
                    else:
                        c.itemconfigure(self._pool_install_text[s], state="hidden")

                    # Priority text
                    prio_cx = _PRIO_X + _PRIO_W // 2
                    c.coords(self._pool_priority_text[s], prio_cx, y_mid)
                    c.itemconfigure(self._pool_priority_text[s],
                                    text=str(priorities.get(i, "")), anchor="center",
                                    fill=TEXT_DIM, font=_FONT_SMALL, state="normal")

                    # Version text
                    version_text = self._mod_versions.get(entry.name, "")
                    if version_text:
                        ver_font = _FONT_SMALL
                        ver_width = _VER_W - scaled(4)
                        display_ver = _truncate_text_for_width(c, version_text, ver_font, ver_width)
                        ver_cx = _VER_X + _VER_W // 2
                        c.coords(self._pool_version_text[s], ver_cx, y_mid)
                        c.itemconfigure(self._pool_version_text[s],
                                        text=display_ver, anchor="center",
                                        fill=TEXT_DIM, font=ver_font, state="normal")
                    else:
                        c.itemconfigure(self._pool_version_text[s], state="hidden")

                    # Enable/disable control (canvas-drawn)
                    # Bundle variants → radio circle (● / ○); normal mods → checkbox
                    if i < len(self._check_vars) and self._check_vars[i] is not None:
                        self._pool_check_vars[s].set(self._check_vars[i].get())
                        checked = self._pool_check_vars[s].get()
                        cb_cx = self._COL_X[0] + scaled(12)
                        if is_bundle_variant:
                            # Invisible hit-area rect filled with the row bg colour so it
                            # still receives mouse events (fill="" / outline="" rects do not).
                            cb_size = scaled(14)
                            x1, y1 = cb_cx - cb_size // 2, y_mid - cb_size // 2
                            x2, y2 = cb_cx + cb_size // 2, y_mid + cb_size // 2
                            c.coords(self._pool_cb_rect[s], x1, y1, x2, y2)
                            c.itemconfigure(self._pool_cb_rect[s],
                                            fill=bg, outline="", state="normal")
                            c.coords(self._pool_cb_mark[s], cb_cx, y_mid)
                            c.itemconfigure(self._pool_cb_mark[s],
                                            text="●" if checked else "○",
                                            fill=ACCENT if checked else TEXT_DIM,
                                            font=_FONT_RADIO,
                                            state="normal")
                        else:
                            cb_size = scaled(14)
                            x1, y1 = cb_cx - cb_size // 2, y_mid - cb_size // 2
                            x2, y2 = cb_cx + cb_size // 2, y_mid + cb_size // 2
                            c.coords(self._pool_cb_rect[s], x1, y1, x2, y2)
                            fill = BG_DEEP if checked else bg
                            c.itemconfigure(self._pool_cb_rect[s],
                                            fill=fill, outline=BORDER, state="normal")
                            c.coords(self._pool_cb_mark[s], cb_cx, y_mid)
                            c.itemconfigure(self._pool_cb_mark[s],
                                            text="✓",
                                            fill=ACCENT,
                                            font=_FONT_CHECK,
                                            state="normal" if checked else "hidden")
                    else:
                        c.itemconfigure(self._pool_cb_rect[s], state="hidden")
                        c.itemconfigure(self._pool_cb_mark[s], state="hidden")

            else:
                # Slot outside the visible range — hide all items
                c.itemconfigure(self._pool_bg[s], state="hidden")
                c.itemconfigure(self._pool_name[s], state="hidden")
                c.itemconfigure(self._pool_flag_icon[s], state="hidden")
                c.itemconfigure(self._pool_flag_icon2[s], state="hidden")
                c.itemconfigure(self._pool_flag_icon3[s], state="hidden")
                c.itemconfigure(self._pool_flag_star[s], state="hidden")
                c.itemconfigure(self._pool_conflict_icon1[s], state="hidden")
                c.itemconfigure(self._pool_conflict_icon2[s], state="hidden")
                c.itemconfigure(self._pool_category_text[s], state="hidden")
                c.itemconfigure(self._pool_install_text[s], state="hidden")
                c.itemconfigure(self._pool_priority_text[s], state="hidden")
                c.itemconfigure(self._pool_version_text[s], state="hidden")
                c.itemconfigure(self._pool_sep_icon[s], state="hidden")
                c.itemconfigure(self._pool_sep_line_l[s], state="hidden")
                c.itemconfigure(self._pool_sep_line_r[s], state="hidden")
                c.itemconfigure(self._pool_sep_badge[s], state="hidden")
                c.itemconfigure(self._pool_cb_rect[s], state="hidden")
                c.itemconfigure(self._pool_cb_mark[s], state="hidden")
                self._pool_data_idx[s] = -1

        # Park lock canvas items for separators not in the current viewport.
        for key, rect_id in self._lock_cb_rects.items():
            if key not in _visited_lock_keys:
                c.coords(rect_id, 0, -200, 0, -200)
                c.coords(self._lock_cb_marks[key], 0, -200)

        # The drag overlay uses its own tagged items drawn on top
        c.configure(scrollregion=(0, 0, cw, max(total_h, canvas_h)))

        self._draw_marker_strip()

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
                font=(_theme.FONT_FAMILY, _theme.FS10, "bold") if is_sep else (_theme.FONT_FAMILY, _theme.FS11),
                tags="drag_overlay",
            )

        # Blue insertion line showing where the item will land when released.
        # _drag_slot is an index into the vis-without-drag list.
        slot = self._drag_slot
        blk_size = len(self._drag_block) if self._drag_is_block else 1
        vis = self._visible_indices
        if self._drag_sel_indices:
            drag_set = set(self._drag_sel_indices)
        else:
            drag_set = set(range(self._drag_idx, self._drag_idx + blk_size))
        vis_without_drag = [i for i in vis if i not in drag_set]

        if slot >= len(vis_without_drag):
            # Inserting after the last rendered row
            line_y = len(vis_without_drag) * gh
        else:
            # slot is the position in vis_without_drag, which is also the rendered
            # row index (the drag entries are hidden/ghosted, so non-drag rows are
            # packed contiguously at 0..n-1). This is correct regardless of sort order.
            line_y = slot * gh

        self._canvas.create_line(
            0, line_y, cw, line_y,
            fill=ACCENT, width=2, tags="drag_overlay",
        )

    def _on_search_change(self, _event=None):
        # Ignore key events that fire after focus has left the search entry
        if self.focus_get() is not self._search_entry:
            return
        self._filter_text = self._search_entry.get().lower()
        if self._filter_text:
            self._search_clear_btn.pack(side="left", padx=(0, 8), pady=4)
        else:
            self._search_clear_btn.pack_forget()
        self._sel_idx = -1
        self._invalidate_derived_caches()
        self._redraw()

    def _on_search_clear(self, _event=None):
        self._search_entry.delete(0, "end")
        self._search_clear_btn.pack_forget()
        self._filter_text = ""
        self._sel_idx = -1
        self._invalidate_derived_caches()
        self._redraw()

    def _compute_visible_indices(self) -> list[int]:
        """Return entry indices that match the current filter, collapsed state, and column sort."""
        # Step 1: basic visibility (filter or collapse)
        if self._filter_text:
            ft = self._filter_text
            # Include mods that match AND separators that own at least one matching mod.
            matched_mods = {i for i, e in enumerate(self._entries)
                            if not e.is_separator and ft in e.name.lower()}
            base = []
            for i, e in enumerate(self._entries):
                if e.is_separator:
                    blk = self._sep_block_range(i)
                    if any(j in matched_mods for j in blk if j != i):
                        base.append(i)
                elif i in matched_mods:
                    base.append(i)
        elif not self._collapsed_seps:
            base = list(range(len(self._entries)))
        else:
            base = []
            skip = False
            _skip_bundle: str | None = None  # bundle_name to skip when collapsing a bundle sep
            for i, entry in enumerate(self._entries):
                if entry.is_separator:
                    skip = False
                    _skip_bundle = None
                    base.append(i)
                    if entry.name in self._collapsed_seps:
                        if self._is_bundle_separator(i):
                            _skip_bundle = entry.display_name
                        else:
                            skip = True
                elif skip:
                    # Keep the dragged entry visible even inside a collapsed block
                    if self._drag_idx >= 0 and i == self._drag_idx:
                        base.append(i)
                elif _skip_bundle is not None and entry.bundle_name == _skip_bundle:
                    pass  # collapsed bundle separator — hide only its variants
                else:
                    base.append(i)

        # Step 2: hide separators filter (keep synthetic Overwrite/Root Folder rows)
        if self._filter_hide_separators:
            base = [i for i in base
                    if not self._entries[i].is_separator
                    or self._entries[i].name in (OVERWRITE_NAME, ROOT_FOLDER_NAME)]

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

        # Step 4c: disabled plugins filter (show only mods with at least one plugin disabled)
        if self._filter_has_disabled_plugins:
            result = []
            for i in base:
                entry = self._entries[i]
                if entry.is_separator:
                    if self._sep_block_has_disabled_plugins(i):
                        result.append(i)
                elif entry.name in self._disabled_plugins_map:
                    result.append(i)
            base = result

        # Step 4c2: mods with plugins filter (show only mods that have at least one plugin)
        if self._filter_has_plugins:
            mods_with_plugins = self._get_mods_with_plugins()
            if mods_with_plugins:
                result = []
                for i in base:
                    entry = self._entries[i]
                    if entry.is_separator:
                        if self._sep_block_has_plugins(i, mods_with_plugins):
                            result.append(i)
                    elif entry.name in mods_with_plugins:
                        result.append(i)
                base = result
            else:
                base = []  # no plugin data yet, show nothing

        # Step 4d: disabled mod files filter (show only mods with at least one file disabled)
        if self._filter_has_disabled_files:
            result = []
            for i in base:
                entry = self._entries[i]
                if entry.is_separator:
                    if self._sep_block_has_disabled_files(i):
                        result.append(i)
                elif entry.name in self._excluded_mod_files_map:
                    result.append(i)
            base = result

        # Step 4e: updates filter (show only mods with an update available)
        if self._filter_has_updates:
            result = []
            for i in base:
                entry = self._entries[i]
                if entry.is_separator:
                    if self._sep_block_has_updates(i):
                        result.append(i)
                elif entry.name in self._update_mods:
                    result.append(i)
            base = result

        # Step 4e2: FOMOD-only filter (show only mods installed via FOMOD)
        if self._filter_fomod_only:
            result = []
            for i in base:
                entry = self._entries[i]
                if entry.is_separator:
                    if self._sep_block_has_fomod(i):
                        result.append(i)
                elif entry.name in self._fomod_mods:
                    result.append(i)
            base = result

        # Step 4f: category filter (show only mods in selected categories)
        if self._filter_categories:
            allowed = self._filter_categories
            result = []
            for i in base:
                entry = self._entries[i]
                if entry.is_separator:
                    if self._sep_block_has_category(i, allowed):
                        result.append(i)
                else:
                    cat = self._category_names.get(entry.name, "") or ""
                    if cat in allowed:
                        result.append(i)
            base = result

        # Step 5: apply column sort (visual only)
        if self._sort_column is not None:
            base = self._apply_column_sort(base)
        return base

    # ------------------------------------------------------------------
    # Column sorting helpers (visual only — never touches modlist.txt)
    # ------------------------------------------------------------------

    def _clear_sort(self) -> tuple[str | None, bool]:
        """Clear any active non-priority column sort. Returns (col, asc) from before clearing."""
        col, asc = self._sort_column, self._sort_ascending
        if self._sort_column is None or self._sort_column == "priority":
            return col, asc
        self._sort_column = None
        self._sort_ascending = True
        self._update_header(self._canvas_w)
        return col, asc

    def _restore_sort(self, col: str | None, asc: bool) -> None:
        """Restore a previously saved sort state and persist it."""
        if col is None:
            return
        self._sort_column = col
        self._sort_ascending = asc
        save_sort_state(col, asc)
        self._update_header(self._canvas_w)

    def _on_header_click(self, sort_key: str):
        """Handle a click on a sortable column header."""
        if sort_key == "priority":
            # Priority has 2 modes: reversed (ascending = 0 at top) and default (no sort)
            if self._sort_column == "priority":
                self._sort_column = None
                self._sort_ascending = True
            else:
                self._sort_column = "priority"
                self._sort_ascending = True
        elif self._sort_column == sort_key:
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
        save_sort_state(self._sort_column, self._sort_ascending)
        self._update_header(self._canvas_w)
        self._invalidate_derived_caches()
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

        # For priority sort, also reverse the separator group order so that
        # higher-priority groups (top of list) move to the bottom when ascending.
        if self._sort_column == "priority" and self._sort_ascending:
            # Ascending = low priority first. Root Folder (lowest) goes to top,
            # Overwrite (highest) goes to bottom; real groups are reversed between them.
            # Overwrite's group may contain ungrouped mods — split those out so
            # they reverse with the other groups while OW stays pinned at bottom.
            ow_group  = next(((s, m) for s, m in groups
                              if s is not None and self._entries[s].name == OVERWRITE_NAME), None)
            rf_group  = next(((s, m) for s, m in groups
                              if s is not None and self._entries[s].name == ROOT_FOLDER_NAME), None)
            middle = [(s, m) for s, m in groups if (s, m) != ow_group and (s, m) != rf_group]
            if ow_group is not None and ow_group[1]:
                # Ungrouped mods live in OW's group — promote them to a separator-less
                # group so they participate in the reversal.
                middle.append((None, ow_group[1]))
                ow_group = (ow_group[0], [])
            groups = (([rf_group] if rf_group else [])
                      + list(reversed(middle))
                      + ([ow_group] if ow_group else []))

        result: list[int] = []
        for sep_idx, mod_indices in groups:
            if sep_idx is not None:
                result.append(sep_idx)
            sorted_mods = sorted(mod_indices, key=key_fn, reverse=not self._sort_ascending)
            result.extend(sorted_mods)
        return result

    def _uninvert_entries_order(self):
        """Convert _entries from inverted-visual order back to natural order.

        Inverted visual order (priority ascending) is:
            [Root, lowest-pri-group, ..., highest-pri-group, OW]
        Natural order (what modlist.txt expects) is:
            [OW, highest-pri-group, ..., lowest-pri-group, Root]

        This reverses the group order (excluding pinned OW/Root) and reverses
        mod order within each group (ascending→descending priority).
        """
        # Split current _entries into groups by separator
        groups: list[tuple[int | None, list[int]]] = []
        current_sep: int | None = None
        current_mods: list[int] = []
        for idx in range(len(self._entries)):
            if self._entries[idx].is_separator:
                if current_sep is not None or current_mods:
                    groups.append((current_sep, current_mods))
                current_sep = idx
                current_mods = []
            else:
                current_mods.append(idx)
        if current_sep is not None or current_mods:
            groups.append((current_sep, current_mods))

        # Identify Root Folder and Overwrite groups
        ow_group = None
        rf_group = None
        middle = []
        for g in groups:
            sep_idx, mods = g
            if sep_idx is not None and self._entries[sep_idx].name == OVERWRITE_NAME:
                ow_group = g
            elif sep_idx is not None and self._entries[sep_idx].name == ROOT_FOLDER_NAME:
                rf_group = g
            else:
                middle.append(g)

        # Reverse middle groups and rebuild in natural order:
        # [OW, highest-pri-group, ..., lowest-pri-group, Root]
        new_groups = (([ow_group] if ow_group else [])
                      + list(reversed(middle))
                      + ([rf_group] if rf_group else []))

        # Rebuild _entries and _check_vars in natural order.
        # Within each group, reverse mod order (ascending → descending priority).
        old_entries = list(self._entries)
        old_vars = list(self._check_vars)
        new_entries = []
        new_vars = []
        for sep_idx, mod_indices in new_groups:
            if sep_idx is not None:
                new_entries.append(old_entries[sep_idx])
                new_vars.append(old_vars[sep_idx])
            for mi in reversed(mod_indices):
                new_entries.append(old_entries[mi])
                new_vars.append(old_vars[mi])

        self._entries[:] = new_entries
        self._check_vars[:] = new_vars

    def _sort_key_fn(self):
        """Return a key function for sorting entry indices by the active column."""
        col = self._sort_column

        # Use the cached priorities dict (populated in _redraw).
        priorities = self._priorities

        if col == "name":
            return lambda i: self._entries[i].name.lower()
        elif col == "installed":
            def _installed_key(i):
                dt = self._install_datetimes.get(self._entries[i].name)
                if dt is None:
                    return (1, datetime.min)  # mods without date sort last
                return (0, dt)
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
        elif col == "category":
            return lambda i: (self._category_names.get(self._entries[i].name, "") or "\uffff").lower()
        elif col == "version":
            def _version_key(i):
                v = self._mod_versions.get(self._entries[i].name, "")
                if not v:
                    return (1, ())
                parts: list = []
                for tok in v.replace("-", ".").split("."):
                    try:
                        parts.append((0, int(tok)))
                    except ValueError:
                        parts.append((1, tok.lower()))
                return (0, tuple(parts))
            return _version_key
        else:
            return lambda i: i

    def _on_canvas_resize(self, event):
        w = event.width
        if self._canvas_resize_after_id is not None:
            self.after_cancel(self._canvas_resize_after_id)
        # Use a short after() delay so rapid resize events are coalesced into
        # one redraw rather than firing on every pixel change.
        self._canvas_resize_after_id = self.after(16, lambda: self._apply_canvas_resize(w))

    def _apply_canvas_resize(self, width: int):
        self._canvas_resize_after_id = None
        self._layout_columns(width)
        # Sync overrides with the actual post-resize widths so future drags
        # start from the correct baseline (avoids adjacent columns jumping).
        if self._col_w_override:
            for slot in range(1, 8):
                dc = self._slot_to_data_col(slot)
                if dc in self._col_w_override:
                    self._col_w_override[dc] = self._COL_W[slot]
        self._update_header(width)
        _clear_truncate_cache()
        self._redraw()

    def _schedule_redraw(self) -> None:
        """Coalesce rapid scroll events into a single _redraw() call via after_idle.

        Without this, rapid scroll events fire _redraw() multiple times per
        event-loop cycle.  Because canvas items (canvas-space) and placed widgets
        (widget-space) are updated together inside _redraw(), a partial repaint
        between two consecutive _redraw() calls can show widgets at stale positions
        — the grey-box / misplaced-widget glitch seen when scrolling.
        """
        if self._redraw_after_id is not None:
            self.after_cancel(self._redraw_after_id)
        self._redraw_after_id = self.after_idle(self._redraw)

    def _on_scroll_up(self, _event):
        self._canvas.yview("scroll", -50, "units")
        self._schedule_redraw()

    def _on_scroll_down(self, _event):
        self._canvas.yview("scroll", 50, "units")
        self._schedule_redraw()

    def _on_mousewheel(self, event):
        self._canvas.yview("scroll", -50 if event.delta > 0 else 50, "units")
        self._schedule_redraw()

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

    _DRAG_SCROLL_ZONE = 40     # pixels from edge to trigger auto-scroll
    _DRAG_SCROLL_INTERVAL = 50  # ms between scroll ticks

    def _maybe_start_drag_autoscroll(self):
        """Start or continue auto-scrolling if cursor is near the canvas edge."""
        if self._drag_idx < 0:
            self._cancel_drag_autoscroll()
            return
        h = self._canvas.winfo_height()
        y = self._drag_last_event_y
        zone = self._DRAG_SCROLL_ZONE
        if y < zone:
            speed = max(1, int(6 * (1.0 - y / zone)))
            self._canvas.yview("scroll", -speed, "units")
            self._redraw()
            self._cancel_drag_autoscroll()
            self._drag_scroll_after = self._canvas.after(
                self._DRAG_SCROLL_INTERVAL, self._maybe_start_drag_autoscroll)
        elif y > h - zone:
            speed = max(1, int(6 * (1.0 - (h - y) / zone)))
            self._canvas.yview("scroll", speed, "units")
            self._redraw()
            self._cancel_drag_autoscroll()
            self._drag_scroll_after = self._canvas.after(
                self._DRAG_SCROLL_INTERVAL, self._maybe_start_drag_autoscroll)
        else:
            self._cancel_drag_autoscroll()

    def _cancel_drag_autoscroll(self):
        if self._drag_scroll_after is not None:
            self._canvas.after_cancel(self._drag_scroll_after)
            self._drag_scroll_after = None

    def _on_mouse_press(self, event):
        try:
            self.winfo_toplevel()._last_list_panel = "mod"
        except Exception:
            pass
        if not self._entries:
            return
        # Cancel any previous pending drag
        self._cancel_drag_timer()
        cy = self._event_canvas_y(event)
        idx = self._canvas_y_to_index(cy)
        shift = bool(event.state & 0x1)
        ctrl  = bool(event.state & 0x4)

        # Checkbox hit-test: if the click is in the enable/disable column and the
        # entry is toggleable, handle it immediately and return.  This avoids
        # relying on tag bindings (which don't reliably block the widget-level binding).
        # Bundle variants are toggleable even when locked=True (locked only prevents drag).
        _cb_x_max = self._COL_X[1] - 4  # right edge of checkbox column
        _is_bundle_var = self._entries[idx].bundle_name is not None
        if (event.x <= _cb_x_max
                and not self._entries[idx].is_separator
                and (not self._entries[idx].locked or _is_bundle_var)
                and idx < len(self._check_vars)
                and self._check_vars[idx] is not None):
            var = self._check_vars[idx]
            var.set(not var.get())
            for s, di in enumerate(self._pool_data_idx):
                if di == idx and s < len(self._pool_check_vars):
                    self._pool_check_vars[s].set(var.get())
                    break
            self._checkbox_click_handled = True
            self._on_toggle(idx)
            return

        # Flag icon hit-test: clicking on missing-reqs or update flag opens the
        # corresponding dialog directly.
        entry = self._entries[idx]
        if not entry.is_separator:
            flag_slot = self._col_pos.get(3, 3)
            _FLAG_X = self._COL_X[flag_slot]
            _FLAG_W = self._COL_W[flag_slot]
            if _FLAG_X <= event.x < _FLAG_X + _FLAG_W:
                _FLAG_ICON_SPACING = 18
                _HIT_RADIUS = _FLAG_ICON_SPACING // 2
                has_missing = (entry.name in self._missing_reqs
                               and entry.name not in self._ignored_missing_reqs)
                _items: list[str] = []
                if has_missing:
                    _items.append("missing")
                if entry.locked:
                    _items.append("star")
                if entry.name in self._update_mods:
                    _items.append("update")
                if entry.name in self._endorsed_mods:
                    _items.append("endorsed")
                if entry.name in self._prertx_mods:
                    _items.append("prertx")
                if entry.name in self._excluded_mod_files_map:
                    _items.append("disabled_files")
                _n = len(_items)
                if _n > 0:
                    _group_w = (_n - 1) * _FLAG_ICON_SPACING
                    _fx_start = _FLAG_X + _FLAG_W // 2 - _group_w // 2
                    for _fi, _kind in enumerate(_items):
                        _fx = _fx_start + _fi * _FLAG_ICON_SPACING
                        if abs(event.x - _fx) <= _HIT_RADIUS:
                            if _kind == "missing":
                                dep_names = self._missing_reqs_detail.get(entry.name, [])
                                self._show_missing_reqs(entry.name, dep_names)
                                return
                            elif _kind == "update":
                                self._update_nexus_mod(entry.name)
                                return
                            break

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
                # Ctrl+click on separator: toggle item in/out of selection
                if ctrl:
                    if idx in self._sel_set:
                        self._sel_set.discard(idx)
                        if self._sel_idx == idx:
                            self._sel_idx = next(iter(self._sel_set)) if self._sel_set else -1
                    else:
                        self._sel_set.add(idx)
                        self._sel_idx = idx
                    if self._on_mod_selected_cb is not None:
                        self._on_mod_selected_cb()
                    self._redraw()
                    return
                # Shift+click on separator: extend selection range (in display order)
                if shift and self._sel_idx >= 0:
                    vis = self._visible_indices
                    try:
                        lo_row = vis.index(self._sel_idx)
                        hi_row = vis.index(idx)
                    except ValueError:
                        self._sel_set = {idx}
                        self._sel_idx = idx
                    else:
                        lo_row, hi_row = min(lo_row, hi_row), max(lo_row, hi_row)
                        self._sel_set = set(vis[lo_row : hi_row + 1])
                    if self._on_mod_selected_cb is not None:
                        self._on_mod_selected_cb()
                    self._redraw()
                    return
                # If this separator is part of an existing multi-selection, preserve
                # it so the whole selection drags together (same as non-separator path).
                _sep_is_locked = self._sep_locks.get(self._entries[idx].name, False)
                if idx in self._sel_set and len(self._sel_set) > 1 and not _sep_is_locked:
                    self._activate_drag(idx, cy, False, [])
                    self._redraw()
                    return
                self._sel_idx = idx
                self._sel_set = {idx}
                if self._on_mod_selected_cb is not None:
                    self._on_mod_selected_cb()
                # Regular separators — activate drag immediately
                if self._sep_locks.get(self._entries[idx].name, False):
                    blk = self._sep_block_range(idx)
                    pending_block = [
                        (self._entries[i], self._check_vars[i])
                        for i in blk
                    ]
                    is_block = True
                else:
                    pending_block = []
                    is_block = False
                self._activate_drag(idx, cy, is_block, pending_block)
                self._redraw()
            return

        # Ctrl+click: toggle individual item in/out of selection
        if ctrl:
            if idx in self._sel_set:
                self._sel_set.discard(idx)
                if self._sel_idx == idx:
                    self._sel_idx = next(iter(self._sel_set)) if self._sel_set else -1
            else:
                self._sel_set.add(idx)
                self._sel_idx = idx
            if self._on_mod_selected_cb is not None:
                self._on_mod_selected_cb()
            self._redraw()
            self._update_info()
            return

        # Shift+click: extend selection from anchor to clicked row (in display order)
        if shift and self._sel_idx >= 0:
            vis = self._visible_indices
            try:
                lo_row = vis.index(self._sel_idx)
                hi_row = vis.index(idx)
            except ValueError:
                self._sel_set = {idx}
                self._sel_idx = idx
            else:
                lo_row, hi_row = min(lo_row, hi_row), max(lo_row, hi_row)
                self._sel_set = set(vis[lo_row : hi_row + 1])
            if self._on_mod_selected_cb is not None:
                self._on_mod_selected_cb()
            self._redraw()
            self._update_info()
            return

        # If clicking inside an existing multi-selection, preserve it so the
        # user can drag the whole group — only collapse to single on release.
        _is_immovable = self._entries[idx].locked or self._entries[idx].bundle_name is not None
        if idx in self._sel_set and len(self._sel_set) > 1:
            if not _is_immovable:
                self._activate_drag(idx, cy, False, [])
            return

        self._sel_idx = idx
        self._sel_set = {idx}
        if self._on_mod_selected_cb is not None:
            self._on_mod_selected_cb()
        self._redraw()
        self._update_info()
        if _is_immovable:
            # locked / bundle-variant entries are selectable but not draggable
            self._drag_idx = -1
            self._drag_moved = False
            self._drag_slot  = -1
            return
        # Activate drag immediately
        self._activate_drag(idx, cy, False, [])

    def _activate_drag(self, idx: int, start_y: int, is_block: bool, block: list):
        """Called after the hold delay — officially begin the drag."""
        self._drag_after_id = None
        self._drag_pending = False

        sel_indices: list[int] = []
        # If multiple items are selected and the dragged item is in the selection,
        # treat the whole selection as the drag block (sorted by entry index).
        if len(self._sel_set) > 1 and idx in self._sel_set and not is_block:
            # Filter out locked / bundle-variant entries — they should not be draggable
            sorted_sel = sorted(
                i for i in self._sel_set
                if not self._entries[i].locked and self._entries[i].bundle_name is None
            )
            if not sorted_sel:
                return
            block = [
                (self._entries[i], self._check_vars[i])
                for i in sorted_sel
            ]
            # Remember the actual (possibly sparse) indices for correct removal later
            sel_indices = sorted_sel
            # Anchor the drag at the first selected index
            idx = sorted_sel[0]
            is_block = True

        self._drag_start_y = start_y
        self._drag_moved = False
        self._drag_is_block = is_block
        self._drag_block = block
        self._drag_sel_indices = sel_indices  # empty list = contiguous block (separator drag)

        # When a column sort is active, real-time reordering mutates _entries
        # which causes the sort to recompute and produce unpredictable results.
        # Save a snapshot so each drag event can restore from the original state.
        if self._sort_column is not None:
            self._drag_entries_snapshot = list(self._entries)
            self._drag_vars_snapshot = list(self._check_vars)
            self._drag_saved_sort_column = self._sort_column
            self._drag_saved_sort_ascending = self._sort_ascending
            # For non-priority sorts, clear immediately so the list shows natural
            # order during the drag.  _entries is already in natural order for
            # these sorts (only _visible_indices was reordered), so no physical
            # reorder is needed.  The snapshot is still used by _on_mouse_drag
            # to reset _entries on each event.
            _is_inverted = (self._sort_column == "priority" and self._sort_ascending)
            if not _is_inverted:
                self._sort_column = None
                self._sort_ascending = True
                self._update_header(self._canvas_w)
                self._invalidate_derived_caches()
                self._vis_dirty = True
        else:
            self._drag_entries_snapshot = None
            self._drag_vars_snapshot = None
            self._drag_saved_sort_column = None
            self._drag_saved_sort_ascending = None

        # When priority-ascending (inverted) sort is active, physically reorder
        # _entries to match the visual display order.  This lets the standard
        # non-inverted drag logic work unchanged — visual position == _entries
        # position.  The snapshot above preserves the original order for restore.
        _inverted = (self._sort_column == "priority" and self._sort_ascending)
        if _inverted:
            # Build the FULL inverted order of every entry (not just the
            # filtered/collapsed visible subset).  Dropping non-visible
            # separators and their mods from new_order would persist that
            # loss on save — see the search+collapsed-sep bug.
            #
            # Natural order is:
            #   [OW, highest-pri-group, ..., lowest-pri-group, Root]
            # Inverted order (priority ascending) is:
            #   [Root, lowest-pri-group, ..., highest-pri-group, OW]
            # and mods within each group are reversed.  This mirrors
            # _uninvert_entries_order so release can cleanly invert back.
            groups: list[tuple[int | None, list[int]]] = []
            current_sep: int | None = None
            current_mods: list[int] = []
            for _i in range(len(self._entries)):
                if self._entries[_i].is_separator:
                    if current_sep is not None or current_mods:
                        groups.append((current_sep, current_mods))
                    current_sep = _i
                    current_mods = []
                else:
                    current_mods.append(_i)
            if current_sep is not None or current_mods:
                groups.append((current_sep, current_mods))

            ow_group = None
            rf_group = None
            middle: list[tuple[int | None, list[int]]] = []
            for g in groups:
                _sep_idx, _mods = g
                if _sep_idx is not None and self._entries[_sep_idx].name == OVERWRITE_NAME:
                    ow_group = g
                elif _sep_idx is not None and self._entries[_sep_idx].name == ROOT_FOLDER_NAME:
                    rf_group = g
                else:
                    middle.append(g)

            # Inverted order: Root first, middle reversed, OW last.
            inv_groups = (([rf_group] if rf_group else [])
                          + list(reversed(middle))
                          + ([ow_group] if ow_group else []))

            new_order: list[int] = []
            for _sep_idx, _mods in inv_groups:
                if _sep_idx is not None:
                    new_order.append(_sep_idx)
                # Within each group, reverse mod order so ascending priority
                # (lowest-pri first) matches the visual display.
                new_order.extend(reversed(_mods))

            new_entries = [self._entries[i] for i in new_order]
            new_vars = [self._check_vars[i] for i in new_order]
            self._entries[:] = new_entries
            self._check_vars[:] = new_vars

            # Remap idx, sel_indices, and block references to new positions
            old_to_new = {old: new for new, old in enumerate(new_order)}
            idx = old_to_new[idx]
            if sel_indices:
                sel_indices = sorted(old_to_new[i] for i in sel_indices)
                self._drag_sel_indices = sel_indices
                # Rebuild block with new entries/vars
                block = [(self._entries[i], self._check_vars[i]) for i in sel_indices]
                self._drag_block = block
                idx = sel_indices[0]
            elif is_block and block:
                # Separator block drag — rebuild block in reordered position order
                new_blk_start = idx
                new_blk_size = len(block)
                block = [
                    (self._entries[i], self._check_vars[i])
                    for i in range(new_blk_start, new_blk_start + new_blk_size)
                ]
                self._drag_block = block

            # Clear column sort so _apply_column_sort is a no-op during drag.
            # The physical order now matches the display.
            self._sort_column = None
            self._sort_ascending = True
            self._invalidate_derived_caches()
            self._vis_dirty = True

            # Save a second snapshot of the reordered state.  _on_mouse_drag
            # restores from this on each event so mutations don't cascade.
            # (_drag_entries_snapshot holds the ORIGINAL pre-reorder state for
            # full restore on cancel.)
            self._drag_reordered_snapshot = list(self._entries)
            self._drag_reordered_vars = list(self._check_vars)

        self._drag_idx = idx
        self._drag_origin_idx = idx

        # Compute the starting slot — position of the group's top among non-drag
        # items.  _on_mouse_drag uses delta-based movement from this baseline.
        if self._vis_dirty:
            self._visible_indices = self._compute_visible_indices()
            self._vis_dirty = False
        vis = self._visible_indices
        if sel_indices:
            drag_set = set(sel_indices)
            first_drag = sel_indices[0]
        else:
            blk_size = len(block) if is_block else 1
            drag_set = set(range(idx, idx + blk_size))
            first_drag = idx
        # Count non-drag vis entries before the first dragged entry (in display order).
        # When a non-priority sort was just cleared the entry's natural-order position
        # may be far off-screen.  Anchor the drag slot to the cursor's canvas Y so
        # the entry appears directly under the cursor from the first drag event.
        _non_inverted_sort_cleared = (
            self._drag_saved_sort_column is not None
            and self._drag_reordered_snapshot is None
        )
        if _non_inverted_sort_cleared:
            vis_no_drag_count = sum(1 for ei in vis if ei not in drag_set)
            start_slot = max(0, min(int(start_y / self.ROW_H), vis_no_drag_count))
        else:
            start_slot = 0
            for ei in vis:
                if ei == first_drag:
                    break
                if ei not in drag_set:
                    start_slot += 1
        self._drag_start_slot = start_slot
        self._drag_slot = start_slot

    def _sep_block_range(self, sep_idx: int) -> range:
        """Return the range of indices [sep_idx, end) belonging to this separator block.
        The block is the separator plus every non-separator entry below it
        until the next separator (or end of list).  For bundle separators the
        block ends as soon as a non-bundle-variant entry is encountered so that
        unrelated mods sitting below the bundle are not absorbed.
        Results are cached."""
        cache = getattr(self, "_sep_block_cache", None)
        if cache is not None:
            cached = cache.get(sep_idx)
            if cached is not None:
                return cached
        end = sep_idx + 1
        is_bsep = self._is_bundle_separator(sep_idx)
        if is_bsep:
            bname = self._entries[sep_idx].display_name
            while end < len(self._entries) and not self._entries[end].is_separator:
                if self._entries[end].bundle_name != bname:
                    break
                end += 1
        else:
            while end < len(self._entries) and not self._entries[end].is_separator:
                end += 1
        result = range(sep_idx, end)
        if cache is not None:
            cache[sep_idx] = result
        return result

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

    def _sep_block_has_disabled_plugins(self, sep_idx: int) -> bool:
        """True if this separator's block contains at least one mod with disabled plugins."""
        for i in self._sep_block_range(sep_idx):
            if not self._entries[i].is_separator:
                if self._entries[i].name in self._disabled_plugins_map:
                    return True
        return False

    def _get_mods_with_plugins(self) -> set[str]:
        """Return the set of mod names that have at least one plugin (from plugin panel's filemap)."""
        app = self.winfo_toplevel()
        if hasattr(app, "_plugin_panel"):
            return set(app._plugin_panel._plugin_mod_map.values())
        return set()

    def _sep_block_has_plugins(self, sep_idx: int, mods_with_plugins: set[str]) -> bool:
        """True if this separator's block contains at least one mod with plugins."""
        for i in self._sep_block_range(sep_idx):
            if not self._entries[i].is_separator:
                if self._entries[i].name in mods_with_plugins:
                    return True
        return False

    def _sep_block_has_disabled_files(self, sep_idx: int) -> bool:
        """True if this separator's block contains at least one mod with disabled files."""
        for i in self._sep_block_range(sep_idx):
            if not self._entries[i].is_separator:
                if self._entries[i].name in self._excluded_mod_files_map:
                    return True
        return False

    def _sep_block_has_updates(self, sep_idx: int) -> bool:
        """True if this separator's block contains at least one mod with an update available."""
        for i in self._sep_block_range(sep_idx):
            if not self._entries[i].is_separator:
                if self._entries[i].name in self._update_mods:
                    return True
        return False

    def _sep_block_has_fomod(self, sep_idx: int) -> bool:
        """True if this separator's block contains at least one FOMOD mod."""
        for i in self._sep_block_range(sep_idx):
            if not self._entries[i].is_separator:
                if self._entries[i].name in self._fomod_mods:
                    return True
        return False

    def _sep_block_has_category(self, sep_idx: int, allowed_categories: frozenset[str]) -> bool:
        """True if this separator's block contains at least one mod in the allowed categories."""
        for i in self._sep_block_range(sep_idx):
            if not self._entries[i].is_separator:
                cat = self._category_names.get(self._entries[i].name, "") or ""
                if cat in allowed_categories:
                    return True
        return False

    def _on_mouse_drag(self, event):
        if self._drag_idx < 0 or not self._entries:
            return

        # Auto-scroll near edges (with repeating timer)
        self._drag_last_event_y = event.y
        self._maybe_start_drag_autoscroll()

        cy = self._event_canvas_y(event)
        blk_size = len(self._drag_block) if self._drag_is_block else 1

        # Restore _entries to a clean state before computing the new insertion.
        # For inverted drag, use the reordered snapshot (physical = visual order).
        # For other column sorts, use the original snapshot.
        _reordered = self._drag_reordered_snapshot
        _snap = _reordered if _reordered is not None else self._drag_entries_snapshot
        _snap_vars = (self._drag_reordered_vars
                      if _reordered is not None
                      else self._drag_vars_snapshot)
        _using_snapshot = _snap is not None
        if _using_snapshot:
            self._entries[:] = list(_snap)
            self._check_vars[:] = list(_snap_vars)
            self._drag_idx = self._drag_origin_idx
            if self._drag_sel_indices:
                # Restore original sparse indices
                _block_ids = {id(e) for e, _ in self._drag_block}
                self._drag_sel_indices = sorted(
                    i for i in range(len(self._entries))
                    if id(self._entries[i]) in _block_ids
                )
            self._invalidate_derived_caches()
            self._vis_dirty = True

        if self._vis_dirty:
            self._visible_indices = self._compute_visible_indices()
            self._vis_dirty = False
        vis = self._visible_indices
        if self._drag_sel_indices:
            drag_set = set(self._drag_sel_indices)
        else:
            drag_set = set(range(self._drag_idx, self._drag_idx + blk_size))

        # vis_without_drag is the list of non-dragged visible entries in display order.
        # slot is an index into this list (0 = before first, len = after last).
        vis_without_drag = [i for i in vis if i not in drag_set]

        # Map cursor movement to slot changes using a pixel delta from drag
        # start.  Because real-time reordering moves the block under the cursor
        # on every slot change, position-based mapping doesn't work — the cursor
        # is always inside the block.  Instead, track cumulative pixel movement
        # and convert to row steps.
        dy = cy - self._drag_start_y
        slot = self._drag_start_slot + int(dy / self.ROW_H)
        slot = max(0, min(slot, len(vis_without_drag)))

        if slot == self._drag_slot:
            if _using_snapshot:
                # Snapshot was restored but slot unchanged — still need to
                # re-apply the insertion so _entries reflects the current slot.
                pass
            else:
                return

        self._drag_slot = slot

        # --- Real-time reorder ---
        # Note: inverted priority sort is handled by physically reordering
        # _entries at drag start (_activate_drag), so by this point
        # _sort_column is None and the standard logic works.

        def _entry_name(ei):
            return self._entries[ei].name

        if slot == 0 and len(vis_without_drag) > 0:
            _pre_removal_insert = vis_without_drag[0]
        elif slot >= len(vis_without_drag):
            _pre_removal_insert = len(self._entries)
        else:
            above_ei = vis_without_drag[slot - 1]
            below_ei = vis_without_drag[slot]
            below_is_sep = self._entries[below_ei].is_separator
            if below_is_sep:
                below_name = _entry_name(below_ei)
                if below_name == OVERWRITE_NAME:
                    _pre_removal_insert = below_ei + 1
                else:
                    _pre_removal_insert = below_ei
            else:
                _pre_removal_insert = below_ei

        # Prevent non-bundle mods from being dropped inside a bundle block.
        _pre_removal_insert = self._clamp_outside_bundle_blocks(_pre_removal_insert)

        _drop_insert_at = _pre_removal_insert - sum(1 for d in drag_set if d < _pre_removal_insert)

        if self._drag_is_block:
            if self._drag_sel_indices:
                for i in sorted(self._drag_sel_indices, reverse=True):
                    self._entries.pop(i)
                    self._check_vars.pop(i)
            else:
                del self._entries[self._drag_idx:self._drag_idx + blk_size]
                del self._check_vars[self._drag_idx:self._drag_idx + blk_size]

            insert_at = self._clamp_insert_at(_drop_insert_at, is_block=True)

            for j, (entry, var) in enumerate(self._drag_block):
                self._entries.insert(insert_at + j, entry)
                self._check_vars.insert(insert_at + j, var)
            self._drag_idx = insert_at
            if self._drag_sel_indices:
                self._drag_sel_indices = list(range(insert_at, insert_at + len(self._drag_block)))
                self._sel_set = set(self._drag_sel_indices)
            else:
                self._sel_set = set(range(insert_at, insert_at + len(self._drag_block)))
            self._sel_idx = insert_at
        else:
            entry = self._entries.pop(self._drag_idx)
            var   = self._check_vars.pop(self._drag_idx)

            insert_at = self._clamp_insert_at(_drop_insert_at, is_block=False)

            self._entries.insert(insert_at, entry)
            self._check_vars.insert(insert_at, var)
            self._drag_idx = insert_at
            self._sel_idx  = insert_at
            self._sel_set  = {insert_at}

        self._drag_moved = True
        self._invalidate_derived_caches()
        self._vis_dirty = True
        self._redraw()

    def _on_mouse_release(self, event):
        self._cancel_drag_timer()
        self._cancel_drag_autoscroll()
        had_multi = len(self._sel_set) > 1

        # Restore sort state if it was cleared for inverted drag
        _saved_col = self._drag_saved_sort_column
        _saved_asc = self._drag_saved_sort_ascending

        if self._drag_idx >= 0 and self._drag_moved:
            # Real-time reorder already happened during drag.
            # If the drag was inverted, _entries is in visual (inverted) order
            # and must be converted back to natural order before saving.
            if _saved_col == "priority" and _saved_asc:
                # Remember the dragged entry object(s) before uninverting so we
                # can find their new positions and update the selection.
                _dragged_entry = self._entries[self._drag_idx] if not self._drag_is_block else None
                _dragged_block_entries = (
                    [e for e, _ in self._drag_block] if self._drag_is_block else []
                )
                self._uninvert_entries_order()
                # Update sel_idx/sel_set to the new positions after uninvert
                if _dragged_entry is not None:
                    new_idx = next(
                        (i for i, e in enumerate(self._entries) if e is _dragged_entry), self._sel_idx
                    )
                    self._sel_idx = new_idx
                    self._sel_set = {new_idx}
                    self._drag_idx = new_idx
                elif _dragged_block_entries:
                    _block_set = {id(e) for e in _dragged_block_entries}
                    new_indices = [
                        i for i, e in enumerate(self._entries) if id(e) in _block_set
                    ]
                    if new_indices:
                        self._sel_idx = new_indices[0]
                        # Collapse to single selection so subsequent clicks don't
                        # re-trigger the multi-drag path with stale indices.
                        self._sel_set = {new_indices[0]}
            self._save_modlist()  # always save, regardless of which branch above ran
            if _saved_col is not None:
                # Restore the sort that was active before the drag
                self._sort_column = _saved_col
                self._sort_ascending = _saved_asc
            self._invalidate_derived_caches()
            self._vis_dirty = True
            self._rebuild_filemap()
        elif self._drag_idx >= 0 and not self._drag_moved:
            # No movement — restore original _entries if we reordered for inverted drag
            _snap = self._drag_entries_snapshot
            if _snap is not None:
                self._entries[:] = _snap
                self._check_vars[:] = list(self._drag_vars_snapshot)
            if _saved_col is not None:
                self._sort_column = _saved_col
                self._sort_ascending = _saved_asc
            self._invalidate_derived_caches()
            self._vis_dirty = True
            if had_multi:
                # Click (no drag) inside a multi-selection — collapse to the clicked item.
                # Recompute visible indices first so _canvas_y_to_index is accurate.
                self._visible_indices = self._compute_visible_indices()
                self._vis_dirty = False
                cy = self._event_canvas_y(event)
                clicked = self._canvas_y_to_index(cy)
                # When inverted drag was active, _sel_set may have stale indices
                # from the previous drag — collapse unconditionally.
                if clicked in self._sel_set or _saved_col is not None:
                    self._sel_idx = clicked
                    self._sel_set = {clicked}
                    self._update_info()
        self._drag_idx = -1
        self._drag_origin_idx = -1
        self._drag_moved = False
        self._drag_slot  = -1
        self._drag_is_block = False
        self._drag_sel_indices = []
        self._drag_entries_snapshot = None
        self._drag_vars_snapshot = None
        self._drag_reordered_snapshot = None
        self._drag_reordered_vars = None
        self._drag_saved_sort_column = None
        self._drag_saved_sort_ascending = None
        if _saved_col is not None:
            self._update_header(self._canvas_w)
        self._redraw()
        self._update_info()

    # ------------------------------------------------------------------
    # Tooltip for missing requirements
    # ------------------------------------------------------------------

    def _show_tooltip(self, x: int, y: int, text: str) -> None:
        """Show a tooltip window near the given screen coordinates."""
        if self._tooltip_win is not None and self._tooltip_text == text:
            return
        self._hide_tooltip()
        tw = tk.Toplevel(self)
        tw.withdraw()
        tw.wm_overrideredirect(True)
        tw.configure(bg="#1a1a2e")
        lbl = tk.Label(
            tw, text=text, justify="left",
            bg="#1a1a2e", fg="#ff6b6b",
            font=(_theme.FONT_FAMILY, _theme.FS10), padx=8, pady=4,
            wraplength=350,
        )
        lbl.pack()
        tw.update_idletasks()
        tip_w = tw.winfo_reqwidth()
        tip_x = x - tip_w - 4
        tw.wm_geometry(f"+{tip_x}+{y + 8}")
        tw.deiconify()
        self._tooltip_win = tw
        self._tooltip_text = text

    def _hide_tooltip(self) -> None:
        if self._tooltip_win:
            self._tooltip_win.destroy()
            self._tooltip_win = None
            self._tooltip_text = ""

    def _on_mouse_motion(self, event):
        """Update hover highlight as the mouse moves over the modlist."""
        if not self._entries or self._drag_idx >= 0:
            self._hide_tooltip()
            return
        cy = self._event_canvas_y(event)
        vis = self._visible_indices
        row = cy // self.ROW_H
        new_hover = vis[row] if 0 <= row < len(vis) else -1
        if new_hover != self._hover_idx:
            self._hover_idx = new_hover
            self._redraw()

        # Show tooltip when hovering over the flags column icons.
        # Replicate the same layout logic as _redraw() to find which icon the cursor is over.
        x = event.x
        flag_slot = self._col_pos.get(3, 3)
        flags_col_start = self._COL_X[flag_slot]
        flags_col_end = flags_col_start + self._COL_W[flag_slot]
        if flags_col_start <= x < flags_col_end and 0 <= row < len(vis):
            entry = self._entries[vis[row]]
            if not entry.is_separator:
                # Build the same ordered flag list as _redraw()
                _FLAG_ICON_SPACING = 18
                _HIT_RADIUS = _FLAG_ICON_SPACING // 2
                _flag_tooltips: list[tuple[int, str]] = []  # (center_x, tooltip_text)
                _flag_x_start: int = 0
                _FLAG_X = flags_col_start
                _FLAG_W = self._COL_W[flag_slot]
                has_missing = (entry.name in self._missing_reqs
                               and entry.name not in self._ignored_missing_reqs)
                _items: list[str] = []
                if has_missing:
                    _items.append("missing")
                if entry.locked:
                    _items.append("star")
                if entry.name in self._update_mods:
                    _items.append("update")
                if entry.name in self._endorsed_mods:
                    _items.append("endorsed")
                if entry.name in self._prertx_mods:
                    _items.append("prertx")
                if entry.name in self._excluded_mod_files_map:
                    _items.append("disabled_files")
                _n = len(_items)
                if _n > 0:
                    _group_w = (_n - 1) * _FLAG_ICON_SPACING
                    _fx_start = _FLAG_X + _FLAG_W // 2 - _group_w // 2
                    for _fi, _kind in enumerate(_items):
                        _fx = _fx_start + _fi * _FLAG_ICON_SPACING
                        if _kind == "missing":
                            missing = self._missing_reqs_detail.get(entry.name, [])
                            tip = ("Missing requirements:\n" + "\n".join(f"  - {m}" for m in missing)
                                   if missing else "Missing requirements")
                        elif _kind == "update":
                            tip = "Update available on Nexus Mods"
                        elif _kind == "prertx":
                            tip = "Pre-RTX mod"
                        elif _kind == "disabled_files":
                            tip = "Has disabled files"
                        else:
                            tip = ""
                        if tip:
                            _flag_tooltips.append((_fx, tip))
                    # Find which icon the cursor is closest to (within hit radius)
                    for _fx, tip in _flag_tooltips:
                        if abs(x - _fx) <= _HIT_RADIUS:
                            self._show_tooltip(event.x_root, event.y_root, tip)
                            return
                    # Cursor is in the flags column but not over a specific icon —
                    # keep any existing tooltip rather than flashing it away
                    return
        self._hide_tooltip()

    def _on_mouse_leave(self, event):
        """Clear hover highlight when mouse leaves the canvas."""
        self._hide_tooltip()
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
        plugin_files: list[str] = []
        if self._modlist_path is not None:
            staging_root = self._staging_root
            if not is_sep:
                mod_dir = staging_root / entry.name
                mod_folder = mod_dir
                if mod_dir.is_dir():
                    ini_files = [p for p in sorted(mod_dir.rglob("*.ini"))
                                 if p.name.lower() != "meta.ini"]
                    app = self.winfo_toplevel()
                    plugin_ext: set[str] = set()
                    if hasattr(app, "_plugin_panel"):
                        plugin_ext = {e.lower() for e in app._plugin_panel._plugin_extensions}
                    if plugin_ext:
                        plugin_files = []
                        for p in mod_dir.rglob("*"):
                            try:
                                if p.is_file() and p.suffix.lower() in plugin_ext:
                                    plugin_files.append(p.name)
                            except PermissionError:
                                pass
                        plugin_files.sort()
            elif entry.name == OVERWRITE_NAME:
                mod_folder = staging_root.parent / "overwrite"
            elif entry.name == ROOT_FOLDER_NAME:
                mod_folder = (
                    self._game.get_effective_root_folder_path()
                    if self._game is not None
                    else staging_root.parent / "Root_Folder"
                )

        self._show_context_menu(event.x_root, event.y_root, idx, is_sep, ini_files,
                                mod_folder=mod_folder, plugin_files=plugin_files)

    def _show_context_menu(self, x: int, y: int, idx: int, is_separator: bool,
                           ini_files: list[Path] | None = None,
                           mod_folder: Path | None = None,
                           plugin_files: list[str] | None = None):
        """CTkPopupMenu for mod list context menu. Supports submenus."""
        if self._context_menu is None:
            self._context_menu = CTkPopupMenu(
                self.winfo_toplevel(), width=220, title=""
            )
        menu = self._context_menu
        menu.clear()

        is_overwrite = self._entries[idx].name == OVERWRITE_NAME
        is_root_folder = self._entries[idx].name == ROOT_FOLDER_NAME
        is_synthetic = is_overwrite or is_root_folder
        _is_real_mod = not is_separator and not is_synthetic
        _is_multi = len(self._sel_set) > 1 and idx in self._sel_set
        _is_bundle_sep = is_separator and self._is_bundle_separator(idx)
        _is_bundle_var = (not is_separator) and (self._entries[idx].bundle_name is not None)
        _is_locked = (not is_separator) and self._entries[idx].locked
        _mod_name = self._entries[idx].name

        # Pre-compute multi-selection subsets
        _remove_multi = [
            i for i in sorted(self._sel_set)
            if 0 <= i < len(self._entries)
            and not self._entries[i].is_separator
            and not self._entries[i].locked
            and self._entries[i].name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
        ] if _is_multi else []

        _multi_sel = [
            i for i in sorted(self._sel_set)
            if 0 <= i < len(self._entries)
            and not self._entries[i].is_separator
            and not self._entries[i].locked
            and self._entries[i].name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
            and self._entries[i].bundle_name is None
        ] if _is_multi else []

        sep_names = [e.name for e in self._entries
                     if e.is_separator and e.name != OVERWRITE_NAME
                     and e.name != ROOT_FOLDER_NAME
                     and e.display_name not in self._bundle_groups]

        toggleable = [
            i for i in sorted(self._sel_set)
            if 0 <= i < len(self._entries)
            and not self._entries[i].is_separator
            and not self._entries[i].locked
            and self._entries[i].name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
        ] if len(self._sel_set) > 1 else []

        # Pre-compute profile data
        _other_profiles: list[str] = []
        _copy_mod_name: str | None = None
        _copy_mod_names: list[str] = []
        if _is_real_mod and self._modlist_path is not None and self._game is not None:
            _app = self.winfo_toplevel()
            _topbar = getattr(_app, "_topbar", None)
            _game_name = _topbar._game_var.get() if _topbar else ""
            _cur_profile = self._modlist_path.parent.name
            _all_profiles = _profiles_for_game(_game_name)
            _other_profiles = [p for p in _all_profiles if p != _cur_profile]
            if _other_profiles:
                if _is_multi:
                    _copy_mod_names = [
                        self._entries[i].name
                        for i in sorted(self._sel_set)
                        if 0 <= i < len(self._entries)
                        and not self._entries[i].is_separator
                        and self._entries[i].name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
                    ]
                else:
                    _copy_mod_name = _mod_name

        # Pre-compute conflict status
        conflict_status = CONFLICT_NONE
        if not _is_multi and (not is_separator or is_overwrite):
            conflict_status = (
                self._conflict_map.get(_mod_name, CONFLICT_NONE)
                if not is_overwrite
                else (CONFLICT_WINS if self._overrides.get(OVERWRITE_NAME) else CONFLICT_NONE)
            )

        # Pre-compute Nexus / meta info
        _ctx_meta = None
        nexus_url: str | None = None
        _domain: str | None = None
        _archive_path: Path | None = None
        if _is_real_mod and self._modlist_path is not None:
            _meta_path = self._staging_root / _mod_name / "meta.ini"
            if _meta_path.is_file():
                try:
                    _ctx_meta = read_meta(_meta_path)
                    if _ctx_meta.mod_id > 0:
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
                    # Reinstall Mod — visible when the source archive is still available
                    if not _is_multi and _ctx_meta.installation_file:
                        _xdg = os.environ.get("XDG_DOWNLOAD_DIR")
                        _dl_dir = Path(_xdg) if _xdg else Path.home() / "Downloads"
                        _search_dirs = [_dl_dir, get_download_cache_dir()]
                        try:
                            from gui.download_locations_overlay import load_extra_download_locations
                            _search_dirs.extend(load_extra_download_locations())
                        except Exception:
                            pass
                        _arc = _dl_dir / _ctx_meta.installation_file
                        if not _arc.is_file():
                            for _d in _search_dirs:
                                _cand = Path(_d) / _ctx_meta.installation_file
                                if _cand.is_file():
                                    _arc = _cand
                                    break
                        if _arc.is_file():
                            _archive_path = _arc
                except Exception:
                    _ctx_meta = None

        # Pre-compute multi-select Nexus URLs
        _nexus_urls: list[str] = []
        if toggleable and self._staging_root is not None:
            app = self.winfo_toplevel()
            _cur_game = _GAMES.get(getattr(
                getattr(app, "_topbar", None), "_game_var", tk.StringVar()).get(), None)
            for _ti in toggleable:
                _tname = self._entries[_ti].name
                _tmeta_path = self._staging_root / _tname / "meta.ini"
                if _tmeta_path.is_file():
                    try:
                        _tmeta = read_meta(_tmeta_path)
                        if _tmeta.mod_id > 0:
                            _tdomain = (
                                _cur_game.nexus_game_domain
                                if _cur_game and _cur_game.nexus_game_domain
                                else _tmeta.nexus_page_url.split("/mods/")[0].rsplit("/", 1)[-1]
                                if "/mods/" in _tmeta.nexus_page_url
                                else _tmeta.game_domain
                            )
                            _nexus_urls.append(
                                f"https://www.nexusmods.com/{_tdomain}/mods/{_tmeta.mod_id}"
                            )
                    except Exception:
                        pass

        # --- Menu items in alphabetical order ---

        # Abstain from Endorsement
        if (_is_real_mod and not _is_multi
                and _ctx_meta is not None and _ctx_meta.mod_id > 0 and _ctx_meta.endorsed):
            menu.add_command("Abstain from Endorsement",
                lambda: self._abstain_nexus_mod(_mod_name, _domain, _ctx_meta))

        # Add separator above / Add separator below
        if not _is_multi:
            menu.add_command("Add separator above", lambda: self._add_separator(idx, above=True))
            menu.add_command("Add separator below", lambda: self._add_separator(idx, above=False))

        # Change separator color
        if is_separator and not is_synthetic:
            menu.add_command("Change separator color", lambda: self._change_separator_color(idx))

        # Change Version
        if (_is_real_mod and not _is_multi
                and _ctx_meta is not None and _ctx_meta.mod_id > 0):
            menu.add_command("Change Version",
                lambda mn=_mod_name: self._update_nexus_mod(mn))

        # Check Updates (single)
        if (_is_real_mod and not _is_multi
                and _ctx_meta is not None and _ctx_meta.mod_id > 0):
            menu.add_command("Check Updates",
                lambda mn=_mod_name: self._on_check_updates_for_mods([mn]))

        # Check Updates (multi)
        if _is_multi and _nexus_urls:
            _check_names = [self._entries[i].name for i in toggleable]
            menu.add_command(f"Check Updates ({len(_check_names)})",
                lambda mns=_check_names: self._on_check_updates_for_mods(mns))

        # Copy to profile
        if _other_profiles:
            if _is_multi and _copy_mod_names:
                menu.add_submenu(
                    f"Copy to profile ({len(_copy_mod_names)})",
                    lambda profs=_other_profiles, mns=_copy_mod_names: self._show_copy_to_profile_picker_multi(
                        mns, profs, parent_dismiss=menu._withdraw, parent_popup=menu,
                    ),
                )
            elif not _is_multi and _copy_mod_name:
                menu.add_submenu(
                    "Copy to profile",
                    lambda profs=_other_profiles, mn=_copy_mod_name: self._show_copy_to_profile_picker(
                        mn, profs, parent_dismiss=menu._withdraw, parent_popup=menu,
                    ),
                )

        # Create empty mod below
        if self._modlist_path is not None and not is_synthetic and not _is_multi:
            menu.add_command("Create empty mod below", lambda: self._create_empty_mod(idx))

        # Disable Plugins…
        if not is_separator and not _is_locked and plugin_files and not _is_multi:
            menu.add_command("Disable Plugins…",
                lambda n=_mod_name, pf=plugin_files: self._show_disable_plugins_dialog(n, pf))

        # Disable selected (n) / Enable selected (n)
        if toggleable:
            _count = len(toggleable)
            menu.add_command(f"Disable selected ({_count})",
                lambda: self._disable_selected_mods(toggleable))
            menu.add_command(f"Enable selected ({_count})",
                lambda: self._enable_selected_mods(toggleable))

        # Endorse Mod
        if (_is_real_mod and not _is_multi
                and _ctx_meta is not None and _ctx_meta.mod_id > 0 and not _ctx_meta.endorsed):
            menu.add_command("Endorse Mod",
                lambda: self._endorse_nexus_mod(_mod_name, _domain, _ctx_meta))

        # INI files
        if not is_separator and not _is_locked and ini_files and not _is_multi:
            menu.add_submenu("INI files",
                lambda: self._show_ini_picker(ini_files,
                    parent_dismiss=menu._withdraw, parent_popup=menu))

        # Missing Requirements
        if _is_real_mod and not _is_multi and _mod_name in self._missing_reqs:
            dep_names = self._missing_reqs_detail.get(_mod_name, [])
            menu.add_command("Missing Requirements",
                lambda: self._show_missing_reqs(_mod_name, dep_names))

        # Move to separator
        if not is_separator and not _is_locked and not _is_bundle_var and sep_names:
            if _multi_sel:
                menu.add_submenu(f"Move to separator ({len(_multi_sel)})",
                    lambda ms=_multi_sel: self._show_separator_picker_multi(ms, sep_names,
                        parent_dismiss=menu._withdraw, parent_popup=menu))
            else:
                menu.add_submenu("Move to separator",
                    lambda: self._show_separator_picker(idx, sep_names,
                        parent_dismiss=menu._withdraw, parent_popup=menu))

        # Open folder
        if mod_folder is not None and not _is_multi:
            menu.add_command("Open folder", lambda: self._open_folder(mod_folder))

        # Open on Nexus (single)
        if (_is_real_mod and not _is_multi
                and _ctx_meta is not None and nexus_url):
            menu.add_command("Open on Nexus",
                lambda u=nexus_url: self._open_nexus_page(u))

        # Open on Nexus (multi)
        if _nexus_urls:
            _urls_cap = list(_nexus_urls)
            menu.add_command(f"Open on Nexus ({len(_urls_cap)})",
                lambda u=_urls_cap: self._open_nexus_pages(u))

        # Reinstall Mod
        if (_is_real_mod and not _is_multi
                and _ctx_meta is not None and _archive_path is not None):
            menu.add_command("Reinstall Mod",
                lambda nc=_mod_name, ap=_archive_path: self._reinstall_mod(nc, ap))

        # Remove mod
        if not is_separator and not _is_locked:
            if _remove_multi and self._modlist_path is not None:
                menu.add_command(f"Remove mod ({len(_remove_multi)})",
                    lambda rm=_remove_multi: self._remove_selected_mods(rm))
            else:
                menu.add_command("Remove mod", lambda: self._remove_mod(idx))

        # Remove separator
        if is_separator and not is_synthetic:
            menu.add_command("Remove separator", lambda: self._remove_separator(idx))

        # Rename mod
        if not is_separator and not _is_locked and not _is_bundle_var and not _is_multi:
            menu.add_command("Rename mod", lambda: self._rename_mod(idx))

        # Rename separator
        if is_separator and not is_synthetic and not _is_bundle_sep:
            menu.add_command("Rename separator", lambda: self._rename_separator(idx))

        # Separator settings…
        if is_separator and not is_synthetic and not _is_bundle_sep:
            menu.add_command("Separator settings…", lambda: self._show_sep_settings(idx))

        # Set deployment paths…
        if not is_separator and not _is_locked and mod_folder is not None and not _is_multi:
            menu.add_command("Set deployment paths…",
                lambda: self._show_mod_strip_dialog(_mod_name, mod_folder))

        # Set priority…
        if not is_separator and not _is_locked and not _is_bundle_var and not _is_multi:
            menu.add_command("Set priority…", lambda: self._set_priority(idx))

        # Show Conflicts
        if conflict_status != CONFLICT_NONE:
            menu.add_command("Show Conflicts",
                lambda: self._show_overwrites_dialog(_mod_name))

        menu.popup(x, y)

    def _on_root_folder_toggle(self) -> None:
        self._root_folder_enabled = not self._root_folder_enabled
        self._save_root_folder_state()
        # Update the synthetic entry's enabled state in-place
        for entry in self._entries:
            if entry.name == ROOT_FOLDER_NAME:
                entry.enabled = self._root_folder_enabled
                break
        self._redraw()

    def _on_sep_lock_toggle(self, sep_name: str) -> None:
        self._sep_locks[sep_name] = not self._sep_locks.get(sep_name, False)
        self._save_sep_locks()
        self._redraw()

    def _toggle_collapse(self, sep_name: str) -> None:
        if sep_name in self._collapsed_seps:
            self._collapsed_seps.discard(sep_name)
        else:
            self._collapsed_seps.add(sep_name)
        self._save_collapsed()
        self._update_expand_collapse_all_btn()
        self._invalidate_derived_caches()
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
        self._invalidate_derived_caches()
        self._redraw()

    def _update_enable_disable_all_btn(self) -> None:
        if not getattr(self, "_enable_disable_all_btn", None):
            return
        mod_entries = [e for e in self._entries if not e.is_separator]
        all_enabled = mod_entries and all(e.enabled for e in mod_entries)
        self._enable_disable_all_btn.configure(
            text="Disable all" if all_enabled else "Enable all"
        )

    def _toggle_all_mods_enabled(self) -> None:
        mod_indices = [i for i, e in enumerate(self._entries) if not e.is_separator]
        if not mod_indices:
            return
        all_enabled = all(self._entries[i].enabled for i in mod_indices)
        any_enabled = any(self._entries[i].enabled for i in mod_indices)
        mixed = any_enabled and not all_enabled
        new_state = not all_enabled
        if mixed:
            action = "enable" if new_state else "disable"
            alert = CTkAlert(
                state="warning",
                title="Mixed Mod States",
                body_text=f"Some mods are enabled and some are disabled.\n\nDo you want to {action} all mods?",
                btn1=action.capitalize() + " all",
                btn2="Cancel",
                parent=self.winfo_toplevel(),
            )
            if alert.get() != action.capitalize() + " all":
                return
        for i in mod_indices:
            self._entries[i].enabled = new_state
            if i < len(self._check_vars) and self._check_vars[i] is not None:
                self._check_vars[i].set(new_state)
            self._sync_plugins_for_toggle(self._entries[i].name, new_state)
        self._vis_dirty = True
        self._save_modlist()
        self._rebuild_filemap()
        self._scan_missing_reqs_flags()
        self._update_enable_disable_all_btn()
        self._redraw()
        self._update_info()

    def _remove_plugins_for_mods(self, mod_names: list[str]) -> None:
        """Remove plugins belonging to the given mods from plugins.txt and loadorder.txt."""
        app = self.winfo_toplevel()
        pp = getattr(app, "_plugin_panel", None)
        if pp is None or pp._plugins_path is None:
            return
        plugin_exts = {e.lower() for e in getattr(pp, "_plugin_extensions", [])}
        if not plugin_exts:
            return
        to_remove: set[str] = set()
        for name in mod_names:
            staging = self._staging_root / name
            if staging.is_dir():
                for f in staging.iterdir():
                    if f.is_file() and f.suffix.lower() in plugin_exts:
                        to_remove.add(f.name.lower())
        if not to_remove:
            return
        existing = read_plugins(pp._plugins_path, star_prefix=pp._plugins_star_prefix)
        new_entries = [e for e in existing if e.name.lower() not in to_remove]
        if len(new_entries) < len(existing):
            write_plugins(pp._plugins_path, new_entries, star_prefix=pp._plugins_star_prefix)
        loadorder_path = pp._plugins_path.parent / "loadorder.txt"
        loadorder = read_loadorder(loadorder_path)
        new_lo = [n for n in loadorder if n.lower() not in to_remove]
        if len(new_lo) < len(loadorder):
            write_loadorder(loadorder_path, [PluginEntry(name=n, enabled=True) for n in new_lo])

    def _remove_separator(self, idx: int):
        if 0 <= idx < len(self._entries) and self._entries[idx].is_separator:
            sname = self._entries[idx].name
            self._entries.pop(idx)
            self._check_vars.pop(idx)
            # Clean up lock canvas items for this separator
            if sname in self._lock_cb_rects:
                self._canvas.delete(self._lock_cb_rects.pop(sname))
            if sname in self._lock_cb_marks:
                self._canvas.delete(self._lock_cb_marks.pop(sname))
            self._sep_locks.pop(sname, None)
            self._save_sep_locks()
            if self._sep_colors.pop(sname, None) is not None:
                self._save_sep_colors()
            self._collapsed_seps.discard(sname)
            self._save_collapsed()
            self._update_expand_collapse_all_btn()
            if self._sel_idx == idx:
                self._sel_idx = -1
            elif self._sel_idx > idx:
                self._sel_idx -= 1
            # If this separator had a custom deploy location, restore any
            # backed-up originals for that path immediately.
            _deploy_info = self._sep_deploy_paths.pop(sname, None)
            if _deploy_info:
                self._save_sep_deploy_paths()
                _custom_path_str = _deploy_info.get("path", "")
                if _custom_path_str and self._filemap_path is not None:
                    restore_custom_deploy_backup_for_path(
                        self._filemap_path, Path(_custom_path_str)
                    )
            self._invalidate_derived_caches()
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

        # For bundle variants, remove all siblings together.
        bundle_indices: list[int] = []
        if entry.bundle_name:
            bundle_indices = sorted(
                self._bundle_groups.get(entry.bundle_name, [idx]), reverse=True
            )
            bundle_name = entry.bundle_name
            variant_labels = ", ".join(
                self._entries[i].variant_name or self._entries[i].name
                for i in sorted(bundle_indices)
            )
            alert = CTkAlert(
                state="warning",
                title="Remove Bundle",
                body_text=(
                    f"Remove all variants of bundle '{bundle_name}'?\n\n"
                    f"Variants: {variant_labels}\n\n"
                    "This will delete all variant folders and cannot be undone."
                ),
                btn1="Remove",
                btn2="Cancel",
                parent=self.winfo_toplevel(),
            )
        else:
            bundle_indices = [idx]
            alert = CTkAlert(
                state="warning",
                title="Remove Mod",
                body_text=f"Are you sure you want to remove '{entry.name}'?\n\nThis will delete the mod folder and cannot be undone.",
                btn1="Remove",
                btn2="Cancel",
                parent=self.winfo_toplevel(),
            )
        if alert.get() != "Remove":
            return
        # Delete staging folders and drop from index — process highest index first
        # so earlier indices remain valid while popping.
        removed_names: list[str] = []
        index_path = self._staging_root.parent / "modindex.bin"
        all_names = [self._entries[i].name for i in bundle_indices]
        if self._modlist_path is not None:
            if self._game is not None:
                undeploy_mod_files(
                    all_names,
                    self._game.get_mod_data_path(),
                    self._game.get_game_path(),
                    index_path,
                )
            self._remove_plugins_for_mods(all_names)
            for rem_idx in bundle_indices:  # already sorted high→low
                rem_entry = self._entries[rem_idx]
                staging = self._staging_root / rem_entry.name
                if staging.is_dir():
                    shutil.rmtree(staging)
                removed_names.append(rem_entry.name)
            remove_from_mod_index(index_path, all_names)
        # Remove from lists (highest index first to keep lower indices stable)
        for rem_idx in bundle_indices:
            self._entries.pop(rem_idx)
            self._check_vars.pop(rem_idx)
            if self._sel_idx == rem_idx:
                self._sel_idx = -1
            elif self._sel_idx > rem_idx:
                self._sel_idx -= 1
        self._compute_bundle_groups()
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._scan_missing_reqs_flags()
        self._redraw()
        self._update_info()
        label = entry.bundle_name or entry.name
        _show_mod_notification(self.winfo_toplevel(), f"Removed: {label}", state="warning")

    def _enable_selected_mods(self, indices: list[int]):
        """Enable all mods at the given indices."""
        for i in indices:
            if 0 <= i < len(self._entries):
                self._entries[i].enabled = True
                if i < len(self._check_vars) and self._check_vars[i] is not None:
                    self._check_vars[i].set(True)
        self._vis_dirty = True
        self._save_modlist()
        self._rebuild_filemap()
        self._scan_missing_reqs_flags()
        self._redraw()
        self._update_info()

    def _disable_selected_mods(self, indices: list[int]):
        """Disable all mods at the given indices."""
        for i in indices:
            if 0 <= i < len(self._entries):
                self._entries[i].enabled = False
                if i < len(self._check_vars) and self._check_vars[i] is not None:
                    self._check_vars[i].set(False)
        self._vis_dirty = True
        self._save_modlist()
        self._rebuild_filemap()
        self._scan_missing_reqs_flags()
        self._redraw()
        self._update_info()

    def _remove_selected_mods(self, indices: list[int]):
        """Remove multiple mods at once (with confirmation)."""
        names = [self._entries[i].name for i in indices
                 if 0 <= i < len(self._entries)]
        if not names:
            return
        alert = CTkAlert(
            state="warning",
            title="Remove Mods",
            body_text=f"Are you sure you want to remove {len(names)} selected mod(s)?\n\nThis will delete the mod folders and cannot be undone.",
            btn1="Remove",
            btn2="Cancel",
            parent=self.winfo_toplevel(),
        )
        if alert.get() != "Remove":
            return
        staging_root = None
        index_path = None
        if self._modlist_path is not None:
            staging_root = self._staging_root
            index_path = self._staging_root.parent / "modindex.bin"
        removed_names: list[str] = []
        # Remove from highest index first to avoid shifting
        for i in sorted(indices, reverse=True):
            if not (0 <= i < len(self._entries)):
                continue
            entry = self._entries[i]
            if entry.is_separator:
                continue
            if staging_root is not None:
                removed_names.append(entry.name)
            self._entries.pop(i)
            self._check_vars.pop(i)
        if index_path is not None and removed_names:
            # Remove deployed files from the game directory before deleting the
            # staging folders so restore_data_core() doesn't misidentify the
            # leftover hardlinks/copies as runtime-generated files.
            if self._game is not None:
                undeploy_mod_files(
                    removed_names,
                    self._game.get_mod_data_path(),
                    self._game.get_game_path(),
                    index_path,
                )
            self._remove_plugins_for_mods(removed_names)
            # Now delete staging folders and update the index.
            for name in removed_names:
                staging = staging_root / name
                if staging.is_dir():
                    shutil.rmtree(staging)
            remove_from_mod_index(index_path, removed_names)
        self._sel_idx = -1
        self._sel_set = set()
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._scan_missing_reqs_flags()
        self._redraw()
        self._update_info()
        if removed_names:
            if len(removed_names) == 1:
                msg = f"Removed: {removed_names[0]}"
            else:
                msg = f"Removed {len(removed_names)} mods"
            _show_mod_notification(self.winfo_toplevel(), msg, state="warning")

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
            staging_root = self._staging_root
            old_folder = staging_root / entry.name
            new_folder = staging_root / new_name
            if old_folder.is_dir():
                if new_folder.exists():
                    show_error(
                        "Rename Failed",
                        f"A mod named '{new_name}' already exists.",
                        parent=top,
                    )
                    return
                old_folder.rename(new_folder)
        # Update entry in memory
        old_name = entry.name
        entry.name = new_name
        self._migrate_mod_name_state(old_name, new_name)
        self._vis_dirty = True  # name change affects text filter
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _migrate_mod_name_state(self, old_name: str, new_name: str) -> None:
        """Move all name-keyed state from *old_name* to *new_name*.

        Called by the two rename paths (_rename_mod / rename_mod_by_name).
        Covers:
          * modindex.bin            — so build_filemap finds the mod's files
          * transient in-memory sets/dicts used by the renderer
          * disk-backed per-mod state (strip prefixes, disabled plugins,
            excluded mod files) — each must be re-persisted because the
            on-disk keys are still the old name.

        Conflict tracking dicts (_conflict_map / _overrides / _overridden_by)
        are intentionally skipped; _rebuild_filemap() rewrites them from
        scratch on its next run.
        """
        # 1. modindex.bin — keyed by modlist name, not folder name
        if self._staging_root is not None:
            rename_in_mod_index(
                self._staging_root.parent / "modindex.bin",
                old_name, new_name,
                normalize_folder_case=self._normalize_folder_case,
            )

        # 2. Transient in-memory flags (no disk representation of their own;
        # rebuilt on reload, so just migrate the current snapshot).
        for s in (self._update_mods, self._missing_reqs, self._ignored_missing_reqs,
                  self._endorsed_mods, self._prertx_mods, self._fomod_mods):
            if old_name in s:
                s.discard(old_name)
                s.add(new_name)
        for d in (self._missing_reqs_detail, self._install_dates,
                  self._install_datetimes, self._category_names, self._mod_versions):
            if old_name in d:
                d[new_name] = d.pop(old_name)

        # 3. Disk-backed per-mod state — migrate and re-persist.
        profile_dir = self._modlist_path.parent if self._modlist_path is not None else None

        if old_name in self._mod_strip_prefixes:
            self._mod_strip_prefixes[new_name] = self._mod_strip_prefixes.pop(old_name)
            if profile_dir is not None:
                try:
                    write_mod_strip_prefixes(profile_dir, self._mod_strip_prefixes)
                except Exception as e:
                    self._log(f"Rename: failed to persist strip prefixes: {e}")

        if old_name in self._disabled_plugins_map:
            self._disabled_plugins_map[new_name] = self._disabled_plugins_map.pop(old_name)
            if profile_dir is not None:
                try:
                    write_disabled_plugins(profile_dir, self._disabled_plugins_map)
                except Exception as e:
                    self._log(f"Rename: failed to persist disabled plugins: {e}")

        if old_name in self._excluded_mod_files_map:
            self._excluded_mod_files_map[new_name] = self._excluded_mod_files_map.pop(old_name)
            if profile_dir is not None:
                try:
                    write_excluded_mod_files(profile_dir, self._excluded_mod_files_map)
                except Exception as e:
                    self._log(f"Rename: failed to persist excluded mod files: {e}")

    def rename_mod_by_name(self, old_name: str, new_name: str) -> bool:
        """Rename a mod by name (on disk + in-memory entry + persisted modlist).
        Returns True on success. Used by the post-install rename prompt so it
        doesn't need to know the mod's index in the list.
        """
        if not old_name or not new_name or old_name == new_name:
            return False
        # Locate the entry by name.
        idx = -1
        for i, e in enumerate(self._entries):
            if not e.is_separator and e.name == old_name:
                idx = i
                break
        if idx < 0:
            return False
        if self._modlist_path is None:
            return False
        staging_root = self._staging_root
        old_folder = staging_root / old_name
        new_folder = staging_root / new_name
        if new_folder.exists():
            return False
        if old_folder.is_dir():
            try:
                old_folder.rename(new_folder)
            except OSError:
                return False
        entry = self._entries[idx]
        entry.name = new_name
        self._migrate_mod_name_state(old_name, new_name)
        self._vis_dirty = True
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()
        return True

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
        if old_name in self._sep_colors:
            self._sep_colors[new_name] = self._sep_colors.pop(old_name)
            self._save_sep_colors()
        if old_name in self._lock_cb_rects:
            self._lock_cb_rects[new_name] = self._lock_cb_rects.pop(old_name)
        if old_name in self._lock_cb_marks:
            self._lock_cb_marks[new_name] = self._lock_cb_marks.pop(old_name)
        entry.name = new_name
        self._save_modlist()
        self._redraw()

    def _change_separator_color(self, idx: int) -> None:
        if not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        if not entry.is_separator:
            return
        current = self._sep_colors.get(entry.name) or None
        app = self.winfo_toplevel()
        show_fn = getattr(app, "show_sep_color_panel", None)
        if show_fn:
            def _on_result(hex_color, reset):
                if reset:
                    self._sep_colors.pop(entry.name, None)
                    self._save_sep_colors()
                    self._redraw()
                elif hex_color is not None:
                    self._sep_colors[entry.name] = hex_color
                    self._save_sep_colors()
                    self._redraw()
            show_fn(entry.name, current, _on_result)

    def _show_sep_settings(self, idx: int) -> None:
        """Open the separator settings panel (overlays the plugin panel)."""
        if not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        if not entry.is_separator:
            return
        sep_name = entry.name
        _info = self._sep_deploy_paths.get(sep_name, {})
        current_path = _info.get("path", "") if isinstance(_info, dict) else ""
        current_raw = _info.get("raw", False) if isinstance(_info, dict) else False

        app = self.winfo_toplevel()
        show_fn = getattr(app, "show_sep_settings_panel", None)
        if show_fn:
            def _on_save(val: str, raw: bool):
                if val or raw:
                    self._sep_deploy_paths[sep_name] = {"path": val, "raw": raw}
                else:
                    self._sep_deploy_paths.pop(sep_name, None)
                self._save_sep_deploy_paths()
                self._redraw()
            show_fn(sep_name, current_path, _on_save, current_raw=current_raw)
            return

        # Fallback: plain tk overlay on self (uses portal_filechooser for Browse)
        self._close_sep_settings()
        overlay = tk.Frame(self, bg=BG_PANEL, bd=0, highlightthickness=0)
        path_var = tk.StringVar(value=current_path)

        title_bar = tk.Frame(overlay, bg=BG_HEADER, height=scaled(36))
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        tk.Label(title_bar, text=f"Separator Settings \u2014 {sep_name}",
                 bg=BG_HEADER, fg=TEXT_MAIN, font=_theme.FONT_BOLD, anchor="w",
        ).pack(side="left", padx=12, pady=8)

        content = tk.Frame(overlay, bg=BG_PANEL)
        content.pack(fill="both", expand=True, padx=16, pady=16)
        tk.Label(content, text="Deployment Location", bg=BG_PANEL, fg=TEXT_SEP,
                 font=_theme.FONT_SMALL, anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        entry_w = tk.Entry(content, textvariable=path_var, bg="#1e1e1e", fg=TEXT_MAIN,
                           insertbackground=TEXT_MAIN, relief="flat", font=_theme.FONT_SMALL)
        entry_w.grid(row=1, column=0, sticky="ew", padx=(0, 6))

        def _browse():
            from Utils.portal_filechooser import pick_folder
            def _cb(chosen):
                if chosen is not None:
                    def _apply():
                        path_var.set(str(chosen))
                    overlay.after(0, _apply)
            pick_folder("Select deployment directory", _cb)

        tk.Button(content, text="Browse", command=_browse, bg=BG_HEADER, fg=TEXT_MAIN,
                  relief="flat", font=_theme.FONT_SMALL, cursor="hand2",
        ).grid(row=1, column=1, padx=(0, 4))
        tk.Button(content, text="Clear", command=lambda: path_var.set(""), bg=BG_HEADER,
                  fg=TEXT_MAIN, relief="flat", font=_theme.FONT_SMALL, cursor="hand2",
        ).grid(row=1, column=2)
        content.columnconfigure(0, weight=1)
        tk.Label(content, text="Leave blank to use the game\u2019s default deployment directory.",
                 bg=BG_PANEL, fg=TEXT_SEP, font=_theme.FONT_SMALL, anchor="w",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        btn_row = tk.Frame(overlay, bg=BG_PANEL)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=12)

        def _save():
            val = path_var.get().strip()
            if val:
                self._sep_deploy_paths[sep_name] = {"path": val, "raw": False}
            else:
                self._sep_deploy_paths.pop(sep_name, None)
            self._save_sep_deploy_paths()
            self._redraw()
            self._close_sep_settings()

        tk.Button(btn_row, text="Save", command=_save, bg="#4a4a8a", fg=TEXT_MAIN,
                  relief="flat", font=_theme.FONT_SMALL, cursor="hand2", width=10,
        ).pack(side="right", padx=(6, 0))
        tk.Button(btn_row, text="Cancel", command=self._close_sep_settings, bg=BG_HEADER,
                  fg=TEXT_MAIN, relief="flat", font=_theme.FONT_SMALL, cursor="hand2", width=10,
        ).pack(side="right")

        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._sep_settings_overlay = overlay
        entry_w.focus_set()

    def _close_sep_settings(self) -> None:
        panel = getattr(self, "_sep_settings_overlay", None)
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
            self._sep_settings_overlay = None

    def _show_picker_popup(self, items: list, displays: list[str],
                           on_pick, parent_dismiss=None,
                           parent_popup=None) -> tk.Toplevel:
        """Generic scrollable picker popup. *items* and *displays* are parallel lists.
        *on_pick(item)* is called when the user clicks an entry.
        Returns the popup widget."""
        popup = tk.Toplevel(self._canvas)
        popup.wm_withdraw()
        popup.wm_overrideredirect(True)
        popup.configure(bg=BORDER)
        cx, cy = popup.winfo_pointerxy()

        _alive = [True]

        def _pick_item(item):
            if _alive[0]:
                _alive[0] = False
                popup.destroy()
                if parent_dismiss:
                    parent_dismiss()
                on_pick(item)

        ROW_H    = scaled(30)
        MAX_ROWS = 20
        FONT     = (_theme.FONT_FAMILY, 11)
        PAD_X    = scaled(24)

        fnt = tkfont.Font(font=FONT)
        max_text_w = max((fnt.measure(d) for d in displays), default=100)
        popup_w = max_text_w + PAD_X * 2

        needs_scroll = len(items) > MAX_ROWS
        visible_rows = min(len(items), MAX_ROWS)
        popup_h      = visible_rows * ROW_H

        outer = tk.Frame(popup, bg=BORDER, bd=0)
        outer.pack(padx=1, pady=1)

        if needs_scroll:
            canvas = tk.Canvas(outer, bg=BG_PANEL, bd=0, highlightthickness=0,
                               width=popup_w, height=popup_h)
            vsb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                               bg=BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
                               highlightthickness=0, bd=0)
            canvas.configure(yscrollcommand=vsb.set)
            canvas.pack(side="left", fill="both", expand=True)
            vsb.pack(side="right", fill="y")
            inner = tk.Frame(canvas, bg=BG_PANEL, bd=0)
            canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")

            def _on_inner_resize(e):
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.itemconfigure(canvas_window, width=canvas.winfo_width())
            inner.bind("<Configure>", _on_inner_resize)

            def _on_wheel(evt):
                if getattr(evt, "delta", 0) > 0:
                    canvas.yview_scroll(-3, "units")
                else:
                    canvas.yview_scroll(3, "units")

            def _bind_scroll(widget):
                widget.bind("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
                widget.bind("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))
                widget.bind("<MouseWheel>", _on_wheel)

            for w in (canvas, vsb, inner, outer, popup):
                _bind_scroll(w)
        else:
            inner = tk.Frame(outer, bg=BG_PANEL, bd=0, width=popup_w)
            inner.pack(fill="both", expand=True)
            _bind_scroll = None

        for item, display in zip(items, displays):
            btn = tk.Label(
                inner, text=display, anchor="w",
                bg=BG_PANEL, fg=TEXT_MAIN,
                font=FONT,
                padx=12, pady=5, cursor="hand2",
                width=0,
            )
            btn.pack(fill="x")
            btn.bind("<ButtonRelease-1>", lambda _e, it=item: _pick_item(it))
            btn.bind("<Enter>", lambda _e, b=btn: b.configure(bg=BG_SELECT))
            btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=BG_PANEL))
            if _bind_scroll:
                _bind_scroll(btn)

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
        px = min(px, app_right - pw)
        py = min(py, app_bottom - ph)
        px = max(px, 0)
        py = max(py, 0)
        popup.wm_geometry(f"+{px}+{py}")
        popup.wm_deiconify()

        return popup

    def _show_separator_picker(self, mod_idx: int, sep_names: list[str],
                               parent_dismiss=None,
                               parent_popup=None) -> tk.Toplevel:
        """Show a popup listing all separators; clicking one moves the mod below it."""
        displays = [
            name[:-len("_separator")] if name.endswith("_separator") else name
            for name in sep_names
        ]
        return self._show_picker_popup(
            sep_names, displays,
            on_pick=lambda sep_name: self._move_to_separator(mod_idx, sep_name),
            parent_dismiss=parent_dismiss, parent_popup=parent_popup,
        )

    def _show_copy_to_profile_picker(self, mod_name: str, profiles: list[str],
                                     parent_dismiss=None,
                                     parent_popup=None) -> tk.Toplevel:
        """Show a popup listing other profiles; clicking one copies the mod there."""
        return self._show_picker_popup(
            profiles, profiles,
            on_pick=lambda profile: self._copy_mod_to_profile(mod_name, profile),
            parent_dismiss=parent_dismiss, parent_popup=parent_popup,
        )

    def _copy_mod_to_profile(self, mod_name: str, target_profile: str) -> None:
        """Copy a mod's staging folder to another profile's staging folder."""
        if self._game is None or self._modlist_path is None:
            return
        src_folder = self._staging_root / mod_name
        if not src_folder.is_dir():
            show_error("Error", f"Mod folder not found:\n{src_folder}", parent=self.winfo_toplevel())
            return

        # Determine the target staging root
        game = self._game
        profile_root = game.get_profile_root()
        target_profile_dir = profile_root / "profiles" / target_profile
        from gui.game_helpers import profile_uses_specific_mods
        if profile_uses_specific_mods(target_profile_dir):
            target_staging = target_profile_dir / "mods"
        else:
            target_staging = game.get_mod_staging_path()

        dest_folder = target_staging / mod_name

        if dest_folder.exists():
            dlg = _ReplaceModDialog(self.winfo_toplevel(), mod_name)
            self.wait_window(dlg)
            if dlg.result == "cancel":
                return
            if dlg.result == "rename":
                new_name = dlg.new_name
                if not new_name:
                    return
                dest_folder = target_staging / new_name
            elif dlg.result == "all":
                def _force_remove(func, path, _exc):
                    os.chmod(path, 0o700)
                    func(path)
                shutil.rmtree(dest_folder, onexc=_force_remove)

        def _do_copy():
            try:
                shutil.copytree(str(src_folder), str(dest_folder))
                self.after(0, lambda: self._log(
                    f"Copied '{mod_name}' → profile '{target_profile}'"))
            except Exception as exc:
                self.after(0, lambda e=exc: show_error(
                    "Copy Failed", f"Failed to copy mod:\n{e}", parent=self.winfo_toplevel()))

        threading.Thread(target=_do_copy, daemon=True).start()

    def _show_copy_to_profile_picker_multi(self, mod_names: list[str], profiles: list[str],
                                          parent_dismiss=None,
                                          parent_popup=None) -> tk.Toplevel:
        """Show a popup listing other profiles; clicking one copies all mods there."""
        return self._show_picker_popup(
            profiles, profiles,
            on_pick=lambda profile: self._copy_mods_to_profile(mod_names, profile),
            parent_dismiss=parent_dismiss, parent_popup=parent_popup,
        )

    def _copy_mods_to_profile(self, mod_names: list[str], target_profile: str) -> None:
        """Copy multiple mods' staging folders to another profile's staging folder."""
        if self._game is None or self._modlist_path is None:
            return
        game = self._game
        profile_root = game.get_profile_root()
        target_profile_dir = profile_root / "profiles" / target_profile
        from gui.game_helpers import profile_uses_specific_mods
        if profile_uses_specific_mods(target_profile_dir):
            target_staging = target_profile_dir / "mods"
        else:
            target_staging = game.get_mod_staging_path()

        # Pre-check which destinations already exist and confirm once.
        existing = [m for m in mod_names if (target_staging / m).exists()]
        replace_existing = False
        if existing:
            replace_existing = ask_yes_no(
                self.winfo_toplevel(),
                f"{len(existing)} mod(s) already exist in profile '{target_profile}':\n\n"
                + "\n".join(existing[:10])
                + ("\n…" if len(existing) > 10 else "")
                + "\n\nReplace them? (No = skip existing)",
                title="Mods Exist",
            )

        def _do_copy():
            copied, skipped = 0, 0
            for mod_name in mod_names:
                src_folder = self._staging_root / mod_name
                if not src_folder.is_dir():
                    skipped += 1
                    continue
                dest_folder = target_staging / mod_name
                try:
                    if dest_folder.exists():
                        if not replace_existing:
                            skipped += 1
                            continue
                        def _force_remove(func, path, _exc):
                            os.chmod(path, 0o700)
                            func(path)
                        shutil.rmtree(dest_folder, onexc=_force_remove)
                    shutil.copytree(str(src_folder), str(dest_folder))
                    copied += 1
                except Exception as exc:
                    self.after(0, lambda e=exc, n=mod_name: show_error(
                        "Copy Failed", f"Failed to copy '{n}':\n{e}", parent=self.winfo_toplevel()))
                    return
            self.after(0, lambda c=copied, s=skipped: self._log(
                f"Copied {c} mod(s) → profile '{target_profile}'"
                + (f" ({s} skipped)" if s else "")))

        threading.Thread(target=_do_copy, daemon=True).start()

    def _show_separator_picker_multi(self, indices: list[int], sep_names: list[str],
                                     parent_dismiss=None,
                                     parent_popup=None) -> tk.Toplevel:
        """Like _show_separator_picker but moves all mods at `indices` to the chosen separator."""
        displays = [
            name[:-len("_separator")] if name.endswith("_separator") else name
            for name in sep_names
        ]
        return self._show_picker_popup(
            sep_names, displays,
            on_pick=lambda sep_name: self._move_selected_to_separator(indices, sep_name),
            parent_dismiss=parent_dismiss, parent_popup=parent_popup,
        )

    def _open_nexus_pages(self, urls: list[str]) -> None:
        """Open multiple Nexus Mods pages, one tab per mod."""
        for url in urls:
            self._open_nexus_page(url)

    def _show_disable_plugins_dialog(self, mod_name: str, plugin_files: list[str]) -> None:
        """Open the Disable Plugins panel/dialog for a mod and save results."""
        if self._modlist_path is None:
            return
        profile_dir = self._modlist_path.parent
        all_disabled = read_disabled_plugins(profile_dir, self.__profile_state)
        currently_disabled = set(all_disabled.get(mod_name, []))

        app = self.winfo_toplevel()
        show_fn = getattr(app, "show_disable_plugins_panel", None)
        if show_fn:
            def _on_panel_done(panel):
                if panel.result is None:
                    return
                self._finish_disable_plugins(mod_name, panel.result, currently_disabled,
                                             profile_dir, all_disabled)
            show_fn(mod_name, plugin_files, currently_disabled, _on_panel_done)
        else:
            dlg = _DisablePluginsDialog(
                self.winfo_toplevel(),
                mod_name=mod_name,
                plugin_names=plugin_files,
                disabled=currently_disabled,
            )
            self.wait_window(dlg)
            if dlg.result is None:
                return
            self._finish_disable_plugins(mod_name, dlg.result, currently_disabled,
                                         profile_dir, all_disabled)

    def _finish_disable_plugins(self, mod_name, result, currently_disabled,
                                profile_dir, all_disabled):
        """Persist disable-plugins result and update plugins.txt immediately."""
        if result:
            all_disabled[mod_name] = sorted(result)
        else:
            all_disabled.pop(mod_name, None)

        # Compute which plugins for this mod were just re-enabled vs newly disabled
        newly_disabled = result - currently_disabled
        newly_enabled  = currently_disabled - result

        write_disabled_plugins(profile_dir, all_disabled)
        self._disabled_plugins_map = all_disabled
        # Keep snapshot in sync so reads that use __profile_state see all mods' disables
        self.__profile_state = read_profile_state(profile_dir)

        # Immediately update plugins.txt and refresh the panel without waiting
        # for the async filemap rebuild to complete.
        app = self.winfo_toplevel()
        if hasattr(app, "_plugin_panel"):
            pp = app._plugin_panel
            if pp._plugins_path is not None:
                changed = False
                existing = read_plugins(pp._plugins_path, star_prefix=pp._plugins_star_prefix)

                loadorder_path = pp._plugins_path.parent / "loadorder.txt"
                loadorder = read_loadorder(loadorder_path)
                lo_changed = False

                # Remove newly-disabled plugins from both plugins.txt and loadorder.txt
                if newly_disabled:
                    disabled_lower = {n.lower() for n in newly_disabled}
                    kept = [e for e in existing if e.name.lower() not in disabled_lower]
                    if len(kept) < len(existing):
                        existing = kept
                        changed = True
                    new_lo = [n for n in loadorder if n.lower() not in disabled_lower]
                    if len(new_lo) < len(loadorder):
                        loadorder = new_lo
                        lo_changed = True

                # Append newly re-enabled plugins to bottom of plugins.txt and loadorder.txt
                if newly_enabled:
                    existing_lower = {e.name.lower() for e in existing}
                    loadorder_lower = {n.lower() for n in loadorder}
                    for name in sorted(newly_enabled):
                        if name.lower() not in existing_lower:
                            existing.append(PluginEntry(name=name, enabled=True))
                            existing_lower.add(name.lower())
                            changed = True
                        if name.lower() not in loadorder_lower:
                            loadorder.append(name)
                            loadorder_lower.add(name.lower())
                            lo_changed = True

                if lo_changed:
                    write_loadorder(loadorder_path, [PluginEntry(name=n, enabled=True) for n in loadorder])

                if changed:
                    write_plugins(pp._plugins_path, existing, star_prefix=pp._plugins_star_prefix)
                    pp._refresh_plugins_tab()

        self._rebuild_filemap()

    def _show_ini_picker(self, ini_files: list[Path],
                         parent_dismiss=None,
                         parent_popup=None) -> tk.Toplevel:
        """Show a submenu listing all INI files; clicking one opens it."""
        displays = [f"Open {p.name}" for p in ini_files]
        return self._show_picker_popup(
            ini_files, displays,
            on_pick=lambda ini_path: self._open_ini(ini_path),
            parent_dismiss=parent_dismiss, parent_popup=parent_popup,
        )

    def _show_mod_strip_dialog(self, mod_name: str, mod_folder: Path) -> None:
        """Open a dialog to set which folders (at any depth) to ignore during deployment.
        Checked folders are stripped so their contents deploy one level up."""
        if not mod_folder.is_dir():
            return

        self._load_mod_strip_prefixes()
        current = self._mod_strip_prefixes.get(mod_name, [])
        use_path_format = any("/" in p for p in current)

        app = self.winfo_toplevel()
        show_fn = getattr(app, "show_deploy_paths_panel", None)
        if show_fn:
            def _on_save(chosen):
                self._mod_strip_prefixes[mod_name] = chosen
                self._save_mod_strip_prefixes()
                self._reload()
                plugin_panel = getattr(app, "_plugin_panel", None)
                if plugin_panel is not None:
                    plugin_panel.show_mod_files(mod_name)
            show_fn(mod_name, mod_folder, current, use_path_format, _on_save)
            return

        # ---- fallback: original Toplevel implementation ----
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
            bg=BG_PANEL, fg=TEXT_MAIN, font=_theme.FONT_SMALL,
            justify="left",
        )
        msg.pack(anchor="w", padx=12, pady=(12, 8))

        # current and use_path_format already loaded by the caller above
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
                        font=("Cantarell", _theme.FS10),
                        bordercolor=BG_ROW, borderwidth=1,
                        focuscolor=_tree_bg)
        style.configure(_heading_style,
                        background=BG_HEADER, foreground=TEXT_SEP,
                        font=("Cantarell", _theme.FS10), borderwidth=0)
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
            win.destroy()
            self._reload()
            plugin_panel = getattr(app, "_plugin_panel", None)
            if plugin_panel is not None:
                plugin_panel.show_mod_files(mod_name)

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
                font=_theme.FONT_SMALL, relief="flat", overrelief="flat",
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
        saved_col, saved_asc = self._clear_sort()

        # Pull the mod out
        entry = self._entries.pop(mod_idx)
        var   = self._check_vars.pop(mod_idx)

        # Recalculate sep_idx after removal
        if mod_idx < sep_idx:
            sep_idx -= 1

        # Insert directly below the separator
        dest = sep_idx + 1
        self._entries.insert(dest, entry)
        self._check_vars.insert(dest, var)

        self._sel_idx = dest
        self._restore_sort(saved_col, saved_asc)
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _move_selected_to_separator(self, indices: list[int], sep_name: str):
        """Move all mods at the given indices to directly below the named separator, grouped together."""
        # Snapshot the entries to move (in list order)
        to_move = []
        for i in sorted(indices):
            if 0 <= i < len(self._entries):
                to_move.append((self._entries[i], self._check_vars[i]))
        if not to_move:
            return
        saved_col, saved_asc = self._clear_sort()

        # Remove them from the list (highest index first to preserve lower indices)
        for i in sorted(indices, reverse=True):
            if 0 <= i < len(self._entries):
                self._entries.pop(i)
                self._check_vars.pop(i)

        # Find separator after removals
        sep_idx = next(
            (i for i, e in enumerate(self._entries)
             if e.is_separator and e.name == sep_name),
            None,
        )
        if sep_idx is None:
            # Separator was removed (shouldn't happen), put items back at end
            for entry, var in to_move:
                self._entries.append(entry)
                self._check_vars.append(var)
            self._restore_sort(saved_col, saved_asc)
            self._invalidate_derived_caches()
            self._save_modlist()
            self._rebuild_filemap()
            self._redraw()
            self._update_info()
            return

        # Insert all mods directly below the separator, preserving their relative order
        dest = sep_idx + 1
        for j, (entry, var) in enumerate(to_move):
            self._entries.insert(dest + j, entry)
            self._check_vars.insert(dest + j, var)

        # Update selection to the moved block
        self._sel_set = set(range(dest, dest + len(to_move)))
        self._sel_idx = dest
        self._restore_sort(saved_col, saved_asc)
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _open_ini(self, path: Path):
        """Open an .ini file in the user's default text editor via xdg-open."""
        try:
            xdg_open(path)
            self._log(f"Opened: {path.name}")
        except Exception as e:
            self._log(f"Could not open {path.name}: {e}")

    def _open_folder(self, path: Path) -> None:
        """Open a directory in the system file manager via xdg-open."""
        if not path.is_dir():
            self._log(f"Folder not found: {path}")
            return
        try:
            xdg_open(path)
        except Exception as e:
            self._log(f"Could not open folder: {e}")

    def _on_middle_click(self, event) -> None:
        """Middle-click: open the hovered mod's Nexus page in the browser."""
        if not self._entries or self._modlist_path is None:
            return
        cy = self._event_canvas_y(event)
        idx = self._canvas_y_to_index(cy)
        entry = self._entries[idx]
        if entry.is_separator:
            return
        staging_root = self._staging_root
        meta_path = staging_root / entry.name / "meta.ini"
        if not meta_path.is_file():
            return
        try:
            meta = read_meta(meta_path)
        except Exception:
            return
        if meta.mod_id <= 0:
            return
        app = self.winfo_toplevel()
        _cur_game = _GAMES.get(getattr(
            getattr(app, "_topbar", None), "_game_var", tk.StringVar()).get(), None)
        domain = (
            _cur_game.nexus_game_domain
            if _cur_game and _cur_game.nexus_game_domain
            else meta.nexus_page_url.split("/mods/")[0].rsplit("/", 1)[-1]
            if "/mods/" in meta.nexus_page_url
            else meta.game_domain
        )
        url = f"https://www.nexusmods.com/{domain}/mods/{meta.mod_id}"
        self._open_nexus_page(url)

    def _open_nexus_page(self, url: str) -> None:
        """Open a Nexus Mods page in the default browser."""
        if url:
            open_url(url)
            self._log(f"Nexus: Opened {url}")

    def _show_missing_reqs(self, mod_name: str, dep_names: list[str]) -> None:
        """Show missing requirements as an inline overlay over the plugin panel."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        if api is None:
            self._log("Nexus: Login to Nexus first.")
            return
        if self._modlist_path is None:
            self._log("No profile loaded.")
            return
        topbar = getattr(app, "_topbar", None)
        game = _GAMES.get(topbar._game_var.get()) if topbar else None
        domain = (game.nexus_game_domain if game and game.is_configured() else "") or ""

        meta_path = self._staging_root / mod_name / "meta.ini"
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

        missing_ids: set[int] = set()
        for pair in (meta.missing_requirements or "").split(";"):
            part = pair.split(":", 1)[0].strip()
            if part:
                try:
                    missing_ids.add(int(part))
                except ValueError:
                    pass

        # Install callback: download and install directly from Nexus (or open browser if not premium)
        from gui.nexus_browser_overlay import install_nexus_mod_from_entry
        mod_panel = self
        game = self._game
        api_for_install = api
        log_fn_install = self._log
        install_from_browse = (
            lambda entry: install_nexus_mod_from_entry(app, api_for_install, game, mod_panel, log_fn_install, entry)
        ) if (api_for_install and game and game.is_configured()) else None

        if hasattr(app, "show_missing_reqs_panel"):
            app.show_missing_reqs_panel(
                mod_name=mod_name,
                domain=domain,
                mod_id=meta.mod_id,
                missing_ids=missing_ids,
                api=api,
                install_from_browse=install_from_browse,
                ignored_set=self._ignored_missing_reqs,
                save_ignored_fn=self._save_ignored_missing_reqs,
                redraw_fn=self._redraw,
            )

    def _endorse_nexus_mod(self, mod_name: str, domain: str, meta) -> None:
        """Endorse a mod on Nexus Mods in a background thread."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        if api is None:
            self._log("Nexus: Login to Nexus first.")
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
                            staging_root = self._staging_root
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
                app.after(0, lambda e=exc: log_fn(f"Nexus: Endorse failed — {e}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _abstain_nexus_mod(self, mod_name: str, domain: str, meta) -> None:
        """Abstain from endorsing a mod on Nexus Mods in a background thread."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        if api is None:
            self._log("Nexus: Login to Nexus first.")
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
                            staging_root = self._staging_root
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
                app.after(0, lambda e=exc: log_fn(f"Nexus: Abstain failed — {e}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _update_nexus_mod(self, mod_name: str) -> None:
        """Show the mod files overlay so the user can pick which file to install."""
        app = self.winfo_toplevel()
        if getattr(app, "_nexus_api", None) is None:
            self._log("Nexus: Login to Nexus first (Nexus button).")
            return
        if self._modlist_path is None:
            return
        staging_root = self._staging_root
        meta_path = staging_root / mod_name / "meta.ini"
        if not meta_path.is_file():
            self._log(f"Nexus: No metadata for {mod_name}")
            return
        try:
            meta = read_meta(meta_path)
        except Exception as exc:
            self._log(f"Nexus: Could not read metadata — {exc}")
            return
        game_name = app._topbar._game_var.get()
        game = _GAMES.get(game_name)
        if game is None or not game.is_configured():
            self._log("Nexus: No configured game selected.")
            return
        game_domain = game.nexus_game_domain or meta.game_domain

        if not meta.mod_id:
            self._log(f"Nexus: No mod ID in metadata for {mod_name}.")
            return

        api = app._nexus_api
        mod_panel = self
        log_fn = self._log

        def _fetch_files():
            files_resp = api.get_mod_files(game_domain, meta.mod_id)
            return files_resp.files

        def _on_install(file_id: int, file_name: str):
            self._download_and_install_nexus_file(
                mod_name=mod_name,
                game_domain=game_domain,
                meta=meta,
                meta_path=meta_path,
                game=game,
                file_id=file_id,
            )

        def _on_ignore(state: bool):
            try:
                m = read_meta(meta_path)
                m.ignore_update = state
                if state:
                    m.has_update = False
                    m.ignored_version = m.latest_version
                else:
                    m.ignored_version = ""
                write_meta(meta_path, m)
            except Exception as exc:
                log_fn(f"Nexus: Could not save ignore flag — {exc}")
            app.after(0, mod_panel._scan_update_flags)
            app.after(0, mod_panel._redraw)

        self._close_mod_files_overlay()
        panel = ModFilesOverlay(
            parent=self,
            mod_name=mod_name,
            game_domain=game_domain,
            mod_id=meta.mod_id,
            installed_file_id=meta.file_id,
            ignore_update=meta.ignore_update,
            on_install=_on_install,
            on_ignore=_on_ignore,
            on_close=self._close_mod_files_overlay,
            fetch_files_fn=_fetch_files,
        )
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._mod_files_panel = panel

    def _close_mod_files_overlay(self):
        panel = getattr(self, "_mod_files_panel", None)
        if panel is not None:
            panel.cleanup()
            panel.place_forget()
            panel.destroy()
            self._mod_files_panel = None

    def _download_and_install_nexus_file(
        self,
        mod_name: str,
        game_domain: str,
        meta,
        meta_path: Path,
        game,
        file_id: int,
    ) -> None:
        """Download a specific Nexus file and install it."""
        app = self.winfo_toplevel()
        log_fn = self._log
        mod_panel = self
        cancel_event = self.get_download_cancel_event()
        self.show_download_progress(f"Updating: {mod_name}", cancel=cancel_event)

        def _worker():
            api = app._nexus_api
            downloader = app._nexus_downloader

            is_premium = False
            try:
                user = api.validate()
                is_premium = user.is_premium
            except Exception:
                pass

            if not is_premium:
                files_url = f"https://www.nexusmods.com/{game_domain}/mods/{meta.mod_id}?tab=files"
                def _fallback():
                    mod_panel.hide_download_progress(cancel=cancel_event)
                    open_url(files_url)
                    log_fn("Nexus: Premium required for direct download.")
                    log_fn("Nexus: Opened files page — click \"Download with Mod Manager\" there.")
                app.after(0, _fallback)
                return

            mod_info = None
            file_info = None
            try:
                mod_info = api.get_mod(game_domain, meta.mod_id)
                files_resp = api.get_mod_files(game_domain, meta.mod_id)
                for f in files_resp.files:
                    if f.file_id == file_id:
                        file_info = f
                        break
            except Exception as exc:
                app.after(0, lambda e=exc: log_fn(f"Nexus: Could not fetch mod info — {e}"))
                app.after(0, lambda: mod_panel.hide_download_progress(cancel=cancel_event))
                return

            result = downloader.download_file(
                game_domain=game_domain,
                mod_id=meta.mod_id,
                file_id=file_id,
                progress_cb=lambda cur, total: app.after(
                    0, lambda c=cur, t=total: mod_panel.update_download_progress(c, t, cancel=cancel_event)
                ),
                cancel=cancel_event,
                dest_dir=get_download_cache_dir(),
            )

            if result.success and result.file_path:
                status_bar = getattr(app, "_status", None)

                def _extract_progress(done: int, total: int, phase: str | None = None):
                    if status_bar is not None:
                        app.after(0, lambda d=done, t=total, p=phase: status_bar.set_progress(d, t, p, title="Extracting"))

                def _install_worker():
                    def _cleanup(is_fomod: bool = False):
                        from Utils.ui_config import (
                            load_clear_archive_after_install,
                            load_keep_fomod_archives,
                        )
                        if not load_clear_archive_after_install():
                            return
                        if is_fomod and load_keep_fomod_archives():
                            return
                        delete_archive_and_sidecar(Path(result.file_path))

                    try:
                        prebuilt = build_meta_from_download(
                            game_domain=game_domain,
                            mod_id=meta.mod_id,
                            file_id=file_id,
                            archive_name=result.file_name,
                            mod_info=mod_info,
                            file_info=file_info,
                        )
                        prebuilt.has_update = False
                    except Exception as exc:
                        log_fn(f"Nexus: Warning — could not build metadata: {exc}")
                        prebuilt = None

                    try:
                        install_mod_from_archive(
                            str(result.file_path), app, log_fn, game, mod_panel,
                            prebuilt_meta=prebuilt,
                            on_installed=_cleanup,
                            progress_fn=_extract_progress,
                            clear_progress_fn=lambda: app.after(0, status_bar.clear_progress) if status_bar is not None else None)
                    finally:
                        if status_bar is not None:
                            app.after(0, status_bar.clear_progress)
                    app.after(0, mod_panel._scan_update_flags)
                    app.after(0, mod_panel._redraw)
                    log_fn(f"Nexus: {mod_name} updated successfully.")

                def _install():
                    try:
                        if app.grab_current() is not None:
                            app.after(500, _install)
                            return
                    except Exception:
                        pass
                    mod_panel.hide_download_progress(cancel=cancel_event)
                    log_fn(f"Nexus: Installing update for {mod_name}...")
                    threading.Thread(target=_install_worker, daemon=True).start()
                app.after(0, _install)
            else:
                def _fail():
                    mod_panel.hide_download_progress(cancel=cancel_event)
                    log_fn(f"Nexus: Update download failed — {result.error}")
                app.after(0, _fail)

        threading.Thread(target=_worker, daemon=True).start()

    def _reinstall_mod(self, mod_name: str, archive_path: Path) -> None:
        """Reinstall a mod from its recorded installation archive in the downloads folder."""
        app = self.winfo_toplevel()
        topbar = getattr(app, "_topbar", None)
        game = _GAMES.get(topbar._game_var.get()) if topbar else None
        if game is None or not game.is_configured():
            self._log("Reinstall: No configured game selected.")
            return
        if not archive_path.is_file():
            self._log(f"Reinstall: Archive not found — {archive_path}")
            return
        self._log(f"Reinstalling '{mod_name}' from {archive_path.name}…")
        status_bar = getattr(app, "_status", None)

        def _extract_progress(done: int, total: int, phase: str | None = None):
            if status_bar is not None:
                app.after(0, lambda d=done, t=total, p=phase: status_bar.set_progress(d, t, p, title="Extracting"))

        def _worker():
            try:
                install_mod_from_archive(
                    str(archive_path), app, self._log, game, mod_panel=self,
                    progress_fn=_extract_progress,
                    clear_progress_fn=lambda: app.after(0, status_bar.clear_progress) if status_bar is not None else None,
                )
            finally:
                if status_bar is not None:
                    app.after(0, status_bar.clear_progress)

        threading.Thread(target=_worker, daemon=True).start()

    def _show_overwrites_dialog(self, mod_name: str) -> None:
        """Open the conflict detail dialog for a mod (I/O runs in a background thread)."""
        if self._modlist_path is None:
            return
        # Snapshot state needed by the worker
        filemap_path = self._filemap_path
        staging_root = self._staging_root
        profile_dir = self._modlist_path.parent
        strip_prefixes = set(self._strip_prefixes)
        beaten_mods = set(self._overrides.get(mod_name, set()))
        call_threadsafe = self._call_threadsafe
        from Games.ue5_game import UE5Game as _UE5Game
        _captured_game = getattr(self, "_game", None)
        _ckfn = None
        if isinstance(_captured_game, _UE5Game):
            def _ckfn(rel: str, _g=_captured_game) -> str:
                dest, final = _g._resolve_entry(rel)
                return ((dest + "/" + final) if dest else final).lower()

        def _worker():
            per_mod = load_per_mod_strip_prefixes(profile_dir)
            strip_lower = {s.lower() for s in strip_prefixes}

            def _strip_for(name: str, rel: str) -> str:
                """Strip prefixes the same way filemap.py does for a given mod."""
                mod_paths = sorted(
                    (p for p in per_mod.get(name, []) if "/" in p),
                    key=lambda p: -len(p),
                )
                if mod_paths:
                    rl = rel.lower()
                    for p in mod_paths:
                        pl = p.lower()
                        if rl.startswith(pl + "/"):
                            rel = rel[len(p) + 1:]
                            break
                        elif rl == pl:
                            rel = ""
                            break
                mod_segs = strip_lower | {s.lower() for s in per_mod.get(name, []) if "/" not in s}
                while "/" in rel and rel.split("/", 1)[0].lower() in mod_segs:
                    rel = rel.split("/", 1)[1]
                return rel

            # Build winner map from filemap.txt, keyed by deploy path (or staged path).
            # When _ckfn is set (UE5), remap via routing so cross-path conflicts are found.
            winning_map: dict[str, tuple[str, str]] = {}
            if filemap_path.is_file():
                with filemap_path.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.rstrip("\n")
                        if "\t" not in line:
                            continue
                        rel_path, winner = line.split("\t", 1)
                        key = _ckfn(rel_path) if _ckfn else rel_path.lower()
                        winning_map[key] = (rel_path, winner)

            # Walk this mod's staging folder
            my_staging = (staging_root.parent / "overwrite"
                          if mod_name == OVERWRITE_NAME else staging_root / mod_name)
            # my_files: deploy_key → display_path
            my_files: dict[str, str] = {}
            if my_staging.is_dir():
                for dirpath, _, fnames in os.walk(my_staging):
                    for fname in fnames:
                        if fname.lower() == "meta.ini":
                            continue
                        full = os.path.join(dirpath, fname)
                        rel = os.path.relpath(full, my_staging).replace("\\", "/")
                        rel = _strip_for(mod_name, rel)
                        if rel:
                            key = _ckfn(rel) if _ckfn else rel.lower()
                            my_files[key] = rel

            # Classify each file
            files_i_win: list[tuple[str, str]] = []
            files_i_lose: list[tuple[str, str]] = []
            for deploy_key, orig_rel in sorted(my_files.items()):
                if deploy_key in winning_map:
                    orig, winner = winning_map[deploy_key]
                    if winner == mod_name:
                        files_i_win.append((deploy_key, ""))
                    else:
                        files_i_lose.append((deploy_key, winner))
                else:
                    files_i_lose.append((deploy_key, "(no winner — disabled?)"))

            # Annotate wins: walk beaten mods to find which files they share
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
                        rel = _strip_for(loser_mod, os.path.relpath(full, loser_staging).replace("\\", "/"))
                        if rel:
                            key = _ckfn(rel) if _ckfn else rel.lower()
                            if key in my_files:
                                rel_to_losers.setdefault(key, []).append(loser_mod)

            files_i_win_final: list[tuple[str, str]] = [
                (deploy_key, beaten_str)
                for deploy_key, _ in files_i_win
                if (beaten_str := ", ".join(rel_to_losers.get(deploy_key, [])))
            ]
            files_no_conflict: list[str] = [
                deploy_key
                for deploy_key, _ in files_i_win
                if not rel_to_losers.get(deploy_key)
            ]

            # Dispatch results back to the main thread
            def _show():
                app = self.winfo_toplevel()
                show_fn = getattr(app, "show_conflicts_panel", None)
                if show_fn:
                    show_fn(mod_name, files_i_win_final, files_i_lose, files_no_conflict)
                else:
                    _OverwritesDialog(
                        app,
                        mod_name=mod_name,
                        files_win=files_i_win_final,
                        files_lose=files_i_lose,
                    )

            if call_threadsafe:
                call_threadsafe(_show)
            else:
                self.after(0, _show)

        threading.Thread(target=_worker, daemon=True).start()

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
        # Keep check_vars aligned (None for separators)
        self._check_vars.insert(insert_at, None)
        if self._sel_idx >= insert_at:
            self._sel_idx += 1
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _generate_separators(self) -> None:
        """Create a separator for each category and move loose mods (not already inside a separator) into it.

        Rules:
        - Mods already inside a separator block are untouched.
        - Loose mods with conflicts stay at the bottom in their original relative order.
        - Loose mods without conflicts are grouped by category, each group placed under
          a new (or existing) separator named after the category.
        - Mods with no category get a separator named "Uncategorized".
        - New separators are inserted just above the conflict/bottom section.
        """
        OVERWRITE = OVERWRITE_NAME  # synthetic first entry
        ROOT = ROOT_FOLDER_NAME     # synthetic last entry

        # --- Step 1: Identify which mods are already inside a separator block ---
        # A mod is "in a separator" if a real (non-synthetic) separator appears above it.
        # Synthetic separators (Overwrite, Root_Folder) don't count as real separators.
        SYNTHETIC = {OVERWRITE, ROOT}
        in_separator: set[str] = set()
        under_real_sep = False
        for entry in self._entries:
            if entry.is_separator:
                if entry.name not in SYNTHETIC:
                    under_real_sep = True
            elif under_real_sep:
                in_separator.add(entry.name)

        # --- Step 2: Collect loose mods (not in a separator, not synthetic, not disabled) ---
        # Preserve original relative order.
        loose: list[ModEntry] = [
            e for e in self._entries
            if not e.is_separator
            and e.name not in in_separator
            and e.name not in (OVERWRITE, ROOT)
            and e.enabled
        ]

        if not loose:
            return  # nothing to do

        # --- Step 3: Split loose mods into conflict vs non-conflict groups ---
        conflict_mods: list[ModEntry] = []
        no_conflict_mods: list[ModEntry] = []
        for entry in loose:
            c = self._conflict_map.get(entry.name, CONFLICT_NONE)
            if c != CONFLICT_NONE:
                conflict_mods.append(entry)
            else:
                no_conflict_mods.append(entry)

        # --- Step 4: Group non-conflict mods by category (preserve order within group) ---
        cat_groups: dict[str, list[ModEntry]] = {}
        for entry in no_conflict_mods:
            cat = self._category_names.get(entry.name, "") or "Uncategorized"
            if cat not in cat_groups:
                cat_groups[cat] = []
            cat_groups[cat].append(entry)

        # --- Step 5: Build the new entries list ---
        # Keep: synthetic Overwrite + all existing separator blocks (untouched)
        # Then append: new category separators + their mods
        # Then append: "Conflicts" separator + conflict mods at bottom

        # Remove all loose mods from _entries (they will be re-inserted).
        loose_names = {e.name for e in loose}
        new_entries: list[ModEntry] = [e for e in self._entries if e.name not in loose_names]

        # Find insertion point: just before ROOT_FOLDER (if present), otherwise end.
        insert_base = len(new_entries)
        for i, e in enumerate(new_entries):
            if e.name == ROOT:
                insert_base = i
                break

        # Build the block to insert: category separators + mods, then conflict mods.
        to_insert: list[ModEntry] = []
        existing_sep_names = {e.name for e in new_entries if e.is_separator}
        for cat, mods in sorted(cat_groups.items()):
            sep_name = cat + "_separator"
            if sep_name not in existing_sep_names:
                sep_entry = ModEntry(name=sep_name, enabled=True, locked=True, is_separator=True)
                to_insert.append(sep_entry)
                existing_sep_names.add(sep_name)
                to_insert.extend(mods)
            else:
                # Separator already exists — find the end of its block and insert mods there.
                sep_idx = next(i for i, e in enumerate(new_entries) if e.name == sep_name)
                # Advance past all non-separator entries already in the block.
                insert_after = sep_idx + 1
                while insert_after < len(new_entries) and not new_entries[insert_after].is_separator:
                    insert_after += 1
                for offset, mod in enumerate(mods):
                    new_entries.insert(insert_after + offset, mod)
                # Update insert_base since new_entries grew.
                insert_base += len(mods)

        if conflict_mods:
            conflicts_sep_name = "Conflicts_separator"
            if conflicts_sep_name not in existing_sep_names:
                to_insert.append(ModEntry(name=conflicts_sep_name, enabled=True, locked=True, is_separator=True))
            to_insert.extend(conflict_mods)

        # Insert the block at the correct position.
        for offset, entry in enumerate(to_insert):
            new_entries.insert(insert_base + offset, entry)

        self._entries = new_entries

        # --- Step 6: Rebuild check vars to stay aligned ---
        self._rebuild_check_widgets()
        self._invalidate_derived_caches()
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
            show_error(
                "Name Conflict",
                f"A mod or separator named '{mod_name}' already exists.",
                parent=self.winfo_toplevel(),
            )
            return
        # Create the staging folder
        staging = self._staging_root / mod_name
        staging.mkdir(parents=True, exist_ok=True)
        # Write a minimal meta.ini so MO2 recognizes the folder (incl. installed in MO2 format)
        installed = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        (staging / "meta.ini").write_text(
            f"[General]\ninstalled={installed}\n", encoding="utf-8"
        )
        insert_at = ref_idx + 1
        entry = ModEntry(name=mod_name, enabled=True, locked=False, is_separator=False)
        self._entries.insert(insert_at, entry)
        # Create logical var for the new mod (visual rendering uses pool)
        var = tk.BooleanVar(value=True)
        self._check_vars.insert(insert_at, var)
        if self._sel_idx >= insert_at:
            self._sel_idx += 1
        self._invalidate_derived_caches()
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
            entry = self._entries[idx]
            now_enabled = var.get()
            entry.enabled = now_enabled
            self._sync_plugins_for_toggle(entry.name, now_enabled)

            self._vis_dirty = True  # enabled state affects show-disabled/enabled filters
            self._save_modlist()
            self._rebuild_filemap()
            self._scan_missing_reqs_flags()
            self._update_enable_disable_all_btn()
            self._redraw()
            self._update_info()

    def _sync_plugins_for_toggle(self, mod_name: str, now_enabled: bool) -> None:
        """Add or remove a mod's plugins from plugins.txt and loadorder.txt on toggle."""
        app = self.winfo_toplevel()
        pp = getattr(app, "_plugin_panel", None)
        if pp is None or pp._plugins_path is None:
            return
        plugin_exts = {e.lower() for e in getattr(pp, "_plugin_extensions", [])}
        if not plugin_exts:
            return
        staging = self._staging_root / mod_name
        if not staging.is_dir():
            return
        mod_plugins = [
            f.name for f in staging.iterdir()
            if f.is_file() and f.suffix.lower() in plugin_exts
        ]
        if not mod_plugins:
            return

        loadorder_path = pp._plugins_path.parent / "loadorder.txt"

        if now_enabled:
            # Append to plugins.txt and loadorder.txt if not already present
            existing = read_plugins(pp._plugins_path, star_prefix=pp._plugins_star_prefix)
            existing_lower = {e.name.lower() for e in existing}
            added = [n for n in mod_plugins if n.lower() not in existing_lower]
            if added:
                for name in added:
                    existing.append(PluginEntry(name=name, enabled=True))
                write_plugins(pp._plugins_path, existing, star_prefix=pp._plugins_star_prefix)
            loadorder = read_loadorder(loadorder_path)
            lo_lower = {n.lower() for n in loadorder}
            lo_added = [n for n in mod_plugins if n.lower() not in lo_lower]
            if lo_added:
                loadorder.extend(lo_added)
                write_loadorder(loadorder_path, [PluginEntry(name=n, enabled=True) for n in loadorder])
        else:
            # Remove from plugins.txt and loadorder.txt
            remove_lower = {n.lower() for n in mod_plugins}
            existing = read_plugins(pp._plugins_path, star_prefix=pp._plugins_star_prefix)
            new_entries = [e for e in existing if e.name.lower() not in remove_lower]
            if len(new_entries) < len(existing):
                write_plugins(pp._plugins_path, new_entries, star_prefix=pp._plugins_star_prefix)
            loadorder = read_loadorder(loadorder_path)
            new_lo = [n for n in loadorder if n.lower() not in remove_lower]
            if len(new_lo) < len(loadorder):
                write_loadorder(loadorder_path, [PluginEntry(name=n, enabled=True) for n in new_lo])

    # ------------------------------------------------------------------
    # Toolbar button handlers
    # ------------------------------------------------------------------

    def _on_collections(self, initial_slug: str | None = None, initial_game_domain: str | None = None, initial_revision: int | None = None):
        """Slide the Collections browser over the modlist panel.

        If initial_slug is provided (e.g. from an nxm:// collection link), the
        dialog will open that collection directly.
        """
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        game = self._game
        domain = (game.nexus_game_domain if game and game.nexus_game_domain else "") or ""
        if not domain:
            self._log("Collections: No game selected or game has no Nexus domain.")
            return
        # Destroy any existing panel first (e.g. double-click)
        self._close_collections()
        panel = CollectionsDialog(
            self, game_domain=domain, api=api, game=game,
            log_fn=self._log,
            app_root=app,
            on_close=self._close_collections,
            on_open_workshop=self._on_workshop,
            initial_slug=initial_slug,
            initial_game_domain=initial_game_domain,
            initial_revision=initial_revision,
        )
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._collections_panel = panel

    def _close_collections(self):
        """Destroy the inline collections panel and restore the modlist."""
        panel = getattr(self, "_collections_panel", None)
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
            self._collections_panel = None

    def _on_workshop(self, entries: list):
        """Open the Workshop overlay over the modlist panel."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        game = self._game
        domain = (game.nexus_game_domain if game and game.nexus_game_domain else "") or ""
        overlay_parent = getattr(app, "_plugin_panel_container", None)
        self._close_workshop()
        panel = WorkshopDialog(
            self, entries=entries, game=game, api=api,
            game_domain=domain, on_close=self._close_workshop,
            overlay_parent=overlay_parent,
        )
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._workshop_panel = panel

    def _close_workshop(self):
        """Destroy the Workshop overlay and restore the modlist."""
        panel = getattr(self, "_workshop_panel", None)
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
            self._workshop_panel = None

    def _on_nexus_browser(self):
        """Show the Nexus Browse/Tracked/Endorsed overlay over the modlist panel."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        game = self._game
        domain = (game.nexus_game_domain if game and game.nexus_game_domain else "") or ""
        if not domain:
            self._log("Nexus: No game selected or game has no Nexus domain.")
            return
        self._close_nexus_browser()
        open_settings = app.get_nexus_settings_opener() if hasattr(app, "get_nexus_settings_opener") else None
        panel = NexusBrowserOverlay(
            self, game_domain=domain, api=api, game=game,
            log_fn=self._log,
            app_root=app,
            on_close=self._close_nexus_browser,
            on_open_settings=open_settings,
        )
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._nexus_browser_panel = panel

    def _close_nexus_browser(self):
        """Destroy the Nexus browser overlay and restore the modlist."""
        panel = getattr(self, "_nexus_browser_panel", None)
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
            self._nexus_browser_panel = None

    def show_profile_settings(self, game_name: str, current_profile: str,
                               on_profile_renamed=None, on_profile_removed=None,
                               on_profiles_changed=None):
        """Show the Profile Settings overlay over the modlist panel."""
        from gui.profile_settings_overlay import ProfileSettingsOverlay
        self._close_profile_settings()
        panel = ProfileSettingsOverlay(
            self,
            game_name=game_name,
            current_profile=current_profile,
            on_close=self._close_profile_settings,
            on_profile_renamed=on_profile_renamed,
            on_profile_removed=on_profile_removed,
            on_profiles_changed=on_profiles_changed,
            log_fn=self._log,
        )
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._profile_settings_panel = panel

    def _close_profile_settings(self):
        """Destroy the profile settings overlay."""
        panel = getattr(self, "_profile_settings_panel", None)
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
            self._profile_settings_panel = None

    def _on_changelog(self):
        """Show the Changelog overlay over the modlist panel."""
        self._close_changelog()
        panel = ChangelogOverlay(self, on_close=self._close_changelog)
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._changelog_panel = panel

    def _close_changelog(self):
        """Destroy the changelog overlay and restore the modlist."""
        panel = getattr(self, "_changelog_panel", None)
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
            self._changelog_panel = None

    def _on_check_updates(self):
        """Check for mod updates and missing requirements in one background pass."""
        app = self.winfo_toplevel()
        if app._nexus_api is None:
            self._log("Nexus: Login to Nexus first (Nexus button).")
            return
        game = self._game
        if game is None or not game.is_configured():
            self._log("No configured game selected.")
            return

        staging = game.get_effective_mod_staging_path()
        enabled_names = {e.name for e in self._entries if not e.is_separator}
        self._update_btn.configure(text="Checking...", state="disabled")
        log_fn = self._log

        def _worker():
            try:
                results, missing = check_for_updates(
                    app._nexus_api, staging,
                    game_domain=game.nexus_game_domain,
                    progress_cb=lambda m: app.after(0, lambda msg=m: log_fn(msg)),
                    enabled_only=enabled_names,
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
                    self._scan_meta_flags_async()
                app.after(0, _done)
            except Exception as exc:
                app.after(0, lambda e=exc: (
                    self._update_btn.configure(text="Check Updates", state="normal"),
                    log_fn(f"Nexus: Check failed — {e}"),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_check_updates_for_mods(self, mod_names: list[str]):
        """Check for updates for a specific set of mods (from right-click menu)."""
        if not mod_names:
            return
        app = self.winfo_toplevel()
        if app._nexus_api is None:
            self._log("Nexus: Login to Nexus first (Nexus button).")
            return
        game = self._game
        if game is None or not game.is_configured():
            self._log("No configured game selected.")
            return

        staging = game.get_effective_mod_staging_path()
        target_names = set(mod_names)
        self._update_btn.configure(text="Checking...", state="disabled")
        log_fn = self._log

        def _worker():
            try:
                results, missing = check_for_updates(
                    app._nexus_api, staging,
                    game_domain=game.nexus_game_domain,
                    progress_cb=lambda m: app.after(0, lambda msg=m: log_fn(msg)),
                    enabled_only=target_names,
                )

                def _done():
                    self._update_btn.configure(text="Check Updates", state="normal")
                    if results:
                        log_fn(f"Nexus: {len(results)} update(s) available!")
                        for u in results:
                            log_fn(f"  ↑ {u.mod_name}: {u.installed_version} → {u.latest_version}")
                    else:
                        log_fn("Nexus: Selected mod(s) are up to date.")
                    if missing:
                        log_fn(f"Nexus: {len(missing)} mod(s) have missing requirements!")
                        for m in missing:
                            names = ", ".join(r.mod_name for r in m.missing[:3])
                            suffix = f" (+{len(m.missing) - 3} more)" if len(m.missing) > 3 else ""
                            log_fn(f"  ⚠ {m.mod_name}: needs {names}{suffix}")
                    self._scan_meta_flags_async()
                app.after(0, _done)
            except Exception as exc:
                app.after(0, lambda e=exc: (
                    self._update_btn.configure(text="Check Updates", state="normal"),
                    log_fn(f"Nexus: Check failed — {e}"),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_open_filters(self):
        """Toggle the inline filter side panel."""
        if getattr(self, "_filter_panel_open", False):
            self._close_filter_side_panel()
        else:
            self._open_filter_side_panel()

    def _open_filter_side_panel(self):
        """Show the filter side panel and sync checkboxes to current state."""
        # Close plugin filter if open (they share the same column)
        plugin_panel = getattr(self.winfo_toplevel(), "_plugin_panel", None)
        if plugin_panel is not None and getattr(plugin_panel, "_plugin_filter_panel_open", False):
            plugin_panel._close_plugin_filter_panel()
        self._filter_panel_open = True
        # Use scaled minsize so panel isn't squeezed at higher UI scale
        self.grid_columnconfigure(0, minsize=scaled(380))
        self._filter_side_panel.grid()
        # Sync checkbox vars to current live filter state
        self._fsp_vars["filter_show_disabled"].set(self._filter_show_disabled)
        self._fsp_vars["filter_show_enabled"].set(self._filter_show_enabled)
        self._fsp_vars["filter_hide_separators"].set(self._filter_hide_separators)
        self._fsp_vars["filter_winning"].set(self._filter_conflict_winning)
        self._fsp_vars["filter_losing"].set(self._filter_conflict_losing)
        self._fsp_vars["filter_partial"].set(self._filter_conflict_partial)
        self._fsp_vars["filter_full"].set(self._filter_conflict_full)
        self._fsp_vars["filter_missing_reqs"].set(self._filter_missing_reqs)
        self._fsp_vars["filter_has_disabled_plugins"].set(self._filter_has_disabled_plugins)
        self._fsp_vars["filter_has_plugins"].set(self._filter_has_plugins)
        self._fsp_vars["filter_has_disabled_files"].set(self._filter_has_disabled_files)
        self._fsp_vars["filter_has_updates"].set(self._filter_has_updates)
        self._fsp_vars["filter_fomod_only"].set(self._filter_fomod_only)
        self._refresh_filter_category_list()
        self._filter_btn.configure(fg_color=ACCENT, hover_color=ACCENT_HOV)

    def _close_filter_side_panel(self):
        """Hide the filter side panel."""
        self._filter_panel_open = False
        self._filter_side_panel.grid_remove()
        self.grid_columnconfigure(0, minsize=0)
        self._filter_btn.configure(fg_color=BG_HEADER, hover_color=BG_HOVER)

    def _on_restore_backup(self):
        """Open the backup restore panel/dialog for the current profile."""
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
        show_fn = getattr(app, "show_backup_restore_panel", None)
        if show_fn:
            show_fn(
                profile_dir,
                profile_name,
                on_restored=lambda: app._topbar._reload_mod_panel(),
            )
        else:
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
        self._filter_has_disabled_plugins = state.get("filter_has_disabled_plugins", False)
        self._filter_has_plugins = state.get("filter_has_plugins", False)
        self._filter_has_disabled_files = state.get("filter_has_disabled_files", False)
        self._filter_has_updates = state.get("filter_has_updates", False)
        self._filter_fomod_only = state.get("filter_fomod_only", False)
        self._filter_categories = state.get("filter_categories") or frozenset()
        self._invalidate_derived_caches()
        self._redraw()

    def _move_up(self):
        indices = sorted(self._sel_set) if self._sel_set else (
            [self._sel_idx] if self._sel_idx >= 0 else []
        )
        if not indices or indices[0] <= 0:
            return
        if any(self._entries[i].locked for i in indices):
            return
        saved_col, saved_asc = self._clear_sort()
        for i in indices:
            self._entries[i], self._entries[i - 1] = self._entries[i - 1], self._entries[i]
            self._check_vars[i], self._check_vars[i - 1] = self._check_vars[i - 1], self._check_vars[i]
        self._sel_set = {i - 1 for i in indices}
        self._sel_idx = self._sel_idx - 1 if self._sel_idx >= 0 else -1
        self._restore_sort(saved_col, saved_asc)
        self._invalidate_derived_caches()
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
        saved_col, saved_asc = self._clear_sort()
        for i in indices:
            self._entries[i], self._entries[i + 1] = self._entries[i + 1], self._entries[i]
            self._check_vars[i], self._check_vars[i + 1] = self._check_vars[i + 1], self._check_vars[i]
        self._sel_set = {i + 1 for i in indices}
        self._sel_idx = self._sel_idx + 1 if self._sel_idx >= 0 else -1
        self._restore_sort(saved_col, saved_asc)
        self._invalidate_derived_caches()
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

        moved_entry = self._entries.pop(from_idx)
        moved_var = self._check_vars.pop(from_idx)

        self._entries.insert(to_idx, moved_entry)
        self._check_vars.insert(to_idx, moved_var)

        self._sel_idx = to_idx
        self._sel_set = {to_idx}
        self._invalidate_derived_caches()
        self._redraw()
        self._update_info()
        self._save_modlist()
        self._rebuild_filemap()
        self._log(f"Set priority for '{moved_entry.name}' to position {value}")

    # ------------------------------------------------------------------
    # Persist + info
    # ------------------------------------------------------------------

    def _rebuild_filemap(self):
        """Kick off a background filemap rebuild. Safe to call from the main thread.

        Calls are debounced: rapid successive calls within 150 ms are coalesced
        into a single rebuild to avoid hammering the disk when the user quickly
        enables/disables several mods in a row.
        """
        if self._modlist_path is None:
            return
        # Cancel any pending debounce timer and reset it.
        if self._filemap_after_id is not None:
            self.after_cancel(self._filemap_after_id)
            self._filemap_after_id = None
        if self._filemap_pending:
            # A rebuild is already running; mark dirty so we re-run when it finishes.
            self._filemap_dirty = True
            return
        # Debounce: wait 150 ms before actually starting the rebuild so that a
        # burst of rapid changes (e.g. toggling several mods) becomes one rebuild.
        self._filemap_after_id = self.after(150, self._rebuild_filemap_now)

    def _rebuild_filemap_now(self):
        """Internal: actually start the rebuild after the debounce delay."""
        self._filemap_after_id = None
        if self._modlist_path is None:
            return
        if self._filemap_pending:
            self._filemap_dirty = True
            return
        self._filemap_pending = True
        self._filemap_dirty = False

        modlist_path        = self._modlist_path
        staging             = self._staging_root
        output              = self._filemap_path
        strip_prefixes      = self._strip_prefixes
        install_extensions  = self._install_extensions
        root_deploy_folders = self._root_deploy_folders
        rescan_index        = self._filemap_rescan_index
        per_mod_strip       = dict(self._mod_strip_prefixes) if self._mod_strip_prefixes else {}
        conflict_ignore_fn  = set(self._conflict_ignore_filenames) if self._conflict_ignore_filenames else None
        exclude_dirs        = set(self._filemap_exclude_dirs) if self._filemap_exclude_dirs else None
        from Games.ue5_game import UE5Game as _UE5Game
        _captured_game = getattr(self, "_game", None)
        _conflict_key_fn = None
        if isinstance(_captured_game, _UE5Game):
            def _conflict_key_fn(rel_key: str, _g=_captured_game) -> str:
                dest, final = _g._resolve_entry(rel_key)
                return (dest + "/" + final) if dest else final
        elif _captured_game is not None:
            _conflict_key_fn = getattr(_captured_game, "filemap_conflict_key_fn", None)
        # Pre-RTX detection: gather the source prefixes that indicate old-format mods
        # (e.g. "natives/x64/" for RE2/RE3/RE7).
        _prertx_prefixes: list[str] = []
        if _captured_game is not None:
            _path_remap = getattr(_captured_game, "mod_deploy_path_remap", {}) or {}
            _prertx_prefixes = [k.lower() for k in _path_remap]
        staging_requires_subdir = self._staging_requires_subdir
        normalize_folder_case   = self._normalize_folder_case
        self._filemap_rescan_index = False
        disabled_plugins    = read_disabled_plugins(modlist_path.parent, None)  # fresh read for rebuild
        self._disabled_plugins_map = disabled_plugins
        _exc_raw            = read_excluded_mod_files(modlist_path.parent, None)
        excluded_mod_files  = {k: set(v) for k, v in _exc_raw.items()} if _exc_raw else None
        self._excluded_mod_files_map = _exc_raw or {}
        if excluded_mod_files:
            total_exc = sum(len(v) for v in excluded_mod_files.values())
            self.after(0, lambda n=total_exc: self._log(
                f"Filemap: excluding {n} file(s) (profile_state excluded_mod_files)"))

        def _log_thread_safe(msg: str) -> None:
            self.after(0, lambda m=msg: self._log(m))

        def _worker():
            nonlocal rescan_index
            try:
                if staging_requires_subdir:
                    fixed = fix_flat_staging_folders(staging)
                    if fixed:
                        rescan_index = True
                        self.after(0, lambda names=fixed: self._log(
                            f"Auto-fixed {len(names)} mod(s) with flat staging structure: "
                            + ", ".join(names)
                        ))
                if rescan_index:
                    rebuild_mod_index(
                        output.parent / "modindex.bin",
                        staging,
                        strip_prefixes=strip_prefixes,
                        per_mod_strip_prefixes=per_mod_strip,
                        allowed_extensions=install_extensions or None,
                        root_deploy_folders=root_deploy_folders or None,
                        normalize_folder_case=normalize_folder_case,
                        exclude_dirs=exclude_dirs,
                        log_fn=_log_thread_safe,
                    )
                count, conflict_map, overrides, overridden_by = build_filemap(
                    modlist_path, staging, output,
                    strip_prefixes=strip_prefixes,
                    per_mod_strip_prefixes=per_mod_strip,
                    allowed_extensions=install_extensions or None,
                    root_deploy_folders=root_deploy_folders or None,
                    disabled_plugins=disabled_plugins or None,
                    conflict_ignore_filenames=conflict_ignore_fn,
                    excluded_mod_files=excluded_mod_files or None,
                    normalize_folder_case=normalize_folder_case,
                    conflict_key_fn=_conflict_key_fn,
                    exclude_dirs=exclude_dirs,
                    log_fn=_log_thread_safe,
                )
                _game = getattr(self, "_game", None)
                if _game is not None:
                    _game.post_build_filemap(output, staging)
                # Detect pre-RTX mods: any mod with at least one file under a
                # remapped source prefix (e.g. natives/x64/).
                prertx_mods: set[str] = set()
                if _prertx_prefixes:
                    _index = read_mod_index(output.parent / "modindex.bin")
                    if _index:
                        for mod_name, (normal, _) in _index.items():
                            for rel_key in normal:
                                if any(rel_key.startswith(p) for p in _prertx_prefixes):
                                    prertx_mods.add(mod_name)
                                    break
                self.after(0, lambda: _done(count, conflict_map, overrides, overridden_by, None, prertx_mods))
            except Exception as exc:
                self.after(0, lambda e=exc: _done(0, {}, {}, {}, e, set()))

        def _done(count, conflict_map, overrides, overridden_by, exc, prertx_mods=set()):
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
                self._prertx_mods   = prertx_mods
                self._log(f"Filemap updated: {count} file(s).")
            self._vis_dirty = True  # conflict filters depend on conflict_map
            self._redraw()
            # Defer _on_filemap_rebuilt to the next event-loop iteration so
            # the current _redraw geometry is fully settled before the plugin
            # panel destroys/creates widgets (framework banners, data tree),
            # which can trigger cascading resize events that cause column
            # items to momentarily disappear.
            if self._on_filemap_rebuilt:
                self.after_idle(self._on_filemap_rebuilt)
            # If something changed while we were running, rebuild again.
            if self._filemap_dirty:
                self._rebuild_filemap()

        threading.Thread(target=_worker, daemon=True).start()

    def _save_modlist(self):
        if self._modlist_path is None:
            return
        from dataclasses import replace as _dc_replace
        entries = []
        for e in self._entries:
            if e.name in (OVERWRITE_NAME, ROOT_FOLDER_NAME):
                continue
            # Bundle variants should never be written as locked (*) — locked only
            # prevents dragging in the panel, not toggling.  Write as +/- instead.
            if e.bundle_name is not None and e.locked:
                e = _dc_replace(e, locked=False)
            entries.append(e)
        write_modlist(self._modlist_path, entries)

    def _update_info(self):
        mods    = [e for e in self._entries if not e.is_separator]
        enabled = sum(1 for e in mods if e.enabled)
        total   = len(mods)
        sel_entry = self._entries[self._sel_idx] if 0 <= self._sel_idx < len(self._entries) else None
        sel = (f" | Selected: {sel_entry.name}"
               if sel_entry and not sel_entry.is_separator else "")
        if self._status_bar is not None:
            self._status_bar.set_mod_count(f"{enabled}/{total} mods active{sel}")

    def set_status_bar(self, status_bar) -> None:
        """Wire up the StatusBar so _update_info can push the mod count into it."""
        self._status_bar = status_bar

    def set_highlighted_mod(self, mod_name: str | None):
        """Highlight the given mod (by name) in the modlist, e.g. when a plugin is selected."""
        if mod_name != self._highlighted_mod:
            self._highlighted_mod = mod_name
            self._redraw()  # _redraw calls _draw_marker_strip internally

    def _on_marker_strip_resize(self, _event):
        if self._marker_strip_after_id is not None:
            self.after_cancel(self._marker_strip_after_id)
        self._marker_strip_after_id = self.after(250, self._draw_marker_strip)

    def _draw_marker_strip(self):
        """Draw colour-coded tick marks on the narrow strip beside the scrollbar.

        - Orange : the mod whose plugin is selected (_highlighted_mod)
        - Green  : mods that win over the selected mod (conflict_higher)
        - Red    : mods that lose to the selected mod (conflict_lower)
        """
        c = self._marker_strip
        c.delete("marker")
        vis = self._visible_indices
        if not vis:
            return
        strip_h = c.winfo_height()
        if strip_h <= 1:
            return
        n = len(vis)

        # Build a name→entry-index lookup for the visible list (and a fast path to
        # the entry-index→row mapping used for collapsed-separator fallback).
        ei_to_row: dict[int, int] = {ei: r for r, ei in enumerate(vis)}
        name_to_row: dict[str, int] = {self._entries[ei].name: r for r, ei in enumerate(vis)}

        def _row_for_mod(mod_name: str) -> int | None:
            """Return the visible row index for mod_name, falling back to its separator."""
            row = name_to_row.get(mod_name)
            if row is not None:
                return row
            sep_ei = self._sep_idx_for_mod(mod_name)
            if sep_ei >= 0:
                return ei_to_row.get(sep_ei)
            return None

        def _tick(row_idx: int, colour: str):
            frac = row_idx / n
            y = max(2, min(int(frac * strip_h), strip_h - 4))
            c.create_rectangle(0, y, 4, y + 3, fill=colour, outline="", tags="marker")

        # Orange tick for the plugin-highlighted mod.
        if self._highlighted_mod:
            row = _row_for_mod(self._highlighted_mod)
            if row is not None:
                _tick(row, plugin_mod)

        # Green/red ticks for conflict highlights when mod(s) are selected.
        sel_indices = sorted(self._sel_set) if self._sel_set else (
            [self._sel_idx] if 0 <= self._sel_idx < len(self._entries) else []
        )
        all_higher: set[str] = set()
        all_lower: set[str] = set()
        for si in sel_indices:
            if si < 0 or si >= len(self._entries):
                continue
            e = self._entries[si]
            if e.is_separator and e.name != OVERWRITE_NAME:
                continue
            all_higher.update(self._overrides.get(e.name, set()))
            all_lower.update(self._overridden_by.get(e.name, set()))
        for mod_name in all_higher:
            row = _row_for_mod(mod_name)
            if row is not None:
                _tick(row, conflict_higher)
        for mod_name in all_lower:
            row = _row_for_mod(mod_name)
            if row is not None:
                _tick(row, conflict_lower)

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