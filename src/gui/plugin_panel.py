"""
Plugin panel: Plugins, Mod Files, Data, Downloads, Tracked, Endorsed tabs.
Used by App. Imports theme, game_helpers, dialogs, install_mod, subpanels.
"""

import json
import os
import subprocess
import threading
import tkinter as tk
import webbrowser
import tkinter.ttk as ttk
from pathlib import Path

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
    plugin_mod,
    _ICONS_DIR,
    load_icon as _load_icon,
)
import gui.theme as _theme
from gui.game_helpers import _GAMES, _vanilla_plugins_for_game
from gui.dialogs import _PriorityDialog, _ExeConfigDialog, _VRAMrPresetDialog, _ExeFilterDialog
from gui.install_mod import install_mod_from_archive
from gui.mod_name_utils import _suggest_mod_names as suggest_mod_names
from gui.downloads_panel import DownloadsPanel
from gui.tracked_mods_panel import TrackedModsPanel
from gui.endorsed_mods_panel import EndorsedModsPanel
from gui.browse_mods_panel import BrowseModsPanel
from gui.ctk_components import CTkTreeview

from Utils.config_paths import get_exe_args_path, get_game_config_dir
from Utils.filemap import build_filemap
from Utils.xdg import xdg_open, open_url
from Utils.plugins import (
    PluginEntry,
    read_plugins,
    write_plugins,
    append_plugin,
    read_loadorder,
    write_loadorder,
    sync_plugins_from_filemap,
    prune_plugins_from_filemap,
    read_excluded_mod_files,
    write_excluded_mod_files,
)
from Utils.plugin_parser import check_missing_masters
from LOOT.loot_sorter import sort_plugins as loot_sort, is_available as loot_available
from Nexus.nexus_meta import build_meta_from_download, write_meta, read_meta


def _read_prefix_runner(compat_data: Path) -> str:
    """Read the Proton runner name from <compat_data>/config_info (first line).
    Returns an empty string if the file is absent or unreadable."""
    try:
        return (compat_data / "config_info").read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


_truncate_cache: dict[tuple, str] = {}

def _truncate_plugin_name(widget: tk.Widget, text: str, font: tuple, max_px: int) -> str:
    """Return *text* truncated with '…' so it fits within *max_px* pixels.
    Results are cached by (text, font, max_px) to avoid repeated Tcl font measure calls."""
    key = (text, font, max_px)
    cached = _truncate_cache.get(key)
    if cached is not None:
        return cached
    if max_px <= 0:
        _truncate_cache[key] = ""
        return ""
    measure = widget.tk.call("font", "measure", font, text)
    if measure <= max_px:
        _truncate_cache[key] = text
        return text
    ellipsis = "…"
    ellipsis_w = widget.tk.call("font", "measure", font, ellipsis)
    while text and widget.tk.call("font", "measure", font, text) + ellipsis_w > max_px:
        text = text[:-1]
    result = text + ellipsis
    _truncate_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# PluginPanel
