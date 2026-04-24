"""
Top bar: game selector, profile selector, Install Mod, Nexus, Proton Tools, etc.
Used by App. Imports theme, game_helpers, dialogs, path_utils, install_mod.
"""

import errno
import os
import shutil
import threading
import tkinter as tk
import customtkinter as ctk

from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    TEXT_ON_ACCENT,
    BG_HEADER,
    BG_HOVER,
    BG_PANEL,
    BORDER,
    FONT_BOLD,
    FONT_NORMAL,
    FONT_SMALL,
    TEXT_MAIN,
    TEXT_DIM,
    load_icon,
)
from gui import game_helpers as _gh
from gui.game_helpers import (
    _create_profile,
    _load_games,
    _load_last_game,
    _save_last_game,
    _handle_missing_profile_root,
    _profiles_for_game,
    _vanilla_plugins_for_game,
)
from gui.dialogs import (
    _GamePickerDialog,
    _ProtonToolsDialog,
    _MewgenicsDeployChoiceDialog,
    _MewgenicsLaunchCommandDialog,
    ask_yes_no,
    confirm_deploy_appdata,
    confirm_cet_symlink,
)
from gui.ctk_components import CTkAlert
from gui.path_utils import pick_file_mod_archive, pick_files_mod_archive
from gui.install_mod import install_mod_from_archive, _show_mod_notification
from gui.add_game_dialog import AddGameDialog
from gui.wizard_dialog import WizardDialog
from Utils.plugin_loader import get_all_wizard_tools
from Utils.config_paths import get_profiles_dir
from Utils.deploy import deploy_root_folder, restore_root_folder, LinkMode, load_per_mod_strip_prefixes, deploy_root_flagged_mods
from Utils.filemap import build_filemap
from Utils.profile_backup import create_backup


