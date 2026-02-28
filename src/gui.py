"""
Amethyst Mod Manager — main entry point.
Builds the main window (App) from gui panels and runs the event loop.
"""

import errno
import os
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.messagebox
from pathlib import Path
import webbrowser

# Set MOD_MANAGER_GAMES so game discovery finds Games/ even when cwd or launcher differs.
# Try script dir and its parent (gui.py in src/ -> src/Games; python -m gui -> gui/ so use parent/Games).
if not os.environ.get("MOD_MANAGER_GAMES"):
    for _origin in (getattr(sys.modules.get("__main__"), "__file__", None), __file__, sys.argv[0] if sys.argv else None):
        if not _origin:
            continue
        _base = Path(_origin).resolve().parent
        for _cand in (_base / "Games", _base.parent / "Games"):
            if _cand.is_dir() and any(_cand.glob("*/*.py")):
                os.environ["MOD_MANAGER_GAMES"] = str(_cand)
                break
        else:
            continue
        break

import customtkinter as ctk

from gui.theme import ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_HOVER, BORDER, FONT_BOLD, FONT_NORMAL, TEXT_MAIN
from gui.game_helpers import (
    _GAMES,
    _vanilla_plugins_for_game,
    _handle_missing_profile_root,
)
from gui.modlist_panel import ModListPanel
from gui.plugin_panel import PluginPanel
from gui.top_bar import TopBar
from gui.status_bar import StatusBar
from gui.install_mod import install_mod_from_archive
from gui.mod_name_utils import _suggest_mod_names
from gui.version_check import (
    _fetch_latest_version,
    _is_newer_version,
    _APP_UPDATE_RELEASES_URL,
    _APP_UPDATE_INSTALLER_URL,
)

from version import __version__
from Utils.app_log import set_app_log
from Utils.plugins import prune_plugins_from_filemap, sync_plugins_from_filemap
from Nexus.nexus_api import NexusAPI, load_api_key
from Nexus.nexus_download import NexusDownloader
from Nexus.nxm_handler import NxmLink, NxmHandler, NxmIPC
from Nexus.nexus_meta import build_meta_from_download, write_meta

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

_INSTALLER_CMD = (
    f"curl -sSL {_APP_UPDATE_INSTALLER_URL} | bash"
)


