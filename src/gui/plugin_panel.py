"""
Plugin panel: Plugins, Archives, Data, Downloads, Tracked, Endorsed tabs.
Used by App. Imports theme, game_helpers, dialogs, install_mod, subpanels.
"""

import json
import os
import threading
import tkinter as tk
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
    FONT_BOLD,
    FONT_HEADER,
    FONT_MONO,
    FONT_NORMAL,
    FONT_SMALL,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_SEP,
    plugin_mod,
    _ICONS_DIR,
    load_icon as _load_icon,
)
from gui.game_helpers import _GAMES, _vanilla_plugins_for_game
from gui.dialogs import _PriorityDialog, _ExeConfigDialog, _VRAMrPresetDialog
from gui.install_mod import install_mod_from_archive
from gui.mod_name_utils import _suggest_mod_names as suggest_mod_names
from gui.wizard_dialog import WizardDialog
from gui.downloads_panel import DownloadsPanel
from gui.tracked_mods_panel import TrackedModsPanel
from gui.endorsed_mods_panel import EndorsedModsPanel
from gui.browse_mods_panel import BrowseModsPanel
from gui.ctk_components import CTkTreeview

from Utils.config_paths import get_exe_args_path
from Utils.filemap import build_filemap
from Utils.plugins import (
    PluginEntry,
    read_plugins,
    write_plugins,
    append_plugin,
    read_loadorder,
    write_loadorder,
    sync_plugins_from_filemap,
    prune_plugins_from_filemap,
)
from Utils.plugin_parser import check_missing_masters
from LOOT.loot_sorter import sort_plugins as loot_sort, is_available as loot_available
from Nexus.nexus_meta import build_meta_from_download, write_meta, read_meta


def _truncate_plugin_name(widget: tk.Widget, text: str, font: tuple, max_px: int) -> str:
    """Return *text* truncated with '…' so it fits within *max_px* pixels."""
    if max_px <= 0:
        return ""
    if widget.tk.call("font", "measure", font, text) <= max_px:
        return text
    ellipsis = "…"
    ellipsis_w = widget.tk.call("font", "measure", font, ellipsis)
    while text and widget.tk.call("font", "measure", font, text) + ellipsis_w > max_px:
        text = text[:-1]
    return text + ellipsis


# ---------------------------------------------------------------------------
# PluginPanel
# ---------------------------------------------------------------------------
class PluginPanel(ctk.CTkFrame):
    """Right panel: tabview with Plugins, Archives, Data, Downloads, Tracked."""

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
        _warn_path = _ICONS_DIR / "warning.png"
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
            width=175, font=FONT_SMALL,
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
            exe_bar, text="⚙", width=30, height=30, font=FONT_SMALL,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_configure_exe,
        ).pack(side="left", padx=4, pady=6)
                
        refresh_icon = _load_icon("refresh.png", size=(16, 16))
        ctk.CTkButton(
            exe_bar, text="" if refresh_icon else "↺", image=refresh_icon,  
            width=30, height=30, font=FONT_SMALL,
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

        for name in ("Plugins", "Archives", "Data", "Downloads", "Tracked", "Endorsed", "Browse"):
            self._tabs.add(name)

        self._build_plugins_tab()
        self._build_data_tab()
        self._build_downloads_tab()
        self._build_tracked_tab()
        self._build_endorsed_tab()
        self._build_browse_tab()

        for name in ("Archives",):
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

    # Extensions detected in the executable dropdown (.exe always, .bat for wrapper support)
    _EXE_SCAN_EXTENSIONS = {".exe", ".bat"}

    def refresh_exe_list(self):
        """Scan for .exe and .bat files and populate the dropdown."""
        exes: list[Path] = []

        if self._game is not None:
            staging = (
                self._game.get_mod_staging_path()
                if hasattr(self._game, "get_mod_staging_path") else None
            )

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
                            if Path(rel_path).suffix.lower() not in self._EXE_SCAN_EXTENSIONS:
                                continue
                            mod_dir = staging / mod_name
                            candidate = mod_dir / rel_path
                            if candidate.is_file():
                                exes.append(candidate)
                    except OSError:
                        pass

            # 2. Scan Profiles/<game>/Applications/ for .exe/.bat files (recursive)
            if staging is not None:
                apps_dir = staging.parent / "Applications"
                if apps_dir.is_dir():
                    for ext in self._EXE_SCAN_EXTENSIONS:
                        for entry in apps_dir.rglob(f"*{ext}"):
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

    # ── .bat wrapper registry ──────────────────────────────────────────
    # Maps lowercase .bat filenames to wrapper launcher methods.
    # When the user tries to "Run" a .bat that has an entry here, the
    # wrapper is invoked instead of launching the .bat through Proton.
    _BAT_WRAPPERS: dict[str, str] = {
        "vramr.bat": "_run_vramr_wrapper",
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

        self._run_exe_via_proton(exe_path, game)

    def _run_exe_via_proton(self, exe_path: Path, game):
        """Standard Proton launch path for .exe files."""
        from Utils.steam_finder import (
            find_any_installed_proton,
            find_proton_for_game,
            find_steam_root_for_proton_script,
        )

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
            proton_script = find_any_installed_proton()
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
            game.get_mod_staging_path()
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
            relief="flat", font=("Segoe UI", 10),
            bd=0, cursor="hand2", highlightthickness=0,
            command=self._refresh_data_tab,
        ).pack(side="left", padx=8, pady=2)

        self._data_search_var = tk.StringVar()
        self._data_search_var.trace_add("write", self._on_data_search_changed)
        search_entry = tk.Entry(
            toolbar, textvariable=self._data_search_var,
            bg=BG_DEEP, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=("Segoe UI", 10), width=30,
            highlightthickness=0, highlightbackground=BG_DEEP,
        )
        search_entry.pack(side="right", padx=8, pady=3)
        search_entry.bind("<Escape>", lambda e: self._data_search_var.set(""))
        tk.Label(
            toolbar, text="Search:", bg=BG_HEADER, fg=TEXT_DIM,
            font=("Segoe UI", 10),
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
                    install_mod_from_archive(
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
                        suggestions = suggest_mod_names(raw_stem)
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
                    install_mod_from_archive(
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
                        suggestions = suggest_mod_names(raw_stem)
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
                    install_mod_from_archive(
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
                        suggestions = suggest_mod_names(raw_stem)
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

        def _cleanup():
            try:
                Path(archive_path).unlink(missing_ok=True)
                self._log(f"Removed archive: {os.path.basename(archive_path)}")
            except OSError:
                pass
            self._downloads_panel.refresh()

        install_mod_from_archive(archive_path, app, self._log, game, mod_panel,
                                 on_installed=_cleanup)

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
        self._pvsb.bind("<B1-Motion>",          lambda e: self._schedule_predraw())
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
                                    font=("Segoe UI", 11), state="hidden")
            idx_id = c.create_text(0, -200, text="", anchor="center", fill="",
                                   font=("Segoe UI", 10), state="hidden")
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
                font=("Segoe UI", 12, "bold"), state="hidden",
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
                    font=("Segoe UI", 9), state="hidden",
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

    def _refresh_plugins_tab(self) -> None:
        """Reload plugin entries from plugins.txt and redraw."""
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
        self._layout_plugin_cols(event.width)
        self._update_plugin_header(event.width)
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


# ---------------------------------------------------------------------------
# Game picker dialog (used by the + button to select an unconfigured game)
# ---------------------------------------------------------------------------