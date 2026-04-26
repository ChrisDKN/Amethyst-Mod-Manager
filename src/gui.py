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

# Load UI scale before the Xft.dpi override so scale detection reads the real
# system DPI.  The override only affects font rasterisation in the main window.
from Utils.ui_config import load_ui_scale, get_ui_scale, load_window_geometry, save_window_geometry, load_allow_prerelease
_UI_SCALE = load_ui_scale()

# Override Xft.dpi to 96 before the main Tk window is created so font
# rasterisation ignores the OS global scaling setting (e.g. 200% sets
# Xft.dpi=192, doubling fonts).
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

ctk.set_widget_scaling(_UI_SCALE)
ctk.set_window_scaling(_UI_SCALE)

from gui.wheel_compat import patch_ctk_scrollable_frame
patch_ctk_scrollable_frame()

from gui.theme import ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_HOVER, BORDER, FONT_BOLD, FONT_NORMAL, FS9, TEXT_DIM, TEXT_MAIN, init_fonts, scaled, scaled_layout_minsize, TEXT_ON_ACCENT
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
from gui.shortcuts import register_shortcuts
from gui.install_mod import install_mod_from_archive, fomod_dialog_active
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
    sync_plugins_from_filemap_combined,
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
from Utils.config_paths import get_download_cache_dir, get_download_cache_dir_for_game

from Utils.ui_config import get_appearance_mode as _get_appearance_mode
from gui.themes import get_ctk_appearance as _get_ctk_appearance
ctk.set_appearance_mode(_get_ctk_appearance(_get_appearance_mode()))
ctk.set_default_color_theme("dark-blue")

