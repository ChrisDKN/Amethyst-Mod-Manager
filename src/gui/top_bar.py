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
    _ProfileNameDialog,
    _ProtonToolsDialog,
    _MewgenicsDeployChoiceDialog,
    _MewgenicsLaunchCommandDialog,
)
from gui.path_utils import _pick_file_zenity
from gui.install_mod import install_mod_from_archive
from gui.add_game_dialog import AddGameDialog
from gui.nexus_settings_dialog import NexusSettingsDialog
from gui.wizard_dialog import WizardDialog
from Utils.config_paths import get_profiles_dir
from Utils.deploy import deploy_root_folder, restore_root_folder, LinkMode
from Utils.profile_backup import create_backup


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
        _last_game = _load_last_game()
        _initial_game = _last_game if (_last_game and _last_game in game_names) else game_names[0]
        self._game_var = tk.StringVar(value=_initial_game)

        ctk.CTkLabel(
            self, text="Game:", font=FONT_BOLD, text_color=TEXT_MAIN
        ).pack(side="left", padx=(12, 4))

        ctk.CTkButton(
            self, text="+", width=32, height=32, font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9a3a", text_color="white",
            command=self._on_add_game
        ).pack(side="left", padx=(0, 4))

        self._game_menu = ctk.CTkOptionMenu(
            self, values=game_names, variable=self._game_var,
            width=180, height=32, font=FONT_NORMAL,
            fg_color=BG_HEADER, button_color=ACCENT, button_hover_color=ACCENT_HOV,
            dropdown_fg_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_game_change
        )
        self._game_menu.pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            self, text="⚙", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_settings
        ).pack(side="left", padx=(0, 16))

        # Profile
        ctk.CTkLabel(
            self, text="Profile:", font=FONT_BOLD, text_color=TEXT_MAIN
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            self, text="+", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_add_profile
        ).pack(side="left", padx=(0, 2))

        ctk.CTkButton(
            self, text="−", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_remove_profile
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
        self._profile_var = tk.StringVar(value=profile_names[0])
        self._profile_menu = ctk.CTkOptionMenu(
            self, values=profile_names, variable=self._profile_var,
            width=160, height=32, font=FONT_NORMAL,
            fg_color=BG_HEADER, button_color=ACCENT, button_hover_color=ACCENT_HOV,
            dropdown_fg_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_profile_change
        )
        self._profile_menu.pack(side="left", padx=(0, 4))

        # Install Mod button
        _install_mod_icon = load_icon("install.png", size=(30, 30))
        ctk.CTkButton(
            self, text="Install Mod", width=100, height=32, font=FONT_BOLD,
            image=_install_mod_icon, compound="left",
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_install_mod
        ).pack(side="left", padx=(0, 8))

        # Deploy button
        _deploy_icon = load_icon("deploy.png", size=(30, 30))
        self._deploy_btn = ctk.CTkButton(
            self, text="Deploy", width=100, height=32, font=FONT_BOLD,
            image=_deploy_icon, compound="left",
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._on_deploy
        )
        self._deploy_btn.pack(side="left", padx=(0, 8))

        # Restore button
        _restore_icon = load_icon("restore.png", size=(30, 30))
        self._restore_btn = ctk.CTkButton(
            self, text="Restore", width=100, height=32, font=FONT_BOLD,
            image=_restore_icon, compound="left",
            fg_color="#8b1a1a", hover_color="#b22222", text_color="white",
            command=self._on_restore
        )
        self._restore_btn.pack(side="left", padx=(0, 8))

        # Proton tools button
        _proton_icon = load_icon("proton.png", size=(30, 30))
        self._proton_btn = ctk.CTkButton(
            self, text="Proton", width=100, height=32, font=FONT_BOLD,
            image=_proton_icon, compound="left",
            fg_color="#7b2d8b", hover_color="#9a3aae", text_color="white",
            command=self._on_proton_tools
        )
        self._proton_btn.pack(side="left", padx=(0, 8))

        # Wizard button (shown only when the game has wizard tools)
        _wizard_icon = load_icon("wizard.png", size=(30, 30))
        self._wizard_btn = ctk.CTkButton(
            self, text="Wizard", width=100, height=32, font=FONT_BOLD,
            image=_wizard_icon, compound="left",
            fg_color="#4a1272", hover_color="#6318a0", text_color="white",
            command=self._on_wizard
        )
        # Don't pack yet — _update_wizard_visibility() will show/hide it

        # Nexus Mods settings button
        _nexus_icon = load_icon("nexus.png", size=(30, 30))
        ctk.CTkButton(
            self, text="Nexus", width=100, height=32, font=FONT_BOLD,
            image=_nexus_icon, compound="left",
            fg_color="#da8e35", hover_color="#e5a04a", text_color="white",
            command=self._on_nexus_settings
        ).pack(side="left", padx=(0, 4))

        # Show/hide wizard button for the initial game
        self._update_wizard_visibility()

    def _on_nexus_settings(self):
        """Open the Nexus Mods settings dialog."""
        app = self.winfo_toplevel()
        def _key_changed():
            app._init_nexus_api()
            self._log("Nexus API key updated.")
        dialog = NexusSettingsDialog(app, on_key_changed=_key_changed, log_fn=self._log)
        app.wait_window(dialog)

    def _on_proton_tools(self):
        game = _gh._GAMES.get(self._game_var.get())
        if game is None or not game.is_configured():
            self._log("Proton Tools: no configured game selected.")
            return
        dlg = _ProtonToolsDialog(self.winfo_toplevel(), game, self._log)
        self.winfo_toplevel().wait_window(dlg)

    def _on_profile_change(self, value: str):
        self._log(f"Profile: {value}")
        self._reload_mod_panel()

    def _on_wizard(self):
        """Open the Wizard tool-selection dialog for the current game."""
        game = _gh._GAMES.get(self._game_var.get())
        if game is None or not game.is_configured():
            self._log("Wizard: no configured game selected.")
            return
        if not game.wizard_tools:
            self._log("Wizard: no tools available for this game.")
            return
        dlg = WizardDialog(self.winfo_toplevel(), game, self._log)
        self.winfo_toplevel().wait_window(dlg)

    def _update_wizard_visibility(self):
        """Show or hide the Wizard button based on the current game."""
        game = _gh._GAMES.get(self._game_var.get())
        if game and game.wizard_tools:
            # Ensure it's packed right after the Proton button
            try:
                self._wizard_btn.pack(side="left", padx=(0, 8),
                                      after=self._proton_btn)
            except Exception:
                self._wizard_btn.pack(side="left", padx=(0, 8))
        else:
            self._wizard_btn.pack_forget()

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
        self._profile_var.set(profiles[0])
        self._update_wizard_visibility()
        self._reload_mod_panel()

    def _reload_mod_panel(self):
        """Tell the mod panel and plugin panel to load the current game + profile."""
        app = self.winfo_toplevel()
        if not hasattr(app, "_mod_panel"):
            return
        game = _gh._GAMES.get(self._game_var.get())
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
        game = _gh._GAMES.get(game_name)
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
        all_names = sorted(_gh._GAMES.keys())
        if not all_names:
            _load_games()
            all_names = sorted(_gh._GAMES.keys())
        if not all_names:
            self._log("No game handlers discovered.")
            return
        picker = _GamePickerDialog(self.winfo_toplevel(), all_names)
        self.winfo_toplevel().wait_window(picker)
        if picker.result is None:
            return
        game = _gh._GAMES.get(picker.result)
        if game is None:
            return
        dialog = AddGameDialog(self.winfo_toplevel(), game)
        self.winfo_toplevel().wait_window(dialog)
        if dialog.result is not None:
            self._log(f"Game path set: {dialog.result}")
            configured = sorted(n for n, g in _gh._GAMES.items() if g.is_configured())
            self._game_menu.configure(values=configured or ["No games configured"])
            if picker.result in configured:
                self._game_var.set(picker.result)
                _save_last_game(picker.result)
                self._reload_mod_panel()

    def _on_settings(self):
        game_name = self._game_var.get()
        game = _gh._GAMES.get(game_name)
        if game is None:
            self._log("No game selected.")
            return
        dialog = AddGameDialog(self.winfo_toplevel(), game)
        self.winfo_toplevel().wait_window(dialog)
        if getattr(dialog, "removed", False):
            self._log(f"Removed instance: {game_name}")
            # Re-load the game handler so it picks up the missing config
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
            choice_dlg = _MewgenicsDeployChoiceDialog(self.winfo_toplevel())
            self.winfo_toplevel().wait_window(choice_dlg)
            if choice_dlg.result is None:
                return
            if choice_dlg.result == "steam":
                launch_string = game.get_modpaths_launch_string(profile)
                launch_dlg = _MewgenicsLaunchCommandDialog(
                    self.winfo_toplevel(), launch_string
                )
                return
            # choice_dlg.result == "repack" -> fall through to normal deploy

        app = self.winfo_toplevel()
        root_folder_enabled = (
            app._mod_panel._root_folder_enabled
            if hasattr(app, "_mod_panel") else True
        )
        root_folder_dir = game.get_mod_staging_path().parent / "Root_Folder"
        game_root = game.get_game_path()

        status_bar = self.winfo_toplevel()._status

        def _worker():
            # Thread-safe log: schedule UI update on the main thread.
            def _tlog(msg):
                self.after(0, lambda m=msg: self._log(m))

            def _progress(done: int, total: int, phase: str | None = None):
                self.after(0, lambda d=done, t=total, p=phase: status_bar.set_progress(d, t, p))

            try:
                if getattr(game, "restore_before_deploy", True) and hasattr(game, "restore"):
                    try:
                        game.restore(log_fn=_tlog, progress_fn=_progress)
                    except RuntimeError:
                        pass
                if root_folder_dir.is_dir() and game_root:
                    restore_root_folder(root_folder_dir, game_root, log_fn=_tlog)

                # Backup modlist/plugins before deploy
                profile_dir = game.get_profile_root() / "profiles" / profile
                try:
                    create_backup(profile_dir, _tlog)
                except Exception as backup_err:
                    _tlog(f"Backup skipped: {backup_err}")

                deploy_mode = game.get_deploy_mode() if hasattr(game, "get_deploy_mode") else LinkMode.HARDLINK
                game.deploy(log_fn=_tlog, profile=profile, progress_fn=_progress,
                            mode=deploy_mode)

                rf_allowed = getattr(game, "root_folder_deploy_enabled", True)
                if rf_allowed and root_folder_enabled and root_folder_dir.is_dir() and game_root:
                    count = deploy_root_folder(root_folder_dir, game_root,
                                            mode=deploy_mode, log_fn=_tlog)
                    if count:
                        _tlog("Root Folder: transferred files to game root.")
            except Exception as e:
                self.after(0, lambda err=e: self._log(f"Deploy error: {err}"))
            finally:
                self.after(0, lambda: self._set_deploy_buttons_enabled(True))
                self.after(1500, status_bar.clear_progress)

        self._set_deploy_buttons_enabled(False)
        threading.Thread(target=_worker, daemon=True).start()

    def _on_restore(self):
        game = _gh._GAMES.get(self._game_var.get())
        if game is None or not game.is_configured():
            self._log("Restore: no configured game selected.")
            return

        root_folder_dir = game.get_mod_staging_path().parent / "Root_Folder"
        game_root = game.get_game_path()
        status_bar = self.winfo_toplevel()._status

        def _worker():
            def _tlog(msg):
                self.after(0, lambda m=msg: self._log(m))

            def _progress(done: int, total: int, phase: str | None = None):
                self.after(0, lambda d=done, t=total, p=phase: status_bar.set_progress(d, t, p))

            try:
                if hasattr(game, "restore"):
                    game.restore(log_fn=_tlog, progress_fn=_progress)
                else:
                    _tlog(f"Restore: '{game.name}' does not support restore.")
                if root_folder_dir.is_dir() and game_root:
                    restore_root_folder(root_folder_dir, game_root, log_fn=_tlog)
            except Exception as e:
                self.after(0, lambda err=e: self._log(f"Restore error: {err}"))
            finally:
                self.after(0, lambda: self._set_deploy_buttons_enabled(True))
                self.after(1500, status_bar.clear_progress)

        self._set_deploy_buttons_enabled(False)
        threading.Thread(target=_worker, daemon=True).start()

    def _on_install_mod(self):
        path = _pick_file_zenity("Select Mod Archive")
        if not path:
            return
        game = _gh._GAMES.get(self._game_var.get())
        if game is None or not game.is_configured():
            self._log("No configured game selected — use + to set the game path first.")
            return
        self._log(f"Installing: {os.path.basename(path)}")
        app = self.winfo_toplevel()
        mod_panel = getattr(app, "_mod_panel", None)
        install_mod_from_archive(path, app, self._log, game, mod_panel)


# ---------------------------------------------------------------------------
# Install logic
# ---------------------------------------------------------------------------