def _run_installer():
    """Run the AppImage installer in a subprocess."""
    try:
        subprocess.Popen(
            ["bash", "-c", _INSTALLER_CMD],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


class _UpdateAvailableDialog(ctk.CTkToplevel):
    """Modal dialog when a new app version is available. Offers update via installer or open releases page."""

    def __init__(self, parent, current_version: str, latest_version: str):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Update available")
        self.geometry("440x220")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._make_modal)

        self._parent = parent
        self._current = current_version
        self._latest = latest_version
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        msg = (
            f"A new version of Amethyst Mod Manager is available.\n\n"
            f"Current: {self._current}\n"
            f"Latest:  {self._latest}"
        )
        ctk.CTkLabel(
            self, text=msg, font=FONT_NORMAL, text_color=TEXT_MAIN,
            justify="left", anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 12))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 20))
        btn_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(
            btn_frame, text="Update via installer",
            width=160, height=32, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_update
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Open releases page",
            width=140, height=32, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_releases
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Later",
            width=80, height=32, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close
        ).pack(side="left")

    def _on_update(self):
        _run_installer()
        self.grab_release()
        self.destroy()
        tk.messagebox.showinfo(
            "Update started",
            "The update installer has been started.\n\n"
            "Please close and reopen the application when the installer finishes.",
            parent=self._parent,
        )

    def _on_releases(self):
        webbrowser.open(_APP_UPDATE_RELEASES_URL)
        self.grab_release()
        self.destroy()

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=BG_DEEP)
        self.geometry("1400x820")
        self.minsize(900, 600)
        # Thread-safe callback queue — background threads must never call
        # widget.after() directly (Python 3.13 Tkinter enforces this).
        # Use  app.call_threadsafe(fn)  instead.
        import queue as _queue
        self._ts_queue: _queue.Queue = _queue.Queue()
        self._poll_threadsafe_queue()
        self._nexus_api: NexusAPI | None = None
        self._nexus_downloader: NexusDownloader | None = None
        self._nexus_username: str | None = None
        self._init_nexus_api()
        self._update_window_title()
        self._build_layout()
        self._startup_log()
        # Process --nxm argument if the app was launched via protocol handler
        self._handle_nxm_argv()
        # Check for app update after a short delay (non-blocking)
        self.after(2000, self._check_for_app_update)
        icon_path = Path(__file__).parent / "icons" / "title-bar.png"
        if icon_path.is_file():
            icon_img = tk.PhotoImage(file=str(icon_path))
            self.iconphoto(False, icon_img)

    # -- Thread-safe callback scheduling ------------------------------------

    def call_threadsafe(self, fn):
        """Schedule *fn* to run on the main/UI thread.

        Safe to call from any thread — the callback is placed on a queue that
        the main-loop polls every 50 ms.  Use this instead of
        ``widget.after(0, fn)`` from background threads.
        """
        self._ts_queue.put(fn)

    def _poll_threadsafe_queue(self):
        import queue as _queue
        while True:
            try:
                fn = self._ts_queue.get_nowait()
                fn()
            except _queue.Empty:
                break
            except Exception:
                pass
        self.after(50, self._poll_threadsafe_queue)

    # -- Nexus API init -----------------------------------------------------

    def _update_window_title(self):
        """Set the window title, showing Nexus username when logged in."""
        base = f"Amethyst Mod Manager v{__version__}"
        if self._nexus_username:
            self.title(f"{base} - Logged in to Nexus as {self._nexus_username}")
        else:
            self.title(base)

    def _init_nexus_api(self):
        """Load saved API key and initialise the Nexus client (if key exists)."""
        key = load_api_key()
        if key:
            self._nexus_api = NexusAPI(api_key=key)
            self._nexus_downloader = NexusDownloader(self._nexus_api)
            # Fetch the username in background so the title updates after the API responds
            def _fetch_user():
                try:
                    user = self._nexus_api.validate()
                    self._nexus_username = user.name
                except Exception:
                    self._nexus_username = None
                self.call_threadsafe(self._update_window_title)
            threading.Thread(target=_fetch_user, daemon=True).start()
        else:
            self._nexus_api = None
            self._nexus_downloader = None
            self._nexus_username = None
            # Update title synchronously when key is absent / cleared
            self.after(0, self._update_window_title)

    # -- App update check ---------------------------------------------------

    def _check_for_app_update(self):
        """Run in background: fetch latest version and prompt to download if newer."""

        def _do_check():
            latest = _fetch_latest_version()
            if latest is None:
                return
            if _is_newer_version(__version__, latest):

                def _show():
                    dlg = _UpdateAvailableDialog(self, __version__, latest)
                    self.wait_window(dlg)

                self.call_threadsafe(_show)

        threading.Thread(target=_do_check, daemon=True).start()

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
        _archive_path = result.file_path
        _installed = False

        def _on_installed():
            nonlocal _installed
            _installed = True

        install_mod_from_archive(str(_archive_path), self, log, game, mod_panel,
                                 on_installed=_on_installed)

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
            # Determine the mod folder name (same logic as install_mod_from_archive)
            raw_stem = os.path.splitext(os.path.basename(str(_archive_path)))[0]
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

        if _installed and _archive_path and _archive_path.is_file():
            try:
                _archive_path.unlink()
                log(f"Nexus: Removed archive {_archive_path.name}")
            except OSError:
                pass
            if hasattr(self, "_plugin_panel"):
                dl_panel = getattr(self._plugin_panel, "_downloads_panel", None)
                if dl_panel:
                    dl_panel.refresh()

    def _build_layout(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)
        self.grid_columnconfigure(0, weight=1)

        # Build status bar first so log_fn is available immediately
        self._status = StatusBar(self)
        self._status.grid(row=2, column=0, sticky="ew")

        log = self._status.log
        set_app_log(log, self.after)

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
            # Highlight plugins belonging to the selected mod
            mod_name = None
            if self._mod_panel._sel_idx >= 0 and self._mod_panel._sel_idx < len(self._mod_panel._entries):
                entry = self._mod_panel._entries[self._mod_panel._sel_idx]
                if not entry.is_separator:
                    mod_name = entry.name
            self._plugin_panel.set_highlighted_plugins(mod_name)
        self._mod_panel._on_mod_selected_cb = _on_mod_selected  # mod selected → clear plugin selection + highlight

        # Load initial game + profile — set plugin paths BEFORE load_game
        # because load_game triggers filemap rebuild which reads _plugins_path.
        game_name = self._topbar._game_var.get()
        initial_game = _GAMES.get(game_name)
        if initial_game and initial_game.is_configured():
            profile = self._topbar._profile_var.get()
            try:
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
            except (FileNotFoundError, OSError) as e:
                if getattr(e, "errno", None) == errno.ENOENT or isinstance(e, FileNotFoundError):
                    _handle_missing_profile_root(self._topbar, self._topbar._game_var.get())
                else:
                    raise
        else:
            # No configured game selected: load empty state so mod/plugin panels redraw
            self._mod_panel.load_game(None, "")
            if hasattr(self._plugin_panel, "_plugin_entries"):
                self._plugin_panel._plugin_entries = []

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
