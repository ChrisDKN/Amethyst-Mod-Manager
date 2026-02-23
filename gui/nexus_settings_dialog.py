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
from typing import Optional

import customtkinter as ctk
import tkinter as tk

from Nexus.nexus_api import NexusAPI, NexusAPIError, load_api_key, save_api_key, clear_api_key
from Nexus.nexus_sso import NexusSSOClient
from Nexus.nxm_handler import NxmHandler

# ---------------------------------------------------------------------------
# Colors / fonts (kept in sync with gui.py)
# ---------------------------------------------------------------------------
BG_DEEP    = "#1a1a1a"
BG_PANEL   = "#252526"
BG_HEADER  = "#2a2a2b"
BG_ROW     = "#2d2d2d"
BG_HOVER   = "#094771"
ACCENT     = "#0078d4"
ACCENT_HOV = "#1084d8"
TEXT_MAIN  = "#d4d4d4"
TEXT_DIM   = "#858585"
TEXT_OK    = "#98c379"
TEXT_ERR   = "#e06c75"
TEXT_WARN  = "#e5c07b"
BORDER     = "#444444"

FONT_NORMAL = ("Segoe UI", 12)
FONT_BOLD   = ("Segoe UI", 12, "bold")
FONT_SMALL  = ("Segoe UI", 10)
FONT_MONO   = ("Courier New", 11)


class NexusSettingsDialog(ctk.CTkToplevel):
    """
    Modal dialog for Nexus Mods API key management.

    Usage:
        dialog = NexusSettingsDialog(parent, on_key_changed=callback)
        parent.wait_window(dialog)
        # dialog.result is True if the key was changed, False/None otherwise
    """

    WIDTH  = 520
    HEIGHT = 500

    def __init__(self, parent, on_key_changed=None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Nexus Mods Settings")
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._on_key_changed = on_key_changed
        self.result: Optional[bool] = None
        self._key_changed = False
        self._sso_client: Optional[NexusSSOClient] = None

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

        # -- SSO Login --
        sso_frame = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=6)
        sso_frame.pack(padx=16, pady=(0, 6), fill="x")

        ctk.CTkLabel(
            sso_frame, text="Browser Login (SSO)",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(side="left", padx=(8, 12), pady=8)

        self._sso_btn = ctk.CTkButton(
            sso_frame, text="Log in via Nexus Mods", width=180, font=FONT_BOLD,
            fg_color="#d98f40", hover_color="#e5a04d", text_color="white",
            command=self._on_sso_login,
        )
        self._sso_btn.pack(side="left", padx=4, pady=8)

        self._sso_cancel_btn = ctk.CTkButton(
            sso_frame, text="Cancel", width=70, font=FONT_SMALL,
            fg_color="#8b1a1a", hover_color="#b22222", text_color="white",
            command=self._on_sso_cancel,
        )
        # hidden by default; shown only while SSO is in progress

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
        save_api_key(key)
        self._key_changed = True
        self._set_status("Key saved.", TEXT_OK)

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

    # -- Helpers ------------------------------------------------------------

    def _set_status(self, text: str, color: str = TEXT_DIM):
        self._status_label.configure(text=text, text_color=color)

    # -- SSO login ----------------------------------------------------------

    def _on_sso_login(self):
        """Start the SSO flow."""
        self._sso_btn.configure(state="disabled", text="Waiting...")
        self._sso_cancel_btn.pack(side="left", padx=(4, 8), pady=8)
        self._set_status("Starting SSO login...", TEXT_DIM)

        self._sso_client = NexusSSOClient(
            on_api_key=self._sso_on_key,
            on_error=self._sso_on_error,
            on_status=self._sso_on_status,
        )
        self._sso_client.start()

    def _on_sso_cancel(self):
        """Cancel a running SSO flow."""
        if self._sso_client:
            self._sso_client.cancel()
            self._sso_client = None
        self._sso_btn.configure(state="normal", text="Log in via Nexus Mods")
        self._sso_cancel_btn.pack_forget()
        self._set_status("SSO login cancelled.", TEXT_WARN)

    def _sso_on_key(self, api_key: str):
        """Called from SSO thread when the API key is received."""
        def _update():
            save_api_key(api_key)
            self._key_var.set(api_key)
            self._key_changed = True
            self._sso_btn.configure(state="normal", text="Log in via Nexus Mods")
            self._sso_cancel_btn.pack_forget()
            self._set_status("✓ Logged in via SSO — API key saved!", TEXT_OK)
        self.after(0, _update)

    def _sso_on_error(self, msg: str):
        """Called from SSO thread on error."""
        def _update():
            self._sso_btn.configure(state="normal", text="Log in via Nexus Mods")
            self._sso_cancel_btn.pack_forget()
            self._set_status(f"✗ SSO: {msg}", TEXT_ERR)
        self.after(0, _update)

    def _sso_on_status(self, msg: str):
        """Called from SSO thread with status updates."""
        self.after(0, lambda: self._set_status(msg, TEXT_DIM))

    def _on_close(self):
        # Cancel any active SSO flow
        if self._sso_client and self._sso_client.is_running:
            self._sso_client.cancel()
        if self._key_changed and self._on_key_changed:
            self._on_key_changed()
        self.result = self._key_changed
        self.grab_release()
        self.destroy()
