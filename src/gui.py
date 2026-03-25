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
from Utils.xdg import open_url

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

# Override Xft.dpi to 96 before Tk initialises so font rasterisation ignores
# the OS global scaling setting (e.g. 200% sets Xft.dpi=192, doubling fonts).
try:
    subprocess.run(
        ["xrdb", "-merge"],
        input="Xft.dpi: 96\n",
        text=True,
        timeout=2,
        check=False,
    )
except Exception:
    pass

import customtkinter as ctk

# Load UI scale from config and apply before any widgets are created.
# Stored in ~/.config/AmethystModManager/amethyst.ini [ui] scale=...
from Utils.ui_config import load_ui_scale, get_ui_scale, load_window_geometry, save_window_geometry
_UI_SCALE = load_ui_scale()
ctk.set_widget_scaling(_UI_SCALE)
ctk.set_window_scaling(_UI_SCALE)

from gui.theme import ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_HOVER, BORDER, FONT_BOLD, FONT_NORMAL, TEXT_MAIN, init_fonts, scaled, scaled_layout_minsize
from gui.game_helpers import (
    _GAMES,
    _vanilla_plugins_for_game,
    _handle_missing_profile_root,
)
from gui.modlist_panel import ModListPanel
from Utils.filemap import OVERWRITE_NAME as _OVERWRITE_NAME
from gui.plugin_panel import PluginPanel
from gui.top_bar import TopBar
from gui.status_bar import StatusBar
from gui.install_mod import install_mod_from_archive
from gui.mod_name_utils import _suggest_mod_names
from gui.version_check import (
    is_appimage,
    is_flatpak,
    _fetch_latest_version,
    _fetch_aur_version,
    _is_newer_version,
    _APP_UPDATE_RELEASES_URL,
    _APP_UPDATE_INSTALLER_URL,
    _AUR_PACKAGE_URL,
)

from version import __version__
from Utils.app_log import app_log, set_app_log
from Utils.plugins import (
    prune_plugins_from_filemap,
    sync_plugins_from_filemap,
    sync_plugins_from_overwrite_dir,
    read_plugins,
    write_plugins,
)
from Utils.profile_state import read_disabled_plugins
from Nexus.nexus_api import NexusAPI, load_api_key, clear_api_key
from Nexus.nexus_oauth import load_oauth_tokens, clear_oauth_tokens
from Nexus.nexus_download import NexusDownloader, delete_archive_and_sidecar
from Nexus.nxm_handler import NxmLink, NxmCollectionLink, NxmHandler, NxmIPC, parse_nxm_url
from Nexus.nexus_meta import build_meta_from_download, write_meta
from Utils.config_paths import get_download_cache_dir

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

def _run_installer():
    """Run the AppImage installer in a detached subprocess.

    The AppImage runtime sets SSL_CERT_FILE / CURL_CA_BUNDLE to a path inside
    its own mount point.  That mount is gone once the app exits, so curl would
    fail with a certificate error.  We scrub those variables (and any other
    AppImage-injected ones) from the child environment before launching.
    Output is logged to $XDG_CONFIG_HOME/amethyst-update.log for debugging.
    sleep 2 gives the app time to fully exit before the installer overwrites
    the running AppImage.
    """
    import os
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "AmethystModManager",
    )
    os.makedirs(config_dir, exist_ok=True)
    log_path = os.path.join(config_dir, "amethyst-update.log")
    cmd = (
        f"sleep 2 && "
        f"SCRIPT=$(mktemp /tmp/amethyst-installer-XXXXXX.sh) && "
        f"curl -sSL {_APP_UPDATE_INSTALLER_URL} -o \"$SCRIPT\" && "
        f"chmod +x \"$SCRIPT\" && "
        f"bash \"$SCRIPT\" && "
        f"rm -f \"$SCRIPT\" && "
        f"nohup \"$HOME/Applications/AmethystModManager-x86_64.AppImage\" &>/dev/null &"
    )

    # Build a clean environment: start from the current env then strip every
    # variable that the AppImage runtime injects and that would be invalid once
    # the mount is gone.
    _APPIMAGE_ENV_PREFIXES = (
        "APPDIR", "APPIMAGE", "OWD",
        "SSL_CERT_FILE", "SSL_CERT_DIR",
        "CURL_CA_BUNDLE",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PYTHONHOME", "PYTHONPATH",
        "GDK_PIXBUF_MODULEDIR", "GDK_PIXBUF_MODULE_FILE",
        "GIO_MODULE_DIR",
        "GSETTINGS_SCHEMA_DIR",
        "GTK_PATH", "GTK_IM_MODULE_FILE",
        "QT_PLUGIN_PATH",
        "PERLLIB", "PERL5LIB",
    )
    clean_env = {
        k: v for k, v in os.environ.items()
        if not any(k.startswith(p) for p in _APPIMAGE_ENV_PREFIXES)
    }

    try:
        subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=clean_env,
        )
    except Exception:
        pass




# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=BG_DEEP)
        self.withdraw()  # hide until fully built; splash covers during load

        # --- Splash screen (child of this root so images share the same Tk) --
        _SPLASH_BG = "#1a1a2e"
        self._splash: tk.Toplevel | None = tk.Toplevel(self)
        self._splash.withdraw()  # hide until geometry is set to prevent flash at 0,0
        self._splash.overrideredirect(True)
        self._splash.configure(bg=_SPLASH_BG)
        _logo_path = Path(__file__).parent / "icons" / "Logo.png"
        self._splash_logo_img = None
        if _logo_path.is_file():
            try:
                from PIL import Image as _PILImage, ImageTk as _PILImageTk
                _pil = _PILImage.open(str(_logo_path)).convert("RGBA")
                # Composite logo onto the dark bg so alpha edges blend correctly
                _bg = _PILImage.new("RGBA", _pil.size, _SPLASH_BG)
                _bg.paste(_pil, mask=_pil.split()[3])
                self._splash_logo_img = _PILImageTk.PhotoImage(_bg.convert("RGB"))
            except Exception:
                self._splash_logo_img = tk.PhotoImage(file=str(_logo_path))
            tk.Label(self._splash, image=self._splash_logo_img,
                     bg=_SPLASH_BG, bd=0).pack()
        else:
            tk.Label(self._splash, text="Loading…", bg=_SPLASH_BG, fg="white",
                     font=("sans-serif", 18)).pack(padx=40, pady=40)
        self._splash.update_idletasks()
        _sw = self._splash.winfo_reqwidth()
        _sh = self._splash.winfo_reqheight()
        # Centre on the monitor containing the mouse pointer.
        # On multi-monitor setups winfo_screenwidth returns the combined virtual
        # desktop width; we use the pointer position + monitor list to find the
        # right monitor.  Several query paths are tried in order.
        def _monitor_rect_for_pointer():
            """Return (mon_x, mon_y, mon_w, mon_h) for the monitor under the pointer."""
            try:
                import re as _re, subprocess as _sp
                _px = self._splash.winfo_pointerx()
                _py = self._splash.winfo_pointery()

                # --- Try xrandr (XWayland / X11) ---
                try:
                    out = _sp.check_output(["xrandr", "--query"], text=True, timeout=2)
                    for _m in _re.finditer(r"(\d+)x(\d+)\+(\d+)\+(\d+)", out):
                        _mw, _mh, _mx, _my = (int(g) for g in _m.groups())
                        if _mx <= _px < _mx + _mw and _my <= _py < _my + _mh:
                            return _mx, _my, _mw, _mh
                except Exception:
                    pass

                # --- Try wlr-randr (wlroots/Sway) ---
                try:
                    out = _sp.check_output(["wlr-randr"], text=True, timeout=2)
                    # Lines: "  1920x1080 px, ..." preceded by position line "  ...at 1920,0"
                    _blocks = _re.findall(
                        r"(\d+)x(\d+) px.*?at (\d+),(\d+)", out, _re.DOTALL)
                    for _mw, _mh, _mx, _my in (
                            (int(a), int(b), int(c), int(d)) for a, b, c, d in _blocks):
                        if _mx <= _px < _mx + _mw and _my <= _py < _my + _mh:
                            return _mx, _my, _mw, _mh
                except Exception:
                    pass

                # --- Heuristic: if screen is wider than tall, assume side-by-side monitors ---
                _sw_total = self.winfo_screenwidth()
                _sh_total = self.winfo_screenheight()
                if _sw_total > _sh_total * 1.5:
                    # Guess monitor width as half the virtual width
                    _mw = _sw_total // 2
                    _mx = (_px // _mw) * _mw
                    return _mx, 0, _mw, _sh_total

            except Exception:
                pass
            return 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()

        _mrx, _mry, _mrw, _mrh = _monitor_rect_for_pointer()
        _sx = _mrx + (_mrw - _sw) // 2
        _sy = _mry + (_mrh - _sh) // 2
        self._splash.geometry(f"{_sw}x{_sh}+{_sx}+{_sy}")
        self._splash.deiconify()  # show only after geometry is set — no flash at 0,0
        self._splash.update()
        # ---------------------------------------------------------------------

        init_fonts(self)
        # Restore saved window size/position, or use default
        saved_geom = load_window_geometry()
        if saved_geom:
            try:
                # Parse WxH or WxH+X+Y
                import re
                m = re.match(r"(\d+)x(\d+)", saved_geom)
                if m:
                    sw = self.winfo_screenwidth()
                    sh = self.winfo_screenheight()
                    w, h = int(m.group(1)), int(m.group(2))
                    if w <= sw and h <= sh:
                        self.geometry(saved_geom)
                    else:
                        self.geometry("1280x720")
                else:
                    self.geometry("1280x720")
            except Exception:
                self.geometry("1280x720")
        else:
            self.geometry("1280x720")
        # Scale minsize with UI scale so window can't be shrunk below the toolbar
        _s = get_ui_scale()
        _min_w = int(1280 * _s)
        _min_h = int(720 * _s)
        self.minsize(_min_w, _min_h)
        self.bind("<Configure>", self._on_window_configure)
        self._geom_save_id: str | None = None
        # Thread-safe callback queue — background threads must never call
        # widget.after() directly (Python 3.13 Tkinter enforces this).
        # Use  app.call_threadsafe(fn)  instead.
        import queue as _queue
        self._ts_queue: _queue.Queue = _queue.Queue()
        self._poll_threadsafe_queue()
        self._nexus_api: NexusAPI | None = None
        self._nexus_downloader: NexusDownloader | None = None
        self._nexus_username: str | None = None
        # Installs that arrived while a modal dialog had the grab are deferred
        # here and replayed once the modal closes.
        self._nxm_install_queue: list = []
        self._init_nexus_api()
        self._update_window_title()
        self._build_layout()
        self._startup_log()
        # Show onboarding if no games are configured yet
        configured = sum(1 for g in _GAMES.values() if g.is_configured())
        if configured == 0:
            self.after(200, self._show_onboarding)
            # No game configured → no filemap rebuild will fire; dismiss splash after layout.
            self.after(500, self._finish_splash)
        # Process --nxm argument if the app was launched via protocol handler
        self._handle_nxm_argv()
        # Check for app update after a short delay (non-blocking)
        self.after(2000, self._check_for_app_update)
        icon_path = Path(__file__).parent / "icons" / "title-bar.png"
        if icon_path.is_file():
            icon_img = tk.PhotoImage(file=str(icon_path))
            self.iconphoto(False, icon_img)

    # -- Splash screen ------------------------------------------------------

    def _finish_splash(self):
        if self._splash is not None:
            try:
                self._splash.destroy()
            except Exception:
                pass
            self._splash = None
        self.update_idletasks()
        self.deiconify()

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
        """Load saved API key (or OAuth tokens) and initialise the Nexus client."""
        # Reset any existing client so a stale session can't persist after credentials are cleared
        self._nexus_api = None
        self._nexus_downloader = None
        # Legacy personal API keys are no longer used — clear any stored key on startup
        clear_api_key()
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
            tokens = load_oauth_tokens()
            if tokens:
                try:
                    self._nexus_api = NexusAPI.from_oauth(tokens)
                except Exception as exc:
                    app_log(f"OAuth token refresh failed, clearing saved tokens: {exc}")
                    clear_oauth_tokens()
                    tokens = None
                if tokens:
                    self._nexus_downloader = NexusDownloader(self._nexus_api)
                    def _fetch_user_oauth():
                        try:
                            import requests as _req
                            resp = _req.get(
                                "https://users.nexusmods.com/oauth/userinfo",
                                headers={"Authorization": f"Bearer {tokens.access_token}"},
                                timeout=15,
                            )
                            resp.raise_for_status()
                            data = resp.json()
                            self._nexus_username = (
                                data.get("name") or data.get("preferred_username") or data.get("sub")
                            )
                        except Exception:
                            self._nexus_username = None
                        self.call_threadsafe(self._update_window_title)
                    threading.Thread(target=_fetch_user_oauth, daemon=True).start()
            if not tokens and not self._nexus_api:
                self._nexus_api = None
                self._nexus_downloader = None
                self._nexus_username = None
                # Update title synchronously when key is absent / cleared
                self.after(0, self._update_window_title)

    # -- App update check ---------------------------------------------------

    def _show_update_overlay(self, current: str, latest: str, *, mode: str = "appimage"):
        """Show an update-available banner overlaid on the mod list panel.

        *mode* is one of ``"appimage"``, ``"flatpak"``, or ``"aur"``.
        """
        container = self._mod_panel_container

        overlay = ctk.CTkFrame(container, fg_color=BG_DEEP, corner_radius=8)
        overlay.place(relx=0.5, rely=0.5, anchor="center")

        def _close():
            overlay.place_forget()
            overlay.destroy()

        inner = ctk.CTkFrame(overlay, fg_color="transparent")
        inner.pack(padx=24, pady=20)

        if mode == "aur":
            msg = (
                f"A new version of Amethyst Mod Manager is available on the AUR.\n\n"
                f"Current: {current}\n"
                f"AUR:     {latest}\n\n"
                f"Update via your AUR helper, e.g.\n"
                f"  yay -Syu amethyst-mod-manager"
            )
        else:
            msg = (
                f"A new version of Amethyst Mod Manager is available.\n\n"
                f"Current: {current}\n"
                f"Latest:  {latest}"
            )

        ctk.CTkLabel(
            inner, text=msg, font=FONT_NORMAL, text_color=TEXT_MAIN,
            justify="left", anchor="w",
        ).pack(anchor="w", pady=(0, 14))

        btn_frame = ctk.CTkFrame(inner, fg_color="transparent")
        btn_frame.pack(anchor="w")

        if mode == "appimage":
            def _on_update():
                _run_installer()
                _close()
                try:
                    from Nexus.nxm_handler import NxmIPC
                    NxmIPC.shutdown()
                except Exception:
                    pass
                self.destroy()

            ctk.CTkButton(
                btn_frame, text="Update via installer",
                width=160, height=32, font=FONT_BOLD,
                fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
                command=_on_update,
            ).pack(side="left", padx=(0, 8))

        if mode == "aur":
            ctk.CTkButton(
                btn_frame, text="Open AUR page",
                width=140, height=32, font=FONT_NORMAL,
                fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
                command=lambda: (open_url(_AUR_PACKAGE_URL), _close()),
            ).pack(side="left", padx=(0, 8))
        else:
            ctk.CTkButton(
                btn_frame, text="Open releases page",
                width=140, height=32, font=FONT_NORMAL,
                fg_color=ACCENT if mode == "flatpak" else BG_HEADER,
                hover_color=ACCENT_HOV if mode == "flatpak" else BG_HOVER,
                text_color="white" if mode == "flatpak" else TEXT_MAIN,
                command=lambda: (open_url(_APP_UPDATE_RELEASES_URL), _close()),
            ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Later",
            width=80, height=32, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=_close,
        ).pack(side="left")

    def _check_for_app_update(self):
        """Run in background: fetch latest version and prompt if newer.

        AppImage installs compare against GitHub releases and offer the
        auto-installer.  System installs (e.g. AUR) compare against the AUR
        package version and show instructions to update via the AUR helper.
        """

        def _do_check():
            if is_appimage():
                latest = _fetch_latest_version()
                if latest is None:
                    return
                if _is_newer_version(__version__, latest):
                    self.call_threadsafe(
                        lambda: self._show_update_overlay(__version__, latest, mode="appimage")
                    )
            elif is_flatpak():
                latest = _fetch_latest_version()
                if latest is None:
                    return
                if _is_newer_version(__version__, latest):
                    self.call_threadsafe(
                        lambda: self._show_update_overlay(__version__, latest, mode="flatpak")
                    )
            else:
                aur_ver = _fetch_aur_version()
                if aur_ver is None:
                    return
                if _is_newer_version(__version__, aur_ver):
                    self.call_threadsafe(
                        lambda: self._show_update_overlay(__version__, aur_ver, mode="aur")
                    )

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
        """Handle an nxm:// link — either download a mod or open a collection."""
        log = self._status.log

        if self._nexus_api is None:
            log("Nexus: No API key configured — cannot use Nexus features.")
            log("Open the Nexus button in the toolbar to set your API key.")
            from tkinter import messagebox
            messagebox.showwarning(
                "Nexus API Key Required",
                "You need to set your Nexus Mods API key.\n\n"
                "Click the \"Nexus\" button in the toolbar to enter your key.\n\n"
                "Get your key from:\nnexusmods.com → Settings → API Keys",
                parent=self,
            )
            return

        try:
            mod_link, coll_link = parse_nxm_url(nxm_url)
        except ValueError as exc:
            log(f"Nexus: Bad nxm:// URL — {exc}")
            return

        if coll_link is not None:
            self._process_nxm_collection_link(coll_link)
            return

        if self._nexus_downloader is None:
            log("Nexus: No downloader configured — cannot download mods.")
            return

        link = mod_link
        log(f"Nexus: Downloading mod {link.mod_id} file {link.file_id} "
            f"from {link.game_domain}...")

        # Show download progress bar on the mod panel
        mod_panel = getattr(self, "_mod_panel", None)
        cancel_event = mod_panel.get_download_cancel_event() if mod_panel else None
        if mod_panel:
            mod_panel.show_download_progress("Downloading...", cancel=cancel_event)

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
            # Fetch mod + file info in a single GraphQL call for metadata
            mod_info = None
            file_info = None
            try:
                mod_info, file_info = self._nexus_api.get_mod_and_file_info_graphql(
                    link.game_domain, link.mod_id, link.file_id)
                # Update the progress bar label with the actual mod name
                if mod_panel and mod_info:
                    self.after(0, lambda: mod_panel.show_download_progress(
                        f"Downloading: {mod_info.name}", cancel=cancel_event))
            except Exception as exc:
                self.after(0, lambda m=str(exc): log(
                    f"Nexus: Could not fetch mod info ({m}) — metadata will be partial."))

            result = self._nexus_downloader.download_from_nxm(
                link,
                known_file_name=file_info.file_name if file_info else "",
                progress_cb=lambda cur, total: self.after(
                    0, lambda c=cur, t=total: (
                        mod_panel.update_download_progress(c, t, cancel=cancel_event)
                        if mod_panel else None
                    )
                ),
                cancel=cancel_event,
                dest_dir=get_download_cache_dir(),
            )
            if result.success and result.file_path:
                self.after(0, lambda: (
                    mod_panel.hide_download_progress(cancel=cancel_event) if mod_panel else None,
                    self._nxm_install(
                        result, matched_game, mod_info=mod_info, file_info=file_info),
                ))
            else:
                self.after(0, lambda: (
                    mod_panel.hide_download_progress(cancel=cancel_event) if mod_panel else None,
                    log(f"Nexus: Download failed — {result.error}"),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _process_nxm_collection_link(self, coll_link: NxmCollectionLink):
        """Switch to the matching game and open the collection page."""
        log = self._status.log
        log(f"Nexus: Opening collection '{coll_link.slug}' from {coll_link.game_domain}")

        # Find the game matching the collection's domain
        matched_game = None
        for name, game in _GAMES.items():
            if game.nexus_game_domain == coll_link.game_domain and game.is_configured():
                matched_game = (name, game)
                break

        if not matched_game:
            log(f"Nexus: No configured game found for domain '{coll_link.game_domain}' — cannot open collection.")
            tkinter.messagebox.showinfo(
                "Collection Link",
                f"No configured game found for Nexus domain '{coll_link.game_domain}'.\n\n"
                "Add and configure the game (e.g. Stardew Valley) to open collections.",
                parent=self,
            )
            return

        # Switch to the matching game if different
        current = self._topbar._game_var.get()
        if current != matched_game[0]:
            self._topbar._game_var.set(matched_game[0])
            self._topbar._on_game_change(matched_game[0])
            log(f"Nexus: Switched to game '{matched_game[0]}'")

        # Open collections panel with this slug (after a short delay so mod panel is ready)
        def _open():
            mod_panel = getattr(self, "_mod_panel", None)
            if mod_panel:
                mod_panel._on_collections(
                    initial_slug=coll_link.slug,
                    initial_game_domain=coll_link.game_domain,
                    initial_revision=coll_link.revision_id or None,
                )

        self.after(200, _open)

    def _nxm_install(self, result, matched_game, mod_info=None, file_info=None):
        """Install a downloaded NXM file into the current game.

        If a modal dialog currently holds the Tk grab (e.g. a FOMOD wizard is
        open), the install is deferred until the modal is dismissed to avoid a
        deadlock from nested wait_window / grab_set calls.
        """
        if self.grab_current() is not None:
            # A modal is open — queue and poll until it's gone.
            self._nxm_install_queue.append((result, matched_game, mod_info, file_info))
            self._poll_nxm_install_queue()
            return
        self._nxm_install_impl(result, matched_game, mod_info=mod_info, file_info=file_info)

    def _poll_nxm_install_queue(self):
        """Retry queued NXM installs once no modal dialog holds the grab."""
        if not self._nxm_install_queue:
            return
        if self.grab_current() is not None:
            # Still blocked — check again shortly.
            self.after(300, self._poll_nxm_install_queue)
            return
        # Modal is gone — run installs in order, stopping if a new modal opens.
        while self._nxm_install_queue:
            result, matched_game, mod_info, file_info = self._nxm_install_queue.pop(0)
            self._nxm_install_impl(result, matched_game, mod_info=mod_info, file_info=file_info)
            if self.grab_current() is not None:
                # This install opened a modal; resume after it closes.
                self.after(300, self._poll_nxm_install_queue)
                break

    def _nxm_install_impl(self, result, matched_game, mod_info=None, file_info=None):
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
            meta_path = game.get_effective_mod_staging_path() / folder_name / "meta.ini"
            if meta_path.parent.is_dir():
                write_meta(meta_path, meta)
                log(f"Nexus: Saved metadata (mod {meta.mod_id}, v{meta.version})")
        except Exception as exc:
            log(f"Nexus: Warning — could not save metadata: {exc}")

        if _installed and _archive_path:
            try:
                delete_archive_and_sidecar(_archive_path)
                log(f"Nexus: Removed archive {_archive_path.name}")
            except OSError:
                pass
            if hasattr(self, "_plugin_panel"):
                dl_panel = getattr(self._plugin_panel, "_downloads_panel", None)
                if dl_panel:
                    dl_panel.refresh()

    def _on_window_configure(self, event: tk.Event) -> None:
        # Only react to top-level window events, not child widgets
        if event.widget is not self:
            return
        # Debounce — save 500ms after the last resize/move event
        if self._geom_save_id is not None:
            self.after_cancel(self._geom_save_id)
        self._geom_save_id = self.after(500, self._save_geometry)

    def _save_geometry(self) -> None:
        self._geom_save_id = None
        try:
            save_window_geometry(self.geometry())
        except Exception:
            pass

    def _resize_status_bar(self, h: int) -> None:
        # Cap to 85% of window height so the top bar stays visible
        max_h = int(self.winfo_height() * 0.85)
        h = min(h, max_h)
        self.grid_rowconfigure(2, minsize=h, weight=0)
        self._status.configure(height=h)

    # --- plugin panel column drag-resize -------------------------------------

    def _on_col_drag_start(self, event: tk.Event) -> None:
        self._col_drag_x = event.x_root
        self._col_drag_w = self._plugin_panel_container.winfo_width()
        self._col_drag_target_w = self._col_drag_w
        self._col_show_ghost(event.x_root)

    def _on_col_drag_motion(self, event: tk.Event) -> None:
        if self._col_drag_x is None:
            return
        delta = self._col_drag_x - event.x_root  # drag left = wider plugin panel
        min_w = scaled(480)
        max_w = self._col_max_plugin_width()
        new_w = max(min_w, min(self._col_drag_w + delta, max_w))
        self._col_drag_target_w = new_w
        # Ghost line follows cursor directly
        self._col_move_ghost(event.x_root)

    def _on_col_drag_end(self, event: tk.Event) -> None:
        self._col_drag_x = None
        self._col_destroy_ghost()
        new_w = max(scaled(480), min(self._col_drag_target_w, self._col_max_plugin_width()))
        self.grid_columnconfigure(2, minsize=new_w, weight=4)
        self._plugin_panel_container.configure(width=new_w)

    def _col_max_plugin_width(self) -> int:
        toolbar = getattr(self._mod_panel, "_toolbar_bar", None)
        if toolbar is not None:
            mod_col_min = toolbar.winfo_reqwidth() + scaled(20)
        else:
            mod_col_min = scaled(720)
        return self.winfo_width() - mod_col_min - self._col_drag_handle.winfo_width()

    def _col_show_ghost(self, x_root: int) -> None:
        self._col_ghost = tk.Toplevel(self)
        self._col_ghost.overrideredirect(True)
        self._col_ghost.attributes("-alpha", 0.6)
        self._col_ghost.configure(bg=ACCENT)
        self._col_ghost.attributes("-topmost", True)
        self._col_move_ghost(x_root)

    def _col_move_ghost(self, x_root: int) -> None:
        if self._col_ghost is None:
            return
        win_y = self.winfo_rooty()
        win_h = self.winfo_height()
        self._col_ghost.geometry(f"2x{win_h}+{x_root}+{win_y}")

    def _col_destroy_ghost(self) -> None:
        if self._col_ghost is not None:
            self._col_ghost.destroy()
            self._col_ghost = None

    def _build_layout(self):
        # Root grid: 3 columns (mod side | separator | plugin side), 3 rows
        # Row 0: top bar (mod side only) + plugin panel top
        # Row 1: mod panel + plugin panel (both expand)
        # Row 2: status bar (full width)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)
        self.grid_columnconfigure(0, weight=5, minsize=scaled(380))
        self.grid_columnconfigure(1, weight=0)
        # Plugin panel: minsize in screen px so column fits scaled container
        self.grid_columnconfigure(2, weight=4, minsize=scaled(480))

        # Build status bar first so log_fn is available immediately
        self._status = StatusBar(self)
        self._status.grid(row=2, column=0, columnspan=3, sticky="nsew")
        self._status.set_resize_callback(self._resize_status_bar)

        log = self._status.log
        set_app_log(log, self.after)

        self._topbar = TopBar(
            self, log_fn=log,
            show_add_game_panel_fn=self.show_game_picker,
            show_reconfigure_panel_fn=self.show_reconfigure_panel,
            show_proton_panel_fn=self.show_proton_panel,
            show_wizard_panel_fn=self.show_wizard_panel,
            show_nexus_panel_fn=self.show_nexus_panel,
            show_custom_game_panel_fn=self.show_custom_game_panel,
            show_download_custom_handler_fn=self.show_download_custom_handler_panel,
            show_mewgenics_deploy_choice_fn=self.show_mewgenics_deploy_choice,
        )
        self._topbar.grid(row=0, column=0, sticky="ew", pady=(4, 0))

        # Vertical separator / drag handle spans rows 0+1
        self._col_drag_handle = tk.Canvas(
            self, bg=BORDER, width=4, highlightthickness=0, cursor="sb_h_double_arrow"
        )
        self._col_drag_handle.grid(row=0, column=1, rowspan=2, sticky="ns")
        self._col_drag_x: int | None = None
        self._col_drag_w: int | None = None
        self._col_ghost: tk.Toplevel | None = None
        self._col_drag_target_w: int = scaled(480)
        self._col_drag_handle.bind("<ButtonPress-1>", self._on_col_drag_start)
        self._col_drag_handle.bind("<B1-Motion>", self._on_col_drag_motion)
        self._col_drag_handle.bind("<ButtonRelease-1>", self._on_col_drag_end)

        main = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        main.grid(row=1, column=0, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(0, weight=1)
        self._main_frame = main
        self._game_picker_panel = None
        self._reconfigure_panel = None
        self._custom_game_panel = None

        self._mod_panel_container = ctk.CTkFrame(main, fg_color="transparent", corner_radius=0)
        self._mod_panel_container.grid(row=0, column=0, sticky="nsew")
        self._mod_panel_container.grid_rowconfigure(0, weight=1)
        self._mod_panel_container.grid_columnconfigure(0, weight=1)

        self._mod_panel = ModListPanel(
            self._mod_panel_container, log_fn=log,
            call_threadsafe_fn=self.call_threadsafe,
        )
        self._mod_panel.grid(row=0, column=0, sticky="nsew")
        self._mod_panel.set_status_bar(self._status)

        self._plugin_panel_container = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self._plugin_panel_container.grid(row=0, column=2, rowspan=2, sticky="nsew")
        self._plugin_panel_container.grid_rowconfigure(0, weight=1)
        self._plugin_panel_container.grid_columnconfigure(0, weight=1)
        self._plugin_panel_container.grid_propagate(False)

        # Design size 480: CTk scales it; scaled() would double-scale.
        # Set once at startup only — no Configure binding (avoids resize lag).
        def _set_plugin_width():
            try:
                self._plugin_panel_container.configure(width=480)
            except Exception:
                pass
        self.after(50, _set_plugin_width)

        self._plugin_panel = PluginPanel(
            self._plugin_panel_container, log_fn=log,
            get_filemap_path=lambda: (
                str(self._mod_panel._filemap_path)
                if self._mod_panel._filemap_path else None
            ),
        )
        self._plugin_panel.grid(row=0, column=0, sticky="nsew")
        self._proton_panel  = None
        self._wizard_panel  = None
        self._nexus_panel   = None
        self._backup_restore_panel = None
        self._exe_config_panel     = None
        self._exe_filter_panel     = None
        self._conflicts_panel      = None
        self._deploy_paths_panel   = None
        self._disable_plugins_panel = None
        self._optional_mods_panel = None
        self._vramr_panel = None
        self._ini_editor_panel = None

        def _on_filemap_rebuilt():
            # 1. Sync plugins.txt from the updated filemap
            filemap_path_str = (
                str(self._mod_panel._filemap_path)
                if self._mod_panel._filemap_path else None
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
                    game.get_vanilla_plugins_path()
                    if game and game.is_configured() and hasattr(game, 'get_vanilla_plugins_path')
                    else game.get_mod_data_path()
                    if game and game.is_configured() and hasattr(game, 'get_mod_data_path')
                    else None
                )
                self._plugin_panel._data_dir = data_dir
                # For games where vanilla plugins are included in plugins.txt
                # (e.g. Oblivion Remastered), don't use the live Data dir to
                # protect plugins from pruning — it may contain deployed mod
                # files from a previous profile. Vanilla plugins are managed
                # separately via _vanilla_plugins / _save_plugins.
                prune_data_dir = (
                    None if game and getattr(game, "plugins_include_vanilla", False)
                    else data_dir
                )
                removed = prune_plugins_from_filemap(
                    Path(filemap_path_str),
                    self._plugin_panel._plugins_path,
                    self._plugin_panel._plugin_extensions,
                    data_dir=prune_data_dir,
                    star_prefix=self._plugin_panel._plugins_star_prefix,
                )
                if removed:
                    self._status.log(f"plugins.txt: removed {removed} plugin(s).")
                # Read per-mod disabled plugin list and prune any already-synced disabled plugins
                profile_dir = (
                    self._mod_panel._modlist_path.parent
                    if self._mod_panel._modlist_path else None
                )
                disabled_map = read_disabled_plugins(profile_dir, None) if profile_dir else {}
                if disabled_map and self._plugin_panel._plugins_path is not None:
                    _sp = self._plugin_panel._plugins_star_prefix
                    existing = read_plugins(self._plugin_panel._plugins_path, star_prefix=_sp)
                    all_disabled_lower = {
                        n.lower() for names in disabled_map.values() for n in names
                    }
                    kept = [e for e in existing if e.name.lower() not in all_disabled_lower]
                    if len(kept) < len(existing):
                        write_plugins(self._plugin_panel._plugins_path, kept, star_prefix=_sp)
                        self._status.log(
                            f"plugins.txt: removed {len(existing) - len(kept)} disabled plugin(s)."
                        )
                added = sync_plugins_from_filemap(
                    Path(filemap_path_str),
                    self._plugin_panel._plugins_path,
                    self._plugin_panel._plugin_extensions,
                    disabled_plugins=disabled_map,
                    star_prefix=self._plugin_panel._plugins_star_prefix,
                )
                # Also sync from overwrite folder directly — filemap uses modindex.bin
                # which only updates overwrite on Refresh; tools (xEdit, Bodyslide, etc.)
                # may write plugins to overwrite without triggering a refresh.
                if game and hasattr(game, "get_effective_overwrite_path"):
                    overwrite_dir = game.get_effective_overwrite_path()
                    added_overwrite = sync_plugins_from_overwrite_dir(
                        overwrite_dir,
                        self._plugin_panel._plugins_path,
                        self._plugin_panel._plugin_extensions,
                        star_prefix=self._plugin_panel._plugins_star_prefix,
                    )
                    added += added_overwrite
                if added:
                    self._status.log(f"plugins.txt: added {added} new plugin(s).")
            # 2. Refresh Data tab and Ini Files tab
            self._plugin_panel._refresh_data_tab()
            self._plugin_panel._refresh_ini_files_tab()
            # 3. Reload Plugins tab from updated plugins.txt
            if (self._plugin_panel._plugins_path is not None
                    and self._plugin_panel._plugin_extensions):
                self._plugin_panel._refresh_plugins_tab()
            else:
                # No plugin extensions (e.g. BepInEx, Stardew Valley) — still
                # refresh framework banners so the status reflects any changes
                # made to the game root since the last reload.
                self._plugin_panel._refresh_framework_banners()
            # Auto deploy: if the game has auto_deploy enabled, trigger deploy
            # after every successful filemap rebuild — but not if deploy itself
            # triggered this rebuild (which would cause an infinite loop).
            _auto_game = _GAMES.get(self._topbar._game_var.get())
            if self._topbar._auto_deploy_in_progress:
                # This rebuild was triggered by auto-deploy; clear the guard and stop.
                self._topbar._auto_deploy_in_progress = False
            elif (_auto_game and _auto_game.is_configured()
                    and getattr(_auto_game, "auto_deploy", False)):
                self._topbar._auto_deploy_in_progress = True
                self._topbar._run_deploy(_auto_game, self._topbar._profile_var.get())

        # Wrap so the splash is dismissed after the very first filemap rebuild,
        # then restore the original callback for all subsequent calls.
        _orig_on_filemap_rebuilt = _on_filemap_rebuilt
        def _on_filemap_rebuilt_with_splash():
            self._mod_panel._on_filemap_rebuilt = _orig_on_filemap_rebuilt
            _orig_on_filemap_rebuilt()
            self._finish_splash()
        self._mod_panel._on_filemap_rebuilt = _on_filemap_rebuilt_with_splash

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
                elif entry.name == _OVERWRITE_NAME:
                    mod_name = _OVERWRITE_NAME
            self._plugin_panel.set_highlighted_plugins(mod_name if mod_name != _OVERWRITE_NAME else None)
            self._plugin_panel.show_mod_files(mod_name)
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
                # Set the active profile dir so get_effective_mod_staging_path works.
                initial_game.set_active_profile_dir(
                    initial_game.get_profile_root() / "profiles" / profile
                )
                self._plugin_panel._plugins_path = plugins_path
                self._plugin_panel._plugin_extensions = initial_game.plugin_extensions
                self._plugin_panel._vanilla_plugins = _vanilla_plugins_for_game(initial_game)
                _staging = initial_game.get_effective_mod_staging_path()
                self._plugin_panel._staging_root = _staging
                data_path = (
                    initial_game.get_vanilla_plugins_path()
                    if hasattr(initial_game, 'get_vanilla_plugins_path')
                    else initial_game.get_mod_data_path()
                    if hasattr(initial_game, 'get_mod_data_path')
                    else None
                )
                self._plugin_panel._data_dir = data_path
                self._plugin_panel._game = initial_game
                # Mod Files tab paths
                _profile_dir = initial_game.get_profile_root() / "profiles" / profile
                self._plugin_panel._mod_files_index_path = _staging.parent / "modindex.bin"
                self._plugin_panel._mod_files_profile_dir = _profile_dir
                from Utils.profile_state import read_excluded_mod_files as _read_exc
                _exc_raw = _read_exc(_profile_dir, None)
                self._plugin_panel._mod_files_excluded = {k: set(v) for k, v in _exc_raw.items()}
                self._plugin_panel._mod_files_on_change = self._mod_panel._rebuild_filemap
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

    # -- Onboarding overlay -------------------------------------------------

    def _show_onboarding(self):
        from gui.onboarding_panel import OnboardingPanel

        def _key_changed():
            self._init_nexus_api()
            self._topbar._log("Nexus API key updated.")
            self.after(200, self._topbar._check_collections_visibility)

        def _add_game():
            # Trigger the same flow as the "+" button in the top bar
            self._topbar._on_add_game()

        self._onboarding_panel = OnboardingPanel(
            self._mod_panel_container,
            on_nexus_key_changed=_key_changed,
            on_add_game=_add_game,
            on_done=self._hide_onboarding,
            already_logged_in=self._nexus_api is not None,
        )
        self._onboarding_panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._onboarding_panel.lift()

    def _hide_onboarding(self):
        panel = getattr(self, "_onboarding_panel", None)
        if panel is not None:
            self._onboarding_panel = None
            try:
                panel.place_forget()
                panel.destroy()
            except Exception:
                pass

    # -- Game picker panel (inline overlay) --------------------------------

    def show_game_picker(self, game_names: list, on_game_selected):
        """Show the game-picker card grid, overlaying the main content area."""
        self.hide_game_picker()

        from gui.dialogs import GamePickerPanel

        def _on_selected(name: str, already_configured: bool):
            self.hide_game_picker()
            on_game_selected(name, already_configured)

        def _on_cancel():
            self.hide_game_picker()

        self._game_picker_panel = GamePickerPanel(
            self._mod_panel_container,
            game_names,
            games=_GAMES,
            on_game_selected=_on_selected,
            on_cancel=_on_cancel,
            show_custom_game_panel_fn=self.show_custom_game_panel,
            show_download_custom_handler_fn=self.show_download_custom_handler_panel,
        )
        self._game_picker_panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._game_picker_panel.lift()

    def hide_game_picker(self):
        """Remove the game-picker panel and restore the normal content area."""
        panel = self._game_picker_panel
        if panel is not None:
            self._game_picker_panel = None
            try:
                panel.place_forget()
                panel.destroy()
            except Exception:
                pass

    # -- Reconfigure game panel (inline overlay) ---------------------------

    def show_reconfigure_panel(self, game, on_done):
        """Show the reconfigure-game panel, overlaying the main content area."""
        self.hide_game_picker()
        self.hide_reconfigure_panel()

        from gui.add_game_dialog import ReconfigureGamePanel

        def _on_panel_done(panel):
            self.hide_reconfigure_panel()
            on_done(panel)

        self._reconfigure_panel = ReconfigureGamePanel(
            self._mod_panel_container, game, on_done=_on_panel_done
        )
        self._reconfigure_panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._reconfigure_panel.lift()

    def hide_reconfigure_panel(self):
        """Remove the reconfigure panel and restore the normal content area."""
        panel = getattr(self, "_reconfigure_panel", None)
        if panel is not None:
            self._reconfigure_panel = None
            try:
                panel.place_forget()
                panel.destroy()
            except Exception:
                pass

    # -- Custom game definition panel (inline overlay) ---------------------

    def show_custom_game_panel(self, existing, on_done):
        """Show the custom game definition panel, overlaying the main content area."""
        self.hide_custom_game_panel()

        from gui.custom_game_dialog import CustomGamePanel

        def _on_panel_done(panel):
            self.hide_custom_game_panel()
            on_done(panel)

        self._custom_game_panel = CustomGamePanel(
            self._mod_panel_container, existing=existing, on_done=_on_panel_done
        )
        self._custom_game_panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._custom_game_panel.lift()

    def hide_custom_game_panel(self):
        """Remove the custom game panel."""
        panel = getattr(self, "_custom_game_panel", None)
        if panel is not None:
            self._custom_game_panel = None
            try:
                panel.place_forget()
                panel.destroy()
            except Exception:
                pass

    # -- Mewgenics deploy overlays (mod-list side) ---------------------------

    def show_mewgenics_deploy_choice(self, on_choice):
        """Show the Mewgenics deploy-method choice panel over the mod list."""
        self.hide_mewgenics_deploy_choice()
        from gui.dialogs import MewgenicsDeployChoicePanel

        def _choice(result):
            self.hide_mewgenics_deploy_choice()
            on_choice(result)

        self._mewgenics_deploy_choice_panel = MewgenicsDeployChoicePanel(
            self._mod_panel_container, on_choice=_choice
        )
        self._mewgenics_deploy_choice_panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._mewgenics_deploy_choice_panel.lift()

    def hide_mewgenics_deploy_choice(self):
        panel = getattr(self, "_mewgenics_deploy_choice_panel", None)
        if panel is not None:
            self._mewgenics_deploy_choice_panel = None
            try:
                panel.place_forget()
                panel.destroy()
            except Exception:
                pass

    def show_mewgenics_launch_command(self, launch_string, modpaths_file=None):
        """Show the Mewgenics Steam launch command panel over the mod list."""
        self.hide_mewgenics_launch_command()
        from gui.dialogs import MewgenicsLaunchCommandPanel

        self._mewgenics_launch_command_panel = MewgenicsLaunchCommandPanel(
            self._mod_panel_container,
            launch_string=launch_string,
            modpaths_file=modpaths_file,
            on_close=self.hide_mewgenics_launch_command,
        )
        self._mewgenics_launch_command_panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._mewgenics_launch_command_panel.lift()

    def hide_mewgenics_launch_command(self):
        panel = getattr(self, "_mewgenics_launch_command_panel", None)
        if panel is not None:
            self._mewgenics_launch_command_panel = None
            try:
                panel.place_forget()
                panel.destroy()
            except Exception:
                pass

    # -- Plugin-side overlay helpers ----------------------------------------

    def _show_plugin_overlay(self, attr: str, factory):
        """Generic: hide any existing plugin overlay, build new one, place it."""
        self._hide_plugin_overlay(attr)
        panel = factory()
        setattr(self, attr, panel)
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        panel.lift()

    def _hide_plugin_overlay(self, attr: str):
        panel = getattr(self, attr, None)
        if panel is not None:
            setattr(self, attr, None)
            try:
                panel.place_forget()
                panel.destroy()
            except Exception:
                pass

    # -- Proton Tools panel --------------------------------------------------

    def show_proton_panel(self, game, log_fn):
        from gui.dialogs import ProtonToolsPanel
        self._show_plugin_overlay(
            "_proton_panel",
            lambda: ProtonToolsPanel(
                self._plugin_panel_container, game, log_fn,
                on_done=lambda p: self._hide_plugin_overlay("_proton_panel"),
            ),
        )

    def hide_proton_panel(self):
        self._hide_plugin_overlay("_proton_panel")

    # -- Wizard panel --------------------------------------------------------

    def show_wizard_panel(self, game, log_fn):
        from gui.wizard_dialog import WizardPanel
        self._show_plugin_overlay(
            "_wizard_panel",
            lambda: WizardPanel(
                self._plugin_panel_container, game, log_fn,
                on_done=lambda p: self._hide_plugin_overlay("_wizard_panel"),
                on_open_tool=self._show_wizard_tool,
            ),
        )

    def _show_wizard_tool(self, cls, game, log_fn, extra: dict):
        """Open an individual wizard tool as a plugin-panel overlay."""
        self._hide_plugin_overlay("_wizard_tool")
        panel = cls(
            self._plugin_panel_container, game, log_fn,
            on_close=lambda: self._hide_plugin_overlay("_wizard_tool"),
            **extra,
        )
        setattr(self, "_wizard_tool", panel)
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        panel.lift()

    def hide_wizard_panel(self):
        self._hide_plugin_overlay("_wizard_panel")

    # -- Nexus Settings panel ------------------------------------------------

    def get_nexus_settings_opener(self):
        """Return a callable that opens the Nexus API settings overlay."""
        def _key_changed():
            self._init_nexus_api()
            self._topbar._log("Nexus API key updated.")
            self._topbar.after(200, self._topbar._check_collections_visibility)
        return lambda: self.show_nexus_panel(_key_changed, self._topbar._log)

    def show_nexus_panel(self, on_key_changed, log_fn):
        from gui.nexus_settings_dialog import NexusSettingsPanel
        _game = _GAMES.get(self._topbar._game_var.get())
        self._show_plugin_overlay(
            "_nexus_panel",
            lambda: NexusSettingsPanel(
                self._plugin_panel_container,
                on_key_changed=on_key_changed,
                log_fn=log_fn,
                nexus_api_getter=lambda: self._nexus_api,
                game_domain_getter=lambda: (getattr(_game, "nexus_game_domain", None) or None),
                on_done=lambda p: self._hide_plugin_overlay("_nexus_panel"),
            ),
        )

    def hide_nexus_panel(self):
        self._hide_plugin_overlay("_nexus_panel")

    # -- Backup Restore panel ------------------------------------------------

    def show_backup_restore_panel(self, profile_dir, profile_name, on_restored):
        from gui.backup_restore_dialog import BackupRestorePanel
        self._show_plugin_overlay(
            "_backup_restore_panel",
            lambda: BackupRestorePanel(
                self._plugin_panel_container,
                profile_dir,
                profile_name,
                on_restored=on_restored,
                on_done=lambda p: self._hide_plugin_overlay("_backup_restore_panel"),
            ),
        )

    def hide_backup_restore_panel(self):
        self._hide_plugin_overlay("_backup_restore_panel")

    # -- EXE Config panel ----------------------------------------------------

    def show_exe_config_panel(self, exe_path, game, saved_args, custom_exes,
                              launch_mode, deploy_before_launch, is_hidden, on_done,
                              proton_override=None, is_data_folder_exe=False,
                              is_apps_exe=False, log_fn=None):
        from gui.dialogs import ExeConfigPanel
        def _factory():
            def _done(panel):
                self._hide_plugin_overlay("_exe_config_panel")
                on_done(panel)
            return ExeConfigPanel(
                self._plugin_panel_container,
                exe_path=exe_path, game=game, saved_args=saved_args,
                custom_exes=custom_exes, launch_mode=launch_mode,
                deploy_before_launch=deploy_before_launch, is_hidden=is_hidden,
                on_done=_done, proton_override=proton_override,
                is_data_folder_exe=is_data_folder_exe, is_apps_exe=is_apps_exe,
                log_fn=log_fn,
            )
        self._show_plugin_overlay("_exe_config_panel", _factory)

    def hide_exe_config_panel(self):
        self._hide_plugin_overlay("_exe_config_panel")

    # -- EXE Filter panel ----------------------------------------------------

    def show_exe_filter_panel(self, load_fn, save_fn, refresh_fn):
        from gui.dialogs import ExeFilterPanel
        self._show_plugin_overlay(
            "_exe_filter_panel",
            lambda: ExeFilterPanel(
                self._plugin_panel_container,
                load_fn=load_fn, save_fn=save_fn, refresh_fn=refresh_fn,
                on_done=lambda p: self._hide_plugin_overlay("_exe_filter_panel"),
            ),
        )

    def hide_exe_filter_panel(self):
        self._hide_plugin_overlay("_exe_filter_panel")

    # -- Conflicts panel (overlays mod list) --------------------------------

    def show_conflicts_panel(self, mod_name, files_win, files_lose,
                             files_no_conflict=None):
        from gui.dialogs import OverwritesPanel
        self._show_plugin_overlay(
            "_conflicts_panel",
            lambda: OverwritesPanel(
                self._main_frame,
                mod_name=mod_name, files_win=files_win, files_lose=files_lose,
                files_no_conflict=files_no_conflict,
                on_done=lambda p: self._hide_plugin_overlay("_conflicts_panel"),
            ),
        )

    def hide_conflicts_panel(self):
        self._hide_plugin_overlay("_conflicts_panel")

    # -- Deploy paths panel (overlays mod list) -----------------------------

    def show_deploy_paths_panel(self, mod_name, mod_folder,
                                current_prefixes, use_path_format, on_save):
        from gui.dialogs import DeploymentPathsPanel
        def _factory():
            def _done(panel):
                self._hide_plugin_overlay("_deploy_paths_panel")
            return DeploymentPathsPanel(
                self._mod_panel_container,
                mod_name=mod_name, mod_folder=mod_folder,
                current_prefixes=current_prefixes, use_path_format=use_path_format,
                on_save=on_save, on_done=_done,
            )
        self._show_plugin_overlay("_deploy_paths_panel", _factory)

    def hide_deploy_paths_panel(self):
        self._hide_plugin_overlay("_deploy_paths_panel")

    # -- Ini file editor panel (overlays mod list) --------------------------

    def show_ini_editor_panel(self, file_path: str, rel_path: str, mod_name: str):
        """Show the ini/json file editor overlay over the mod list."""
        from gui.dialogs import IniFileEditorPanel
        self._show_plugin_overlay(
            "_ini_editor_panel",
            lambda: IniFileEditorPanel(
                self._mod_panel_container,
                file_path=file_path,
                rel_path=rel_path,
                mod_name=mod_name,
                on_done=lambda p: self._hide_plugin_overlay("_ini_editor_panel"),
            ),
        )

    def hide_ini_editor_panel(self):
        self._hide_plugin_overlay("_ini_editor_panel")

    # -- Separator settings panel (overlays plugin panel) -------------------

    def show_sep_settings_panel(self, sep_name, current_path, on_save, current_raw=False):
        from gui.dialogs import SepSettingsPanel
        def _factory():
            def _done(panel):
                self._hide_plugin_overlay("_sep_settings_panel")
            return SepSettingsPanel(
                self._plugin_panel_container,
                sep_name=sep_name, current_path=current_path,
                current_raw=current_raw, on_save=on_save, on_done=_done,
            )
        self._show_plugin_overlay("_sep_settings_panel", _factory)

    def hide_sep_settings_panel(self):
        self._hide_plugin_overlay("_sep_settings_panel")

    # -- Disable plugins panel (overlays plugin panel) ----------------------

    def show_disable_plugins_panel(self, mod_name, plugin_names, disabled, on_done):
        from gui.dialogs import DisablePluginsPanel
        def _factory():
            def _done(panel):
                self._hide_plugin_overlay("_disable_plugins_panel")
                on_done(panel)
            return DisablePluginsPanel(
                self._plugin_panel_container,
                mod_name=mod_name, plugin_names=plugin_names, disabled=disabled,
                on_done=_done,
            )
        self._show_plugin_overlay("_disable_plugins_panel", _factory)

    def hide_disable_plugins_panel(self):
        self._hide_plugin_overlay("_disable_plugins_panel")

    # -- Optional mods panel (overlays plugin panel) --------------------------

    def show_optional_mods_panel(self, optional_mods: list, on_done):
        """Show OptionalModsPanel as overlay on plugin panel. on_done(panel) receives the
        panel; panel.result is None (cancelled) or set of file_ids to skip."""
        from gui.collections_dialog import OptionalModsPanel
        def _factory():
            def _done(panel):
                self._hide_plugin_overlay("_optional_mods_panel")
                on_done(panel)
            return OptionalModsPanel(
                self._plugin_panel_container,
                optional_mods=optional_mods,
                on_done=_done,
            )
        self._show_plugin_overlay("_optional_mods_panel", _factory)

    def hide_optional_mods_panel(self):
        self._hide_plugin_overlay("_optional_mods_panel")

    # -- VRAMr preset panel (overlays plugin panel) ------------------------

    def show_vramr_panel(self, bat_dir, game_data_dir, output_dir, log_fn):
        """Show VRAMr preset picker as overlay on the plugin panel."""
        from gui.dialogs import VRAMrPresetPanel
        self._show_plugin_overlay(
            "_vramr_panel",
            lambda: VRAMrPresetPanel(
                self._plugin_panel_container,
                bat_dir=bat_dir,
                game_data_dir=game_data_dir,
                output_dir=output_dir,
                log_fn=log_fn,
                on_done=lambda p: self._hide_plugin_overlay("_vramr_panel"),
            ),
        )

    def hide_vramr_panel(self):
        self._hide_plugin_overlay("_vramr_panel")

    # -- Missing requirements panel (overlays plugin panel) -----------------

    def show_missing_reqs_panel(self, mod_name, domain, mod_id, missing_ids,
                                api, install_from_browse,
                                ignored_set, save_ignored_fn, redraw_fn):
        from gui.dialogs import MissingReqsPanel
        def _factory():
            def _done(panel):
                self._hide_plugin_overlay("_missing_reqs_panel")
                redraw_fn()
            return MissingReqsPanel(
                self._plugin_panel_container,
                mod_name=mod_name, domain=domain, mod_id=mod_id,
                missing_ids=missing_ids, api=api,
                install_from_browse=install_from_browse,
                ignored_set=ignored_set, save_ignored_fn=save_ignored_fn,
                on_done=_done,
            )
        self._show_plugin_overlay("_missing_reqs_panel", _factory)

    def hide_missing_reqs_panel(self):
        self._hide_plugin_overlay("_missing_reqs_panel")

    # -- Download Custom Handler panel (overlays plugin panel) --------------

    def show_download_custom_handler_panel(self):
        """Show overlay listing custom handlers from GitHub for download."""
        from gui.dialogs import DownloadCustomHandlerPanel

        def _on_done(p):
            self._hide_plugin_overlay("_download_custom_handler_panel")

        def _on_downloaded():
            # Refresh game picker immediately when a handler is downloaded
            panel = getattr(self, "_game_picker_panel", None)
            if panel is not None:
                panel.refresh()

        self._show_plugin_overlay(
            "_download_custom_handler_panel",
            lambda: DownloadCustomHandlerPanel(
                self._plugin_panel_container,
                on_done=_on_done,
                on_downloaded=_on_downloaded,
                log_fn=self._status.log,
            ),
        )

    def hide_download_custom_handler_panel(self):
        self._hide_plugin_overlay("_download_custom_handler_panel")

    # -- Settings panel ------------------------------------------------------

    def show_settings_panel(self):
        from gui.status_bar import SettingsPanel
        self._show_plugin_overlay(
            "_settings_panel",
            lambda: SettingsPanel(
                self._plugin_panel_container,
                on_done=lambda p: self._hide_plugin_overlay("_settings_panel"),
            ),
        )

    def hide_settings_panel(self):
        self._hide_plugin_overlay("_settings_panel")

    def _startup_log(self):
        from Utils.ui_config import get_ui_scale, get_screen_info
        w, h, detected = get_screen_info()
        self._status.log(
            f"Display: {w}x{h}, HiDPI detected={detected}, scale={get_ui_scale()}"
        )
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
    from Utils.portal_filechooser import set_main_thread_dispatcher
    set_main_thread_dispatcher(app.call_threadsafe)
    app._start_nxm_ipc()          # listen for NXM links from future instances
    app.protocol("WM_DELETE_WINDOW", lambda: (NxmIPC.shutdown(), app.destroy()))
    app.mainloop()