# ---------------------------------------------------------------------------
# TopBar
# ---------------------------------------------------------------------------
class TopBar(ctk.CTkFrame):
    # Fallback threshold; overwritten dynamically once widgets are measured.
    _WRAP_THRESHOLD = 1200

    def __init__(self, parent, log_fn=None, show_add_game_panel_fn=None,
                 show_reconfigure_panel_fn=None, show_proton_panel_fn=None,
                 show_wizard_panel_fn=None, show_nexus_panel_fn=None,
                 show_custom_game_panel_fn=None,
                 show_download_custom_handler_fn=None,
                 show_mewgenics_deploy_choice_fn=None):
        super().__init__(parent, fg_color=BG_PANEL, corner_radius=0)
        self._log = log_fn or (lambda msg: None)
        self._show_add_game_panel_fn = show_add_game_panel_fn
        self._show_reconfigure_panel_fn = show_reconfigure_panel_fn
        self._show_proton_panel_fn = show_proton_panel_fn
        self._show_wizard_panel_fn = show_wizard_panel_fn
        self._show_nexus_panel_fn = show_nexus_panel_fn
        self._show_custom_game_panel_fn = show_custom_game_panel_fn
        self._show_download_custom_handler_fn = show_download_custom_handler_fn
        self._show_mewgenics_deploy_choice_fn = show_mewgenics_deploy_choice_fn
        self._two_rows: bool | None = None  # unknown until first configure
        self._auto_deploy_in_progress: bool = False

        # ── Content area (above separator) ───────────────────────────────────
        # _row1 holds game/profile selectors; _row2 holds action buttons.
        # Layout:
        #   Wide  → _row1 left, _row2 right, both on the same visual line
        #           (achieved by packing _row2 side="right" inside _content)
        #   Narrow → _row1 top, _row2 below (stacked)
        self._content = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self._content.pack(side="top", fill="both", expand=True)

        self._row1 = ctk.CTkFrame(self._content, fg_color="transparent", corner_radius=0)
        self._row2 = ctk.CTkFrame(self._content, fg_color="transparent", corner_radius=0)
        # Initial pack deferred to _apply_layout() after widgets are built

        # Bottom separator line
        self._sep = ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0)
        self._sep.pack(side="bottom", fill="x")

        # ── Game selector (row 1) ────────────────────────────────────────────
        game_names = _load_games()
        _last_game = _load_last_game()
        _initial_game = _last_game if (_last_game and _last_game in game_names) else game_names[0]
        self._game_var = tk.StringVar(value=_initial_game)

        ctk.CTkLabel(
            self._row1, text="Game:", font=FONT_BOLD, text_color=TEXT_MAIN
        ).pack(side="left", padx=(12, 4))

        ctk.CTkButton(
            self._row1, text="+", width=32, height=32, font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9a3a", text_color="white",
            command=self._on_add_game
        ).pack(side="left", padx=(0, 4))

        self._game_menu = ctk.CTkOptionMenu(
            self._row1, values=game_names, variable=self._game_var,
            width=180, height=32, font=FONT_NORMAL,
            fg_color=BG_HEADER, button_color=ACCENT, button_hover_color=ACCENT_HOV,
            dropdown_fg_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_game_change
        )
        self._game_menu.pack(side="left", padx=(0, 4))

        _game_settings_icon = load_icon("settings.png", size=(18, 18))
        ctk.CTkButton(
            self._row1, text="" if _game_settings_icon else "⚙",
            image=_game_settings_icon, width=32, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_settings
        ).pack(side="left", padx=(0, 16))

        # ── Profile selector (row 1) ─────────────────────────────────────────
        ctk.CTkLabel(
            self._row1, text="Profile:", font=FONT_BOLD, text_color=TEXT_MAIN
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            self._row1, text="+", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_add_profile
        ).pack(side="left", padx=(0, 2))

        _profile_settings_icon = load_icon("settings.png", size=(18, 18))
        ctk.CTkButton(
            self._row1, text="" if _profile_settings_icon else "⚙",
            image=_profile_settings_icon, width=32, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_profile_settings
        ).pack(side="left", padx=(0, 4))

        initial_game_name = _initial_game
        try:
            profile_names = _profiles_for_game(initial_game_name)
        except (FileNotFoundError, OSError) as e:
            if getattr(e, "errno", None) == errno.ENOENT or isinstance(e, FileNotFoundError):
                _handle_missing_profile_root(self, initial_game_name)
                initial_game_name = self._game_var.get()
                profile_names = _profiles_for_game(initial_game_name)
            else:
                raise
        _initial_game_obj = _gh._GAMES.get(initial_game_name)
        _last_profile = _initial_game_obj.get_last_active_profile() if _initial_game_obj else "default"
        _initial_profile = _last_profile if _last_profile in profile_names else profile_names[0]
        self._profile_var = tk.StringVar(value=_initial_profile)
        self._profile_menu = ctk.CTkOptionMenu(
            self._row1, values=profile_names, variable=self._profile_var,
            width=160, height=32, font=FONT_NORMAL,
            dynamic_resizing=False,
            fg_color=BG_HEADER, button_color=ACCENT, button_hover_color=ACCENT_HOV,
            dropdown_fg_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_profile_change
        )
        self._profile_menu.pack(side="left", padx=(0, 4))
        self.after_idle(self._update_profile_menu_color)

        # ── Collections button (row 1, right of profile dropdown) ────────────
        _collections_icon = load_icon("collection.png", size=(30, 30))
        self._collections_btn = ctk.CTkButton(
            self._row1, text="Collections", width=125, height=32, font=FONT_BOLD,
            image=_collections_icon, compound="left",
            fg_color="#c07320", hover_color="#d4832a", text_color="white",
            command=self._on_collections
        )
        # Hidden until premium status confirmed — _check_collections_visibility() shows it
        # self._collections_btn.pack(...)  deferred

        # ── Action buttons (row 2 container, but children created here) ──────
        # Install Mod button
        self._disable_extract = False
        _install_mod_icon = load_icon("install.png", size=(30, 30))
        self._install_mod_btn = ctk.CTkButton(
            self._row2, text="Install Mod", width=125, height=32, font=FONT_BOLD,
            image=_install_mod_icon, compound="left",
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_install_mod
        )
        self._install_mod_btn.pack(side="left", padx=(8, 8))
        self._install_mod_btn.bind("<Button-3>", self._on_install_mod_right_click)

        # Deploy button
        _deploy_icon = load_icon("deploy.png", size=(30, 30))
        self._deploy_btn = ctk.CTkButton(
            self._row2, text="Deploy", width=110, height=32, font=FONT_BOLD,
            image=_deploy_icon, compound="left",
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._on_deploy
        )
        self._deploy_btn.pack(side="left", padx=(0, 8))

        # Restore button
        _restore_icon = load_icon("restore.png", size=(30, 30))
        self._restore_btn = ctk.CTkButton(
            self._row2, text="Restore", width=115, height=32, font=FONT_BOLD,
            image=_restore_icon, compound="left",
            fg_color="#8b1a1a", hover_color="#b22222", text_color="white",
            command=self._on_restore
        )
        self._restore_btn.pack(side="left", padx=(0, 8))

        # Proton tools button
        _proton_icon = load_icon("proton.png", size=(30, 30))
        self._proton_btn = ctk.CTkButton(
            self._row2, text="Proton", width=110, height=32, font=FONT_BOLD,
            image=_proton_icon, compound="left",
            fg_color="#7b2d8b", hover_color="#9a3aae", text_color="white",
            command=self._on_proton_tools
        )
        self._proton_btn.pack(side="left", padx=(0, 8))

        # Wizard button (shown only when the game has wizard tools)
        _wizard_icon = load_icon("wizard.png", size=(30, 30))
        self._wizard_btn = ctk.CTkButton(
            self._row2, text="Wizard", width=110, height=32, font=FONT_BOLD,
            image=_wizard_icon, compound="left",
            fg_color="#4a1272", hover_color="#6318a0", text_color="white",
            command=self._on_wizard
        )
        # Don't pack yet — _update_wizard_visibility() will show/hide it

        # Nexus Mods settings button
        _nexus_icon = load_icon("nexus.png", size=(30, 30))
        self._nexus_btn = ctk.CTkButton(
            self._row2, text="Nexus", width=115, height=32, font=FONT_BOLD,
            image=_nexus_icon, compound="left",
            fg_color="#da8e35", hover_color="#e5a04a", text_color="white",
            command=self._on_nexus_settings
        )
        self._nexus_btn.pack(side="left", padx=(0, 4))

        # Show/hide wizard button for the initial game
        self._update_wizard_visibility()

        # Measure natural widths after layout pass, set threshold, then apply
        self.after_idle(self._init_threshold)
        self.bind("<Configure>", self._on_configure)

        # Check premium status in background; show Collections btn if premium
        # Delay slightly so _init_nexus_api() on the App has time to run first
        self.after(500, self._check_collections_visibility)

    # ── Responsive layout ────────────────────────────────────────────────────

    def _init_threshold(self):
        """Measure the natural widths of both rows and set the wrap threshold."""
        # Temporarily pack both rows to get their requested widths
        self._row1.pack(side="top", fill="x")
        self._row2.pack(side="top", fill="x")
        self.update_idletasks()
        row1_w = self._row1.winfo_reqwidth()
        row2_w = self._row2.winfo_reqwidth()
        # Use 5% buffer so the threshold accounts for measurement rounding at any scale
        self._WRAP_THRESHOLD = round((row1_w + row2_w) * 1.05)
        # Now unpack and apply the correct layout for the current window width
        self._row1.pack_forget()
        self._row2.pack_forget()
        self._two_rows = None  # reset so _apply_layout always runs
        self._apply_layout(self.winfo_width())
        # Notify the app so it can widen the modlist column minsize to match.
        app = self.winfo_toplevel()
        sync = getattr(app, "_sync_mod_col_minsize", None)
        if sync is not None:
            sync()

    def _on_configure(self, event):
        """Re-evaluate single-row vs two-row layout whenever our width changes."""
        self._apply_layout(event.width)

    def _apply_layout(self, width: int):
        two_rows = width < self._WRAP_THRESHOLD
        if two_rows == self._two_rows:
            return  # no change
        self._two_rows = two_rows
        # Unpack both first to avoid pack-order conflicts
        self._row1.pack_forget()
        self._row2.pack_forget()
        if two_rows:
            # Stacked: both rows centred within the top bar width
            self._row1.pack(side="top", anchor="center", pady=(4, 0))
            self._row2.pack(side="top", anchor="center", pady=(4, 4))
        else:
            # Side by side on one line, centred together
            self._row2.pack(side="right", fill="y", pady=2, padx=(0, 4))
            self._row1.pack(side="left", fill="y", pady=2)

    def _check_collections_visibility(self):
        """Show the Collections button only for Nexus premium members whose
        current game has collections enabled."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)

        # Hide immediately if the active game disables collections.
        game = _gh._GAMES.get(self._game_var.get())
        if game is not None and getattr(game, "collections_disabled", False):
            self._collections_btn.pack_forget()
            self.after_idle(self._init_threshold)
            return

        if api is None:
            self._collections_btn.pack_forget()
            self.after_idle(self._init_threshold)
            return

        def _worker():
            try:
                user = api.validate()
                premium = bool(user.is_premium)
                self.after(0, lambda: self._log(f"Nexus: logged in as {user.name} (premium={premium})"))
            except Exception as e:
                self.after(0, lambda err=e: self._log(f"Nexus: premium check failed: {err}"))
                premium = False

            def _apply():
                # Re-check game flag in case it changed while the thread ran.
                _game = _gh._GAMES.get(self._game_var.get())
                _disabled = _game is not None and getattr(_game, "collections_disabled", False)
                if not _disabled:
                    self._collections_btn.pack(side="left", padx=(0, 4))
                else:
                    self._collections_btn.pack_forget()
                self.after_idle(self._init_threshold)

            self.after(0, _apply)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_collections(self):
        """Delegate to the mod panel's collections handler."""
        app = self.winfo_toplevel()
        if hasattr(app, "_mod_panel"):
            app._mod_panel._on_collections()

    def _on_nexus_settings(self):
        """Open the Nexus Browse/Tracked/Endorsed overlay on the modlist panel."""
        app = self.winfo_toplevel()
        if hasattr(app, "_mod_panel"):
            app._mod_panel._on_nexus_browser()

    def _on_proton_tools(self):
        game = _gh._GAMES.get(self._game_var.get())
        if game is None or not game.is_configured():
            self._log("Proton Tools: no configured game selected.")
            return
        if self._show_proton_panel_fn:
            self._show_proton_panel_fn(game, self._log)
        else:
            dlg = _ProtonToolsDialog(self.winfo_toplevel(), game, self._log)
            self.winfo_toplevel().wait_window(dlg)

    def _update_profile_menu_color(self):
        """Color the profile dropdown green when the selected profile is deployed."""
        game_obj = _gh._GAMES.get(self._game_var.get())
        deployed_profile = (
            game_obj.get_last_deployed_profile()
            if game_obj is not None and game_obj.get_deploy_active()
            else None
        )
        selected_is_deployed = deployed_profile == self._profile_var.get()
        if selected_is_deployed:
            self._profile_menu.configure(
                fg_color="#1e3a1e",
                button_color="#2d7a2d",
                button_hover_color="#3a9a3a",
                text_color="white",
            )
        else:
            self._profile_menu.configure(
                fg_color=BG_HEADER,
                button_color=ACCENT,
                button_hover_color=ACCENT_HOV,
                text_color=TEXT_MAIN,
            )

        # Colour the deployed profile entry green in the dropdown list
        try:
            dropdown = self._profile_menu._dropdown_menu
            values = self._profile_menu._values
            for i, value in enumerate(values):
                color = "#4caf50" if value == deployed_profile else ""
                dropdown.entryconfigure(i, foreground=color)
        except Exception:
            pass

    def _on_profile_change(self, value: str):
        self._log(f"Profile: {value}")
        game = _gh._GAMES.get(self._game_var.get())
        if game:
            game.save_last_active_profile(value)
        self._update_profile_menu_color()
        self._reload_mod_panel()

    def _on_wizard(self):
        """Open the Wizard tool-selection panel (or dialog fallback) for the current game."""
        game = _gh._GAMES.get(self._game_var.get())
        if game is None or not game.is_configured():
            self._log("Wizard: no configured game selected.")
            return
        if not get_all_wizard_tools(game):
            self._log("Wizard: no tools available for this game.")
            return
        if self._show_wizard_panel_fn:
            self._show_wizard_panel_fn(game, self._log)
        else:
            dlg = WizardDialog(self.winfo_toplevel(), game, self._log)
            self.winfo_toplevel().wait_window(dlg)

    def _update_wizard_visibility(self):
        """Show or hide the Wizard button based on the current game."""
        game = _gh._GAMES.get(self._game_var.get())
        if game and get_all_wizard_tools(game):
            # Ensure it's packed right after the Proton button
            try:
                self._wizard_btn.pack(side="left", padx=(0, 8),
                                      after=self._proton_btn)
            except Exception:
                self._wizard_btn.pack(side="left", padx=(0, 8))
        else:
            self._wizard_btn.pack_forget()
        self.after_idle(self._init_threshold)

    def _on_game_change(self, value: str):
        _save_last_game(value)
        game = _gh._GAMES.get(value)
        if game and game.is_configured():
            self._log(f"Game: {value} — {game.get_game_path()}")
        else:
            self._log(f"Game: {value} — not configured (click + to set path)")
        # Refresh profile dropdown for the new game
        profiles = _profiles_for_game(value)
        self._profile_menu.configure(values=profiles)
        game_obj = _gh._GAMES.get(value)
        last = game_obj.get_last_active_profile() if game_obj else "default"
        self._profile_var.set(last if last in profiles else profiles[0])
        self._update_wizard_visibility()
        self._check_collections_visibility()
        self._update_profile_menu_color()
        self._reload_mod_panel()

    def _reload_mod_panel(self):
        """Tell the mod panel and plugin panel to load the current game + profile."""
        app = self.winfo_toplevel()
        if not hasattr(app, "_mod_panel"):
            return
        game = _gh._GAMES.get(self._game_var.get())
        if game and game.is_configured():
            # Tell the game which profile directory is active so that
            # get_effective_mod_staging_path() can resolve profile-specific mods.
            profile_dir = (
                game.get_profile_root()
                / "profiles" / self._profile_var.get()
            )
            game.set_active_profile_dir(profile_dir)
            # Update plugin panel paths BEFORE load_game, because load_game
            # triggers _rebuild_filemap → _on_filemap_rebuilt which reads
            # _plugins_path. If we update after, the old game's path is used.
            # Also clear _plugin_entries immediately so any pending save callbacks
            # cannot write the old game's plugins to the new game's file.
            if hasattr(app, "_plugin_panel"):
                plugins_path = profile_dir / "plugins.txt"
                app._plugin_panel._plugin_entries = []
                app._plugin_panel._plugins_path = plugins_path
                app._plugin_panel._plugin_extensions = game.plugin_extensions
                app._plugin_panel._vanilla_plugins = _vanilla_plugins_for_game(game)
                _staging = game.get_effective_mod_staging_path()
                app._plugin_panel._staging_root = _staging
                data_path = (
                    game.get_vanilla_plugins_path()
                    if hasattr(game, 'get_vanilla_plugins_path')
                    else game.get_mod_data_path()
                    if hasattr(game, 'get_mod_data_path')
                    else None
                )
                app._plugin_panel._data_dir = data_path
                app._plugin_panel._game = game
                # Mod Files tab paths
                from Utils.profile_state import read_excluded_mod_files as _read_exc
                app._plugin_panel._mod_files_index_path = _staging.parent / "modindex.bin"
                app._plugin_panel._mod_files_profile_dir = profile_dir
                _exc_raw = _read_exc(profile_dir, None)
                app._plugin_panel._mod_files_excluded = {k: set(v) for k, v in _exc_raw.items()}
                app._plugin_panel._mod_files_on_change = app._mod_panel._rebuild_filemap
                app._plugin_panel._plugin_order_on_change = app._mod_panel.recompute_bsa_conflicts
                app._plugin_panel.show_mod_files(None)
                # Archive tab — gated on game.archive_extensions
                app._plugin_panel._bsa_index_path = _staging.parent / "bsa_index.bin"
                app._plugin_panel._bsa_conflict_cache = None
                app._plugin_panel._update_archive_tab_visibility()
                app._plugin_panel.show_mod_archives(None)
            try:
                app._mod_panel.load_game(game, self._profile_var.get())
            except (FileNotFoundError, OSError) as e:
                if getattr(e, "errno", None) == errno.ENOENT or isinstance(e, FileNotFoundError):
                    _handle_missing_profile_root(self, self._game_var.get())
                    return
                raise
            # load_game already triggered _on_filemap_rebuilt which refreshed
            # the plugins tab, so just ensure state is consistent.
            if hasattr(app, "_plugin_panel"):
                app._plugin_panel._refresh_plugins_tab()
                app._plugin_panel.refresh_exe_list()
        else:
            if hasattr(app, "_plugin_panel"):
                app._plugin_panel._plugin_entries = []
            app._mod_panel.load_game(None, "")

    def _on_add_profile(self):
        game_name = self._game_var.get()
        if game_name not in _gh._GAMES:
            self._log("No game selected.")
            return
        app = self.winfo_toplevel()
        if not hasattr(app, "_mod_panel"):
            return

        def _on_create(name: str, use_specific_mods: bool):
            # Reject names that clash with 'default' or already exist
            existing = _profiles_for_game(game_name)
            if name in existing:
                self._log(f"Profile '{name}' already exists.")
                return
            _create_profile(game_name, name, profile_specific_mods=use_specific_mods)
            self._log(f"Profile '{name}' created.")
            profiles = _profiles_for_game(game_name)
            self._profile_menu.configure(values=profiles)
            self._profile_var.set(name)
            self._update_profile_menu_color()
            self._reload_mod_panel()

        app._mod_panel.show_new_profile_bar(_on_create)

    def _on_profile_settings(self):
        game_name = self._game_var.get()
        if game_name not in _gh._GAMES:
            self._log("No game selected.")
            return
        app = self.winfo_toplevel()
        if not hasattr(app, "_mod_panel"):
            return

        def _on_renamed(old_name: str, new_name: str):
            profiles = _profiles_for_game(game_name)
            self._profile_menu.configure(values=profiles)
            if self._profile_var.get() == old_name:
                self._profile_var.set(new_name)
                self._reload_mod_panel()
            self._update_profile_menu_color()

        def _on_removed(name: str):
            profiles = _profiles_for_game(game_name)
            self._profile_menu.configure(values=profiles)
            if self._profile_var.get() == name:
                self._profile_var.set(profiles[0])
                self._reload_mod_panel()
            self._update_profile_menu_color()

        def _on_profiles_changed():
            profiles = _profiles_for_game(game_name)
            self._profile_menu.configure(values=profiles)

        app._mod_panel.show_profile_settings(
            game_name=game_name,
            current_profile=self._profile_var.get(),
            on_profile_renamed=_on_renamed,
            on_profile_removed=_on_removed,
            on_profiles_changed=_on_profiles_changed,
        )

    def _on_remove_profile(self):
        game_name = self._game_var.get()
        profile = self._profile_var.get()
        if profile == "default":
            self._log("Cannot remove the default profile.")
            return
        alert = CTkAlert(
            state="warning",
            title="Remove Profile",
            body_text=f"Are you sure you want to remove the '{profile}' profile?\n\nThe game will be restored first if this profile is deployed.",
            btn1="Remove",
            btn2="Cancel",
            parent=self.winfo_toplevel(),
        )
        if alert.get() != "Remove":
            return
        game = _gh._GAMES.get(game_name)
        if game is not None:
            profile_dir = game.get_profile_root() / "profiles" / profile
        else:
            from Utils.config_paths import get_profiles_dir
            profile_dir = get_profiles_dir() / game_name / "profiles" / profile

        # Restore deployed mod files before deleting the profile so we don't
        # leave orphaned mod files in the game folder.  Point the game at this
        # profile so get_effective_filemap_path() resolves correctly.
        if game is not None and game.is_configured():
            game.set_active_profile_dir(profile_dir)
            try:
                if hasattr(game, "restore"):
                    game.restore()
            except Exception:
                pass
            try:
                from Utils.deploy import restore_root_folder
                root_folder_dir = game.get_effective_root_folder_path()
                game_root = game.get_game_path()
                if root_folder_dir.is_dir() and game_root:
                    restore_root_folder(root_folder_dir, game_root)
            except Exception:
                pass
            # Clear the stale active-profile reference; _reload_mod_panel will
            # set it correctly for whatever profile is selected after deletion.
            game.set_active_profile_dir(None)

        if profile_dir.is_dir():
            from gui.game_helpers import profile_uses_specific_mods
            if profile_uses_specific_mods(profile_dir):
                confirm = CTkAlert(
                    state="warning",
                    title="Remove Profile",
                    body_text=(
                        f"The '{profile}' profile has profile-specific mods.\n\n"
                        "Removing it will permanently delete its installed mods "
                        "and modlist. Continue?"
                    ),
                    btn1="Remove",
                    btn2="Cancel",
                    parent=self.winfo_toplevel(),
                )
                if confirm.get() != "Remove":
                    return
            shutil.rmtree(profile_dir)
        self._log(f"Profile '{profile}' removed.")
        profiles = _profiles_for_game(game_name)
        self._profile_menu.configure(values=profiles)
        self._profile_var.set(profiles[0])
        self._update_profile_menu_color()
        self._reload_mod_panel()

    def _on_add_game(self):
        all_names = sorted(_gh._GAMES.keys())
        if not all_names:
            _load_games()
            all_names = sorted(_gh._GAMES.keys())
        if not all_names:
            self._log("No game handlers discovered.")
            return

        if self._show_add_game_panel_fn:
            # Show the picker inline (replaces the mod-list area)
            self._show_add_game_panel_fn(all_names, self._handle_game_picked)
        else:
            # Fallback: original modal dialog
            picker = _GamePickerDialog(
                self.winfo_toplevel(), all_names, games=_gh._GAMES,
                show_download_custom_handler_fn=self._show_download_custom_handler_fn,
            )
            self.winfo_toplevel().wait_window(picker)
            if picker.result is None:
                return
            self._handle_game_picked(
                picker.result,
                getattr(picker, "selected_only", False),
            )

    def _handle_game_picked(self, result: str | None, already_configured: bool):
        """Process the result of the game picker (inline panel or modal dialog)."""
        if result is None:
            return

        # If the result is not yet in _GAMES (new custom game), reload the registry
        if result not in _gh._GAMES:
            _load_games()

        game = _gh._GAMES.get(result)
        if game is None:
            return

        # Game already configured — just switch to it without re-running AddGameDialog
        if already_configured:
            configured = sorted(n for n, g in _gh._GAMES.items() if g.is_configured())
            self._game_menu.configure(values=configured or ["No games configured"])
            self._game_var.set(result)
            _save_last_game(result)
            self._update_wizard_visibility()
            # Reset profile dropdown for the newly selected game BEFORE reloading
            new_profiles = _profiles_for_game(result)
            self._profile_menu.configure(values=new_profiles)
            game_obj = _gh._GAMES.get(result)
            last_profile = game_obj.get_last_active_profile() if game_obj else "default"
            self._profile_var.set(last_profile if last_profile in new_profiles else new_profiles[0])
            self._update_profile_menu_color()
            self._reload_mod_panel()
            return

        def _on_add_done(panel):
            if panel.result is not None:
                self._log(f"Game path set: {panel.result}")
                configured = sorted(n for n, g in _gh._GAMES.items() if g.is_configured())
                self._game_menu.configure(values=configured or ["No games configured"])
                if result in configured:
                    self._game_var.set(result)
                    _save_last_game(result)
                    self._update_wizard_visibility()
                    # Reset profile dropdown for the newly added game BEFORE reloading
                    # so the old game's profiles are not inherited.
                    new_profiles = _profiles_for_game(result)
                    self._profile_menu.configure(values=new_profiles)
                    game_obj = _gh._GAMES.get(result)
                    last_profile = game_obj.get_last_active_profile() if game_obj else "default"
                    self._profile_var.set(last_profile if last_profile in new_profiles else new_profiles[0])
                    self._update_profile_menu_color()
                    self._reload_mod_panel()

        if self._show_reconfigure_panel_fn:
            self._show_reconfigure_panel_fn(game, _on_add_done)
        else:
            dialog = AddGameDialog(self.winfo_toplevel(), game)
            self.winfo_toplevel().wait_window(dialog)
            _on_add_done(dialog)

    def _on_settings(self):
        game_name = self._game_var.get()
        game = _gh._GAMES.get(game_name)
        if game is None:
            self._log("No game selected.")
            return

        # For user-defined custom games, open the definition editor first
        # (skipped when editable=false, e.g. repo-distributed handlers)
        if getattr(game, "is_custom", False) and getattr(game, "editable", True):
            existing_defn = getattr(game, "_defn", None)
            if self._show_custom_game_panel_fn:
                def _on_custom_game_done(panel):
                    if panel.deleted:
                        self._log(f"Deleted custom game: {game_name}")
                        _gh._GAMES.pop(game_name, None)
                        game.load_paths()
                        configured = sorted(n for n, g in _gh._GAMES.items() if g.is_configured())
                        self._game_menu.configure(values=configured or ["No games configured"])
                        if configured:
                            self._game_var.set(configured[0])
                            self._on_game_change(configured[0])
                        else:
                            self._game_var.set("No games configured")
                            self._on_game_change("No games configured")
                    elif panel.saved_game is not None:
                        _load_games()
                        updated_game = _gh._GAMES.get(panel.saved_game.name) or game
                        if self._show_reconfigure_panel_fn:
                            def _on_reconfigure_done(p):
                                if getattr(p, "removed", False):
                                    self._log(f"Removed instance: {panel.saved_game.name}")
                                    updated_game.load_paths()
                                    configured = sorted(n for n, g in _gh._GAMES.items() if g.is_configured())
                                    self._game_menu.configure(values=configured or ["No games configured"])
                                    if configured:
                                        self._game_var.set(configured[0])
                                        self._on_game_change(configured[0])
                                    else:
                                        self._game_var.set("No games configured")
                                        self._on_game_change("No games configured")
                                elif p.result is not None:
                                    self._log(f"Game path updated: {p.result}")
                                    self._reload_mod_panel()
                            self._show_reconfigure_panel_fn(updated_game, _on_reconfigure_done)
                self._show_custom_game_panel_fn(existing_defn, _on_custom_game_done)
                return
            else:
                from gui.custom_game_dialog import CustomGameDialog
                defn_dlg = CustomGameDialog(self.winfo_toplevel(), existing=existing_defn)
                self.winfo_toplevel().wait_window(defn_dlg)
                if defn_dlg.deleted:
                    self._log(f"Deleted custom game: {game_name}")
                    _gh._GAMES.pop(game_name, None)
                    game.load_paths()
                    configured = sorted(n for n, g in _gh._GAMES.items() if g.is_configured())
                    self._game_menu.configure(values=configured or ["No games configured"])
                    if configured:
                        self._game_var.set(configured[0])
                        self._on_game_change(configured[0])
                    else:
                        self._game_var.set("No games configured")
                        self._on_game_change("No games configured")
                    return
                if defn_dlg.saved_game is not None:
                    _load_games()
                    game = _gh._GAMES.get(defn_dlg.saved_game.name) or game

        if self._show_reconfigure_panel_fn:
            def _on_reconfigure_done(panel):
                if getattr(panel, "removed", False):
                    self._log(f"Removed instance: {game_name}")
                    game.load_paths()
                    configured = sorted(n for n, g in _gh._GAMES.items() if g.is_configured())
                    self._game_menu.configure(values=configured or ["No games configured"])
                    if configured:
                        self._game_var.set(configured[0])
                        self._on_game_change(configured[0])
                    else:
                        self._game_var.set("No games configured")
                        self._on_game_change("No games configured")
                elif panel.result is not None:
                    self._log(f"Game path updated: {panel.result}")
                    self._reload_mod_panel()
            self._show_reconfigure_panel_fn(game, _on_reconfigure_done)
        else:
            dialog = AddGameDialog(self.winfo_toplevel(), game)
            self.winfo_toplevel().wait_window(dialog)
            if getattr(dialog, "removed", False):
                self._log(f"Removed instance: {game_name}")
                game.load_paths()
                configured = sorted(n for n, g in _gh._GAMES.items() if g.is_configured())
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
        game = _gh._GAMES.get(self._game_var.get())
        if game is None or not game.is_configured():
            self._log("Deploy: no configured game selected.")
            return
        if not hasattr(game, "deploy"):
            self._log(f"Deploy: '{game.name}' does not support deployment.")
            return

        profile = self._profile_var.get()

        # Mewgenics: ask whether to use Steam launch command or repack
        if game.name == "Mewgenics":
            if self._show_mewgenics_deploy_choice_fn:
                def _on_mewgenics_choice(result, _game=game, _profile=profile):
                    if result is None:
                        return
                    if result == "steam":
                        launch_string, modpaths_file = _game.get_modpaths_launch_string(_profile)
                        app = self.winfo_toplevel()
                        if hasattr(app, "show_mewgenics_launch_command"):
                            app.show_mewgenics_launch_command(launch_string, modpaths_file)
                        else:
                            _MewgenicsLaunchCommandDialog(
                                self.winfo_toplevel(), launch_string, modpaths_file
                            )
                        return
                    # result == "repack" -> run normal deploy
                    self._run_deploy(_game, _profile)
                self._show_mewgenics_deploy_choice_fn(_on_mewgenics_choice)
                return
            else:
                choice_dlg = _MewgenicsDeployChoiceDialog(self.winfo_toplevel())
                self.winfo_toplevel().wait_window(choice_dlg)
                if choice_dlg.result is None:
                    return
                if choice_dlg.result == "steam":
                    launch_string, modpaths_file = game.get_modpaths_launch_string(profile)
                    _MewgenicsLaunchCommandDialog(
                        self.winfo_toplevel(), launch_string, modpaths_file
                    )
                    return
                # choice_dlg.result == "repack" -> fall through to normal deploy

        self._run_deploy(game, profile)

    def _run_deploy(self, game, profile):
        """Execute the deploy worker thread for *game* / *profile*."""
        if not confirm_deploy_appdata(self.winfo_toplevel(), game):
            self._log("Deploy: cancelled — AppData folder missing.")
            return
        app = self.winfo_toplevel()
        root_folder_enabled = (
            app._mod_panel._root_folder_enabled
            if hasattr(app, "_mod_panel") else True
        )
        game_root = game.get_game_path()

        status_bar = self.winfo_toplevel()._status

        def _worker():
            # Thread-safe log: schedule UI update on the main thread.
            def _tlog(msg):
                self.after(0, lambda m=msg: self._log(m))

            def _progress(done: int, total: int, phase: str | None = None):
                self.after(0, lambda d=done, t=total, p=phase: status_bar.set_progress(d, t, p))

            try:
                # Restore must use the last-deployed profile's paths so that
                # runtime files (ShaderCache, saves, etc.) are moved back to
                # the correct overwrite/ folder, not the currently-active one.
                last_deployed = game.get_last_deployed_profile()
                if last_deployed:
                    game.set_active_profile_dir(
                        game.get_profile_root() / "profiles" / last_deployed
                    )
                if getattr(game, "restore_before_deploy", True) and hasattr(game, "restore"):
                    try:
                        game.restore(log_fn=_tlog, progress_fn=_progress)
                    except RuntimeError:
                        pass
                # Restore Root_Folder using the last-deployed profile's Root_Folder.
                last_root_folder_dir = game.get_effective_root_folder_path()
                if last_root_folder_dir.is_dir() and game_root:
                    restore_root_folder(last_root_folder_dir, game_root, log_fn=_tlog)

                # Switch to the target profile before building the filemap and deploying.
                game.set_active_profile_dir(
                    game.get_profile_root() / "profiles" / profile
                )

                # Rebuild filemap.txt before deploy so any files rescued into
                # overwrite/ during restore are included with [Overwrite] priority.
                profile_root = game.get_profile_root()
                staging      = game.get_effective_mod_staging_path()
                modlist_path = profile_root / "profiles" / profile / "modlist.txt"
                filemap_out  = staging.parent / "filemap.txt"
                if modlist_path.is_file():
                    try:
                        from Utils.profile_state import read_excluded_mod_files as _read_exc
                        from Nexus.nexus_meta import collect_root_flagged_mods as _collect_rf
                        _exc_raw = _read_exc(modlist_path.parent, None)
                        _exc = {k: set(v) for k, v in _exc_raw.items()} if _exc_raw else None
                        _rf_mods = _collect_rf(modlist_path, staging, log_fn=_tlog)
                        from Utils.ui_config import load_normalize_folder_case as _load_norm_case
                        from Games.ue5_game import UE5Game as _UE5Game
                        _norm_case = getattr(game, "normalize_folder_case", True) and _load_norm_case()
                        _cfn = None
                        if isinstance(game, _UE5Game):
                            def _cfn(rk: str, _g=game) -> str:
                                dest, final = _g._resolve_entry(rk)
                                return (dest + "/" + final) if dest else final
                        else:
                            _cfn = getattr(game, "filemap_conflict_key_fn", None)
                        build_filemap(
                            modlist_path, staging, filemap_out,
                            strip_prefixes=game.mod_folder_strip_prefixes or None,
                            per_mod_strip_prefixes=load_per_mod_strip_prefixes(modlist_path.parent),
                            allowed_extensions=game.mod_install_extensions or None,
                            root_deploy_folders=game.mod_root_deploy_folders or None,
                            excluded_mod_files=_exc,
                            conflict_ignore_filenames=getattr(game, "conflict_ignore_filenames", None) or None,
                            exclude_dirs=getattr(game, "filemap_exclude_dirs", None) or None,
                            normalize_folder_case=_norm_case,
                            conflict_key_fn=_cfn,
                            root_folder_mods=_rf_mods or None,
                        )
                    except Exception as fm_err:
                        _tlog(f"Filemap rebuild warning: {fm_err}")

                # CET + symlink warning (Cyberpunk 2077 only, after filemap rebuild)
                _cet_result = {"proceed": True}
                _cet_done = threading.Event()
                def _ask_cet():
                    try:
                        _cet_result["proceed"] = confirm_cet_symlink(
                            self.winfo_toplevel(), game
                        )
                    finally:
                        _cet_done.set()
                self.after(0, _ask_cet)
                _cet_done.wait()
                if not _cet_result["proceed"]:
                    _tlog("Deploy: cancelled — CET requires Hardlink mode.")
                    return

                # Backup modlist/plugins before deploy
                profile_dir = modlist_path.parent
                try:
                    create_backup(profile_dir, _tlog)
                except Exception as backup_err:
                    _tlog(f"Backup skipped: {backup_err}")

                deploy_mode = game.get_deploy_mode() if hasattr(game, "get_deploy_mode") else LinkMode.HARDLINK
                game.deploy(log_fn=_tlog, profile=profile, progress_fn=_progress,
                            mode=deploy_mode)

                # Apply Wine DLL overrides (user-added + handler-defined)
                from Utils.wine_dll_config import deploy_game_wine_dll_overrides
                _pfx = game.get_prefix_path()
                if _pfx and _pfx.is_dir():
                    deploy_game_wine_dll_overrides(game.name, _pfx, game.wine_dll_overrides, log_fn=_tlog)

                # Record this profile as the last successfully deployed so that
                # a future restore knows which overwrite/ folder to use.
                game.save_last_deployed_profile(profile)

                # Deploy Root_Folder using the target profile's Root_Folder
                # (active profile dir is already set to target profile above).
                target_root_folder_dir = game.get_effective_root_folder_path()
                rf_allowed = getattr(game, "root_folder_deploy_enabled", True)

                # Step A: deploy shared Root_Folder first (its log must exist before Step B).
                if rf_allowed and root_folder_enabled and target_root_folder_dir.is_dir() and game_root:
                    count = deploy_root_folder(target_root_folder_dir, game_root,
                                            mode=deploy_mode, log_fn=_tlog)
                    if count:
                        _tlog("Root Folder: transferred files to game root.")

                # Step B: deploy root-flagged mods; merges into Step A log (Root_Folder wins conflicts).
                if game_root:
                    _filemap_root_path = game.get_effective_filemap_path().parent / "filemap_root.txt"
                    _staging = game.get_effective_mod_staging_path()
                    _strip   = getattr(game, "mod_folder_strip_prefixes", None)
                    _per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
                    _rf_count = deploy_root_flagged_mods(
                        _filemap_root_path, game_root, _staging,
                        mode=deploy_mode, strip_prefixes=_strip,
                        per_mod_strip_prefixes=_per_mod_strip or None,
                        log_fn=_tlog,
                    )
                    if _rf_count:
                        _tlog(f"Root-flagged mods: {_rf_count} file(s) deployed to game root.")

                # Launcher swap runs after root-folder deploy so that script
                # extender executables (e.g. nvse_loader.exe) are present first.
                if hasattr(game, "swap_launcher"):
                    game.swap_launcher(_tlog)
            except Exception as e:
                import traceback as _tb
                self.after(0, lambda err=e, tb=_tb.format_exc(): self._log(f"Deploy error: {err}\n{tb}"))
            finally:
                # Ensure active profile dir always reflects the UI selection on exit.
                game.set_active_profile_dir(
                    game.get_profile_root() / "profiles" / profile
                )
                self.after(0, lambda: self._set_deploy_buttons_enabled(True))
                self.after(0, self._reload_mod_panel)
                self.after(0, self._update_profile_menu_color)
                self.after(1500, status_bar.clear_progress)

        self._set_deploy_buttons_enabled(False)
        threading.Thread(target=_worker, daemon=True).start()

    def _on_restore(self):
        game = _gh._GAMES.get(self._game_var.get())
        if game is None or not game.is_configured():
            self._log("Restore: no configured game selected.")
            return

        game_root = game.get_game_path()
        status_bar = self.winfo_toplevel()._status
        game_name = game.name

        _show_mod_notification(self, f"Restoring {game_name}", state="info")

        _success = [True]

        def _worker():
            def _tlog(msg):
                self.after(0, lambda m=msg: self._log(m))

            def _progress(done: int, total: int, phase: str | None = None):
                self.after(0, lambda d=done, t=total, p=phase: status_bar.set_progress(d, t, p))

            try:
                # Use the last-deployed profile's paths for restore so runtime
                # files go back to the right overwrite/ folder.
                current_profile = self._profile_var.get()
                last_deployed = game.get_last_deployed_profile()
                if last_deployed:
                    game.set_active_profile_dir(
                        game.get_profile_root() / "profiles" / last_deployed
                    )
                if hasattr(game, "restore"):
                    game.restore(log_fn=_tlog, progress_fn=_progress)
                else:
                    _tlog(f"Restore: '{game_name}' does not support restore.")
                # Restore Root_Folder using the last-deployed profile's Root_Folder.
                root_folder_dir = game.get_effective_root_folder_path()
                if root_folder_dir.is_dir() and game_root:
                    restore_root_folder(root_folder_dir, game_root, log_fn=_tlog)
            except Exception as e:
                _success[0] = False
                self.after(0, lambda err=e: self._log(f"Restore error: {err}"))
            finally:
                # Always restore _active_profile_dir to the currently-selected profile.
                game.set_active_profile_dir(
                    game.get_profile_root() / "profiles" / current_profile
                )
                self.after(0, lambda: self._set_deploy_buttons_enabled(True))
                self.after(0, self._reload_mod_panel)
                self.after(1500, status_bar.clear_progress)
                if _success[0]:
                    game.clear_deploy_active()
                    self.after(0, self._update_profile_menu_color)
                    self.after(0, lambda: _show_mod_notification(self, f"{game_name} Restored", state="success"))

        self._set_deploy_buttons_enabled(False)
        threading.Thread(target=_worker, daemon=True).start()

    def _on_install_mod_right_click(self, event):
        menu = tk.Menu(self, tearoff=0, bg="#2b2b2b", fg="white",
                       activebackground=ACCENT, activeforeground="white",
                       relief="flat", bd=1)
        label = ("✓ Disable Extract (active)" if self._disable_extract
                 else "Disable Extract")
        menu.add_command(label=label, command=self._toggle_disable_extract)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _toggle_disable_extract(self):
        self._disable_extract = not self._disable_extract
        if self._disable_extract:
            self._install_mod_btn.configure(
                text="Install Mod [no extract]",
                fg_color="#7a5a00", hover_color="#a07800",
            )
            self._log("Install Mod: extraction disabled — archives will be moved as-is.")
        else:
            self._install_mod_btn.configure(
                text="Install Mod",
                fg_color=ACCENT, hover_color=ACCENT_HOV,
            )
            self._log("Install Mod: extraction re-enabled.")
        self.after_idle(self._init_threshold)

    def _on_install_mod(self):
        def _on_files_picked(paths: list[str]) -> None:
            if not paths:
                return
            game = _gh._GAMES.get(self._game_var.get())
            if game is None or not game.is_configured():
                self._log("No configured game selected — use + to set the game path first.")
                return
            app = self.winfo_toplevel()
            mod_panel = getattr(app, "_mod_panel", None)
            status_bar = getattr(app, "_status", None)

            if len(paths) == 1:
                self._log(f"Installing: {os.path.basename(paths[0])}")
            else:
                self._log(f"Queuing {len(paths)} mods for installation: {', '.join(os.path.basename(p) for p in paths)}")

            def _extract_progress(done: int, total: int, phase: str | None = None):
                if status_bar is not None:
                    app.after(0, lambda d=done, t=total, p=phase: status_bar.set_progress(d, t, p, title="Extracting"))

            def _worker():
                for i, path in enumerate(paths):
                    if len(paths) > 1:
                        app.after(0, lambda name=os.path.basename(path), idx=i + 1, total=len(paths):
                                  self._log(f"Installing ({idx}/{total}): {name}"))
                    try:
                        install_mod_from_archive(path, app, self._log, game, mod_panel,
                                                 disable_extract=self._disable_extract,
                                                 progress_fn=_extract_progress,
                                                 clear_progress_fn=lambda: app.after(0, status_bar.clear_progress) if status_bar is not None else None)
                    finally:
                        if status_bar is not None:
                            app.after(0, status_bar.clear_progress)

            threading.Thread(target=_worker, daemon=True).start()

        pick_files_mod_archive("Select Mod Archive(s)", lambda ps: self.after(0, lambda: _on_files_picked(ps)))


# ---------------------------------------------------------------------------
# Install logic
# ---------------------------------------------------------------------------