# ---------------------------------------------------------------------------
class PluginPanel(ctk.CTkFrame):
    """Right panel: tabview with Plugins, Mod Files, Data, Downloads, Tracked."""

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
        self._highlighted_plugins: set[str] = set()  # plugin names highlighted when their mod is selected
        self._on_plugin_selected_cb = None  # callable(mod_name: str | None)
        self._on_mod_selected_cb = None     # callable() — notify mod panel a plugin was selected

        # Missing masters detection
        self._missing_masters: dict[str, list[str]] = {}
        self._staging_root: Path | None = None
        self._data_dir: Path | None = None

        # Warning icon for missing masters (canvas-compatible PhotoImage)
        self._warning_icon: ImageTk.PhotoImage | None = None
        _warn_path = _ICONS_DIR / "warning2.png"
        if _warn_path.is_file():
            _img = PilImage.open(_warn_path).convert("RGBA").resize((16, 16), PilImage.LANCZOS)
            self._warning_icon = ImageTk.PhotoImage(_img)

        # Lock icon
        self._icon_lock: ImageTk.PhotoImage | None = None
        _lock_path = _ICONS_DIR / "lock.png"
        if _lock_path.is_file():
            self._icon_lock = ImageTk.PhotoImage(
                PilImage.open(_lock_path).convert("RGBA").resize((14, 14), PilImage.LANCZOS))

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

        # Framework status banners (one CTkFrame per framework entry)
        self._framework_banner_widgets: list[ctk.CTkFrame] = []

        # Virtual-list pool (fixed-size widget + canvas item pool for visible rows)
        self._pool_size: int = 60
        self._pool_data_idx: list[int] = []
        self._pool_bg: list[int] = []
        self._pool_name: list[int] = []
        self._pool_idx_text: list[int] = []
        self._pool_warn: list[int | None] = []
        self._pool_check_rects: list[int] = []
        self._pool_check_marks: list[int] = []
        self._pool_lock_rects: list[int] = []
        self._pool_lock_marks: list[int] = []
        self._predraw_after_id: str | None = None

        # Canvas dimensions
        self._pcanvas_w: int = 400

        # Mod Files tab state
        self._mod_files_mod_name: str | None = None   # currently displayed mod
        self._mod_files_index_path: Path | None = None  # modindex.bin path
        self._mod_files_excluded_path: Path | None = None  # excluded_mod_files.json path
        self._mod_files_excluded: dict[str, set[str]] = {}  # mod_name → set of excluded rel_keys
        self._mod_files_on_change: callable | None = None  # called when exclusions change

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
        self._game_exe_path: Path | None = None
        self._exe_menu = ctk.CTkOptionMenu(
            exe_bar, values=["(no executables)"], variable=self._exe_var,
            width=175, font=_theme.FONT_SMALL,
            fg_color=BG_PANEL, button_color=ACCENT, button_hover_color=ACCENT_HOV,
            dropdown_fg_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_exe_selected,
        )
        self._exe_menu.pack(side="left", padx=(8, 4), pady=6)

        self._run_exe_btn = ctk.CTkButton(
            exe_bar, text="▶ Run EXE", width=90, height=28, font=_theme.FONT_SMALL,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_run_exe,
        )
        self._run_exe_btn.pack(side="left", padx=4, pady=6)

        self._exe_args_var = tk.StringVar(value="")

        ctk.CTkButton(
            exe_bar, text="⚙", width=30, height=30, font=_theme.FONT_SMALL,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_configure_exe,
        ).pack(side="left", padx=4, pady=6)
                
        refresh_icon = _load_icon("refresh.png", size=(16, 16))
        ctk.CTkButton(
            exe_bar, text="" if refresh_icon else "↺", image=refresh_icon,
            width=30, height=30, font=_theme.FONT_SMALL,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self.refresh_exe_list,
        ).pack(side="left", padx=4, pady=6)

        ctk.CTkButton(
            exe_bar, text="📂", width=30, height=30, font=_theme.FONT_SMALL,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._open_applications_folder,
        ).pack(side="left", padx=(0, 4), pady=6)

        ctk.CTkButton(
            exe_bar, text="⊘", width=30, height=30, font=_theme.FONT_SMALL,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_exe_filter,
        ).pack(side="left", padx=(0, 4), pady=6)

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

        for name in ("Plugins", "Mod Files", "Data", "Downloads", "Tracked", "Endorsed", "Browse"):
            self._tabs.add(name)

        self._build_plugins_tab()
        self._build_mod_files_tab()
        self._build_data_tab()
        self._build_downloads_tab()
        self._build_tracked_tab()
        self._build_endorsed_tab()
        self._build_browse_tab()

    # ------------------------------------------------------------------
    # Executable toolbar — scan / run
    # ------------------------------------------------------------------

    # Extensions detected in the executable dropdown (.exe always, .bat for wrapper support)
    _EXE_SCAN_EXTENSIONS = {".exe", ".bat"}

    # These exes only work correctly when run from the game's Data folder.
    # Only show them in the dropdown if they appear under Data/ in the filemap
    # (i.e. they have been deployed there).
    _DATA_FOLDER_ONLY_EXES = frozenset({
        "OutfitStudio x64.exe",
        "OutfitStudio.exe",
        "BodySlide x64.exe",
        "BodySlide.exe",
    })

    # Exe names (lowercase) hidden from the dropdown by default.  These are
    # helper / sub-process / redistributable executables that are commonly
    # bundled inside mod tool archives but are never meant to be launched
    # directly by the user.  Custom EXEs added via '+ Add custom EXE…'
    # always bypass this filter.  Extend this list here in source when new
    # noise exes are identified — the change ships with the application.
    _EXE_FILTER_DEFAULTS: frozenset[str] = frozenset({
        # DirectXTex utilities (ship with many texture tools / Creation Kit)
        "texconv.exe",
        "bc7.exe",
        "bendr.exe",
        "optimise.exe",
        "loose.exe",
        "layerprep.exe",
        "filter.exe",
        "extract.exe",
        "exclude.exe",
        "bsa.exe",
        "prepparallax.exe",
        "outputqc.exe",
        "makeunpack.exe",
        "loosecopy.exe",
        "extractbsa.exe",
        "exclusions.exe",
        "convert.exe",
        "bendrfilter.exe",
        "alphanormalsql.exe",
        "pgtools.exe",
        "userguide.exe",
        "lodgen.exe",
        "lodgenx64.exe",
        "ssedump.exe",
        "ssedump64.exe",
        "texconvx64.exe",
        "merge.bat",
        "re-uv.bat",
        "treelod.exe",
        "kdiff3.exe",
        "quickbms.exe",
        "quickbms_4gb_files.exe",
        "wcc_lite.exe",
        "fo4dump.exe",
        "fo4dump64.exe",
        "reimport.bat",
        "reimport2.bat",
        "reimport2_4gb_files.bat",
        "reimport3_localizations.bat",
        "reimport_4gb_files.bat",
        "fnvdump.exe",
        "fnvdump64.exe",
        "fo3dump.exe",
        "fo3dump64.exe",
        "tes4dump.exe",
        "tes4dump64.exe",
        "tes5dump.exe",
        "tes5dump64.exe",
        "7z.exe",
        "ffdec_orig.bat",
        "xdelta.exe"
    })

    def refresh_exe_list(self):
        """Scan for .exe and .bat files and populate the dropdown."""
        exes: list[Path] = []
        game_exe_path: Path | None = None

        if self._game is not None:
            # 0. Add the game's own exe (exe_name resolved against game_path)
            game_path = self._game.get_game_path() if hasattr(self._game, "get_game_path") else None
            exe_name = self._game.exe_name if hasattr(self._game, "exe_name") else None
            if game_path and exe_name:
                candidate = game_path / exe_name
                if candidate.is_file():
                    game_exe_path = candidate
                    exes.append(candidate)

            staging = (
                self._game.get_effective_mod_staging_path()
                if hasattr(self._game, "get_mod_staging_path") else None
            )

            # Build a set of Data-folder-only exe names that are actually present
            # under game_path/Data/ (recursively) after deployment.
            data_folder_deployed: set[str] = set()
            if game_path is not None:
                data_dir = game_path / "Data"
                if data_dir.is_dir():
                    for name in self._DATA_FOLDER_ONLY_EXES:
                        for _ in data_dir.rglob(name):
                            data_folder_deployed.add(name)
                            break  # one hit is enough

            # 1. Scan filemap for .exe/.bat files — resolve from the mods staging folder
            if staging is not None and staging.is_dir():
                filemap_path = staging.parent / "filemap.txt"
                if filemap_path.is_file():
                    try:
                        for line in filemap_path.read_text(encoding="utf-8").splitlines():
                            line = line.strip()
                            if not line or "\t" not in line:
                                continue
                            rel_path, mod_name = line.split("\t", 1)
                            rel = Path(rel_path)
                            if rel.suffix.lower() not in self._EXE_SCAN_EXTENSIONS:
                                continue
                            # Exes that require the Data folder are only shown
                            # if they have been deployed there.
                            if rel.name in self._DATA_FOLDER_ONLY_EXES:
                                if rel.name not in data_folder_deployed:
                                    continue
                            mod_dir = staging / mod_name
                            candidate = mod_dir / rel_path
                            if candidate.is_file():
                                exes.append(candidate)
                    except OSError:
                        pass

            # 2. Scan Profiles/<game>/Applications/ for .exe/.bat files (recursive),
            #    excluding custom_exes.json entries (added separately below)
            if staging is not None:
                apps_dir = staging.parent / "Applications"
                if apps_dir.is_dir():
                    for ext in self._EXE_SCAN_EXTENSIONS:
                        for entry in apps_dir.rglob(f"*{ext}"):
                            if entry.is_file() and entry.name not in self._DATA_FOLDER_ONLY_EXES:
                                if not any(part.startswith("prefix_") for part in entry.parts):
                                    exes.append(entry)

            # 3. Custom exes saved via "Add custom EXE" (arbitrary paths on disk)
            for p in self._load_custom_exes():
                if p not in exes:
                    exes.append(p)

        # Sort: game exe first, then Applications/, then custom/filemap entries, alpha within each
        apps_dir_root = None
        if self._game and hasattr(self._game, "get_mod_staging_path"):
            staging = self._game.get_effective_mod_staging_path()
            apps_dir_root = staging.parent / "Applications"

        custom_set = set(self._load_custom_exes())

        def _sort_key(p: Path):
            if game_exe_path is not None and p == game_exe_path:
                return (0, p.name.lower())
            in_apps = apps_dir_root is not None and p.is_relative_to(apps_dir_root)
            if in_apps:
                return (1, p.name.lower())
            if p in custom_set:
                return (2, p.name.lower())
            return (3, p.name.lower())

        exes.sort(key=_sort_key)

        # Apply exe filter — built-in defaults combined with user-hidden list.
        # Custom exes always bypass the filter.
        _filtered_names = self._EXE_FILTER_DEFAULTS | {n.lower() for n in self._load_exe_filter()}
        if _filtered_names:
            exes = [
                p for p in exes
                if p.name.lower() not in _filtered_names or p in custom_set
            ]

        # Auto-populate exe_args.json with default prefixes for known tools
        if self._game is not None and exes:
            try:
                from Utils.exe_args_builder import build_default_exe_args
                build_default_exe_args(exes, self._game, log_fn=self._log)
            except Exception:
                pass

        self._exe_paths = exes
        self._game_exe_path = game_exe_path
        labels = [p.name for p in exes] + [self._ADD_CUSTOM_SENTINEL]
        if not exes:
            labels = ["(no executables)", self._ADD_CUSTOM_SENTINEL]
        self._exe_menu.configure(values=labels)
        if exes:
            self._exe_var.set(labels[0])
            self._on_exe_selected(labels[0])
        else:
            self._exe_var.set("(no executables)")

    def _on_exe_selected(self, name: str):
        """Called when the user selects an exe from the dropdown. Loads saved args if present."""
        if name == self._ADD_CUSTOM_SENTINEL:
            self._add_custom_exe()
            return
        idx = self._exe_var_index()
        if idx < 0 or not self._exe_paths:
            self._exe_args_var.set("")
            self._update_run_exe_btn(None)
            return
        exe_path = self._exe_paths[idx]
        loaded_args = self._load_exe_args(exe_path.name)
        self._exe_args_var.set(self._apply_profile_output_to_args(exe_path.name, loaded_args))
        self._update_run_exe_btn(exe_path)

    def _update_run_exe_btn(self, exe_path: "Path | None") -> None:
        """Switch the Run EXE button to green ▶ Play when the game's launch exe is selected."""
        is_game_exe = (
            exe_path is not None
            and self._game_exe_path is not None
            and exe_path == self._game_exe_path
        )
        if is_game_exe:
            self._run_exe_btn.configure(
                text="▶  Play",
                fg_color="#2d7a2d",
                hover_color="#3a9a3a",
            )
        else:
            self._run_exe_btn.configure(
                text="▶ Run EXE",
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
            )

    _EXE_ARGS_FILE = get_exe_args_path()
    _ADD_CUSTOM_SENTINEL = "+ Add custom EXE…"
    _CUSTOM_EXES_FILE = "custom_exes.json"
    _EXE_FILTER_FILE  = "exe_filter.json"
    _LAUNCH_MODE_FILE = "exe_launch_mode.json"

    def _get_launch_mode_path(self) -> "Path | None":
        """Return path to <game>/Applications/exe_launch_mode.json, or None if no game."""
        if self._game is None:
            return None
        staging = (
            self._game.get_effective_mod_staging_path()
            if hasattr(self._game, "get_mod_staging_path") else None
        )
        if staging is None:
            return None
        return staging.parent / "Applications" / self._LAUNCH_MODE_FILE

    def _load_launch_mode(self, exe_name: str) -> str:
        """Return saved launch mode for exe_name ('auto', 'steam', 'heroic', 'none')."""
        p = self._get_launch_mode_path()
        if p is None or not p.is_file():
            return "auto"
        try:
            return json.loads(p.read_text(encoding="utf-8")).get(exe_name, "auto")
        except (OSError, ValueError):
            return "auto"

    def _save_launch_mode(self, exe_name: str, mode: str) -> None:
        p = self._get_launch_mode_path()
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except (OSError, ValueError):
            data = {}
        data[exe_name] = mode
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load_deploy_before_launch(self) -> bool:
        """Return whether deploy-before-launch is enabled (default True)."""
        p = self._get_launch_mode_path()
        if p is None or not p.is_file():
            return True
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("__deploy_before_launch", True)
        except (OSError, ValueError):
            return True

    def _load_proton_override(self, exe_name: str) -> "str | None":
        """Return saved Proton override name for exe_name, '' for game default, or None if never saved."""
        p = self._get_launch_mode_path()
        if p is None or not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            key = f"__proton_override_{exe_name}"
            if key not in data:
                return None
            return data[key]
        except (OSError, ValueError):
            return None

    def _save_proton_override(self, exe_name: str, proton_name: str) -> None:
        p = self._get_launch_mode_path()
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except (OSError, ValueError):
            data = {}
        key = f"__proton_override_{exe_name}"
        if proton_name:
            data[key] = proton_name
        else:
            data.pop(key, None)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _save_deploy_before_launch(self, enabled: bool) -> None:
        p = self._get_launch_mode_path()
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except (OSError, ValueError):
            data = {}
        data["__deploy_before_launch"] = enabled
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _is_game_exe(self, exe_path: "Path") -> bool:
        """Return True if exe_path is this game's own launcher exe."""
        if self._game is None:
            return False
        game_exe_name = getattr(self._game, "exe_name", None)
        if not game_exe_name:
            return False
        return exe_path.name.lower() == Path(game_exe_name).name.lower()

    def _open_applications_folder(self) -> None:
        """Open the Profiles/<game>/Applications folder in the file manager."""
        if self._game is None:
            self._log("Open Applications folder: no game selected.")
            return
        staging = (
            self._game.get_effective_mod_staging_path()
            if hasattr(self._game, "get_mod_staging_path") else None
        )
        if staging is None:
            self._log("Open Applications folder: could not determine staging path.")
            return
        apps_dir = staging.parent / "Applications"
        apps_dir.mkdir(parents=True, exist_ok=True)
        try:
            xdg_open(apps_dir)
        except Exception as e:
            self._log(f"Could not open Applications folder: {e}")

    def _get_exe_filter_path(self) -> "Path | None":
        """Return path to ~/.config/AmethystModManager/games/<game>/exe_filter.json."""
        if self._game is None:
            return None
        return get_game_config_dir(self._game.name) / self._EXE_FILTER_FILE

    def _load_exe_filter(self) -> "list[str]":
        """Return list of exe names (lowercase) that should be hidden from the dropdown."""
        p = self._get_exe_filter_path()
        if p is None or not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return [s.lower() for s in data if isinstance(s, str)]
        except (OSError, ValueError):
            return []

    def _save_exe_filter(self, names: "list[str]") -> None:
        p = self._get_exe_filter_path()
        if p is None:
            return
        p.write_text(json.dumps([n.lower() for n in names], indent=2), encoding="utf-8")

    def _get_custom_exes_path(self) -> "Path | None":
        """Return path to <game>/Applications/custom_exes.json, or None if no game."""
        if self._game is None:
            return None
        staging = (
            self._game.get_effective_mod_staging_path()
            if hasattr(self._game, "get_mod_staging_path") else None
        )
        if staging is None:
            return None
        return staging.parent / "Applications" / self._CUSTOM_EXES_FILE

    def _load_custom_exes(self) -> "list[Path]":
        """Return list of custom exe Paths saved in custom_exes.json."""
        p = self._get_custom_exes_path()
        if p is None or not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return [Path(s) for s in data if Path(s).is_file()]
        except (OSError, ValueError):
            return []

    def _save_custom_exes(self, paths: "list[Path]") -> None:
        p = self._get_custom_exes_path()
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps([str(x) for x in paths], indent=2), encoding="utf-8")

    _EXE_PICKER_FILTERS = [
        ("Executables (*.exe, *.bat)", ["*.exe", "*.bat"]),
        ("All files", ["*"]),
    ]

    def _on_exe_filter(self) -> None:
        """Open the EXE filter list panel/dialog."""
        app = self.winfo_toplevel()
        show_fn = getattr(app, "show_exe_filter_panel", None)
        if show_fn:
            show_fn(
                load_fn=self._load_exe_filter,
                save_fn=self._save_exe_filter,
                refresh_fn=self.refresh_exe_list,
            )
        else:
            _ExeFilterDialog(
                self.winfo_toplevel(),
                load_fn=self._load_exe_filter,
                save_fn=self._save_exe_filter,
                refresh_fn=self.refresh_exe_list,
            )

    def _add_custom_exe(self) -> None:
        """Open native file browser (XDG portal / zenity), save chosen exe, refresh list."""
        from Utils.portal_filechooser import _run_file_picker_worker

        def _on_picked(chosen: "Path | None") -> None:
            if chosen is None:
                # User cancelled — restore previous selection
                if self._exe_paths:
                    self.after(0, lambda: self._exe_var.set(self._exe_paths[0].name))
                else:
                    self.after(0, lambda: self._exe_var.set("(no executables)"))
                return
            existing = self._load_custom_exes()
            if chosen not in existing:
                existing.append(chosen)
                self._save_custom_exes(existing)

            def _refresh_and_select():
                self.refresh_exe_list()
                for p in self._exe_paths:
                    if p == chosen:
                        self._exe_var.set(p.name)
                        self._on_exe_selected(p.name)
                        break

            self.after(0, _refresh_and_select)

        threading.Thread(
            target=_run_file_picker_worker,
            args=("Select executable", self._EXE_PICKER_FILTERS, _on_picked),
            daemon=True,
        ).start()

    def _load_exe_args(self, exe_name: str) -> str:
        """Load saved args for an exe from Utils/exe_args.json."""
        try:
            import json as _json
            data = _json.loads(self._EXE_ARGS_FILE.read_text(encoding="utf-8"))
            return data.get(exe_name, "")
        except (OSError, ValueError):
            return ""

    def _apply_profile_output_to_args(self, exe_name: str, args_str: str) -> str:
        """If the active profile uses profile-specific mods, rewrite the output
        path in *args_str* so it points at the profile's effective overwrite
        folder. Returns the string unchanged for standard profiles."""
        import re
        game = self._game
        if game is None:
            return args_str
        active_dir = getattr(game, "_active_profile_dir", None)
        if active_dir is None:
            return args_str
        try:
            from gui.game_helpers import profile_uses_specific_mods  # type: ignore
            if not profile_uses_specific_mods(active_dir):
                return args_str
        except Exception:
            return args_str
        from Utils.exe_args_builder import EXE_PROFILES, _to_wine_path  # type: ignore
        profile_def = EXE_PROFILES.get(exe_name)
        if profile_def is None or not profile_def.output_flag:
            return args_str
        new_path = _to_wine_path(game.get_effective_overwrite_path())
        flag_re = re.escape(profile_def.output_flag)
        # Replace flagged argument (quoted path first, then unquoted token)
        result = re.sub(
            rf'({flag_re}\s*)"[^"]*"',
            lambda m: f'{m.group(1)}"{new_path}"',
            args_str,
        )
        if result == args_str:
            result = re.sub(
                rf'({flag_re}\s*)(\S+)',
                lambda m: f'{m.group(1)}"{new_path}"',
                args_str,
            )
        return result

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
        custom_exes = self._load_custom_exes()
        is_game_exe = self._is_game_exe(exe_path)
        saved_launch_mode = self._load_launch_mode(exe_path.name) if is_game_exe else None
        deploy_before_launch = self._load_deploy_before_launch() if is_game_exe else None
        saved_proton_override = self._load_proton_override(exe_path.name) if not is_game_exe else None
        # Determine current hidden state from user filter (builtin filter names
        # are always hidden and can't be toggled, so we only look at the user list).
        user_filter = {n.lower() for n in self._load_exe_filter()}
        is_hidden = exe_path.name.lower() in user_filter
        app = self.winfo_toplevel()
        show_fn = getattr(app, "show_exe_config_panel", None)
        if show_fn:
            def _on_config_done(panel):
                if panel.result is not None:
                    self._exe_args_var.set(panel.result)
                if panel.launch_mode is not None:
                    self._save_launch_mode(exe_path.name, panel.launch_mode)
                if panel.deploy_before_launch is not None:
                    self._save_deploy_before_launch(panel.deploy_before_launch)
                if panel.proton_override is not None:
                    self._save_proton_override(exe_path.name, panel.proton_override)
                if panel.removed:
                    remaining = [p for p in custom_exes if p != exe_path]
                    self._save_custom_exes(remaining)
                    self.refresh_exe_list()
                if panel.hide is not None:
                    name = exe_path.name.lower()
                    current = list(self._load_exe_filter())
                    if panel.hide and name not in current:
                        current.append(name)
                        self._save_exe_filter(current)
                        self.refresh_exe_list()
                    elif not panel.hide and name in current:
                        current.remove(name)
                        self._save_exe_filter(current)
                        self.refresh_exe_list()
            show_fn(
                exe_path=exe_path, game=game, saved_args=saved_args,
                custom_exes=custom_exes, launch_mode=saved_launch_mode,
                deploy_before_launch=deploy_before_launch, is_hidden=is_hidden,
                proton_override=saved_proton_override,
                log_fn=self._log,
                on_done=_on_config_done,
            )
        else:
            dialog = _ExeConfigDialog(
                self.winfo_toplevel(),
                exe_path=exe_path,
                game=game,
                saved_args=saved_args,
                custom_exes=custom_exes,
                launch_mode=saved_launch_mode,
                deploy_before_launch=deploy_before_launch,
                is_hidden=is_hidden,
                proton_override=saved_proton_override,
                log_fn=self._log,
            )
            self.winfo_toplevel().wait_window(dialog)
            if dialog.result is not None:
                self._exe_args_var.set(dialog.result)
            if dialog.launch_mode is not None:
                self._save_launch_mode(exe_path.name, dialog.launch_mode)
            if dialog.deploy_before_launch is not None:
                self._save_deploy_before_launch(dialog.deploy_before_launch)
            if dialog.proton_override is not None:
                self._save_proton_override(exe_path.name, dialog.proton_override)
            if dialog.removed:
                remaining = [p for p in custom_exes if p != exe_path]
                self._save_custom_exes(remaining)
                self.refresh_exe_list()
            if dialog.hide is not None:
                name = exe_path.name.lower()
                current = list(self._load_exe_filter())
                if dialog.hide and name not in current:
                    current.append(name)
                    self._save_exe_filter(current)
                    self.refresh_exe_list()
                elif not dialog.hide and name in current:
                    current.remove(name)
                    self._save_exe_filter(current)
                    self.refresh_exe_list()

    def _exe_var_index(self) -> int:
        """Return the index of the currently selected exe in _exe_paths."""
        name = self._exe_var.get()
        for i, p in enumerate(self._exe_paths):
            if p.name == name:
                return i
        return -1

    # ── .bat wrapper registry ──────────────────────────────────────────
    # Maps lowercase .bat filenames to wrapper launcher methods.
    # When the user tries to "Run" a .bat that has an entry here, the
    # wrapper is invoked instead of launching the .bat through Proton.
    _BAT_WRAPPERS: dict[str, str] = {
        "vramr.bat": "_run_vramr_wrapper",
        "bendr.bat": "_run_bendr_wrapper",
        "parallaxr.bat": "_run_parallaxr_wrapper",
    }

    def _on_run_exe(self):
        """Launch the selected exe/bat in the game's Proton prefix."""
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

        # Check for a native wrapper before falling through to Proton
        wrapper_method = self._BAT_WRAPPERS.get(exe_path.name.lower())
        if wrapper_method is not None:
            getattr(self, wrapper_method)(exe_path)
            return

        is_game_exe = self._is_game_exe(exe_path)

        if is_game_exe and self._load_deploy_before_launch():
            # Run deploy in a background thread, then launch the game when done.
            self._log("Run EXE: deploying mods before launch…")
            self._run_deploy_then_launch(exe_path, game)
            return

        self._launch_exe(exe_path, game)

    def _run_deploy_then_launch(self, exe_path: "Path", game):
        """Deploy mods in a background thread, then call _launch_exe on the main thread."""
        from Utils.filemap import build_filemap
        from Utils.deploy import LinkMode, deploy_root_folder, restore_root_folder, load_per_mod_strip_prefixes

        try:
            topbar = self.winfo_toplevel()._topbar
            profile = topbar._profile_var.get()
        except AttributeError:
            profile = "default"

        game_root = game.get_game_path()

        def _worker():
            def _tlog(msg):
                self.after(0, lambda m=msg: self._log(m))

            try:
                if getattr(game, "restore_before_deploy", True) and hasattr(game, "restore"):
                    try:
                        game.restore(log_fn=_tlog)
                    except RuntimeError:
                        pass
                # Restore Root_Folder using the last-deployed profile's Root_Folder.
                restore_rf_dir = game.get_effective_root_folder_path()
                if restore_rf_dir.is_dir() and game_root:
                    restore_root_folder(restore_rf_dir, game_root, log_fn=_tlog)

                # Switch to the target profile before deploy.
                game.set_active_profile_dir(
                    game.get_profile_root() / "profiles" / profile
                )

                profile_root = game.get_profile_root()
                staging = game.get_effective_mod_staging_path()
                modlist_path = profile_root / "profiles" / profile / "modlist.txt"
                filemap_out = profile_root / "filemap.txt"
                if modlist_path.is_file():
                    try:
                        _exc_raw = read_excluded_mod_files(modlist_path.parent / "excluded_mod_files.json")
                        _exc = {k: set(v) for k, v in _exc_raw.items()} if _exc_raw else None
                        build_filemap(
                            modlist_path, staging, filemap_out,
                            strip_prefixes=game.mod_folder_strip_prefixes or None,
                            per_mod_strip_prefixes=load_per_mod_strip_prefixes(modlist_path.parent),
                            allowed_extensions=game.mod_install_extensions or None,
                            root_deploy_folders=game.mod_root_deploy_folders or None,
                            excluded_mod_files=_exc,
                        )
                    except Exception as fm_err:
                        _tlog(f"Run EXE: filemap rebuild warning: {fm_err}")

                deploy_mode = game.get_deploy_mode() if hasattr(game, "get_deploy_mode") else LinkMode.HARDLINK
                game.deploy(log_fn=_tlog, profile=profile, mode=deploy_mode)

                # Deploy Root_Folder using the target profile's Root_Folder.
                target_rf_dir = game.get_effective_root_folder_path()
                rf_allowed = getattr(game, "root_folder_deploy_enabled", True)
                if rf_allowed and target_rf_dir.is_dir() and game_root:
                    deploy_root_folder(target_rf_dir, game_root, mode=deploy_mode, log_fn=_tlog)

                _tlog("Run EXE: deploy complete, launching…")
                self.after(0, lambda: self._launch_exe(exe_path, game))
            except Exception as e:
                self.after(0, lambda err=e: self._log(f"Run EXE: deploy error: {err}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _launch_exe(self, exe_path: "Path", game):
        """Route to Steam/Heroic/Proton depending on launch mode (game exe) or always Proton."""
        if self._is_game_exe(exe_path):
            mode = self._load_launch_mode(exe_path.name)  # 'auto'|'steam'|'heroic'|'none'
            steam_id = getattr(game, "steam_id", "")
            heroic_app_names = getattr(game, "heroic_app_names", [])

            if mode == "steam":
                if steam_id:
                    self._run_game_via_steam(steam_id)
                else:
                    self._log("Run EXE: launch mode is Steam but game has no Steam ID.")
                return

            if mode == "heroic":
                if heroic_app_names:
                    self._run_game_via_heroic(heroic_app_names)
                else:
                    self._log("Run EXE: launch mode is Heroic but game has no Heroic app name.")
                return

            if mode == "none":
                pass  # fall through to direct Proton launch below

            else:  # "auto"
                if steam_id and self._game_is_steam_install(game):
                    self._run_game_via_steam(steam_id)
                    return
                if heroic_app_names and self._game_is_heroic_install(game):
                    self._run_game_via_heroic(heroic_app_names)
                    return

        self._run_exe_via_proton(exe_path, game)

    def _run_exe_via_proton(self, exe_path: Path, game):
        """Standard Proton launch path for .exe files."""
        from Utils.steam_finder import (
            find_any_installed_proton,
            find_proton_for_game,
            find_steam_root_for_proton_script,
        )

        # Check for a per-exe Proton override (user-selected version with own prefix)
        proton_override_name = self._load_proton_override(exe_path.name)
        if proton_override_name:
            from Utils.steam_finder import list_installed_proton
            # Try exact match first, then prefix match (e.g. "Proton 10" → "Proton 10.0")
            proton_script = find_any_installed_proton(proton_override_name)
            if proton_script is None:
                override_lower = proton_override_name.lower()
                for candidate in list_installed_proton():
                    if candidate.parent.name.lower().startswith(override_lower):
                        proton_script = candidate
                        break
            if proton_script is None:
                self._log(f"Run EXE: Proton override '{proton_override_name}' not found.")
                return
            # Use a dedicated prefix next to the exe so it's isolated from the game prefix
            compat_data = exe_path.parent / f"prefix_{proton_script.parent.name}"
            compat_data.mkdir(parents=True, exist_ok=True)
            self._log(f"Run EXE: using {proton_script.parent.name} with isolated prefix.")
        else:
            prefix_path = (
                game.get_prefix_path()
                if hasattr(game, "get_prefix_path") else None
            )
            if prefix_path is None or not prefix_path.is_dir():
                self._log("Run EXE: Proton prefix not configured for this game.")
                return

            compat_data = prefix_path.parent

            steam_id = getattr(game, "steam_id", "")
            proton_script = find_proton_for_game(steam_id) if steam_id else None
            if proton_script is None:
                # Read the runner name from the prefix's config_info so we use the
                # same Proton version the prefix was built with (e.g. GE-Proton10-28).
                preferred_runner = _read_prefix_runner(compat_data)
                proton_script = find_any_installed_proton(preferred_runner)
                if proton_script is None:
                    if steam_id:
                        self._log(
                            f"Run EXE: could not find Proton version for app {steam_id}, "
                            "and no installed Proton tool was found."
                        )
                    else:
                        self._log("Run EXE: no Steam ID and no installed Proton tool was found.")
                    return
                self._log(
                    f"Run EXE: using fallback Proton tool {proton_script.parent.name} "
                    "(no per-game Steam mapping found)."
                )

        steam_root = find_steam_root_for_proton_script(proton_script)
        if steam_root is None:
            self._log("Run EXE: could not determine Steam root for the selected Proton tool.")
            return

        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
        # Proton expects these to locate the game install and per-game shader/compat caches.
        # Without them GE-Proton (and others) fall back to app ID 0 / skip library detection.
        game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
        if game_path and not proton_override_name:
            env["STEAM_COMPAT_INSTALL_PATH"] = str(game_path)
        if not proton_override_name:
            steam_id = getattr(game, "steam_id", "")
            if steam_id:
                env.setdefault("SteamAppId", steam_id)
                env.setdefault("SteamGameId", steam_id)

        import shlex
        # Re-apply profile-specific output substitution at launch time so the
        # correct path is used even if the profile changed after the exe was
        # selected in the dropdown.
        runtime_args_str = self._apply_profile_output_to_args(
            exe_path.name, self._exe_args_var.get()
        )
        try:
            extra_args = shlex.split(runtime_args_str)
        except ValueError as e:
            self._log(f"Run EXE: invalid arguments — {e}")
            return

        # Inject runtime-only args not saved to exe_args.json
        if exe_path.name in ("xLODGenx64.exe", "xLODGen.exe"):
            from Utils.exe_args_builder import _XLODGEN_GAME_FLAGS
            game_id = getattr(game, "game_id", None)
            xlodgen_flag = _XLODGEN_GAME_FLAGS.get(game_id, "") if game_id else ""
            if xlodgen_flag and xlodgen_flag not in extra_args:
                extra_args.append(xlodgen_flag)

        if exe_path.name == "Wrye Bash.exe":
            game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
            if game_path and "-o" not in extra_args:
                # WB computes its .wbtemp dir from the drive of the game path.
                # Z:\ paths (Linux root mapped by Wine) are not writable, so
                # we create a symlink inside the Wine prefix's drive_c and pass
                # a C:\ path instead — WB then uses C:\users\steamuser\AppData
                # \Local\Temp which is always writable.
                real_game = game_path.resolve()
                c_games = compat_data / "pfx" / "drive_c" / "wb_games"
                c_games.mkdir(parents=True, exist_ok=True)
                link = c_games / real_game.name
                if not link.exists() and not link.is_symlink():
                    link.symlink_to(real_game)
                extra_args += ["-o", f"C:\\wb_games\\{real_game.name}"]

        # For exes that must run from the game's Data folder, resolve the
        # deployed path so both the exe path and cwd point there.
        launch_path = exe_path
        if exe_path.name in self._DATA_FOLDER_ONLY_EXES and game_path is not None:
            data_dir = game_path / "Data"
            for hit in data_dir.rglob(exe_path.name):
                launch_path = hit
                break

        self._log(f"Run EXE: launching {exe_path.name} via {proton_script.parent.name} ...")

        def _worker():
            try:
                subprocess.Popen(
                    ["python3", str(proton_script), "run", str(launch_path)] + extra_args,
                    env=env,
                    cwd=launch_path.parent,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                self.after(0, lambda err=e: self._log(f"Run EXE error: {err}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _game_is_steam_install(self, game) -> bool:
        """Return True if the game folder lives inside a Steam library (steamapps/common)."""
        game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
        if game_path is None:
            return False
        from Utils.steam_finder import find_steam_libraries
        try:
            resolved = game_path.resolve()
            for lib in find_steam_libraries():
                if resolved.is_relative_to(lib.resolve()):
                    return True
        except Exception:
            pass
        return False

    def _run_game_via_steam(self, steam_id: str) -> None:
        """Launch the game through Steam (steam://rungameid) so the Steam API initialises."""
        self._log(f"Run EXE: launching via Steam (app {steam_id}) ...")

        def _worker():
            try:
                subprocess.Popen(
                    ["steam", f"steam://rungameid/{steam_id}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                self.after(0, lambda err=e: self._log(f"Run EXE error: {err}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _game_is_heroic_install(self, game) -> bool:
        """Return True if Heroic knows about this game (it's in an Epic/GOG library)."""
        app_names = getattr(game, "heroic_app_names", [])
        if not app_names:
            return False
        from Utils.heroic_finder import find_heroic_launch_info
        try:
            return find_heroic_launch_info(app_names) is not None
        except Exception:
            return False

    def _run_game_via_heroic(self, heroic_app_names: list) -> None:
        """Launch the game through Heroic (heroic://launch) so Epic/GOG auth initialises."""
        from Utils.heroic_finder import find_heroic_launch_info
        info = find_heroic_launch_info(heroic_app_names)
        if info is None:
            self._log("Run EXE: game not found in Heroic library, falling through to Proton.")
            return
        store, app_name = info
        url = f"heroic://launch/{store}/{app_name}"
        self._log(f"Run EXE: launching via Heroic ({store}/{app_name}) ...")

        def _worker():
            try:
                xdg_open(url)
            except Exception as e:
                self.after(0, lambda err=e: self._log(f"Run EXE error: {err}"))

        threading.Thread(target=_worker, daemon=True).start()

    # ── VRAMr wrapper ────────────────────────────────────────────────

    def _run_vramr_wrapper(self, bat_path: Path):
        """Show a preset picker and run VRAMr natively via the Linux wrapper."""
        game = self._game
        if game is None:
            self._log("VRAMr: no game selected.")
            return

        data_dir = (
            game.get_mod_data_path()
            if hasattr(game, "get_mod_data_path") else None
        )
        if data_dir is None or not data_dir.is_dir():
            self._log("VRAMr: game Data directory not configured or missing.")
            return

        staging = (
            game.get_effective_mod_staging_path()
            if hasattr(game, "get_mod_staging_path") else None
        )
        if staging is None:
            self._log("VRAMr: mod staging path not configured.")
            return

        output_dir = staging / "VRAMr"

        _VRAMrPresetDialog(
            self.winfo_toplevel(),
            bat_dir=bat_path.parent,
            game_data_dir=data_dir,
            output_dir=output_dir,
            log_fn=self._log,
        )

    # ── BENDr wrapper ─────────────────────────────────────────────────

    def _run_bendr_wrapper(self, bat_path: Path):
        """Run BENDr normal-map pipeline via the Linux wrapper."""
        game = self._game
        if game is None:
            self._log("BENDr: no game selected.")
            return

        data_dir = (
            game.get_mod_data_path()
            if hasattr(game, "get_mod_data_path") else None
        )
        if data_dir is None or not data_dir.is_dir():
            self._log("BENDr: game Data directory not configured or missing.")
            return

        staging = (
            game.get_effective_mod_staging_path()
            if hasattr(game, "get_mod_staging_path") else None
        )
        if staging is None:
            self._log("BENDr: mod staging path not configured.")
            return

        output_dir = staging / "BENDr"

        from gui.dialogs import _BENDrRunDialog
        _BENDrRunDialog(
            self.winfo_toplevel(),
            bat_dir=bat_path.parent,
            game_data_dir=data_dir,
            output_dir=output_dir,
            log_fn=self._log,
        )

    # ── ParallaxR wrapper ──────────────────────────────────────────────

    def _run_parallaxr_wrapper(self, bat_path: Path):
        """Run ParallaxR parallax-texture pipeline via the Linux wrapper."""
        game = self._game
        if game is None:
            self._log("ParallaxR: no game selected.")
            return

        data_dir = (
            game.get_mod_data_path()
            if hasattr(game, "get_mod_data_path") else None
        )
        if data_dir is None or not data_dir.is_dir():
            self._log("ParallaxR: game Data directory not configured or missing.")
            return

        staging = (
            game.get_effective_mod_staging_path()
            if hasattr(game, "get_mod_staging_path") else None
        )
        if staging is None:
            self._log("ParallaxR: mod staging path not configured.")
            return

        output_dir = staging / "ParallaxR"

        from gui.dialogs import _ParallaxRRunDialog
        _ParallaxRRunDialog(
            self.winfo_toplevel(),
            bat_dir=bat_path.parent,
            game_data_dir=data_dir,
            output_dir=output_dir,
            log_fn=self._log,
        )

    # ------------------------------------------------------------------
    # Mod Files tab
    # ------------------------------------------------------------------

    def _build_mod_files_tab(self):
        tab = self._tabs.tab("Mod Files")
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=0)

        # Toolbar
        toolbar = tk.Frame(tab, bg=BG_HEADER, height=28, highlightthickness=0)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew")
        toolbar.grid_propagate(False)

        self._mod_files_label = tk.Label(
            toolbar, text="(no mod selected)",
            bg=BG_HEADER, fg=TEXT_DIM,
            font=("Segoe UI", _theme.FS10),
        )
        self._mod_files_label.pack(side="left", padx=8, pady=4)

        # Treeview — styled to match CTkTreeview / Data tab
        from PIL import Image as _PIL_Image
        from gui.ctk_components import ICON_PATH as _ICON_PATH, _load_icon_image as _load_iim
        _im_open  = _load_iim(_ICON_PATH.get("arrow"))
        _im_close = _im_open.rotate(90)
        _im_empty = _PIL_Image.new("RGBA", (15, 15), "#00000000")

        style = ttk.Style()
        style.theme_use("default")
        # Use unique names to avoid conflicting with CTkTreeview's global elements
        _img_open_mf  = ImageTk.PhotoImage(_im_open,  name="img_open_mf",  size=(15, 15))
        _img_close_mf = ImageTk.PhotoImage(_im_close, name="img_close_mf", size=(15, 15))
        _img_empty_mf = ImageTk.PhotoImage(_im_empty, name="img_empty_mf", size=(15, 15))
        self._mf_arrow_images = (_img_open_mf, _img_close_mf, _img_empty_mf)  # keep refs

        try:
            style.element_create("Treeitem.mfindicator", "image", "img_close_mf",
                ("user1", "!user2", "img_open_mf"), ("user2", "img_empty_mf"),
                sticky="w", width=15, height=15)
            style.layout("ModFiles.Treeview.Item", [
                ("Treeitem.padding", {"sticky": "nsew", "children": [
                    ("Treeitem.mfindicator", {"side": "left", "sticky": "nsew"}),
                    ("Treeitem.image",       {"side": "left", "sticky": "nsew"}),
                    ("Treeitem.focus",       {"side": "left", "sticky": "nsew", "children": [
                        ("Treeitem.text",    {"side": "left", "sticky": "nsew"}),
                    ]}),
                ]}),
            ])
        except Exception:
            pass  # element may already exist on re-open

        _bg = BG_DEEP
        _fg = TEXT_MAIN
        style.configure("ModFiles.Treeview",
            background=_bg, foreground=_fg,
            fieldbackground=_bg, borderwidth=0,
            rowheight=22, font=("Segoe UI", _theme.FS10),
            focuscolor=_bg,
        )
        style.map("ModFiles.Treeview",
            background=[("selected", _bg), ("focus", _bg)],
            foreground=[("selected", ACCENT)],
        )
        style.configure("ModFiles.Treeview.Heading",
            background=_bg, foreground=_fg,
            font=("Segoe UI", _theme.FS10, "bold"), relief="flat",
        )

        self._mf_tree = ttk.Treeview(
            tab,
            columns=("check",),
            style="ModFiles.Treeview",
            selectmode="browse",
            show="tree headings",
        )
        self._mf_tree.heading("check", text="")
        self._mf_tree.column("#0", stretch=True, minwidth=150)
        self._mf_tree.column("check", width=30, minwidth=30, stretch=False, anchor="center")

        _sb_bg     = "#383838"
        _sb_trough = "#1a1a1a"
        _sb_active = "#0078d4"
        vsb = tk.Scrollbar(
            tab, orient="vertical", command=self._mf_tree.yview,
            bg=_sb_bg, troughcolor=_sb_trough, activebackground=_sb_active,
            highlightthickness=0, bd=0,
        )
        self._mf_tree.configure(yscrollcommand=vsb.set)
        self._mf_tree.grid(row=1, column=0, sticky="nsew")
        vsb.grid(row=1, column=1, sticky="ns")

        self._mf_tree.bind("<Button-4>", lambda e: self._mf_tree.yview_scroll(-3, "units"))
        self._mf_tree.bind("<Button-5>", lambda e: self._mf_tree.yview_scroll(3, "units"))
        self._mf_tree.bind("<Button-1>", self._on_mf_click)
        self._mf_tree.bind("<space>", self._on_mf_space)

        self._mf_checked: dict[str, bool] = {}   # iid → checked state
        self._mf_iid_to_key: dict[str, str | None] = {}  # iid → rel_key (None for folders)
        self._mf_folder_iids: set[str] = set()

    # Checkbox rendering helpers
    _MF_CHECK   = "☑"
    _MF_UNCHECK = "☐"
    _MF_PARTIAL = "☒"   # folder with some excluded children

    def _mf_check_symbol(self, iid: str) -> str:
        if iid in self._mf_folder_iids:
            children = self._mf_all_leaf_iids(iid)
            if not children:
                return self._MF_CHECK
            all_checked = all(self._mf_checked.get(c, True) for c in children)
            none_checked = not any(self._mf_checked.get(c, True) for c in children)
            if all_checked:
                return self._MF_CHECK
            if none_checked:
                return self._MF_UNCHECK
            return self._MF_PARTIAL
        return self._MF_CHECK if self._mf_checked.get(iid, True) else self._MF_UNCHECK

    def _mf_all_leaf_iids(self, iid: str) -> list[str]:
        result = []
        for child in self._mf_tree.get_children(iid):
            if child in self._mf_folder_iids:
                result.extend(self._mf_all_leaf_iids(child))
            else:
                result.append(child)
        return result

    def _mf_refresh_ancestors(self, iid: str):
        parent = self._mf_tree.parent(iid)
        while parent:
            sym = self._mf_check_symbol(parent)
            self._mf_tree.set(parent, "check", sym)
            parent = self._mf_tree.parent(parent)

    def _on_mf_click(self, event):
        iid = self._mf_tree.identify_row(event.y)
        if not iid:
            return
        # Only toggle when clicking the checkbox column (col #1), not the
        # tree/name column (#0) which handles expand/collapse.
        col = self._mf_tree.identify_column(event.x)
        if col != "#1":
            return
        self._mf_toggle(iid)

    def _on_mf_space(self, event):
        sel = self._mf_tree.selection()
        if sel:
            self._mf_toggle(sel[0])

    def _mf_set_subtree(self, iid: str, new_state: bool):
        """Recursively set all leaves and sub-folder symbols under iid."""
        for child in self._mf_tree.get_children(iid):
            if child in self._mf_folder_iids:
                self._mf_set_subtree(child, new_state)
                self._mf_tree.set(child, "check", self._mf_check_symbol(child))
            else:
                self._mf_checked[child] = new_state
                self._mf_tree.set(child, "check", self._MF_CHECK if new_state else self._MF_UNCHECK)

    def _mf_toggle(self, iid: str):
        if iid in self._mf_folder_iids:
            leaves = self._mf_all_leaf_iids(iid)
            all_checked = all(self._mf_checked.get(c, True) for c in leaves)
            new_state = not all_checked
            self._mf_set_subtree(iid, new_state)
            self._mf_tree.set(iid, "check", self._mf_check_symbol(iid))
            self._mf_refresh_ancestors(iid)
        else:
            current = self._mf_checked.get(iid, True)
            self._mf_checked[iid] = not current
            self._mf_tree.set(iid, "check", self._MF_CHECK if not current else self._MF_UNCHECK)
            self._mf_refresh_ancestors(iid)
        self._mf_save_and_rebuild()

    def _mf_save_and_rebuild(self):
        """Persist current exclusions for the displayed mod and trigger filemap rebuild."""
        if self._mod_files_mod_name is None or self._mod_files_excluded_path is None:
            return
        mod_name = self._mod_files_mod_name
        excluded_keys = [
            self._mf_iid_to_key[iid]
            for iid, checked in self._mf_checked.items()
            if not checked and self._mf_iid_to_key.get(iid) is not None
        ]
        all_excluded = read_excluded_mod_files(self._mod_files_excluded_path)
        if excluded_keys:
            all_excluded[mod_name] = sorted(excluded_keys)
        else:
            all_excluded.pop(mod_name, None)
        write_excluded_mod_files(self._mod_files_excluded_path, all_excluded)
        self._mod_files_excluded = {k: set(v) for k, v in all_excluded.items()}
        self._log(f"Mod Files: saved {len(excluded_keys)} exclusion(s) for '{mod_name}' → {self._mod_files_excluded_path}")
        if self._mod_files_on_change is not None:
            self._mod_files_on_change()

    def show_mod_files(self, mod_name: str | None):
        """Populate the Mod Files tab for the given mod name."""
        self._mod_files_mod_name = mod_name
        # Clear tree
        self._mf_tree.delete(*self._mf_tree.get_children())
        self._mf_checked.clear()
        self._mf_iid_to_key.clear()
        self._mf_folder_iids.clear()

        if mod_name is None:
            self._mod_files_label.configure(text="(no mod selected)")
            return

        self._mod_files_label.configure(text=mod_name)

        # Load current exclusions for this mod
        excluded_keys: set[str] = set()
        if self._mod_files_excluded_path is not None:
            excluded_keys = self._mod_files_excluded.get(mod_name, set())

        # Load file list from mod index
        files: dict[str, str] = {}   # rel_key → rel_str
        if self._mod_files_index_path is not None:
            from Utils.filemap import read_mod_index
            index = read_mod_index(self._mod_files_index_path)
            if index and mod_name in index:
                normal, root = index[mod_name]
                files.update(normal)
                files.update(root)

        if not files:
            self._mf_tree.insert("", "end", text="  (no files found — try refreshing)", tags=("dim",))
            self._mf_tree.tag_configure("dim", foreground=TEXT_DIM)
            return

        # Build tree structure
        tree_dict: dict = {}
        for rel_key, rel_str in sorted(files.items()):
            parts = rel_str.replace("\\", "/").split("/")
            node = tree_dict
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node.setdefault("__files__", []).append((parts[-1], rel_key))

        def insert_node(parent_id, name, subtree, depth=0):
            iid = self._mf_tree.insert(
                parent_id, "end",
                text=name,
                values=(self._MF_CHECK,),
                open=(depth == 0),
            )
            self._mf_folder_iids.add(iid)
            self._mf_iid_to_key[iid] = None
            for child in sorted(k for k in subtree if k != "__files__"):
                insert_node(iid, child, subtree[child], depth + 1)
            for fname, rel_key in sorted(subtree.get("__files__", [])):
                checked = rel_key not in excluded_keys
                leaf_iid = self._mf_tree.insert(
                    iid, "end",
                    text=fname,
                    values=(self._MF_CHECK if checked else self._MF_UNCHECK,),
                )
                self._mf_checked[leaf_iid] = checked
                self._mf_iid_to_key[leaf_iid] = rel_key
            # Set correct folder symbol now that all children exist
            self._mf_tree.set(iid, "check", self._mf_check_symbol(iid))

        for top in sorted(k for k in tree_dict if k != "__files__"):
            insert_node("", top, tree_dict[top])
        # Root-level files (unlikely but handle anyway)
        for fname, rel_key in sorted(tree_dict.get("__files__", [])):
            checked = rel_key not in excluded_keys
            leaf_iid = self._mf_tree.insert(
                "", "end", text=fname,
                values=(self._MF_CHECK if checked else self._MF_UNCHECK,),
            )
            self._mf_checked[leaf_iid] = checked
            self._mf_iid_to_key[leaf_iid] = rel_key

    def _build_data_tab(self):
        tab = self._tabs.tab("Data")
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        toolbar = tk.Frame(tab, bg=BG_HEADER, height=28, highlightthickness=0)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)
        tk.Button(
            toolbar, text="↺ Refresh",
            bg=ACCENT, fg=TEXT_MAIN, activebackground=ACCENT_HOV,
            relief="flat", font=("Segoe UI", _theme.FS10),
            bd=0, cursor="hand2", highlightthickness=0,
            command=self._refresh_data_tab,
        ).pack(side="left", padx=8, pady=2)

        self._data_search_var = tk.StringVar()
        self._data_search_var.trace_add("write", self._on_data_search_changed)
        search_entry = tk.Entry(
            toolbar, textvariable=self._data_search_var,
            bg=BG_DEEP, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=("Segoe UI", _theme.FS10), width=30,
            highlightthickness=0, highlightbackground=BG_DEEP,
        )
        search_entry.pack(side="right", padx=8, pady=3)
        search_entry.bind("<Escape>", lambda e: self._data_search_var.set(""))
        tk.Label(
            toolbar, text="Search:", bg=BG_HEADER, fg=TEXT_DIM,
            font=("Segoe UI", _theme.FS10),
        ).pack(side="right")

        self._data_tree = CTkTreeview(
            tab,
            columns=("mod",),
            headings={"#0": "Path", "mod": "Winning Mod"},
            column_config={
                "#0": {"minwidth": 200, "stretch": True},
                "mod": {"minwidth": 160, "width": 200, "stretch": False},
            },
            selectmode="browse",
            show_label=False,
        )
        self._data_tree.grid(row=1, column=0, sticky="nsew")

        self._data_tree.treeview.bind("<Button-4>",
            lambda e: self._data_tree.treeview.yview_scroll(-3, "units"))
        self._data_tree.treeview.bind("<Button-5>",
            lambda e: self._data_tree.treeview.yview_scroll(3, "units"))
        self._data_tree.treeview.bind("<<TreeviewSelect>>", self._on_data_file_selected)

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
        raw_entries = self._parse_filemap(filemap_path)
        self._data_filemap_entries = self._resolve_data_entries(raw_entries)
        self._build_data_tree_from_entries(self._data_filemap_entries)

    def _resolve_data_entries(self, entries):
        """For UE5 games, prefix each entry's path with its resolved deploy destination
        so the Data tab shows where files will actually land in the game."""
        from Games.ue5_game import UE5Game
        game = self._game
        if not isinstance(game, UE5Game):
            return entries
        resolved = []
        for rel_path, mod_name in entries:
            rule = game._match_rule(rel_path)
            if rule is not None:
                dest = rule.dest
                final_rel = game._apply_strip(rel_path, rule.strip)
            else:
                dest = game.ue5_default_dest
                final_rel = rel_path.replace("\\", "/")
            if dest:
                full_path = dest + "/" + final_rel
            else:
                full_path = final_rel
            resolved.append((full_path, mod_name))
        return resolved

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

    def _on_data_file_selected(self, _event=None):
        """When a file row is selected in the Data tab, highlight its mod in the modlist."""
        sel = self._data_tree.treeview.selection()
        if not sel:
            return
        item = sel[0]
        values = self._data_tree.treeview.item(item, "values")
        mod_name = values[0] if values else ""
        if not mod_name:
            # Folder row — clear highlight
            if self._on_plugin_selected_cb is not None:
                self._on_plugin_selected_cb(None)
            return
        if self._on_mod_selected_cb is not None:
            self._on_mod_selected_cb()
        if self._on_plugin_selected_cb is not None:
            self._on_plugin_selected_cb(mod_name)

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
        cancel_event_tracked = mod_panel.get_download_cancel_event() if mod_panel else None
        if mod_panel:
            mod_panel.show_download_progress(f"Installing: {mod_name}", cancel=cancel_event_tracked)
        log_fn = self._log

        def _worker():
            downloader = getattr(app, "_nexus_downloader", None)
            if downloader is None:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress(cancel=cancel_event_tracked) if mod_panel else None,
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
                        mod_panel.hide_download_progress(cancel=cancel_event_tracked)
                    log_fn("Tracked Mods: Premium required for direct download.")
                    log_fn(f"Tracked Mods: Opening files page — click \"Download with Mod Manager\" there.")
                    log_fn(f"Tracked Mods: {files_url}")
                    try:
                        open_url(files_url)
                    except Exception as exc:
                        log_fn(f"Tracked Mods: Could not open browser — {exc}")
                app.after(0, _fallback)
                return

            # Premium user — find the latest MAIN file and download directly
            file_info = None
            try:
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
                    mod_panel.hide_download_progress(cancel=cancel_event_tracked) if mod_panel else None,
                    log_fn(f"Tracked Mods: Could not fetch file list — {exc}"),
                ))
                return

            if file_info is None:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress(cancel=cancel_event_tracked) if mod_panel else None,
                    log_fn(f"Tracked Mods: No files found for '{mod_name}'."),
                ))
                return

            result = downloader.download_file(
                game_domain=domain,
                mod_id=mod_id,
                file_id=file_info.file_id,
                progress_cb=lambda cur, total: app.after(
                    0, lambda c=cur, t=total: (
                        mod_panel.update_download_progress(c, t, cancel=cancel_event_tracked)
                        if mod_panel else None
                    )
                ),
                cancel=cancel_event_tracked,
                known_file_name=file_info.file_name,
            )

            if result.success and result.file_path:
                def _install():
                    # Defer if a modal dialog (e.g. FOMOD wizard) currently has
                    # the input grab — running install_mod_from_archive inside
                    # wait_window's event loop causes the UI to freeze.
                    try:
                        if app.grab_current() is not None:
                            app.after(500, _install)
                            return
                    except Exception:
                        pass
                    if mod_panel:
                        mod_panel.hide_download_progress(cancel=cancel_event_tracked)
                    log_fn(f"Tracked Mods: Installing '{mod_name}'...")
                    try:
                        _prebuilt = build_meta_from_download(
                            game_domain=domain,
                            mod_id=mod_id,
                            file_id=file_info.file_id,
                            archive_name=result.file_name,
                            mod_info=entry,
                            file_info=file_info,
                        )
                    except Exception:
                        _prebuilt = None
                    install_mod_from_archive(
                        str(result.file_path), app, log_fn, game, mod_panel,
                        prebuilt_meta=_prebuilt)
                app.after(0, _install)
            else:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress(cancel=cancel_event_tracked) if mod_panel else None,
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
        cancel_event_endorsed = mod_panel.get_download_cancel_event() if mod_panel else None
        if mod_panel:
            mod_panel.show_download_progress(f"Installing: {mod_name}", cancel=cancel_event_endorsed)
        log_fn = self._log

        def _worker():
            downloader = getattr(app, "_nexus_downloader", None)
            if downloader is None:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress(cancel=cancel_event_endorsed) if mod_panel else None,
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
                        mod_panel.hide_download_progress(cancel=cancel_event_endorsed)
                    log_fn("Endorsed Mods: Premium required for direct download.")
                    log_fn(f"Endorsed Mods: Opening files page — click \"Download with Mod Manager\" there.")
                    log_fn(f"Endorsed Mods: {files_url}")
                    try:
                        open_url(files_url)
                    except Exception as exc:
                        log_fn(f"Endorsed Mods: Could not open browser — {exc}")
                app.after(0, _fallback)
                return

            # Premium user — find the latest MAIN file and download directly
            file_info = None
            try:
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
                    mod_panel.hide_download_progress(cancel=cancel_event_endorsed) if mod_panel else None,
                    log_fn(f"Endorsed Mods: Could not fetch file list — {exc}"),
                ))
                return

            if file_info is None:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress(cancel=cancel_event_endorsed) if mod_panel else None,
                    log_fn(f"Endorsed Mods: No files found for '{mod_name}'."),
                ))
                return

            result = downloader.download_file(
                game_domain=domain,
                mod_id=mod_id,
                file_id=file_info.file_id,
                progress_cb=lambda cur, total: app.after(
                    0, lambda c=cur, t=total: (
                        mod_panel.update_download_progress(c, t, cancel=cancel_event_endorsed)
                        if mod_panel else None
                    )
                ),
                cancel=cancel_event_endorsed,
                known_file_name=file_info.file_name,
            )

            if result.success and result.file_path:
                def _install():
                    # Defer if a modal dialog (e.g. FOMOD wizard) currently has
                    # the input grab — running install_mod_from_archive inside
                    # wait_window's event loop causes the UI to freeze.
                    try:
                        if app.grab_current() is not None:
                            app.after(500, _install)
                            return
                    except Exception:
                        pass
                    if mod_panel:
                        mod_panel.hide_download_progress(cancel=cancel_event_endorsed)
                    log_fn(f"Endorsed Mods: Installing '{mod_name}'...")
                    try:
                        _prebuilt = build_meta_from_download(
                            game_domain=domain,
                            mod_id=mod_id,
                            file_id=file_info.file_id,
                            archive_name=result.file_name,
                            mod_info=entry,
                            file_info=file_info,
                        )
                    except Exception:
                        _prebuilt = None
                    install_mod_from_archive(
                        str(result.file_path), app, log_fn, game, mod_panel,
                        prebuilt_meta=_prebuilt)
                app.after(0, _install)
            else:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress(cancel=cancel_event_endorsed) if mod_panel else None,
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
        cancel_event_browse = mod_panel.get_download_cancel_event() if mod_panel else None
        if mod_panel:
            mod_panel.show_download_progress(f"Installing: {mod_name}", cancel=cancel_event_browse)
        log_fn = self._log

        def _worker():
            downloader = getattr(app, "_nexus_downloader", None)
            if downloader is None:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress(cancel=cancel_event_browse) if mod_panel else None,
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
                        mod_panel.hide_download_progress(cancel=cancel_event_browse)
                    log_fn("Browse: Premium required for direct download.")
                    log_fn(f'Browse: Opening files page — click "Download with Mod Manager" there.')
                    log_fn(f"Browse: {files_url}")
                    try:
                        open_url(files_url)
                    except Exception as exc:
                        log_fn(f"Browse: Could not open browser — {exc}")
                app.after(0, _fallback)
                return

            file_info = None
            try:
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
                    mod_panel.hide_download_progress(cancel=cancel_event_browse) if mod_panel else None,
                    log_fn(f"Browse: Could not fetch file list — {exc}"),
                ))
                return

            if file_info is None:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress(cancel=cancel_event_browse) if mod_panel else None,
                    log_fn(f"Browse: No files found for '{mod_name}'."),
                ))
                return

            result = downloader.download_file(
                game_domain=domain,
                mod_id=mod_id,
                file_id=file_info.file_id,
                progress_cb=lambda cur, total: app.after(
                    0, lambda c=cur, t=total: (
                        mod_panel.update_download_progress(c, t, cancel=cancel_event_browse)
                        if mod_panel else None
                    )
                ),
                cancel=cancel_event_browse,
                known_file_name=file_info.file_name,
            )

            if result.success and result.file_path:
                def _install():
                    if mod_panel:
                        mod_panel.hide_download_progress(cancel=cancel_event_browse)
                    log_fn(f"Browse: Installing '{mod_name}'...")
                    # Build metadata now (we already have entry + file_info)
                    # and pass it straight into the installer so it doesn't
                    # need to fire extra get_mod / get_mod_files API calls.
                    try:
                        _prebuilt = build_meta_from_download(
                            game_domain=domain,
                            mod_id=mod_id,
                            file_id=file_info.file_id,
                            archive_name=result.file_name,
                            mod_info=entry,
                            file_info=file_info,
                        )
                    except Exception:
                        _prebuilt = None
                    install_mod_from_archive(
                        str(result.file_path), app, log_fn, game, mod_panel,
                        prebuilt_meta=_prebuilt)
                app.after(0, _install)
            else:
                app.after(0, lambda: (
                    mod_panel.hide_download_progress(cancel=cancel_event_browse) if mod_panel else None,
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

        def _cleanup():
            self._downloads_panel.refresh()

        disable_extract = getattr(topbar, "_disable_extract", False)
        install_mod_from_archive(archive_path, app, self._log, game, mod_panel,
                                 on_installed=_cleanup,
                                 disable_extract=disable_extract)

    def _build_plugins_tab(self):
        tab = self._tabs.tab("Plugins")
        tab.grid_rowconfigure(2, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        self._pheader = ctk.CTkFrame(tab, fg_color=BG_HEADER, corner_radius=0, height=28)
        self._pheader.grid(row=0, column=0, sticky="ew")
        self._pheader.grid_propagate(False)
        self._pheader_labels: list[ctk.CTkLabel] = []

        # Framework status banners (populated by _refresh_framework_banners)
        self._framework_banners_frame = ctk.CTkFrame(
            tab, fg_color=BG_PANEL, corner_radius=0
        )
        self._framework_banners_frame.grid(row=1, column=0, sticky="ew")
        self._framework_banners_frame.grid_columnconfigure(0, weight=1)

        canvas_frame = tk.Frame(tab, bg=BG_DEEP, bd=0, highlightthickness=0)
        canvas_frame.grid(row=2, column=0, sticky="nsew")
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
        self._pvsb.bind("<B1-Motion>",          lambda e: self._schedule_predraw())
        self._pcanvas.bind("<ButtonPress-1>",   self._on_pmouse_press)
        self._pcanvas.bind("<B1-Motion>",       self._on_pmouse_drag)
        self._pcanvas.bind("<ButtonRelease-1>", self._on_pmouse_release)
        self._pcanvas.bind("<Motion>",          self._on_pmouse_motion)
        self._pcanvas.bind("<Leave>",           self._on_pmouse_leave)
        self._pcanvas.bind("<ButtonRelease-3>", self._on_plugin_right_click)

        toolbar = ctk.CTkFrame(tab, fg_color=BG_PANEL, corner_radius=0, height=36)
        toolbar.grid(row=3, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        ctk.CTkButton(
            toolbar, text="Sort Plugins", width=110, height=26,
            fg_color="#2e6b30", hover_color="#3a8a3d",
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._sort_plugins_loot,
        ).pack(side="left", padx=8, pady=5)

        self._create_pool()

    # ------------------------------------------------------------------
    # Virtual-list pool
    # ------------------------------------------------------------------

    def _create_pool(self) -> None:
        """Pre-allocate a fixed set of canvas items and checkbutton widgets."""
        c = self._pcanvas
        for s in range(self._pool_size):
            self._pool_data_idx.append(-1)

            bg_id = c.create_rectangle(0, -200, 0, -200, fill="", outline="", state="hidden")
            name_id = c.create_text(0, -200, text="", anchor="w", fill="",
                                    font=("Segoe UI", _theme.FS11), state="hidden")
            idx_id = c.create_text(0, -200, text="", anchor="center", fill="",
                                   font=("Segoe UI", _theme.FS10), state="hidden")
            warn_id: int | None = None
            if self._warning_icon:
                warn_id = c.create_image(0, -200, image=self._warning_icon,
                                         anchor="center", state="hidden")

            self._pool_bg.append(bg_id)
            self._pool_name.append(name_id)
            self._pool_idx_text.append(idx_id)
            self._pool_warn.append(warn_id)

            cb_tag = f"pcb_{s}"
            cb_rect = c.create_rectangle(
                0, -200, 0, -200, outline=BORDER, width=1, state="hidden",
                tags=(cb_tag, "pcb"),
            )
            cb_mark = c.create_text(
                0, -200, text="✓", anchor="center", fill=ACCENT,
                font=("Segoe UI", _theme.FS12, "bold"), state="hidden",
                tags=(cb_tag, "pcb"),
            )
            self._pool_check_rects.append(cb_rect)
            self._pool_check_marks.append(cb_mark)
            def _cb_release(e, slot=s):
                self._on_pool_check_toggle(slot)
                return "break"
            def _cb_enter(e):
                c.config(cursor="hand2")
            def _cb_leave(e):
                c.config(cursor="")
            c.tag_bind(cb_tag, "<ButtonRelease-1>", _cb_release)
            c.tag_bind(cb_tag, "<Enter>", _cb_enter)
            c.tag_bind(cb_tag, "<Leave>", _cb_leave)

            lk_tag = f"plk_{s}"
            lk_rect = c.create_rectangle(
                0, -200, 0, -200, outline=BORDER, width=1, state="hidden",
                tags=(lk_tag, "plk"),
            )
            if self._icon_lock:
                lk_mark = c.create_image(
                    0, -200, anchor="center",
                    image=self._icon_lock,
                    state="hidden",
                    tags=(lk_tag, "plk"),
                )
            else:
                lk_mark = c.create_text(
                    0, -200, text="🔒", anchor="center", fill=TEXT_MAIN,
                    font=("Segoe UI", _theme.FS9), state="hidden",
                    tags=(lk_tag, "plk"),
                )
            self._pool_lock_rects.append(lk_rect)
            self._pool_lock_marks.append(lk_mark)
            def _lk_release(e, slot=s):
                self._on_pool_lock_toggle(slot)
                return "break"
            c.tag_bind(lk_tag, "<ButtonRelease-1>", _lk_release)
            c.tag_bind(lk_tag, "<Enter>", _cb_enter)
            c.tag_bind(lk_tag, "<Leave>", _cb_leave)

    def _on_pool_check_toggle(self, slot: int) -> None:
        """A pooled enable-checkbox was clicked — map back to data row."""
        data_idx = self._pool_data_idx[slot] if slot < len(self._pool_data_idx) else -1
        if data_idx < 0 or data_idx >= len(self._plugin_entries):
            return
        entry = self._plugin_entries[data_idx]
        if entry.name.lower() in self._vanilla_plugins:
            return
        entry.enabled = not entry.enabled
        self._save_plugins()
        self._check_all_masters()
        self._predraw()

    def _on_pool_lock_toggle(self, slot: int) -> None:
        """A pooled lock-checkbox was clicked — map back to data row."""
        data_idx = self._pool_data_idx[slot] if slot < len(self._pool_data_idx) else -1
        if data_idx < 0 or data_idx >= len(self._plugin_entries):
            return
        name = self._plugin_entries[data_idx].name
        locked = not bool(self._plugin_locks.get(name, False))
        if locked:
            self._plugin_locks[name] = True
        else:
            self._plugin_locks.pop(name, None)
        self._save_plugin_locks()
        self._predraw()

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
        staging_root = game.get_effective_mod_staging_path()

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
        flags_w = 50
        flags_x = max(80, w - idx_w - lock_w - flags_w)
        self._pcol_x = [4, 32, flags_x, flags_x + flags_w, flags_x + flags_w + lock_w]

    def _update_plugin_header(self, w: int):
        """Rebuild header labels to match current column positions."""
        # Guard against callbacks firing after the widget has been destroyed.
        try:
            if not self._pheader.winfo_exists():
                return
        except Exception:
            return
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
                font=("Segoe UI", _theme.FS11, "bold"), fg=TEXT_SEP, bg=BG_HEADER,
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

    def set_highlighted_plugins(self, mod_name: str | None):
        """Highlight plugins belonging to the given mod (orange), e.g. when a mod is selected."""
        if mod_name is None:
            new_highlighted = set()
        else:
            new_highlighted = {p for p, m in self._plugin_mod_map.items() if m == mod_name}
        if new_highlighted != self._highlighted_plugins:
            self._highlighted_plugins = new_highlighted
            self._predraw()

    # ------------------------------------------------------------------
    # Plugins tab refresh (canvas-based)
    # ------------------------------------------------------------------

    # Colours for framework status banners
    _FW_GREEN_BG   = "#1b4d1b"
    _FW_GREEN_TEXT = "#c8ffc8"
    _FW_RED_BG     = "#4d1b1b"
    _FW_RED_TEXT   = "#ffc8c8"

    def _refresh_framework_banners(self) -> None:
        """Rebuild the framework status banners at the top of the Plugins tab."""
        # Destroy old banners and hide the container until we know there's content
        for widget in self._framework_banner_widgets:
            widget.destroy()
        self._framework_banner_widgets.clear()
        self._framework_banners_frame.grid_remove()

        if self._game is None:
            return

        frameworks: dict[str, str] = {}
        try:
            frameworks = self._game.frameworks
        except Exception:
            return

        if not frameworks:
            return

        game_root: "Path | None" = None
        try:
            game_root = self._game.get_game_path() if hasattr(self._game, "get_game_path") else None
        except Exception:
            pass

        root_folder: "Path | None" = None
        try:
            root_folder = self._game.get_effective_root_folder_path()
        except Exception:
            pass

        # Show the container now that we know there's at least one banner to display
        self._framework_banners_frame.grid(row=1, column=0, sticky="ew")

        for idx, (label, exe) in enumerate(frameworks.items()):
            exe_path = Path(exe)
            present = False
            # Check game root directory
            if game_root is not None:
                if (game_root / exe_path).is_file():
                    present = True
            # Check profile Root_Folder staging directory
            if not present and root_folder is not None:
                if (root_folder / exe_path).is_file():
                    present = True

            if present:
                bg    = self._FW_GREEN_BG
                fg    = self._FW_GREEN_TEXT
                msg   = f"✔  {label} Installed"
            else:
                bg    = self._FW_RED_BG
                fg    = self._FW_RED_TEXT
                msg   = f"✘  {label} Not Present"

            row_frame = ctk.CTkFrame(
                self._framework_banners_frame,
                fg_color=bg,
                corner_radius=0,
                height=22,
            )
            row_frame.grid(row=idx, column=0, sticky="ew", padx=0, pady=(1, 0))
            row_frame.grid_propagate(False)

            ctk.CTkLabel(
                row_frame,
                text=msg,
                font=_theme.FONT_SMALL,
                text_color=fg,
                fg_color="transparent",
                anchor="w",
            ).pack(side="left", padx=10, fill="y", expand=False)

            self._framework_banner_widgets.append(row_frame)

    def _refresh_plugins_tab(self) -> None:
        """Reload plugin entries from plugins.txt and redraw."""
        self._refresh_framework_banners()
        self._sel_idx = -1
        self._psel_set = set()
        self._drag_idx = -1
        self._highlighted_plugins = set()

        if self._plugins_path is None or not self._plugin_extensions:
            self._plugin_entries = []
            self._predraw()
            return

        self._load_plugin_locks()
        mod_entries = read_plugins(self._plugins_path)
        mod_map = {e.name.lower(): e for e in mod_entries}

        loadorder_path = self._plugins_path.parent / "loadorder.txt"
        saved_order = read_loadorder(loadorder_path)

        if saved_order:
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

            for e in mod_entries:
                if e.name.lower() not in seen:
                    ordered.append(e)
                    seen.add(e.name.lower())

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

        self._check_all_masters()

        # Sync loadorder.txt: prune removed entries and append new ones
        known_lower = {e.name.lower() for e in self._plugin_entries}
        lo_lower = {n.lower() for n in saved_order}
        pruned_lo = [n for n in saved_order if n.lower() in known_lower]
        new_entries = [e.name for e in self._plugin_entries if e.name.lower() not in lo_lower]
        final_lo = pruned_lo + new_entries
        if final_lo != saved_order:
            write_loadorder(loadorder_path, [PluginEntry(name=n, enabled=True) for n in final_lo])

        self._predraw()

    def _save_plugins(self) -> None:
        """Write current plugin entries to plugins.txt and loadorder.txt.

        plugins.txt — mod plugins only (vanilla excluded, the game strips them).
        loadorder.txt — full order including vanilla, so their LOOT-sorted
        positions are preserved across refreshes.
        """
        if self._plugins_path is None:
            return
        mod_entries: list[PluginEntry] = []
        for entry in self._plugin_entries:
            if entry.name.lower() not in self._vanilla_plugins:
                mod_entries.append(entry)
        write_plugins(self._plugins_path, mod_entries)
        write_loadorder(self._plugins_path.parent / "loadorder.txt", self._plugin_entries)

    # ------------------------------------------------------------------
    # Canvas drawing
    # ------------------------------------------------------------------

    def _predraw(self):
        """Redraw by reconfiguring the pre-allocated pool of canvas items."""
        self._predraw_after_id = None
        c = self._pcanvas
        cw = self._pcanvas_w
        entries = self._plugin_entries
        dragging = self._drag_idx >= 0 and self._drag_moved
        n = len(entries)
        total_h = n * self.ROW_H

        canvas_top = int(c.canvasy(0))
        canvas_h = c.winfo_height()
        first_row = max(0, canvas_top // self.ROW_H)
        last_row = min(n, (canvas_top + canvas_h) // self.ROW_H + 2)
        vis_count = last_row - first_row

        for s in range(self._pool_size):
            row = first_row + s
            if s < vis_count and row < n:
                entry = entries[row]
                y_top = row * self.ROW_H
                y_bot = y_top + self.ROW_H
                y_mid = y_top + self.ROW_H // 2

                is_sel = (row in self._psel_set) or (row == self._drag_idx and self._drag_moved)
                if is_sel:
                    bg = BG_SELECT
                elif entry.name in self._highlighted_plugins:
                    bg = plugin_mod
                elif row == self._phover_idx:
                    bg = BG_HOVER_ROW
                else:
                    bg = BG_ROW if row % 2 == 0 else BG_ROW_ALT

                c.coords(self._pool_bg[s], 0, y_top, cw, y_bot)
                c.itemconfigure(self._pool_bg[s], fill=bg, state="normal")

                name_color = TEXT_DIM if not entry.enabled else TEXT_MAIN
                name_max_px = self._pcol_x[2] - self._pcol_x[1] - 4
                name_font = ("Segoe UI", 11)
                display_name = _truncate_plugin_name(c, entry.name, name_font, name_max_px)
                c.coords(self._pool_name[s], self._pcol_x[1], y_mid)
                c.itemconfigure(self._pool_name[s], text=display_name,
                                fill=name_color, state="normal")

                c.coords(self._pool_idx_text[s], self._pcol_x[4] + 25, y_mid)
                c.itemconfigure(self._pool_idx_text[s], text=f"{row:03d}",
                                fill=TEXT_DIM, state="normal")

                warn_id = self._pool_warn[s]
                if warn_id is not None:
                    if entry.name in self._missing_masters:
                        flags_mid_x = (self._pcol_x[2] + self._pcol_x[3]) // 2
                        c.coords(warn_id, flags_mid_x, y_mid)
                        c.itemconfigure(warn_id, state="normal")
                    else:
                        c.itemconfigure(warn_id, state="hidden")

                self._pool_data_idx[s] = row

                if not dragging:
                    is_vanilla = entry.name.lower() in self._vanilla_plugins
                    cb_cx = self._pcol_x[0] + 12
                    cb_size = 14
                    cx1, cy1 = cb_cx - cb_size // 2, y_mid - cb_size // 2
                    cx2, cy2 = cb_cx + cb_size // 2, y_mid + cb_size // 2
                    c.coords(self._pool_check_rects[s], cx1, cy1, cx2, cy2)
                    c.itemconfigure(self._pool_check_rects[s],
                                    fill=BG_DEEP if entry.enabled else bg,
                                    state="normal")
                    c.coords(self._pool_check_marks[s], cb_cx, y_mid)
                    c.itemconfigure(self._pool_check_marks[s],
                                    fill=TEXT_DIM if is_vanilla else ACCENT,
                                    state="normal" if entry.enabled else "hidden")

                    is_locked = bool(self._plugin_locks.get(entry.name, False))
                    lk_cx = self._pcol_x[3] + 12
                    c.coords(self._pool_lock_rects[s], lk_cx - cb_size // 2, cy1,
                             lk_cx + cb_size // 2, cy2)
                    c.itemconfigure(self._pool_lock_rects[s],
                                    fill=BG_DEEP if is_locked else bg,
                                    state="normal")
                    c.coords(self._pool_lock_marks[s], lk_cx, y_mid)
                    c.itemconfigure(self._pool_lock_marks[s],
                                    state="normal" if is_locked else "hidden")
                else:
                    c.itemconfigure(self._pool_check_rects[s], state="hidden")
                    c.itemconfigure(self._pool_check_marks[s], state="hidden")
                    c.itemconfigure(self._pool_lock_rects[s], state="hidden")
                    c.itemconfigure(self._pool_lock_marks[s], state="hidden")
            else:
                c.itemconfigure(self._pool_bg[s], state="hidden")
                c.itemconfigure(self._pool_name[s], state="hidden")
                c.itemconfigure(self._pool_idx_text[s], state="hidden")
                if self._pool_warn[s] is not None:
                    c.itemconfigure(self._pool_warn[s], state="hidden")
                c.itemconfigure(self._pool_check_rects[s], state="hidden")
                c.itemconfigure(self._pool_check_marks[s], state="hidden")
                c.itemconfigure(self._pool_lock_rects[s], state="hidden")
                c.itemconfigure(self._pool_lock_marks[s], state="hidden")
                self._pool_data_idx[s] = -1

        c.configure(scrollregion=(0, 0, cw, max(total_h, canvas_h)))

    def _schedule_predraw(self) -> None:
        """Debounced _predraw — coalesces rapid scroll/resize events."""
        if self._predraw_after_id is not None:
            self.after_cancel(self._predraw_after_id)
        self._predraw_after_id = self.after_idle(self._predraw)

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
            font=("Segoe UI", _theme.FS10), padx=8, pady=4,
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

    def _update_row_bg(self, data_row: int) -> None:
        """Update just the background colour of a single data row's pool slot."""
        for s in range(self._pool_size):
            if self._pool_data_idx[s] == data_row:
                entry = self._plugin_entries[data_row] if data_row < len(self._plugin_entries) else None
                is_sel = data_row in self._psel_set
                if is_sel:
                    bg = BG_SELECT
                elif entry and entry.name in self._highlighted_plugins:
                    bg = plugin_mod
                elif data_row == self._phover_idx:
                    bg = BG_HOVER_ROW
                else:
                    bg = BG_ROW if data_row % 2 == 0 else BG_ROW_ALT
                self._pcanvas.itemconfigure(self._pool_bg[s], fill=bg)
                break

    def _on_pmouse_motion(self, event) -> None:
        """Show tooltip when hovering over a warning icon in the Flags column, and update hover highlight."""
        canvas_y = int(self._pcanvas.canvasy(event.y))
        row = canvas_y // self.ROW_H
        if row < 0 or row >= len(self._plugin_entries):
            self._hide_tooltip()
            if self._phover_idx != -1:
                old = self._phover_idx
                self._phover_idx = -1
                self._update_row_bg(old)
            return

        if row != self._phover_idx:
            old = self._phover_idx
            self._phover_idx = row
            if old >= 0:
                self._update_row_bg(old)
            self._update_row_bg(row)

        x = event.x
        if len(self._pcol_x) >= 5 and self._pcol_x[2] <= x < self._pcol_x[3]:
            entry = self._plugin_entries[row]
            missing = self._missing_masters.get(entry.name)
            if missing:
                screen_x = event.x_root
                screen_y = event.y_root
                text = "Missing masters:\n" + "\n".join(f"  - {m}" for m in missing)
                if self._tooltip_win is None:
                    self._show_tooltip(screen_x, screen_y, text)
                return

        self._hide_tooltip()

    def _on_pmouse_leave(self, event) -> None:
        self._hide_tooltip()
        if self._phover_idx != -1:
            old = self._phover_idx
            self._phover_idx = -1
            self._update_row_bg(old)

    # ------------------------------------------------------------------
    # Scroll events
    # ------------------------------------------------------------------

    def _on_pcanvas_resize(self, event):
        self._pcanvas_w = event.width
        if hasattr(self, '_pcanvas_resize_after_id') and self._pcanvas_resize_after_id:
            self.after_cancel(self._pcanvas_resize_after_id)
        self._pcanvas_resize_after_id = self.after(250, lambda w=event.width: self._apply_pcanvas_resize(w))

    def _apply_pcanvas_resize(self, width: int):
        self._layout_plugin_cols(width)
        self._update_plugin_header(width)
        _truncate_cache.clear()
        self._schedule_predraw()

    def _on_pscroll_up(self, _event):
        self._pcanvas.yview("scroll", -50, "units")
        self._schedule_predraw()

    def _on_pscroll_down(self, _event):
        self._pcanvas.yview("scroll", 50, "units")
        self._schedule_predraw()

    def _on_pmousewheel(self, event):
        self._pcanvas.yview("scroll", -50 if event.delta > 0 else 50, "units")
        self._schedule_predraw()

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
        self._highlighted_plugins = set()  # clear mod→plugin highlight when selecting a plugin
        self._predraw()
        plugin_name = self._plugin_entries[idx].name
        if self._on_mod_selected_cb is not None:
            self._on_mod_selected_cb()
        if self._on_plugin_selected_cb is not None:
            mod_name = self._plugin_mod_map.get(plugin_name)
            self._on_plugin_selected_cb(mod_name)

    def _on_pmouse_drag(self, event):
        if self._drag_idx < 0 or not self._plugin_entries:
            return

        h = self._pcanvas.winfo_height()
        if event.y < 40:
            self._pcanvas.yview("scroll", -1, "units")
        elif event.y > h - 40:
            self._pcanvas.yview("scroll", 1, "units")

        cy = self._pevent_canvas_y(event)
        n = len(self._plugin_entries)

        if len(self._psel_set) > 1 and self._drag_idx in self._psel_set:
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

            extracted = []
            for i in sorted(sorted_sel, reverse=True):
                extracted.insert(0, self._plugin_entries.pop(i))

            insert_at = max(0, min(slot, len(self._plugin_entries)))
            for j, entry in enumerate(extracted):
                self._plugin_entries.insert(insert_at + j, entry)

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
            insert_at = max(0, min(slot, len(self._plugin_entries)))
            self._plugin_entries.insert(insert_at, entry)

            self._drag_idx = insert_at
            self._sel_idx = insert_at
            self._psel_set = {insert_at}

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
                font=("Segoe UI", _theme.FS11),
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
        self._save_plugins()
        self._check_all_masters()
        self._predraw()

    def _disable_selected_plugins(self, indices: list[int]):
        """Disable all plugins at the given indices."""
        for i in indices:
            if 0 <= i < len(self._plugin_entries):
                self._plugin_entries[i].enabled = False
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
        self._drag_idx = -1
        self._drag_moved = False
        self._drag_slot = -1
        self._predraw()