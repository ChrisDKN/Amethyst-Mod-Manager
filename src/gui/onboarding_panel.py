"""
onboarding_panel.py
First-run onboarding overlay, shown when no games are configured.

Pages:
  0 — Welcome  (Next button)
  1 — Nexus Mods login (optional, skippable; skip becomes Next after login)
  2 — Add a game (opens the game picker)
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk

from Nexus.nexus_oauth import NexusOAuthClient, OAuthTokens, CLIENT_ID
from Utils.config_paths import get_profiles_dir
from Utils.ui_config import load_default_staging_path, save_default_staging_path
from Utils.xdg import open_url
from gui.theme import (
    BG_DEEP,
    BG_PANEL,
    BG_HEADER,
    BG_HOVER,
    ACCENT,
    ACCENT_HOV,
    TEXT_ON_ACCENT,
    TEXT_MAIN,
    TEXT_DIM,
    TEXT_OK,
    TEXT_ERR,
    BORDER,
    FONT_NORMAL,
    FONT_BOLD,
    FONT_SMALL,
    load_icon,
    scaled,
)

_TOTAL_PAGES = 3


class OnboardingPanel(ctk.CTkFrame):
    """
    Full-area overlay shown on first launch (no games configured).
    Placed over the mod-list container via place(relx=0, rely=0, relwidth=1, relheight=1).
    """

    def __init__(
        self,
        parent,
        on_nexus_key_changed: Optional[Callable] = None,
        on_add_game: Optional[Callable] = None,
        on_done: Optional[Callable] = None,
        already_logged_in: bool = False,
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_nexus_key_changed = on_nexus_key_changed or (lambda: None)
        self._on_add_game = on_add_game or (lambda: None)
        self._on_done = on_done or (lambda: None)
        self._already_logged_in = already_logged_in

        self._page = 0
        self._logged_in = already_logged_in
        self._oauth_client: Optional[NexusOAuthClient] = None

        # Page frames — built lazily
        self._page_frames: dict[int, ctk.CTkFrame] = {}

        self.grid_rowconfigure(0, weight=0)   # header
        self.grid_rowconfigure(1, weight=1)   # content
        self.grid_rowconfigure(2, weight=0)   # footer
        self.grid_columnconfigure(0, weight=1)

        self._build_header()
        self._content_host = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self._content_host.grid(row=1, column=0, sticky="nsew")
        self._content_host.grid_rowconfigure(0, weight=1)
        self._content_host.grid_columnconfigure(0, weight=1)
        self._build_footer()

        # If already logged in, skip the Nexus page (go straight to welcome)
        self._show_page(0)

    # ------------------------------------------------------------------ header

    def _build_header(self):
        bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=scaled(48))
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)

        ctk.CTkLabel(
            bar,
            text="Welcome to Amethyst Mod Manager",
            font=FONT_BOLD,
            text_color=TEXT_MAIN,
        ).pack(side="left", padx=16, pady=0)

        self._step_label = ctk.CTkLabel(
            bar,
            text=f"Step 1 of {_TOTAL_PAGES}",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
        )
        self._step_label.pack(side="right", padx=16, pady=0)

    # ------------------------------------------------------------------ footer

    def _build_footer(self):
        foot = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=scaled(52))
        foot.grid(row=2, column=0, sticky="ew")
        foot.grid_propagate(False)
        ctk.CTkFrame(foot, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x", side="top")

        self._footer_btn = ctk.CTkButton(
            foot,
            text="Next →",
            width=scaled(100),
            font=FONT_BOLD,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            text_color=TEXT_ON_ACCENT,
            command=self._on_footer_btn,
        )
        self._footer_btn.pack(side="right", padx=12, pady=10)

        self._prev_btn = ctk.CTkButton(
            foot,
            text="← Back",
            width=scaled(100),
            font=FONT_NORMAL,
            fg_color=BG_PANEL,
            hover_color=BG_HOVER,
            text_color=TEXT_DIM,
            command=self._on_prev_btn,
        )
        self._prev_btn.pack(side="left", padx=12, pady=10)

    # ------------------------------------------------------------------ pages

    def _show_page(self, page: int):
        for frame in self._page_frames.values():
            frame.grid_forget()

        self._page = page
        self._step_label.configure(text=f"Step {page + 1} of {_TOTAL_PAGES}")

        # Hide back button on first page
        if page == 0:
            self._prev_btn.pack_forget()
        else:
            self._prev_btn.pack(side="left", padx=12, pady=10)

        if page == 0:
            self._footer_btn.configure(
                text="Next →", text_color="white",
                fg_color=ACCENT, hover_color=ACCENT_HOV,
            )
        elif page == 1:
            # Nexus page — starts as Skip, upgrades to Next on login
            if self._logged_in:
                self._footer_btn.configure(
                    text="Next →", text_color=TEXT_ON_ACCENT,
                    fg_color=ACCENT, hover_color=ACCENT_HOV,
                )
            else:
                self._footer_btn.configure(
                    text="Skip", text_color=TEXT_DIM,
                    fg_color=BG_PANEL, hover_color=BG_HOVER,
                )
        else:
            # Last page — footer button becomes Skip (closes without adding game)
            self._footer_btn.configure(
                text="Skip", text_color=TEXT_DIM,
                fg_color=BG_PANEL, hover_color=BG_HOVER,
            )

        if page not in self._page_frames:
            builders = {
                0: self._build_page_welcome,
                1: self._build_page_nexus,
                2: self._build_page_add_game,
            }
            self._page_frames[page] = builders[page](self._content_host)

        self._page_frames[page].grid(row=0, column=0, sticky="nsew")

    # ---------------------------------------------------------------- page 0 — welcome

    def _build_page_welcome(self, parent) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(frame, fg_color=BG_PANEL, corner_radius=10)
        inner.grid(row=0, column=0, padx=scaled(60), pady=scaled(40), sticky="nsew")
        inner.grid_rowconfigure(0, weight=1)
        inner.grid_columnconfigure(0, weight=1)

        body = ctk.CTkFrame(inner, fg_color="transparent")
        body.grid(row=0, column=0, sticky="", padx=scaled(40), pady=scaled(30))

        # Logo
        self._logo_img = load_icon("Logo.png", size=(120, 120))
        if self._logo_img:
            ctk.CTkLabel(body, image=self._logo_img, text="").pack(pady=(0, 20))

        ctk.CTkLabel(
            body,
            text="Welcome to Amethyst Mod Manager",
            font=FONT_BOLD,
            text_color=TEXT_MAIN,
        ).pack(pady=(0, 10))

        ctk.CTkLabel(
            body,
            text="See the wiki for guides on how to use the Manager",
            font=FONT_NORMAL,
            text_color=TEXT_DIM,
            justify="center",
            wraplength=scaled(460),
        ).pack(pady=(0, 20))

        ctk.CTkButton(
            body,
            text="Open Wiki",
            width=scaled(160),
            height=scaled(34),
            font=FONT_BOLD,
            fg_color="#d98f40",
            hover_color="#e5a04d",
            text_color="white",
            command=lambda: open_url("https://github.com/ChrisDKN/Amethyst-Mod-Manager/wiki"),
        ).pack()

        return frame

    # ---------------------------------------------------------------- page 1 — nexus

    def _build_page_nexus(self, parent) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(frame, fg_color=BG_PANEL, corner_radius=10)
        inner.grid(row=0, column=0, padx=scaled(60), pady=scaled(40), sticky="nsew")
        inner.grid_rowconfigure(0, weight=1)
        inner.grid_columnconfigure(0, weight=1)

        body = ctk.CTkFrame(inner, fg_color="transparent")
        body.grid(row=0, column=0, sticky="", padx=scaled(40), pady=scaled(30))

        # Nexus icon
        self._nexus_img = load_icon("nexus.png", size=(80, 80))
        if self._nexus_img:
            ctk.CTkLabel(body, image=self._nexus_img, text="").pack(pady=(0, 16))

        ctk.CTkLabel(
            body,
            text="Connect to Nexus Mods",
            font=FONT_BOLD,
            text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))

        ctk.CTkLabel(
            body,
            text=(
                "Logging in lets you browse and download mods directly within the app.\n"
                "You can skip this and connect later from the Nexus button in the toolbar."
            ),
            font=FONT_NORMAL,
            text_color=TEXT_DIM,
            justify="center",
            wraplength=scaled(480),
        ).pack(pady=(0, 24))

        self._sso_btn = ctk.CTkButton(
            body,
            text="Log in via Nexus Mods",
            width=scaled(220),
            height=scaled(38),
            font=FONT_BOLD,
            fg_color="#d98f40",
            hover_color="#e5a04d",
            text_color="white",
            command=self._on_sso_login,
        )
        self._sso_btn.pack(pady=(0, 8))
        if not CLIENT_ID:
            self._sso_btn.configure(state="disabled")

        self._sso_cancel_btn = ctk.CTkButton(
            body,
            text="Cancel login",
            width=scaled(140),
            font=FONT_SMALL,
            fg_color="#8b1a1a",
            hover_color="#b22222",
            text_color="white",
            command=self._on_sso_cancel,
        )
        # shown only while OAuth is in-flight

        self._nexus_status = ctk.CTkLabel(
            body,
            text="",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
            wraplength=scaled(460),
            justify="center",
        )
        self._nexus_status.pack(pady=(4, 0))

        return frame

    def _on_sso_login(self):
        self._sso_btn.configure(state="disabled", text="Waiting for browser...")
        self._sso_cancel_btn.pack(pady=(8, 0))
        self._set_nexus_status("Browser login started — complete it in your browser.", TEXT_DIM)
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
        self._sso_btn.configure(
            state="normal" if CLIENT_ID else "disabled",
            text="Log in via Nexus Mods",
        )
        self._sso_cancel_btn.pack_forget()
        self._set_nexus_status("Login cancelled.", TEXT_DIM)

    def _oauth_on_token(self, tokens: OAuthTokens):
        def _update():
            if self._oauth_client:
                self._oauth_client = None
            self._logged_in = True
            self._sso_btn.configure(
                state="normal" if CLIENT_ID else "disabled",
                text="Log in via Nexus Mods",
            )
            self._sso_cancel_btn.pack_forget()
            self._set_nexus_status("✓ Logged in to Nexus Mods!", TEXT_OK)
            self._on_nexus_key_changed()
            # Upgrade footer button from Skip → Next
            self._footer_btn.configure(
                text="Next →",
                text_color="white",
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
            )
            self._fetch_nexus_username(tokens)
        self.after(0, _update)

    def _fetch_nexus_username(self, tokens: OAuthTokens):
        def _worker():
            try:
                import requests as _req
                resp = _req.get(
                    "https://users.nexusmods.com/oauth/userinfo",
                    headers={"Authorization": f"Bearer {tokens.access_token}"},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                name = (data.get("name") or data.get("preferred_username")
                        or data.get("username") or data.get("sub", ""))
                self.after(0, lambda: self._set_nexus_status(f"✓ Logged in as {name}", TEXT_OK))
            except Exception:
                pass
        threading.Thread(target=_worker, daemon=True).start()

    def _oauth_on_error(self, msg: str):
        def _update():
            self._sso_btn.configure(
                state="normal" if CLIENT_ID else "disabled",
                text="Log in via Nexus Mods",
            )
            self._sso_cancel_btn.pack_forget()
            self._set_nexus_status(f"✗ Login failed: {msg}", TEXT_ERR)
        self.after(0, _update)

    def _oauth_on_status(self, msg: str):
        self.after(0, lambda: self._set_nexus_status(msg, TEXT_DIM))

    def _set_nexus_status(self, text: str, color: str = TEXT_DIM):
        if self._nexus_status:
            self._nexus_status.configure(text=text, text_color=color)

    # ---------------------------------------------------------------- page 2 — add game

    def _build_page_add_game(self, parent) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(frame, fg_color=BG_PANEL, corner_radius=10)
        inner.grid(row=0, column=0, padx=scaled(60), pady=scaled(40), sticky="nsew")
        inner.grid_rowconfigure(0, weight=1)
        inner.grid_columnconfigure(0, weight=1)

        body = ctk.CTkFrame(inner, fg_color="transparent")
        body.grid(row=0, column=0, sticky="", padx=scaled(40), pady=scaled(30))

        # --- Default mod staging folder ---
        ctk.CTkLabel(
            body,
            text="Default Mod Staging Folder",
            font=FONT_BOLD,
            text_color=TEXT_MAIN,
        ).pack(pady=(0, 4))

        default_root = get_profiles_dir()
        ctk.CTkLabel(
            body,
            text=f"Default: {default_root}",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
            justify="center",
            wraplength=scaled(480),
        ).pack(pady=(0, 6))

        staging_row = ctk.CTkFrame(body, fg_color="transparent")
        staging_row.pack(pady=(0, 4))

        self._onb_staging_var = tk.StringVar(value=load_default_staging_path())
        ctk.CTkEntry(
            staging_row, textvariable=self._onb_staging_var,
            font=FONT_NORMAL, width=scaled(340),
            placeholder_text="Leave blank to use the default",
            height=scaled(28),
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            staging_row, text="Browse", width=scaled(70), height=scaled(28),
            font=FONT_NORMAL, fg_color=BG_HOVER, hover_color=ACCENT, text_color=TEXT_MAIN,
            command=self._browse_staging,
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            staging_row, text="Clear", width=scaled(56), height=scaled(28),
            font=FONT_NORMAL, fg_color=BG_DEEP, hover_color=BG_HOVER, text_color=TEXT_DIM,
            command=lambda: self._onb_staging_var.set(""),
        ).pack(side="left")

        ctk.CTkLabel(
            body,
            text="When set, new games will use <this>/<game name> as their\n"
                 "mod staging folder. You can change this later in Settings.",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
            justify="center",
            wraplength=scaled(480),
        ).pack(pady=(0, 24))

        ctk.CTkLabel(
            body,
            text="Add Your First Game",
            font=FONT_BOLD,
            text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))

        ctk.CTkLabel(
            body,
            text="Select a game to manage.",
            font=FONT_NORMAL,
            text_color=TEXT_DIM,
            justify="center",
            wraplength=scaled(480),
        ).pack(pady=(0, 20))

        ctk.CTkButton(
            body,
            text="Add a Game",
            width=scaled(200),
            height=scaled(38),
            font=FONT_BOLD,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            text_color=TEXT_ON_ACCENT,
            command=self._on_add_game_clicked,
        ).pack()

        return frame

    def _browse_staging(self):
        from Utils.portal_filechooser import pick_folder

        def _on_chosen(chosen):
            if chosen:
                try:
                    self._onb_staging_var.set(str(chosen))
                except Exception:
                    pass

        pick_folder("Select Default Mod Staging Folder", _on_chosen)

    def _on_add_game_clicked(self):
        try:
            save_default_staging_path(self._onb_staging_var.get())
        except Exception:
            pass
        if self._oauth_client:
            self._oauth_client.cancel()
            self._oauth_client = None
        self._on_done()
        self._on_add_game()

    # ---------------------------------------------------------------- footer nav

    def _on_prev_btn(self):
        if self._page > 0:
            target = self._page - 1
            # If already logged in, skip the Nexus page going backwards too
            if target == 1 and self._already_logged_in:
                target = 0
            self._show_page(target)

    def _on_footer_btn(self):
        if self._page == 0:
            # Welcome → Nexus (or skip to add-game if already logged in)
            self._show_page(2 if self._already_logged_in else 1)
        elif self._page == 1:
            # Nexus → Add game (whether skipping or advancing after login)
            if self._oauth_client:
                self._oauth_client.cancel()
                self._oauth_client = None
            self._show_page(2)
        else:
            # Skip on last page — dismiss without adding a game
            self._on_done()

    # ---------------------------------------------------------------- cleanup

    def destroy(self):
        if self._oauth_client:
            try:
                self._oauth_client.cancel()
            except Exception:
                pass
            self._oauth_client = None
        super().destroy()
