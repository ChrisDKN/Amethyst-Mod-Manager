"""
nexus_settings_dialog.py
Modal dialog for configuring the Nexus Mods API key and NXM handler.

Allows the user to:
  - Enter / paste their Nexus Mods personal API key
  - Validate the key against the API
  - Register / unregister the nxm:// protocol handler
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk

from Nexus.nexus_api import (
    NexusAPI,
    NexusAPIError,
    NexusRateLimits,
    load_api_key,
    save_api_key,
    clear_api_key,
)
from Nexus.nexus_oauth import NexusOAuthClient, OAuthTokens, clear_oauth_tokens, CLIENT_ID
from Nexus.nxm_handler import NxmHandler

from gui.theme import (
    BG_DEEP,
    BG_PANEL,
    BG_HEADER,
    BG_HOVER,
    BG_ROW,
    ACCENT,
    ACCENT_HOV,
    TEXT_MAIN,
    TEXT_DIM,
    TEXT_OK,
    TEXT_ERR,
    TEXT_WARN,
    BORDER,
    FONT_NORMAL,
    FONT_BOLD,
    FONT_SMALL,
    FONT_MONO,
)


class NexusSettingsDialog(ctk.CTkToplevel):
    """
    Modal dialog for Nexus Mods API key management.

    Usage:
        dialog = NexusSettingsDialog(parent, on_key_changed=callback)
        parent.wait_window(dialog)
        # dialog.result is True if the key was changed, False/None otherwise
    """

    WIDTH  = 560
    HEIGHT = 620

    def __init__(self, parent, on_key_changed=None, log_fn: Optional[Callable[[str], None]] = None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Nexus Mods Settings")
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._on_key_changed = on_key_changed
        self._log = log_fn or (lambda _: None)
        self.result: Optional[bool] = None
        self._key_changed = False
        self._oauth_client: Optional[NexusOAuthClient] = None

        self._build()
        self.after(50, self._safe_grab)

    def _safe_grab(self):
        """Grab focus once the window is actually visible."""
        try:
            self.grab_set()
        except tk.TclError:
            # Window not yet viewable — retry shortly
            self.after(50, self._safe_grab)

    def _build(self):
        pad = {"padx": 16, "pady": (8, 0)}

        # -- Header --
        ctk.CTkLabel(
            self, text="Nexus Mods API Key",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(**pad, anchor="w")

        ctk.CTkLabel(
            self,
            text="Log in via browser, or paste a personal API key manually.",
            font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(padx=16, pady=(2, 8), anchor="w")

        # -- OAuth Login --
        sso_frame = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=6)
        sso_frame.pack(padx=16, pady=(0, 6), fill="x")

        ctk.CTkLabel(
            sso_frame, text="Browser Login",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(side="left", padx=(8, 12), pady=8)

        self._sso_btn = ctk.CTkButton(
            sso_frame, text="Log in via Nexus Mods", width=180, font=FONT_BOLD,
            fg_color="#d98f40", hover_color="#e5a04d", text_color="white",
            command=self._on_sso_login,
        )
        self._sso_btn.pack(side="left", padx=4, pady=8)
        # Disable button until a CLIENT_ID is configured
        if not CLIENT_ID:
            self._sso_btn.configure(state="disabled")

        self._sso_cancel_btn = ctk.CTkButton(
            sso_frame, text="Cancel", width=70, font=FONT_SMALL,
            fg_color="#8b1a1a", hover_color="#b22222", text_color="white",
            command=self._on_sso_cancel,
        )
        # hidden by default; shown only while OAuth is in progress

        # -- Separator --
        ctk.CTkFrame(self, fg_color=BORDER, height=1).pack(fill="x", padx=16, pady=2)

        ctk.CTkLabel(
            self,
            text="Or paste a personal API key (nexusmods.com → Settings → API Keys):",
            font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(padx=16, pady=(4, 4), anchor="w")

        # -- Key entry --
        key_frame = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=6)
        key_frame.pack(padx=16, pady=4, fill="x")

        self._key_var = tk.StringVar(value=load_api_key())
        self._key_entry = ctk.CTkEntry(
            key_frame, textvariable=self._key_var,
            placeholder_text="Paste your API key here...",
            font=FONT_MONO, text_color=TEXT_MAIN,
            fg_color=BG_ROW, border_color=BORDER,
            show="•",
            width=380,
        )
        self._key_entry.pack(side="left", padx=(8, 4), pady=8, fill="x", expand=True)

        self._show_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            key_frame, text="Show",
            variable=self._show_var,
            font=FONT_SMALL, text_color=TEXT_DIM,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            command=self._toggle_show,
        ).pack(side="right", padx=8, pady=8)

        # -- Buttons --
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(padx=16, pady=8, fill="x")

        ctk.CTkButton(
            btn_frame, text="Validate Key", width=120, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_validate,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Save Key", width=100, font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._on_save,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Clear Key", width=100, font=FONT_BOLD,
            fg_color="#8b1a1a", hover_color="#b22222", text_color="white",
            command=self._on_clear,
        ).pack(side="left")

        # -- Status label --
        self._status_label = ctk.CTkLabel(
            self, text="", font=FONT_SMALL, text_color=TEXT_DIM,
        )
        self._status_label.pack(padx=16, pady=(4, 8), anchor="w")

        # -- Separator --
        ctk.CTkFrame(self, fg_color=BORDER, height=1).pack(fill="x", padx=16, pady=4)

        # -- API Rate Limit section --
        ctk.CTkLabel(
            self, text="API Rate Limit",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(padx=16, pady=(8, 2), anchor="w")

        ctk.CTkLabel(
            self,
            text="Current Nexus API request quota (hourly and daily). Click Refresh to fetch latest.",
            font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(padx=16, pady=(0, 6), anchor="w")

        rate_frame = ctk.CTkFrame(self, fg_color="transparent")
        rate_frame.pack(padx=16, pady=4, fill="x")

        self._rate_limit_label = ctk.CTkLabel(
            rate_frame, text="", font=FONT_SMALL, text_color=TEXT_DIM,
        )
        self._rate_limit_label.pack(side="left", padx=(0, 12))

        self._rate_refresh_btn = ctk.CTkButton(
            rate_frame, text="Refresh", width=80, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_refresh_rate_limits,
        )
        self._rate_refresh_btn.pack(side="left")
        self._update_rate_limit_display()

        # -- Separator --
        ctk.CTkFrame(self, fg_color=BORDER, height=1).pack(fill="x", padx=16, pady=4)

        # -- NXM Handler section --
        ctk.CTkLabel(
            self, text="NXM Protocol Handler",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(padx=16, pady=(8, 2), anchor="w")

        ctk.CTkLabel(
            self,
            text="Handles nxm:// links from the \"Download with Manager\" button.",
            font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(padx=16, pady=(0, 8), anchor="w")

        nxm_frame = ctk.CTkFrame(self, fg_color="transparent")
        nxm_frame.pack(padx=16, pady=4, fill="x")

        self._nxm_status = ctk.CTkLabel(
            nxm_frame, text="", font=FONT_SMALL, text_color=TEXT_DIM,
        )
        self._nxm_status.pack(side="left", padx=(0, 12))

        ctk.CTkButton(
            nxm_frame, text="Register", width=100, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_register_nxm,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            nxm_frame, text="Unregister", width=100, font=FONT_BOLD,
            fg_color="#8b1a1a", hover_color="#b22222", text_color="white",
            command=self._on_unregister_nxm,
        ).pack(side="left")

        self._update_nxm_status()

    # -- Show/hide key ------------------------------------------------------

    def _toggle_show(self):
        self._key_entry.configure(show="" if self._show_var.get() else "•")

    # -- Validate -----------------------------------------------------------

    def _on_validate(self):
        key = self._key_var.get().strip()
        if not key:
            self._set_status("Enter an API key first.", TEXT_WARN)
            return

        self._set_status("Validating...", TEXT_DIM)

        def _worker():
            try:
                api = NexusAPI(api_key=key)
                user = api.validate()
                premium = " (Premium)" if user.is_premium else ""
                self.after(0, lambda: self._set_status(
                    f"✓ Valid — {user.name}{premium}", TEXT_OK))
            except NexusAPIError as exc:
                self.after(0, lambda: self._set_status(
                    f"✗ {exc}", TEXT_ERR))
            except Exception as exc:
                self.after(0, lambda: self._set_status(
                    f"✗ Error: {exc}", TEXT_ERR))

        threading.Thread(target=_worker, daemon=True).start()

    # -- Save / clear -------------------------------------------------------

    def _on_save(self):
        key = self._key_var.get().strip()
        if not key:
            self._set_status("Nothing to save — key is empty.", TEXT_WARN)
            return
        try:
            save_api_key(key)
        except RuntimeError:
            self._show_keyring_error_popup()
            self._set_status("Save failed — no keyring available.", TEXT_ERR)
            return
        self._key_changed = True
        self._set_status("Key saved.", TEXT_OK)

    def _show_keyring_error_popup(self):
        """Show a themed popup explaining that no keyring backend is available."""
        W, H = 560, 500
        dlg = ctk.CTkToplevel(self, fg_color=BG_DEEP)
        dlg.title("No Keyring Available")
        dlg.resizable(False, False)
        dlg.transient(self)
        try:
            x = self.winfo_rootx() + (self.winfo_width() - W) // 2
            y = self.winfo_rooty() + (self.winfo_height() - H) // 2
            dlg.geometry(f"{W}x{H}+{x}+{y}")
        except Exception:
            dlg.geometry(f"{W}x{H}")

        # -- Button bar (packed first so it stays fixed at the bottom) --
        bar = ctk.CTkFrame(dlg, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x", side="top")
        ctk.CTkButton(bar, text="OK", width=90, height=32, font=FONT_BOLD,
                      fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
                      command=dlg.destroy).pack(side="right", padx=12, pady=10)

        # -- Scrollable body --
        scroll = ctk.CTkScrollableFrame(dlg, fg_color=BG_DEEP,
                                        scrollbar_button_color=BG_PANEL,
                                        scrollbar_button_hover_color=BG_HOVER)
        scroll.pack(fill="both", expand=True)
        scroll.grid_columnconfigure(0, weight=1)

        # Forward mouse-wheel scroll events to the scrollable canvas
        def _fwd_scroll(event):
            scroll._parent_canvas.yview_scroll(-1 if event.num == 4 else 1, "units")
        dlg.bind_all("<Button-4>", _fwd_scroll)
        dlg.bind_all("<Button-5>", _fwd_scroll)
        dlg.bind("<Destroy>", lambda e: (
            dlg.unbind_all("<Button-4>"),
            dlg.unbind_all("<Button-5>"),
        ) if e.widget is dlg else None)

        # -- Header --
        hdr = ctk.CTkFrame(scroll, fg_color="transparent")
        hdr.pack(fill="x", padx=20, pady=(16, 6))
        ctk.CTkLabel(hdr, text="✕", font=("Segoe UI", 22, "bold"),
                     text_color=TEXT_ERR, width=32).pack(side="left", anchor="n", padx=(0, 10))
        ctk.CTkLabel(hdr, text="No keyring service found on your system.",
                     font=FONT_BOLD, text_color=TEXT_MAIN,
                     wraplength=460, justify="left").pack(side="left", anchor="n")

        ctk.CTkLabel(scroll,
                     text="Your API key could not be saved. A keyring backend is required to "
                          "securely store credentials. Install and enable one for your distribution:",
                     font=FONT_NORMAL, text_color=TEXT_DIM,
                     wraplength=500, justify="left").pack(anchor="w", padx=20, pady=(0, 10))

        _CMD_FONT = ("Courier New", 11)

        def _make_cmd_row(parent, cmd_text, is_comment):
            """Add a comment label or a copyable command row to parent."""
            if is_comment:
                ctk.CTkLabel(parent, text=cmd_text, font=_CMD_FONT,
                             text_color=TEXT_DIM, anchor="w"
                             ).pack(anchor="w", padx=12, pady=(4, 0))
                return

            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=(4, 0))
            row.grid_columnconfigure(0, weight=1)

            entry = tk.Entry(
                row,
                readonlybackground=BG_ROW, bg=BG_ROW, fg=TEXT_MAIN,
                font=_CMD_FONT, relief="flat", bd=0,
                state="readonly", cursor="xterm",
                highlightthickness=1, highlightbackground=BORDER,
                insertbackground=TEXT_MAIN, disabledforeground=TEXT_MAIN,
            )
            entry.configure(state="normal")
            entry.insert(0, cmd_text)
            entry.configure(state="readonly")
            entry.grid(row=0, column=0, sticky="ew", ipady=5, padx=(2, 4))

            # Forward scroll from the entry widget too
            entry.bind("<Button-4>", lambda e: scroll._parent_canvas.yview_scroll(-1, "units"))
            entry.bind("<Button-5>", lambda e: scroll._parent_canvas.yview_scroll( 1, "units"))

            copy_btn = ctk.CTkButton(
                row, text="Copy", width=58, height=28, font=FONT_SMALL,
                fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
                corner_radius=4,
            )
            copy_btn.grid(row=0, column=1, sticky="e", padx=(0, 2))

            def _copy(btn=copy_btn, text=cmd_text):
                dlg.clipboard_clear()
                dlg.clipboard_append(text)
                dlg.update()
                btn.configure(text="Copied!")
                dlg.after(1500, lambda: btn.configure(text="Copy"))

            copy_btn.configure(command=_copy)

        distros = [
            ("Arch Linux / SteamOS", [
                ("sudo pacman -S gnome-keyring libsecret", False),
                ("systemctl --user enable --now gnome-keyring-daemon", False),
            ]),
            ("Ubuntu / Debian / Linux Mint", [
                ("sudo apt install gnome-keyring libsecret-1-0", False),
                ("# Log out and back in, or run:", True),
                ("dbus-run-session -- gnome-keyring-daemon --unlock", False),
            ]),
            ("Fedora / RHEL / CentOS", [
                ("sudo dnf install gnome-keyring libsecret", False),
                ("systemctl --user enable --now gnome-keyring-daemon", False),
            ]),
            ("openSUSE", [
                ("sudo zypper install gnome-keyring libsecret-1-0", False),
                ("systemctl --user enable --now gnome-keyring-daemon", False),
            ]),
            ("Generic fallback (any distro)", [
                ("pip install keyring secretstorage", False),
                ("# Or install KWallet if on a KDE desktop.", True),
            ]),
        ]

        for section_title, cmds in distros:
            ctk.CTkLabel(scroll, text=section_title, font=FONT_BOLD,
                         text_color=TEXT_MAIN, anchor="w"
                         ).pack(anchor="w", padx=20, pady=(6, 2))
            block = ctk.CTkFrame(scroll, fg_color=BG_PANEL, corner_radius=6)
            block.pack(fill="x", padx=20, pady=(0, 8))
            for cmd_text, is_comment in cmds:
                _make_cmd_row(block, cmd_text, is_comment)
            ctk.CTkFrame(block, fg_color="transparent", height=6).pack()

        ctk.CTkLabel(scroll,
                     text="After installing, restart the application and try again.",
                     font=FONT_SMALL, text_color=TEXT_DIM,
                     wraplength=500, justify="left").pack(anchor="w", padx=20, pady=(0, 14))

        dlg.after(50, dlg.grab_set)
        dlg.wait_window()

    def _on_clear(self):
        clear_api_key()
        self._key_var.set("")
        self._key_changed = True
        self._set_status("Key cleared.", TEXT_WARN)

    # -- NXM handler --------------------------------------------------------

    def _on_register_nxm(self):
        ok = NxmHandler.register()
        if ok:
            self._set_status("NXM handler registered.", TEXT_OK)
        else:
            self._set_status("Failed to register — xdg-mime not found?", TEXT_ERR)
        self._update_nxm_status()

    def _on_unregister_nxm(self):
        NxmHandler.unregister()
        self._set_status("NXM handler unregistered.", TEXT_WARN)
        self._update_nxm_status()

    def _update_nxm_status(self):
        if NxmHandler.is_registered():
            self._nxm_status.configure(text="Status: Registered ✓", text_color=TEXT_OK)
        else:
            self._nxm_status.configure(text="Status: Not registered", text_color=TEXT_DIM)

    # -- API Rate Limit -----------------------------------------------------

    def _get_nexus_api(self) -> Optional[NexusAPI]:
        """Return the app's Nexus API instance if available (parent is the main App)."""
        app = self.master  # dialog was created with parent=App, not winfo_toplevel() which is self
        return getattr(app, "_nexus_api", None)

    def _format_rate_limits(self, r: NexusRateLimits) -> str:
        def _line(label: str, remaining: int, limit: int) -> str:
            if remaining < 0 or limit < 0:
                return f"{label}: —"
            return f"{label}: {remaining:,} / {limit:,} remaining"

        hourly = _line("Hourly", r.hourly_remaining, r.hourly_limit)
        daily = _line("Daily", r.daily_remaining, r.daily_limit)
        return f"{hourly}  ·  {daily}"

    def _update_rate_limit_display(self) -> None:
        """Update the rate limit label from the app's Nexus API (main thread only)."""
        api = self._get_nexus_api()
        if api is None:
            self._rate_limit_label.configure(
                text="Save and validate your API key, then click Refresh.",
                text_color=TEXT_DIM,
            )
            return
        r = api.rate_limits
        if r.hourly_remaining < 0 and r.daily_remaining < 0:
            self._rate_limit_label.configure(
                text="No data yet. Click Refresh to check your quota.",
                text_color=TEXT_DIM,
            )
            return
        self._rate_limit_label.configure(
            text=self._format_rate_limits(r),
            text_color=TEXT_MAIN,
        )

    def _on_refresh_rate_limits(self) -> None:
        """Fetch fresh rate limits via a lightweight API call (runs in thread)."""
        api = self._get_nexus_api()
        if api is None:
            self._set_status("Set and save your API key first.", TEXT_WARN)
            return

        self._rate_refresh_btn.configure(state="disabled", text="Refreshing...")
        self._set_status("Checking API rate limits...", TEXT_DIM)

        def _worker() -> None:
            err: Optional[Exception] = None
            try:
                api.refresh_rate_limits()  # GET /games, same as Vortex; returns real cumulative remaining
            except Exception as e:
                err = e

            def _on_done() -> None:
                self._rate_refresh_btn.configure(state="normal", text="Refresh")
                if err is not None:
                    self._set_status(f"✗ {err}", TEXT_ERR)
                else:
                    self._set_status("Rate limits updated.", TEXT_OK)
                self._update_rate_limit_display()

            self.after(0, _on_done)

        threading.Thread(target=_worker, daemon=True).start()

    # -- Helpers ------------------------------------------------------------

    def _set_status(self, text: str, color: str = TEXT_DIM):
        self._status_label.configure(text=text, text_color=color)

    # -- OAuth login ---------------------------------------------------------

    def _on_sso_login(self):
        """Start the OAuth 2.0 + PKCE browser login flow."""
        self._sso_btn.configure(state="disabled", text="Waiting...")
        self._sso_cancel_btn.pack(side="left", padx=(4, 8), pady=8)
        self._set_status("Starting browser login...", TEXT_DIM)

        self._oauth_client = NexusOAuthClient(
            on_token=self._oauth_on_token,
            on_error=self._oauth_on_error,
            on_status=self._oauth_on_status,
        )
        self._oauth_client.start()

    def _on_sso_cancel(self):
        """Cancel a running OAuth flow."""
        if self._oauth_client:
            self._oauth_client.cancel()
            self._oauth_client = None
        self._sso_btn.configure(state="normal" if CLIENT_ID else "disabled",
                                text="Log in via Nexus Mods")
        self._sso_cancel_btn.pack_forget()
        self._set_status("Login cancelled.", TEXT_WARN)

    def _oauth_on_token(self, tokens: OAuthTokens):
        """Called from OAuth thread when tokens are received."""
        def _update():
            # Validate via API and show username, using Bearer token
            self._key_changed = True
            self._sso_btn.configure(state="normal" if CLIENT_ID else "disabled",
                                    text="Log in via Nexus Mods")
            self._sso_cancel_btn.pack_forget()
            self._set_status("✓ Logged in via Nexus Mods!", TEXT_OK)
            # Trigger a validate call so the user name appears
            self._validate_oauth(tokens)
        self.after(0, _update)

    def _validate_oauth(self, tokens: OAuthTokens):
        """Validate the OAuth token and display the user's name."""
        def _worker():
            try:
                from Nexus.nexus_api import NexusAPI
                api = NexusAPI.from_oauth(tokens)
                user = api.validate()
                premium = " (Premium)" if user.is_premium else ""
                self.after(0, lambda: self._set_status(
                    f"✓ Logged in as {user.name}{premium}", TEXT_OK))
            except Exception as exc:
                self.after(0, lambda: self._set_status(
                    f"✓ Logged in (could not fetch user info: {exc})", TEXT_OK))
        threading.Thread(target=_worker, daemon=True).start()

    def _oauth_on_error(self, msg: str):
        """Called from OAuth thread on error."""
        def _update():
            self._sso_btn.configure(state="normal" if CLIENT_ID else "disabled",
                                    text="Log in via Nexus Mods")
            self._sso_cancel_btn.pack_forget()
            self._set_status(f"✗ Login failed: {msg}", TEXT_ERR)
        self.after(0, _update)

    def _oauth_on_status(self, msg: str):
        """Called from OAuth thread with status updates."""
        self.after(0, lambda: self._set_status(msg, TEXT_DIM))

    def _on_close(self):
        # Cancel any active OAuth flow
        if self._oauth_client and self._oauth_client.is_running:
            self._oauth_client.cancel()
        if self._key_changed and self._on_key_changed:
            self._on_key_changed()
        self.result = self._key_changed
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# NexusSettingsPanel — inline (non-modal) version of NexusSettingsDialog
# ---------------------------------------------------------------------------