def _run_installer(allow_prerelease: bool = False):
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
    installer_args = " --prerelease" if allow_prerelease else ""
    cmd = (
        f"sleep 2 && "
        f"SCRIPT=$(mktemp /tmp/amethyst-installer-XXXXXX.sh) && "
        f"curl -sSL {_APP_UPDATE_INSTALLER_URL} -o \"$SCRIPT\" && "
        f"chmod +x \"$SCRIPT\" && "
        f"bash \"$SCRIPT\"{installer_args} && "
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

        # --- Splash screen (transparent GTK subprocess) --------------------
        self._splash_proc: subprocess.Popen | None = None
        _logo_path = Path(__file__).parent / "icons" / "Logo.png"
        _splash_script = Path(__file__).parent / "splash_gtk.py"
        if _logo_path.is_file() and _splash_script.is_file():
            # Read PNG dimensions from header to compute centred position
            def _png_size(_p):
                try:
                    import struct
                    with open(_p, 'rb') as _f:
                        _f.read(16)  # skip sig (8) + IHDR chunk length (4) + 'IHDR' (4)
                        return struct.unpack('>II', _f.read(8))
                except Exception:
                    return 0, 0

            _iw, _ih = _png_size(_logo_path)
            # Centre on the monitor containing the mouse pointer.
            # On multi-monitor setups winfo_screenwidth returns the combined virtual
            # desktop width; we use the pointer position + monitor list to find the
            # right monitor.  Several query paths are tried in order.
            def _monitor_rect_for_pointer():
                """Return (mon_x, mon_y, mon_w, mon_h) for the monitor under the pointer."""
                try:
                    import re as _re, subprocess as _sp
                    _px = self.winfo_pointerx()
                    _py = self.winfo_pointery()

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
            _sx = _mrx + (_mrw - _iw) // 2 if _iw else _mrx + _mrw // 2
            _sy = _mry + (_mrh - _ih) // 2 if _ih else _mry + _mrh // 2
            try:
                _splash_env = os.environ.copy()
                # Force X11 backend so Gtk.WindowType.POPUP sets override_redirect
                # at the X11 level — under the Wayland backend this flag is ignored
                # and KWin adds decorations regardless.
                _splash_env['GDK_BACKEND'] = 'x11'
                self._splash_proc = subprocess.Popen(
                    [sys.executable, str(_splash_script), str(_logo_path), str(_sx), str(_sy)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=_splash_env,
                )
            except Exception:
                pass
        # -------------------------------------------------------------------

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
        # Cap to screen dimensions so the window is always resizable on smaller monitors
        _s = get_ui_scale()
        _min_w = min(int(1280 * _s), self.winfo_screenwidth() - 50)
        _min_h = min(int(720 * _s), self.winfo_screenheight() - 100)
        self.minsize(_min_w, _min_h)
        self.bind("<Configure>", self._on_window_configure)
        self._geom_save_id: str | None = None
        self._minsize_sync_id: str | None = None
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
        # Wipe the GitHub cache when the app version changes so users on a
        # fresh build pick up updated handlers/plugins immediately instead of
        # waiting for the 1h throttle window to elapse.
        try:
            from Utils.gh_cache import clear_if_version_changed
            clear_if_version_changed(__version__)
        except Exception:
            pass
        # Silently sync all custom handlers and plugins from GitHub on startup
        self.after(3000, self._sync_custom_handlers)
        self.after(3500, self._sync_plugins)
        icon_path = Path(__file__).parent / "icons" / "title-bar.png"
        if icon_path.is_file():
            icon_img = tk.PhotoImage(file=str(icon_path))
            self.iconphoto(False, icon_img)

        # Global click handler: defocus text entry widgets when clicking
        # outside of them, so search bars etc. don't stay focused.
        # Runs on bindtag "all" (after widget/class/toplevel bindings). Widgets
        # that return "break" from their own <Button-1> (e.g. canvas tag_bind
        # checkboxes in the modlist) would suppress us — so we hook both press
        # and release. Release rarely returns "break" in this codebase, so it
        # catches the cases press misses.
        self.bind_all("<ButtonPress-1>", self._defocus_on_outside_click, add="+")
        self.bind_all("<ButtonRelease-1>", self._defocus_on_outside_click, add="+")

        # Global keyboard shortcuts (F2 rename, Ctrl+D deploy, Ctrl+R restore,
        # Up/Down reorder selection).
        register_shortcuts(self)

    # -- Focus management ---------------------------------------------------
    _TEXT_INPUT_CLASSES = (
        "Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox",
    )

    def _defocus_on_outside_click(self, event: tk.Event) -> None:
        """Move focus away from Entry/Text widgets when the user clicks
        somewhere that isn't itself a text input widget.

        Uses <ButtonPress-1> on the root via bindtag "all" — runs AFTER
        widget/class bindings, so widgets that return "break" will prevent
        this from firing on them. We work around that by also re-binding
        on each new Toplevel if needed. We force focus to the root window
        (which always succeeds) so the Entry loses focus unconditionally.
        """
        w = event.widget
        try:
            cls = w.winfo_class() if hasattr(w, "winfo_class") else ""
        except Exception:
            cls = ""
        # If the click landed on a text-entry-like widget, leave focus alone.
        if cls in self._TEXT_INPUT_CLASSES:
            return
        # Only act if a text input currently has focus (cheap check).
        try:
            focused = self.focus_get()
        except Exception:
            focused = None
        if focused is None:
            return
        try:
            fcls = focused.winfo_class()
        except Exception:
            return
        if fcls not in self._TEXT_INPUT_CLASSES:
            return
        # Force focus onto the toplevel containing the click — this always
        # succeeds and reliably removes focus from the Entry on Wayland/X11.
        try:
            top = w.winfo_toplevel()
            top.focus_set()
        except Exception:
            try:
                self.focus_set()
            except Exception:
                pass

    # -- Splash screen ------------------------------------------------------

    def _finish_splash(self):
        if self._splash_proc is not None:
            try:
                self._splash_proc.terminate()
            except Exception:
                pass
            self._splash_proc = None
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

    def _show_update_overlay(self, current: str, latest: str, *, mode: str = "appimage", is_prerelease: bool = False, is_downgrade: bool = False):
        """Show an update-available banner overlaid on the mod list panel.

        *mode* is one of ``"appimage"``, ``"flatpak"``, or ``"aur"``.
        *is_downgrade* swaps the copy when the offered version is older than
        the running one (e.g. user opted out of the pre-release channel while
        running a beta).
        """
        container = self._mod_panel_container

        # Dismiss any prior overlay so re-checking (e.g. after toggling the
        # pre-release setting) doesn't stack multiple banners.
        prior = getattr(self, "_update_overlay", None)
        if prior is not None:
            try:
                prior.place_forget()
                prior.destroy()
            except Exception:
                pass

        overlay = ctk.CTkFrame(container, fg_color=BG_DEEP, corner_radius=8)
        overlay.place(relx=0.5, rely=0.5, anchor="center")
        self._update_overlay = overlay

        def _close():
            overlay.place_forget()
            overlay.destroy()
            if getattr(self, "_update_overlay", None) is overlay:
                self._update_overlay = None

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
        elif is_downgrade:
            offered_label = "Pre-release" if is_prerelease else "Stable"
            msg = (
                f"You're running a pre-release. Switch to the latest {offered_label.lower()} build?\n\n"
                f"Current:     {current}\n"
                f"{offered_label}: {latest}\n\n"
                f"This will downgrade your installation."
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
                _run_installer(allow_prerelease=is_prerelease)
                _close()
                try:
                    from Nexus.nxm_handler import NxmIPC
                    NxmIPC.shutdown()
                except Exception:
                    pass
                self.destroy()

            ctk.CTkButton(
                btn_frame, text="Switch to stable" if is_downgrade else "Update via installer",
                width=160, height=32, font=FONT_BOLD,
                fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
                command=_on_update,
            ).pack(side="left", padx=(0, 8))

        if mode == "aur":
            ctk.CTkButton(
                btn_frame, text="Open AUR page",
                width=140, height=32, font=FONT_NORMAL,
                fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
                command=lambda: (open_url(_AUR_PACKAGE_URL), _close()),
            ).pack(side="left", padx=(0, 8))
        else:
            ctk.CTkButton(
                btn_frame, text="Open releases page",
                width=140, height=32, font=FONT_NORMAL,
                fg_color=ACCENT if mode == "flatpak" else BG_HEADER,
                hover_color=ACCENT_HOV if mode == "flatpak" else BG_HOVER,
                text_color=TEXT_ON_ACCENT if mode == "flatpak" else TEXT_MAIN,
                command=lambda: (open_url(_APP_UPDATE_RELEASES_URL), _close()),
            ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Later",
            width=80, height=32, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=_close,
        ).pack(side="left")

    def _check_for_app_update(self, force_downgrade_prompt: bool = False, force_fresh: bool = False):
        """Run in background: fetch latest version and prompt if newer.

        AppImage installs compare against GitHub releases and offer the
        auto-installer.  System installs (e.g. AUR) compare against the AUR
        package version and show instructions to update via the AUR helper.

        When *force_downgrade_prompt* is True (e.g. the user just toggled the
        pre-release channel off while running a beta), the AppImage/Flatpak
        branches will surface the latest stable even if it's older than the
        currently-running version, with downgrade-aware copy.

        When *force_fresh* is True (or implied by force_downgrade_prompt), the
        ETag-cache throttle is bypassed so a manual user action triggers an
        immediate re-check instead of waiting out the 1-hour throttle window.
        """

        force_fresh = bool(force_fresh or force_downgrade_prompt)

        def _do_check():
            allow_pre = load_allow_prerelease()
            if is_appimage():
                result = _fetch_latest_version(allow_prerelease=allow_pre, force=force_fresh)
                if result is None:
                    return
                latest, is_pre = result
                newer = _is_newer_version(__version__, latest)
                if newer or (force_downgrade_prompt and latest != __version__):
                    is_downgrade = not newer
                    self.call_threadsafe(
                        lambda: self._show_update_overlay(
                            __version__, latest, mode="appimage",
                            is_prerelease=is_pre, is_downgrade=is_downgrade,
                        )
                    )
            elif is_flatpak():
                result = _fetch_latest_version(allow_prerelease=allow_pre, force=force_fresh)
                if result is None:
                    return
                latest, is_pre = result
                newer = _is_newer_version(__version__, latest)
                if newer or (force_downgrade_prompt and latest != __version__):
                    is_downgrade = not newer
                    self.call_threadsafe(
                        lambda: self._show_update_overlay(
                            __version__, latest, mode="flatpak",
                            is_prerelease=is_pre, is_downgrade=is_downgrade,
                        )
                    )
            else:
                aur_ver = _fetch_aur_version(force=force_fresh)
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

        # Enderal can install Skyrim mods; Enderal SE can install Skyrim SE mods.
        # If the user is already on Enderal(SE) and the link is for the
        # Skyrim(SE) counterpart, stay on Enderal instead of switching away.
        _ENDERAL_ACCEPTS = {
            "enderal": "skyrim",
            "enderalspecialedition": "skyrimspecialedition",
        }
        current_name = self._topbar._game_var.get()
        current_game = _GAMES.get(current_name)
        current_domain = getattr(current_game, "nexus_game_domain", "") if current_game else ""
        stay_on_current = (
            current_game is not None
            and current_game.is_configured()
            and _ENDERAL_ACCEPTS.get(current_domain) == link.game_domain
        )

        # Try to auto-select the matching game
        matched_game = None
        if stay_on_current:
            matched_game = (current_name, current_game)
        else:
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

            _dest_game_name = matched_game[0] if matched_game else ""
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
                dest_dir=get_download_cache_dir_for_game(_dest_game_name),
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

        # Enderal can use Skyrim collections; keep the user on Enderal(SE) if
        # they're already there and the collection is for the Skyrim counterpart.
        _ENDERAL_ACCEPTS = {
            "enderal": "skyrim",
            "enderalspecialedition": "skyrimspecialedition",
        }
        current_name = self._topbar._game_var.get()
        current_game = _GAMES.get(current_name)
        current_domain = getattr(current_game, "nexus_game_domain", "") if current_game else ""
        stay_on_current = (
            current_game is not None
            and current_game.is_configured()
            and _ENDERAL_ACCEPTS.get(current_domain) == coll_link.game_domain
        )

        # Find the game matching the collection's domain
        matched_game = None
        if stay_on_current:
            matched_game = (current_name, current_game)
        else:
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

        if getattr(matched_game[1], "collections_disabled", False):
            log(f"Nexus: Collections are not supported for '{matched_game[0]}' — link ignored.")
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

        If a modal dialog currently holds the Tk grab or a FOMOD wizard overlay
        is open, the install is deferred until the dialog is dismissed to avoid
        a second FomodDialog being placed on top of the first.
        """
        if self.grab_current() is not None or fomod_dialog_active.is_set():
            # A dialog is open — queue and poll until it's gone.
            self._nxm_install_queue.append((result, matched_game, mod_info, file_info))
            self._poll_nxm_install_queue()
            return
        self._nxm_install_impl(result, matched_game, mod_info=mod_info, file_info=file_info)

    def _poll_nxm_install_queue(self):
        """Retry queued NXM installs once no modal dialog holds the grab."""
        if not self._nxm_install_queue:
            return
        if self.grab_current() is not None or fomod_dialog_active.is_set():
            # Still blocked — check again shortly.
            self.after(300, self._poll_nxm_install_queue)
            return
        # Modal is gone — run installs in order, stopping if a new modal opens.
        while self._nxm_install_queue:
            result, matched_game, mod_info, file_info = self._nxm_install_queue.pop(0)
            self._nxm_install_impl(result, matched_game, mod_info=mod_info, file_info=file_info)
            if self.grab_current() is not None or fomod_dialog_active.is_set():
                # This install opened a dialog; resume after it closes.
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
        _installed_is_fomod = False

        def _on_installed(is_fomod: bool = False):
            nonlocal _installed, _installed_is_fomod
            _installed = True
            _installed_is_fomod = is_fomod

        # Build metadata now from the NXM link data so install_mod_from_archive
        # can write it directly, skipping the async detection thread entirely.
        prebuilt_meta = build_meta_from_download(
            game_domain=result.game_domain,
            mod_id=result.mod_id,
            file_id=result.file_id,
            archive_name=result.file_name,
            mod_info=mod_info,
            file_info=file_info,
        )

        installed_name = install_mod_from_archive(str(_archive_path), self, log, game, mod_panel,
                                                  on_installed=_on_installed,
                                                  prebuilt_meta=prebuilt_meta)

        if installed_name:
            log(f"Nexus: Saved metadata (mod {prebuilt_meta.mod_id}, file {prebuilt_meta.file_id})")

        if _installed and _archive_path:
            from Utils.ui_config import (
                load_clear_archive_after_install,
                load_keep_fomod_archives,
            )
            _should_clear = load_clear_archive_after_install()
            if _should_clear and _installed_is_fomod and load_keep_fomod_archives():
                _should_clear = False
                log(f"Nexus: Keeping FOMOD archive {_archive_path.name}")
            if _should_clear:
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
        # Re-cap col 0 minsize against the new window width (debounced via
        # after_idle) so the plugin panel isn't clipped when the WM forces
        # the window below col_sum.  Debouncing prevents feedback loops
        # where configure → minsize change → configure → ...
        if hasattr(self, "_mod_panel") and getattr(self, "_minsize_sync_id", None) is None:
            self._minsize_sync_id = self.after_idle(self._deferred_minsize_sync)
        # Debounce — save 500ms after the last resize/move event
        if self._geom_save_id is not None:
            self.after_cancel(self._geom_save_id)
        self._geom_save_id = self.after(500, self._save_geometry)

    def _deferred_minsize_sync(self) -> None:
        self._minsize_sync_id = None
        self._sync_mod_col_minsize()

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

    def _on_col_sep_press(self, event: tk.Event) -> None:
        self._col_sep_press_x = event.x_root
        self._col_sep_press_y = event.y_root
        self._on_col_drag_start(event)

    def _on_col_sep_release(self, event: tk.Event) -> None:
        dx = abs(event.x_root - self._col_sep_press_x)
        dy = abs(event.y_root - self._col_sep_press_y)
        if dx < 4 and dy < 4:  # treat as click, not drag
            self._col_drag_x = None  # cancel any pending drag state
            self._col_destroy_ghost()
            self._toggle_plugin_panel()
        else:
            self._on_col_drag_end(event)

    def _on_col_drag_start(self, event: tk.Event) -> None:
        self._col_drag_x = event.x_root
        self._col_drag_w = 0 if not self._plugin_panel_visible else self._plugin_panel_container.winfo_width()
        self._col_drag_target_w = self._col_drag_w
        self._col_show_ghost(event.x_root)

    def _on_col_drag_motion(self, event: tk.Event) -> None:
        if self._col_drag_x is None:
            return
        delta = self._col_drag_x - event.x_root  # drag left = wider plugin panel
        max_w = self._col_max_plugin_width()
        # Allow dragging past min width — collapse threshold is half of min width
        raw_w = self._col_drag_w + delta
        clamped_w = max(0, min(raw_w, max_w))
        self._col_drag_target_w = clamped_w
        # Ghost line clamped to [max-width, fully-collapsed] range so it stops
        # at the panel's min/max bounds instead of following the cursor off-range.
        ghost_x = event.x_root + (raw_w - clamped_w)
        self._col_move_ghost(ghost_x)

    def _on_col_drag_end(self, event: tk.Event) -> None:
        self._col_drag_x = None
        self._col_destroy_ghost()
        min_w = scaled(480)
        if not self._plugin_panel_visible:
            # Dragging from collapsed — expand if dragged far enough left
            if self._col_drag_target_w >= min_w // 2:
                new_w = max(min_w, min(self._col_drag_target_w, self._col_max_plugin_width()))
                self._plugin_panel_saved_w = new_w
                self._plugin_panel_visible = False  # _toggle_plugin_panel will flip it
                self._toggle_plugin_panel()
            return
        if self._col_drag_target_w < min_w // 2:
            # Dragged far enough right — collapse
            self._plugin_panel_visible = True  # _toggle_plugin_panel will flip it
            saved = self._plugin_panel_saved_w
            self._toggle_plugin_panel()
            self._plugin_panel_saved_w = saved
        else:
            new_w = max(min_w, min(self._col_drag_target_w, self._col_max_plugin_width()))
            self.grid_columnconfigure(2, minsize=new_w, weight=4)
            self._plugin_panel_container.configure(width=new_w)
            self._plugin_panel_saved_w = new_w
            self._col2_min_applied = new_w

    def _ensure_plugin_panel_visible(self) -> None:
        if not self._plugin_panel_visible:
            self._toggle_plugin_panel()

    def _toggle_plugin_panel(self) -> None:
        self._plugin_panel_visible = not self._plugin_panel_visible
        if self._plugin_panel_visible:
            self._plugin_panel_container.grid()
            self.grid_columnconfigure(2, weight=4, minsize=self._plugin_panel_saved_w)
            self._plugin_panel_container.configure(width=self._plugin_panel_saved_w)
            self._col2_min_applied = self._plugin_panel_saved_w
            self._col_drag_handle.configure(cursor="sb_h_double_arrow")
            self._col_drag_handle.itemconfigure("arrow", text="▶")
        else:
            self._plugin_panel_saved_w = self._plugin_panel_container.winfo_width() or self._plugin_panel_saved_w
            self._plugin_panel_container.grid_remove()
            self.grid_columnconfigure(2, weight=0, minsize=0)
            self._col_drag_handle.configure(cursor="hand2")
            self._col_drag_handle.itemconfigure("arrow", text="◀")

    def _mod_col_required_width(self) -> int:
        """Minimum width the modlist column needs to avoid clipping the topbar
        or bottom toolbar. Scales with UI scale because widget req widths already do."""
        mod_panel = getattr(self, "_mod_panel", None)
        toolbar = getattr(mod_panel, "_toolbar_bar", None) if mod_panel is not None else None
        toolbar_w = toolbar.winfo_reqwidth() if toolbar is not None else scaled(700)
        topbar = getattr(self, "_topbar", None)
        topbar_w = 0
        if topbar is not None:
            row1 = getattr(topbar, "_row1", None)
            row2 = getattr(topbar, "_row2", None)
            r1w = row1.winfo_reqwidth() if row1 is not None else 0
            r2w = row2.winfo_reqwidth() if row2 is not None else 0
            # Once the topbar wraps to two rows, min width is the wider row.
            topbar_w = max(r1w, r2w)
        return max(toolbar_w, topbar_w) + scaled(20)

    def _sync_mod_col_minsize(self) -> None:
        """Keep column 0's minsize in sync with the topbar/toolbar's current
        required width, so grid weight distribution can't clip them.

        When the window is narrower than col0_required + sep + col2_saved
        (WM tiling, unmaximize, or a saved plugin width that exceeds the
        window), col 2 is shrunk toward ``scaled(480)`` and col 0 capped so
        both fit — otherwise col 2 pins at its saved width and squashes
        the modlist column."""
        # Re-entrancy guard: our own grid_columnconfigure calls can trigger a
        # <Configure> on the root (on fractional-scaled Wayland / tiling WMs
        # that contest the requested size), which would re-enter this sync
        # and oscillate the layout between two states.
        if getattr(self, "_syncing_col_minsize", False):
            return
        self._syncing_col_minsize = True
        try:
            required = self._mod_col_required_width()
            win_w = self.winfo_width()
            new_col2_min: int | None = None
            if win_w > 1 and self._plugin_panel_visible:
                sep_w = self._col_drag_handle.winfo_reqwidth() or scaled(14)
                plugin_min = scaled(480)
                saved = max(plugin_min, self._plugin_panel_saved_w)
                # How wide can the plugin panel be without overflowing?
                max_plugin = max(plugin_min, win_w - required - sep_w)
                plugin_w = min(saved, max_plugin)
                new_col2_min = plugin_w
                if required + sep_w + plugin_w > win_w:
                    required = max(scaled(320), win_w - sep_w - plugin_w)
            # Only reconfigure when the value actually changed — each call can
            # emit a <Configure> even when the value is identical. Drop the
            # direct container.configure(width=...) entirely: grid_propagate
            # is False and the grid column's minsize drives the container.
            if new_col2_min is not None and new_col2_min != getattr(self, "_col2_min_applied", None):
                self.grid_columnconfigure(2, weight=4, minsize=new_col2_min)
                self._col2_min_applied = new_col2_min
            if required != getattr(self, "_col0_min_applied", None):
                self.grid_columnconfigure(0, weight=5, minsize=required)
                self._col0_min_applied = required
        finally:
            self._syncing_col_minsize = False

    def _col_max_plugin_width(self) -> int:
        return self.winfo_width() - self._mod_col_required_width() - self._col_drag_handle.winfo_width()

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
            self, bg=BORDER, width=14, highlightthickness=0, cursor="sb_h_double_arrow"
        )
        self._col_drag_handle.grid(row=0, column=1, rowspan=2, sticky="ns")
        self._col_drag_x: int | None = None
        self._col_drag_w: int | None = None
        self._col_ghost: tk.Toplevel | None = None
        self._col_drag_target_w: int = scaled(480)
        self._plugin_panel_visible: bool = True
        self._plugin_panel_saved_w: int = scaled(480)
        self._col_sep_press_x: int = 0
        self._col_sep_press_y: int = 0
        # Arrow toggle indicator (centered in the separator)
        self._col_drag_handle.create_text(
            7, 0, anchor="n", text="▶", fill=TEXT_DIM,
            font=("", FS9), tags="arrow",
        )
        self._col_drag_handle.bind("<Configure>", lambda e: self._col_drag_handle.coords("arrow", 7, e.height // 2 - 8))
        self._col_drag_handle.bind("<ButtonPress-1>", self._on_col_sep_press)
        self._col_drag_handle.bind("<B1-Motion>", self._on_col_drag_motion)
        self._col_drag_handle.bind("<ButtonRelease-1>", self._on_col_sep_release)

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
                self._sync_mod_col_minsize()
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
                    self._plugin_panel._staging_root = game.get_effective_mod_staging_path()
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
                profile_dir = (
                    self._mod_panel._modlist_path.parent
                    if self._mod_panel._modlist_path else None
                )
                disabled_map = read_disabled_plugins(profile_dir, None) if profile_dir else {}
                removed, added = sync_plugins_from_filemap_combined(
                    Path(filemap_path_str),
                    self._plugin_panel._plugins_path,
                    self._plugin_panel._plugin_extensions,
                    data_dir=prune_data_dir,
                    disabled_plugins=disabled_map,
                    star_prefix=self._plugin_panel._plugins_star_prefix,
                )
                if removed:
                    self._status.log(f"plugins.txt: removed {removed} plugin(s).")
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
            # Always call _refresh_plugins_tab() so toolbar visibility (loot
            # toolbar, search bar) is updated correctly on app launch as well
            # as on game switch.  For games without plugin_extensions it
            # early-returns after hiding the toolbar and refreshing banners.
            self._plugin_panel._refresh_plugins_tab()
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
            # Highlight plugins belonging to the selected mod(s)
            mod_name = None
            if self._mod_panel._sel_idx >= 0 and self._mod_panel._sel_idx < len(self._mod_panel._entries):
                entry = self._mod_panel._entries[self._mod_panel._sel_idx]
                if not entry.is_separator:
                    mod_name = entry.name
                elif entry.name == _OVERWRITE_NAME:
                    mod_name = _OVERWRITE_NAME
            # Collect all selected mod names for multi-select plugin highlighting
            all_mod_names: set[str] = set()
            for si in self._mod_panel._sel_set:
                if 0 <= si < len(self._mod_panel._entries):
                    e = self._mod_panel._entries[si]
                    if not e.is_separator:
                        all_mod_names.add(e.name)
                    elif e.name not in (_OVERWRITE_NAME,):
                        # Separator selected — include all child mods
                        for ci in self._mod_panel._sep_block_range(si):
                            child = self._mod_panel._entries[ci]
                            if not child.is_separator:
                                all_mod_names.add(child.name)
            # BSA conflict: gather the mods the selection beats (higher) and
            # mods that beat the selection (lower), minus the selected mods
            # themselves, so their plugins can be painted green/red.
            bsa_higher: set[str] = set()
            bsa_lower: set[str] = set()
            for mn in all_mod_names or ({mod_name} if mod_name else set()):
                bsa_higher.update(self._mod_panel._bsa_overrides.get(mn, set()))
                bsa_lower.update(self._mod_panel._bsa_overridden_by.get(mn, set()))
            sel_names_set = all_mod_names or ({mod_name} if mod_name else set())
            bsa_higher -= sel_names_set
            bsa_lower -= sel_names_set
            self._plugin_panel.set_highlighted_plugins(
                mod_name if mod_name and mod_name != _OVERWRITE_NAME else None,
                mod_names=all_mod_names if all_mod_names else None,
                bsa_higher_mods=bsa_higher or None,
                bsa_lower_mods=bsa_lower or None,
            )
            self._plugin_panel.show_mod_files(mod_name)
            self._plugin_panel.show_mod_archives(mod_name)
            # If the missing-requirements panel is currently open and the newly
            # selected mod also has missing requirements, switch the panel to it.
            if (getattr(self, "_missing_reqs_panel", None) is not None
                    and mod_name and mod_name != _OVERWRITE_NAME
                    and mod_name in self._mod_panel._missing_reqs):
                dep_names = self._mod_panel._missing_reqs_detail.get(mod_name, [])
                self._mod_panel._show_missing_reqs(mod_name, dep_names)
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
                self._plugin_panel._plugin_order_on_change = self._mod_panel.recompute_bsa_conflicts
                # Archive tab — gated on game.archive_extensions
                self._plugin_panel._bsa_index_path = _staging.parent / "bsa_index.bin"
                self._plugin_panel._bsa_conflict_cache = None
                self._plugin_panel._update_archive_tab_visibility()
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
        self._ensure_plugin_panel_visible()
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
        self._ensure_plugin_panel_visible()
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

    # -- Wine DLL Overrides panel --------------------------------------------

    def show_wine_dll_panel(self, game, log_fn):
        self._ensure_plugin_panel_visible()
        from gui.wine_dll_overrides_panel import WineDllOverridesPanel
        self._show_plugin_overlay(
            "_wine_dll_panel",
            lambda: WineDllOverridesPanel(
                self._plugin_panel_container, game, log_fn,
                on_done=lambda p: self._hide_plugin_overlay("_wine_dll_panel"),
            ),
        )

    def hide_wine_dll_panel(self):
        self._hide_plugin_overlay("_wine_dll_panel")

    # -- Wizard panel --------------------------------------------------------

    def show_wizard_panel(self, game, log_fn):
        self._ensure_plugin_panel_visible()
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
        self._ensure_plugin_panel_visible()
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
                              is_apps_exe=False, saved_launch_options="", log_fn=None):
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
                saved_launch_options=saved_launch_options,
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

    # -- Ini file editor panel (overlays mod list) --------------------------

    def show_ini_editor_panel(self, file_path: str, rel_path: str, mod_name: str,
                              highlight: "str | None" = None):
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
                highlight=highlight,
            ),
        )

    def hide_ini_editor_panel(self):
        self._hide_plugin_overlay("_ini_editor_panel")

    # -- Separator settings panel (overlays plugin panel) -------------------

    def show_sep_settings_panel(self, sep_name, current_path, on_save, current_raw=False):
        self._ensure_plugin_panel_visible()
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

    # -- Separator color panel (overlays plugin panel) ----------------------

    def show_sep_color_panel(self, sep_name: str, initial_color: str | None, on_result):
        self._ensure_plugin_panel_visible()
        from gui.dialogs import SepColorPanel
        def _factory():
            def _done(panel):
                self._hide_plugin_overlay("_sep_color_panel")
            return SepColorPanel(
                self._plugin_panel_container,
                sep_name=sep_name, initial_color=initial_color,
                on_result=on_result, on_done=_done,
            )
        self._show_plugin_overlay("_sep_color_panel", _factory)

    # -- Theme color panel (reuses SepColorPanel) ---------------------------

    def show_theme_color_panel(self, title: str, initial_color: str | None, on_result):
        """Show the wheel/slider/hex colour picker over the plugin panel for
        Theme settings. Reuses SepColorPanel; *title* is displayed in the
        header (e.g. "Theme Color — Conflict winner").
        """
        self._ensure_plugin_panel_visible()
        from gui.dialogs import SepColorPanel
        def _factory():
            def _done(panel):
                self._hide_plugin_overlay("_theme_color_panel")
            return SepColorPanel(
                self._plugin_panel_container,
                sep_name=title, initial_color=initial_color,
                on_result=on_result, on_done=_done,
                title=f"Theme Color \u2014 {title}",
            )
        self._show_plugin_overlay("_theme_color_panel", _factory)

    # -- Disable plugins panel (overlays plugin panel) ----------------------

    def show_disable_plugins_panel(self, mod_name, plugin_names, disabled, on_done):
        self._ensure_plugin_panel_visible()
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

    def show_optional_mods_panel(self, optional_mods: list, on_done, pre_skipped_fids=None):
        """Show OptionalModsPanel as overlay on plugin panel. on_done(panel) receives the
        panel; panel.result is None (cancelled) or set of file_ids to skip."""
        self._ensure_plugin_panel_visible()
        from gui.collections_dialog import OptionalModsPanel
        def _factory():
            def _done(panel):
                self._hide_plugin_overlay("_optional_mods_panel")
                on_done(panel)
            return OptionalModsPanel(
                self._plugin_panel_container,
                optional_mods=optional_mods,
                on_done=_done,
                pre_skipped_fids=pre_skipped_fids,
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
        self._ensure_plugin_panel_visible()
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
        self._ensure_plugin_panel_visible()
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

    def _sync_custom_handlers(self):
        """Background-download every custom handler from GitHub, overwriting stale copies."""
        import json as _json
        from gui.dialogs import _CUSTOM_HANDLERS_API_URL
        from Utils.config_paths import get_custom_games_dir as _gcgd
        from Utils.gh_cache import fetch_text as _gh_fetch_text
        from Utils.ui_config import load_dev_mode

        if load_dev_mode():
            return

        def _do():
            try:
                listing = _gh_fetch_text(
                    _CUSTOM_HANDLERS_API_URL,
                    timeout=15,
                )
                if listing is None:
                    return
                data = _json.loads(listing)
                handlers = [
                    e for e in data
                    if isinstance(e, dict) and e.get("name", "").endswith(".json")
                ]
                changed = False
                for h in handlers:
                    filename = h.get("name", "")
                    download_url = h.get("download_url")
                    if not download_url:
                        continue
                    try:
                        raw = _gh_fetch_text(
                            download_url,
                            accept="*/*",
                            timeout=10,
                            min_interval=3600,
                        )
                        if raw is None:
                            continue
                        _json.loads(raw)  # validate
                        dest = _gcgd() / filename
                        # Only write if content changed (avoids unnecessary disk I/O)
                        if not dest.is_file() or dest.read_text(encoding="utf-8") != raw:
                            dest.write_text(raw, encoding="utf-8")
                            changed = True
                    except Exception:
                        pass
                if changed:
                    self.call_threadsafe(self._reload_games_after_handler_sync)
            except Exception:
                pass

        threading.Thread(target=_do, daemon=True).start()

    def _reload_games_after_handler_sync(self):
        """Refresh the game registry after a background handler sync."""
        from gui.game_helpers import _load_games
        _load_games()
        panel = getattr(self, "_game_picker_panel", None)
        if panel is not None:
            try:
                panel.refresh()
            except Exception:
                pass

    def _sync_plugins(self):
        """Background-download every wizard plugin from GitHub, overwriting stale copies."""
        import json as _json
        from Utils.config_paths import get_plugins_dir as _gpd
        from Utils.gh_cache import fetch_text as _gh_fetch_text

        _PLUGINS_API_URL = (
            "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/contents/"
            "Plugins?ref=main"
        )

        def _do():
            try:
                listing = _gh_fetch_text(
                    _PLUGINS_API_URL,
                    timeout=15,
                )
                if listing is None:
                    return
                data = _json.loads(listing)
                plugins = [
                    e for e in data
                    if isinstance(e, dict) and e.get("name", "").endswith(".py")
                ]
                changed = False
                for p in plugins:
                    filename = p.get("name", "")
                    download_url = p.get("download_url")
                    if not download_url:
                        continue
                    try:
                        raw = _gh_fetch_text(
                            download_url,
                            accept="*/*",
                            timeout=10,
                            min_interval=3600,
                        )
                        if raw is None:
                            continue
                        dest = _gpd() / filename
                        if not dest.is_file() or dest.read_text(encoding="utf-8") != raw:
                            dest.write_text(raw, encoding="utf-8")
                            changed = True
                    except Exception:
                        pass
                if changed:
                    from Utils.plugin_loader import discover_plugins
                    discover_plugins(force=True)
            except Exception:
                pass

        threading.Thread(target=_do, daemon=True).start()

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

    def _restore_all_on_close():
        """Synchronously restore every configured game that has an active deployment."""
        from Utils.deploy import restore_root_folder

        games = [g for g in _GAMES.values()
                 if g.is_configured() and g.get_deploy_active()
                 and getattr(g, "restore_on_close_eligible", True)]
        if not games:
            return

        status = getattr(app, "_status", None)
        log_fn = getattr(status, "log", None) or (lambda _msg: None)
        log_fn(f"Restore-on-close: restoring {len(games)} game(s)...")

        for game in games:
            try:
                game_root = game.get_game_path()
                last_deployed = game.get_last_deployed_profile()
                original_profile_dir = game._active_profile_dir if hasattr(game, "_active_profile_dir") else None
                if last_deployed:
                    game.set_active_profile_dir(
                        game.get_profile_root() / "profiles" / last_deployed
                    )
                try:
                    if hasattr(game, "restore"):
                        game.restore(log_fn=log_fn)
                    root_folder_dir = game.get_effective_root_folder_path()
                    if root_folder_dir.is_dir() and game_root:
                        restore_root_folder(root_folder_dir, game_root, log_fn=log_fn)
                    game.clear_deploy_active()
                finally:
                    if original_profile_dir is not None:
                        game.set_active_profile_dir(original_profile_dir)
            except Exception as e:
                log_fn(f"Restore-on-close error for {game.name}: {e}")

    app = App()
    from Utils.portal_filechooser import set_main_thread_dispatcher
    set_main_thread_dispatcher(app.call_threadsafe)
    app._start_nxm_ipc()          # listen for NXM links from future instances

    _shutdown_done = {"v": False}

    def _run_shutdown():
        if _shutdown_done["v"]:
            return
        _shutdown_done["v"] = True
        try:
            from Utils.ui_config import load_restore_on_close
            if load_restore_on_close():
                _restore_all_on_close()
        except Exception:
            pass
        try:
            NxmIPC.shutdown()
        except Exception:
            pass

    def _on_app_close():
        _run_shutdown()
        try:
            app.destroy()
        except Exception:
            pass

    # WM_DELETE_WINDOW only fires on a graceful window close (clicking X).
    # Some desktop environments / session managers close the app via SIGTERM
    # or SIGHUP instead, which would skip restore-on-close. atexit + signal
    # handlers catch those paths too.
    import atexit
    import signal
    atexit.register(_run_shutdown)

    def _on_signal(signum, _frame):
        # Tk is not signal-safe — don't touch widgets here. Defer shutdown
        # onto the main loop and let Tk exit gracefully. If the loop isn't
        # running or can't schedule, fall back to a bare exit.
        try:
            app.after(0, _on_app_close)
            return
        except Exception:
            pass
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except Exception:
            sys.exit(0)

    for _sig_name in ("SIGTERM", "SIGHUP", "SIGINT"):
        _sig = getattr(signal, _sig_name, None)
        if _sig is not None:
            try:
                signal.signal(_sig, _on_signal)
            except (ValueError, OSError):
                pass

    app.protocol("WM_DELETE_WINDOW", _on_app_close)
    app.mainloop()