class NexusSettingsPanel(ctk.CTkFrame):
    """Inline panel for Nexus Mods settings — overlays the plugin panel while open."""

    def __init__(self, parent, on_key_changed=None,
                 log_fn: Optional[Callable[[str], None]] = None,
                 nexus_api_getter=None, on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_key_changed = on_key_changed
        self._log = log_fn or (lambda _: None)
        self._nexus_api_getter = nexus_api_getter or (lambda: None)
        self._on_done = on_done or (lambda p: None)
        self.result: Optional[bool] = None
        self._key_changed = False
        self._oauth_client: Optional[NexusOAuthClient] = None
        self._build()

    def _build(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_propagate(False)
        ctk.CTkLabel(
            title_bar, text="Nexus Mods Settings",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="✕", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4, pady=4)

        # Scrollable body
        body = ctk.CTkScrollableFrame(
            self, fg_color=BG_DEEP, corner_radius=0,
            scrollbar_button_color=BG_HEADER,
            scrollbar_button_hover_color=ACCENT,
        )
        body.grid(row=1, column=0, sticky="nsew")

        pad = {"padx": 16, "pady": (8, 0)}

        ctk.CTkLabel(
            body, text="Nexus Mods API Key",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(**pad, anchor="w")

        ctk.CTkLabel(
            body,
            text="Log in via browser, or paste a personal API key manually.",
            font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(padx=16, pady=(2, 8), anchor="w")

        # OAuth Login
        sso_frame = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
        sso_frame.pack(padx=16, pady=(0, 6), fill="x")

        ctk.CTkLabel(
            sso_frame, text="Browser Login",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(side="left", padx=(8, 12), pady=8)

        self._sso_btn = ctk.CTkButton(
            sso_frame, text="Log in via Nexus Mods", width=180, font=FONT_BOLD,
            fg_color="#d98f40", hover_color="#e5a04d", text_color="white",
            command=self._on_sso_login,
        )
        self._sso_btn.pack(side="left", padx=4, pady=8)
        if not CLIENT_ID:
            self._sso_btn.configure(state="disabled")

        self._sso_cancel_btn = ctk.CTkButton(
            sso_frame, text="Cancel", width=70, font=FONT_SMALL,
            fg_color="#8b1a1a", hover_color="#b22222", text_color="white",
            command=self._on_sso_cancel,
        )

        ctk.CTkFrame(body, fg_color=BORDER, height=1).pack(fill="x", padx=16, pady=2)

        ctk.CTkLabel(
            body,
            text="Or paste a personal API key (nexusmods.com → Settings → API Keys):",
            font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(padx=16, pady=(4, 4), anchor="w")

        key_frame = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
        key_frame.pack(padx=16, pady=4, fill="x")

        self._key_var = tk.StringVar(value=load_api_key())
        self._key_entry = ctk.CTkEntry(
            key_frame, textvariable=self._key_var,
            placeholder_text="Paste your API key here...",
            font=FONT_MONO, text_color=TEXT_MAIN,
            fg_color=BG_ROW, border_color=BORDER,
            show="•", width=300,
        )
        self._key_entry.pack(side="left", padx=(8, 4), pady=8, fill="x", expand=True)

        self._show_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            key_frame, text="Show",
            variable=self._show_var,
            font=FONT_SMALL, text_color=TEXT_DIM,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            command=self._toggle_show,
        ).pack(side="right", padx=8, pady=8)

        btn_frame = ctk.CTkFrame(body, fg_color="transparent")
        btn_frame.pack(padx=16, pady=8, fill="x")

        ctk.CTkButton(
            btn_frame, text="Validate Key", width=120, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_validate,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Save Key", width=100, font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._on_save,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Clear Key", width=100, font=FONT_BOLD,
            fg_color="#8b1a1a", hover_color="#b22222", text_color="white",
            command=self._on_clear,
        ).pack(side="left")

        self._status_label = ctk.CTkLabel(
            body, text="", font=FONT_SMALL, text_color=TEXT_DIM,
        )
        self._status_label.pack(padx=16, pady=(4, 8), anchor="w")

        ctk.CTkFrame(body, fg_color=BORDER, height=1).pack(fill="x", padx=16, pady=4)

        # Rate limit
        ctk.CTkLabel(
            body, text="API Rate Limit",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(padx=16, pady=(8, 2), anchor="w")

        ctk.CTkLabel(
            body,
            text="Current Nexus API request quota (hourly and daily). Click Refresh to fetch latest.",
            font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(padx=16, pady=(0, 6), anchor="w")

        rate_frame = ctk.CTkFrame(body, fg_color="transparent")
        rate_frame.pack(padx=16, pady=4, fill="x")

        self._rate_limit_label = ctk.CTkLabel(
            rate_frame, text="", font=FONT_SMALL, text_color=TEXT_DIM,
        )
        self._rate_limit_label.pack(side="left", padx=(0, 12))

        self._rate_refresh_btn = ctk.CTkButton(
            rate_frame, text="Refresh", width=80, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_refresh_rate_limits,
        )
        self._rate_refresh_btn.pack(side="left")
        self._update_rate_limit_display()

        ctk.CTkFrame(body, fg_color=BORDER, height=1).pack(fill="x", padx=16, pady=4)

        # NXM Handler
        ctk.CTkLabel(
            body, text="NXM Protocol Handler",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(padx=16, pady=(8, 2), anchor="w")

        ctk.CTkLabel(
            body,
            text="Handles nxm:// links from the \"Download with Manager\" button.",
            font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(padx=16, pady=(0, 8), anchor="w")

        nxm_frame = ctk.CTkFrame(body, fg_color="transparent")
        nxm_frame.pack(padx=16, pady=(4, 12), fill="x")

        self._nxm_status = ctk.CTkLabel(
            nxm_frame, text="", font=FONT_SMALL, text_color=TEXT_DIM,
        )
        self._nxm_status.pack(side="left", padx=(0, 12))

        ctk.CTkButton(
            nxm_frame, text="Register", width=100, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            command=self._on_register_nxm,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            nxm_frame, text="Unregister", width=100, font=FONT_BOLD,
            fg_color="#8b1a1a", hover_color="#b22222", text_color="white",
            command=self._on_unregister_nxm,
        ).pack(side="left")

        self._update_nxm_status()

    # -- Show/hide key -------------------------------------------------------

    def _toggle_show(self):
        self._key_entry.configure(show="" if self._show_var.get() else "•")

    # -- Validate ------------------------------------------------------------

    def _on_validate(self):
        key = self._key_var.get().strip()
        if not key:
            self._set_status("Enter an API key first.", TEXT_WARN)
            return
        self._set_status("Validating...", TEXT_DIM)

        def _worker():
            try:
                api = NexusAPI(api_key=key)
                user = api.validate()
                premium = " (Premium)" if user.is_premium else ""
                self.after(0, lambda: self._set_status(f"✓ Valid — {user.name}{premium}", TEXT_OK))
            except NexusAPIError as exc:
                self.after(0, lambda: self._set_status(f"✗ {exc}", TEXT_ERR))
            except Exception as exc:
                self.after(0, lambda: self._set_status(f"✗ Error: {exc}", TEXT_ERR))

        threading.Thread(target=_worker, daemon=True).start()

    # -- Save / clear --------------------------------------------------------

    def _on_save(self):
        key = self._key_var.get().strip()
        if not key:
            self._set_status("Nothing to save — key is empty.", TEXT_WARN)
            return
        try:
            save_api_key(key)
        except RuntimeError:
            self._show_keyring_error_popup()
            self._set_status("Save failed — no keyring available.", TEXT_ERR)
            return
        self._key_changed = True
        self._set_status("Key saved.", TEXT_OK)

    def _show_keyring_error_popup(self):
        """Show a themed popup explaining that no keyring backend is available."""
        # Re-use the same implementation as NexusSettingsDialog but parent to toplevel
        parent = self.winfo_toplevel()
        W, H = 560, 500
        dlg = ctk.CTkToplevel(parent, fg_color=BG_DEEP)
        dlg.title("No Keyring Available")
        dlg.resizable(False, False)
        dlg.transient(parent)
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - W) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - H) // 2
            dlg.geometry(f"{W}x{H}+{x}+{y}")
        except Exception:
            dlg.geometry(f"{W}x{H}")

        bar = ctk.CTkFrame(dlg, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x", side="top")
        ctk.CTkButton(bar, text="OK", width=90, height=32, font=FONT_BOLD,
                      fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
                      command=dlg.destroy).pack(side="right", padx=12, pady=10)

        scroll = ctk.CTkScrollableFrame(dlg, fg_color=BG_DEEP,
                                        scrollbar_button_color=BG_PANEL,
                                        scrollbar_button_hover_color=BG_HOVER)
        scroll.pack(fill="both", expand=True)
        scroll.grid_columnconfigure(0, weight=1)

        def _fwd_scroll(event):
            scroll._parent_canvas.yview_scroll(-1 if event.num == 4 else 1, "units")
        dlg.bind_all("<Button-4>", _fwd_scroll)
        dlg.bind_all("<Button-5>", _fwd_scroll)
        dlg.bind("<Destroy>", lambda e: (
            dlg.unbind_all("<Button-4>"), dlg.unbind_all("<Button-5>"),
        ) if e.widget is dlg else None)

        hdr = ctk.CTkFrame(scroll, fg_color="transparent")
        hdr.pack(fill="x", padx=20, pady=(16, 6))
        ctk.CTkLabel(hdr, text="✕", font=("Segoe UI", 22, "bold"),
                     text_color=TEXT_ERR, width=32).pack(side="left", anchor="n", padx=(0, 10))
        ctk.CTkLabel(hdr, text="No keyring service found on your system.",
                     font=FONT_BOLD, text_color=TEXT_MAIN,
                     wraplength=460, justify="left").pack(side="left", anchor="n")

        ctk.CTkLabel(scroll,
                     text="Your API key could not be saved. A keyring backend is required to "
                          "securely store credentials. Install and enable one for your distribution:",
                     font=FONT_NORMAL, text_color=TEXT_DIM,
                     wraplength=500, justify="left").pack(anchor="w", padx=20, pady=(0, 10))

        _CMD_FONT = ("Courier New", 11)

        def _make_cmd_row(p, cmd_text, is_comment):
            if is_comment:
                ctk.CTkLabel(p, text=cmd_text, font=_CMD_FONT,
                             text_color=TEXT_DIM, anchor="w").pack(anchor="w", padx=12, pady=(4, 0))
                return
            row = ctk.CTkFrame(p, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=(4, 0))
            row.grid_columnconfigure(0, weight=1)
            entry = tk.Entry(row, readonlybackground=BG_ROW, bg=BG_ROW, fg=TEXT_MAIN,
                             font=_CMD_FONT, relief="flat", bd=0, state="readonly",
                             cursor="xterm", highlightthickness=1, highlightbackground=BORDER,
                             insertbackground=TEXT_MAIN, disabledforeground=TEXT_MAIN)
            entry.configure(state="normal")
            entry.insert(0, cmd_text)
            entry.configure(state="readonly")
            entry.grid(row=0, column=0, sticky="ew", ipady=5, padx=(2, 4))
            entry.bind("<Button-4>", lambda e: scroll._parent_canvas.yview_scroll(-1, "units"))
            entry.bind("<Button-5>", lambda e: scroll._parent_canvas.yview_scroll( 1, "units"))
            copy_btn = ctk.CTkButton(row, text="Copy", width=58, height=28, font=FONT_SMALL,
                                     fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
                                     corner_radius=4)
            copy_btn.grid(row=0, column=1, sticky="e", padx=(0, 2))
            def _copy(btn=copy_btn, text=cmd_text):
                dlg.clipboard_clear(); dlg.clipboard_append(text); dlg.update()
                btn.configure(text="Copied!")
                dlg.after(1500, lambda: btn.configure(text="Copy"))
            copy_btn.configure(command=_copy)

        distros = [
            ("Arch Linux / SteamOS", [
                ("sudo pacman -S gnome-keyring libsecret", False),
                ("systemctl --user enable --now gnome-keyring-daemon", False),
            ]),
            ("Ubuntu / Debian / Linux Mint", [
                ("sudo apt install gnome-keyring libsecret-1-0", False),
                ("# Log out and back in, or run:", True),
                ("dbus-run-session -- gnome-keyring-daemon --unlock", False),
            ]),
            ("Fedora / RHEL / CentOS", [
                ("sudo dnf install gnome-keyring libsecret", False),
                ("systemctl --user enable --now gnome-keyring-daemon", False),
            ]),
            ("openSUSE", [
                ("sudo zypper install gnome-keyring libsecret-1-0", False),
                ("systemctl --user enable --now gnome-keyring-daemon", False),
            ]),
            ("Generic fallback (any distro)", [
                ("pip install keyring secretstorage", False),
                ("# Or install KWallet if on a KDE desktop.", True),
            ]),
        ]
        for section_title, cmds in distros:
            ctk.CTkLabel(scroll, text=section_title, font=FONT_BOLD,
                         text_color=TEXT_MAIN, anchor="w").pack(anchor="w", padx=20, pady=(6, 2))
            block = ctk.CTkFrame(scroll, fg_color=BG_PANEL, corner_radius=6)
            block.pack(fill="x", padx=20, pady=(0, 8))
            for cmd_text, is_comment in cmds:
                _make_cmd_row(block, cmd_text, is_comment)
            ctk.CTkFrame(block, fg_color="transparent", height=6).pack()

        ctk.CTkLabel(scroll, text="After installing, restart the application and try again.",
                     font=FONT_SMALL, text_color=TEXT_DIM,
                     wraplength=500, justify="left").pack(anchor="w", padx=20, pady=(0, 14))

        dlg.after(50, dlg.grab_set)
        dlg.wait_window()

    def _on_clear(self):
        clear_api_key()
        self._key_var.set("")
        self._key_changed = True
        self._set_status("Key cleared.", TEXT_WARN)

    # -- NXM handler ---------------------------------------------------------

    def _on_register_nxm(self):
        ok = NxmHandler.register()
        if ok:
            self._set_status("NXM handler registered.", TEXT_OK)
        else:
            self._set_status("Failed to register — xdg-mime not found?", TEXT_ERR)
        self._update_nxm_status()

    def _on_unregister_nxm(self):
        NxmHandler.unregister()
        self._set_status("NXM handler unregistered.", TEXT_WARN)
        self._update_nxm_status()

    def _update_nxm_status(self):
        if NxmHandler.is_registered():
            self._nxm_status.configure(text="Status: Registered ✓", text_color=TEXT_OK)
        else:
            self._nxm_status.configure(text="Status: Not registered", text_color=TEXT_DIM)

    # -- API Rate Limit ------------------------------------------------------

    def _get_nexus_api(self) -> Optional[NexusAPI]:
        return self._nexus_api_getter()

    def _format_rate_limits(self, r: NexusRateLimits) -> str:
        def _line(label, remaining, limit):
            if remaining < 0 or limit < 0:
                return f"{label}: —"
            return f"{label}: {remaining:,} / {limit:,} remaining"
        return f"{_line('Hourly', r.hourly_remaining, r.hourly_limit)}  ·  {_line('Daily', r.daily_remaining, r.daily_limit)}"

    def _update_rate_limit_display(self) -> None:
        api = self._get_nexus_api()
        if api is None:
            self._rate_limit_label.configure(
                text="Save and validate your API key, then click Refresh.", text_color=TEXT_DIM)
            return
        r = api.rate_limits
        if r.hourly_remaining < 0 and r.daily_remaining < 0:
            self._rate_limit_label.configure(
                text="No data yet. Click Refresh to check your quota.", text_color=TEXT_DIM)
            return
        self._rate_limit_label.configure(text=self._format_rate_limits(r), text_color=TEXT_MAIN)

    def _on_refresh_rate_limits(self) -> None:
        api = self._get_nexus_api()
        if api is None:
            self._set_status("Set and save your API key first.", TEXT_WARN)
            return
        self._rate_refresh_btn.configure(state="disabled", text="Refreshing...")
        self._set_status("Checking API rate limits...", TEXT_DIM)

        def _worker():
            err: Optional[Exception] = None
            try:
                api.refresh_rate_limits()
            except Exception as e:
                err = e
            def _on_done():
                self._rate_refresh_btn.configure(state="normal", text="Refresh")
                if err is not None:
                    self._set_status(f"✗ {err}", TEXT_ERR)
                else:
                    self._set_status("Rate limits updated.", TEXT_OK)
                self._update_rate_limit_display()
            self.after(0, _on_done)

        threading.Thread(target=_worker, daemon=True).start()

    # -- Helpers -------------------------------------------------------------

    def _set_status(self, text: str, color: str = TEXT_DIM):
        self._status_label.configure(text=text, text_color=color)

    # -- OAuth login ---------------------------------------------------------

    def _on_sso_login(self):
        self._sso_btn.configure(state="disabled", text="Waiting...")
        self._sso_cancel_btn.pack(side="left", padx=(4, 8), pady=8)
        self._set_status("Starting browser login...", TEXT_DIM)
        self._oauth_client = NexusOAuthClient(
            on_token=self._oauth_on_token,
            on_error=self._oauth_on_error,
            on_status=self._oauth_on_status,
        )
        self._oauth_client.start()

    def _on_sso_cancel(self):
        if self._oauth_client:
            self._oauth_client.cancel()
            self._oauth_client = None
        self._sso_btn.configure(state="normal" if CLIENT_ID else "disabled",
                                text="Log in via Nexus Mods")
        self._sso_cancel_btn.pack_forget()
        self._set_status("Login cancelled.", TEXT_WARN)

    def _oauth_on_token(self, tokens: OAuthTokens):
        def _update():
            self._key_changed = True
            self._sso_btn.configure(state="normal" if CLIENT_ID else "disabled",
                                    text="Log in via Nexus Mods")
            self._sso_cancel_btn.pack_forget()
            self._set_status("✓ Logged in via Nexus Mods!", TEXT_OK)
            self._validate_oauth(tokens)
        self.after(0, _update)

    def _validate_oauth(self, tokens: OAuthTokens):
        def _worker():
            try:
                api = NexusAPI.from_oauth(tokens)
                user = api.validate()
                premium = " (Premium)" if user.is_premium else ""
                self.after(0, lambda: self._set_status(f"✓ Logged in as {user.name}{premium}", TEXT_OK))
            except Exception as exc:
                self.after(0, lambda: self._set_status(
                    f"✓ Logged in (could not fetch user info: {exc})", TEXT_OK))
        threading.Thread(target=_worker, daemon=True).start()

    def _oauth_on_error(self, msg: str):
        def _update():
            self._sso_btn.configure(state="normal" if CLIENT_ID else "disabled",
                                    text="Log in via Nexus Mods")
            self._sso_cancel_btn.pack_forget()
            self._set_status(f"✗ Login failed: {msg}", TEXT_ERR)
        self.after(0, _update)

    def _oauth_on_status(self, msg: str):
        self.after(0, lambda: self._set_status(msg, TEXT_DIM))

    def _on_close(self):
        if self._oauth_client and self._oauth_client.is_running:
            self._oauth_client.cancel()
        if self._key_changed and self._on_key_changed:
            self._on_key_changed()
        self.result = self._key_changed
        self._on_done(self)
