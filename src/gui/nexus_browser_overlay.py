"""
Nexus browser overlay — Browse, Tracked, Endorsed, Trending mods from Nexus Mods.

Embeds inside the ModListPanel area as an overlay (like Collections).
Shows when the Nexus toolbar button is pressed.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
from Utils.xdg import open_url
from Utils.config_paths import get_download_cache_dir

from gui.tracked_mods_panel import TrackedModsPanel
from gui.endorsed_mods_panel import EndorsedModsPanel
from gui.browse_mods_panel import BrowseModsPanel
from gui.trending_mods_panel import TrendingModsPanel
from gui.install_mod import install_mod_from_archive
from Nexus.nexus_meta import build_meta_from_download, scan_installed_mods
from Nexus.nexus_download import delete_archive_and_sidecar

from gui.theme import (
    BG_DEEP,
    BG_HEADER,
    BG_PANEL,
    ACCENT,
    ACCENT_HOV,
    TEXT_MAIN,
    TEXT_DIM,
    FONT_BOLD,
    FONT_NORMAL,
)


def install_nexus_mod_from_entry(app, api, game, mod_panel, log_fn, entry,
                                 label: str = "Missing Requirements"):
    """Download and install a mod from Nexus by entry (mod_id, domain_name, name).
    Used by missing-requirements panel and Nexus browser overlay.
    """
    domain = getattr(entry, "domain_name", "") or ""
    mod_id = getattr(entry, "mod_id", 0)
    mod_name = getattr(entry, "name", "") or f"Mod {mod_id}"

    if api is None:
        log_fn(f"{label}: Login to Nexus first")
        return
    if game is None or not game.is_configured():
        log_fn(f"{label}: No configured game selected.")
        return

    cancel_ev = mod_panel.get_download_cancel_event() if mod_panel else None
    if mod_panel:
        mod_panel.show_download_progress(f"Installing: {mod_name}", cancel=cancel_ev)

    def _worker():
        downloader = getattr(app, "_nexus_downloader", None)
        if downloader is None:
            app.after(0, lambda: (
                mod_panel.hide_download_progress(cancel=cancel_ev) if mod_panel else None,
                log_fn(f"{label}: Downloader not initialised."),
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
                    mod_panel.hide_download_progress(cancel=cancel_ev)
                log_fn(f"{label}: Premium required for direct download.")
                log_fn(f'{label}: Opening files page — click "Download with Mod Manager" there.')
                log_fn(f"{label}: {files_url}")
                try:
                    open_url(files_url)
                except Exception as exc:
                    log_fn(f"{label}: Could not open browser — {exc}")
            app.after(0, _fallback)
            return

        file_info = None
        try:
            files_resp = api.get_mod_files(domain, mod_id)
            main_files = [f for f in files_resp.files if f.category_name == "MAIN"]
            if main_files:
                file_info = max(main_files, key=lambda f: f.uploaded_timestamp)
            elif files_resp.files:
                file_info = max(files_resp.files, key=lambda f: f.uploaded_timestamp)
        except Exception as exc:
            app.after(0, lambda: (
                mod_panel.hide_download_progress(cancel=cancel_ev) if mod_panel else None,
                log_fn(f"{label}: Could not fetch file list — {exc}"),
            ))
            return

        if file_info is None:
            app.after(0, lambda: (
                mod_panel.hide_download_progress(cancel=cancel_ev) if mod_panel else None,
                log_fn(f"{label}: No files found for '{mod_name}'."),
            ))
            return

        mod_info_for_meta = None
        try:
            mod_info_for_meta, _ = api.get_mod_and_file_info_graphql(
                domain, mod_id, file_info.file_id
            )
        except Exception:
            pass

        result = downloader.download_file(
            game_domain=domain,
            mod_id=mod_id,
            file_id=file_info.file_id,
            progress_cb=lambda cur, total: app.after(
                0, lambda c=cur, t=total: (
                    mod_panel.update_download_progress(c, t, cancel=cancel_ev)
                    if mod_panel else None
                )
            ),
            cancel=cancel_ev,
            known_file_name=file_info.file_name,
            dest_dir=get_download_cache_dir(),
        )

        if result.success and result.file_path:
            _archive_path = result.file_path

            def _install():
                try:
                    if app.grab_current() is not None:
                        app.after(500, _install)
                        return
                except Exception:
                    pass
                if mod_panel:
                    mod_panel.hide_download_progress(cancel=cancel_ev)
                log_fn(f"{label}: Installing '{mod_name}'...")
                try:
                    _mod_info = mod_info_for_meta if mod_info_for_meta is not None else entry
                    _prebuilt = build_meta_from_download(
                        game_domain=domain,
                        mod_id=mod_id,
                        file_id=file_info.file_id,
                        archive_name=result.file_name,
                        mod_info=_mod_info,
                        file_info=file_info,
                    )
                except Exception:
                    _prebuilt = None

                status_bar = getattr(app, "_status", None)

                def _extract_progress(done: int, total: int, phase: str | None = None):
                    if status_bar is not None:
                        app.after(0, lambda d=done, t=total, p=phase: status_bar.set_progress(d, t, p, title="Extracting"))

                def _cleanup():
                    delete_archive_and_sidecar(Path(_archive_path))

                def _install_worker():
                    try:
                        install_mod_from_archive(
                            str(_archive_path), app, log_fn, game, mod_panel,
                            prebuilt_meta=_prebuilt,
                            on_installed=_cleanup,
                            progress_fn=_extract_progress,
                            clear_progress_fn=lambda: app.after(0, status_bar.clear_progress) if status_bar is not None else None)
                    finally:
                        if status_bar is not None:
                            app.after(0, status_bar.clear_progress)

                threading.Thread(target=_install_worker, daemon=True).start()
            app.after(0, _install)
        else:
            app.after(0, lambda: (
                mod_panel.hide_download_progress(cancel=cancel_ev) if mod_panel else None,
                log_fn(f"{label}: Download failed — {result.error}"),
            ))

    log_fn(f"{label}: Installing '{mod_name}'...")
    threading.Thread(target=_worker, daemon=True).start()


class NexusBrowserOverlay(tk.Frame):
    """
    Browse / Tracked / Endorsed mods panel — embeds inside the ModListPanel area.
    """

    def __init__(
        self,
        parent: tk.Widget,
        game_domain: str,
        api,
        game=None,
        log_fn: Optional[Callable] = None,
        app_root: Optional[tk.Widget] = None,
        on_close: Optional[Callable] = None,
        on_open_settings: Optional[Callable] = None,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._game_domain = game_domain
        self._api = api
        self._game = game
        self._app_root = app_root or parent.winfo_toplevel()
        self._log = log_fn or (lambda msg: None)
        self._on_close = on_close
        self._on_open_settings = on_open_settings

        self._build()

    def _build(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = tk.Frame(self, bg=BG_HEADER, height=42)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        def _btn(name: str, cmd, w=85):
            return ctk.CTkButton(
                toolbar, text=name, width=w, height=30,
                fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
                font=FONT_BOLD, command=cmd,
            )

        self._browse_btn = _btn("Browse", lambda: self._show_panel("Browse"), 90)
        self._browse_btn.pack(side="left", padx=4, pady=5)
        self._tracked_btn = _btn("Tracked", lambda: self._show_panel("Tracked"), 95)
        self._tracked_btn.pack(side="left", padx=4, pady=5)
        self._endorsed_btn = _btn("Endorsed", lambda: self._show_panel("Endorsed"), 105)
        self._endorsed_btn.pack(side="left", padx=4, pady=5)
        self._trending_btn = _btn("Trending", lambda: self._show_panel("Trending"), 100)
        self._trending_btn.pack(side="left", padx=4, pady=5)

        if self._on_open_settings:
            ctk.CTkButton(
                toolbar, text="⚙ Settings", width=100, height=30,
                fg_color="#da8e35", hover_color="#e5a04a", text_color="white",
                font=FONT_BOLD, command=self._on_open_settings,
            ).pack(side="left", padx=(12, 6), pady=5)


        ctk.CTkButton(
            toolbar, text="✕ Close", width=85, height=36,
            fg_color="#6b3333", hover_color="#8c4444", text_color="white",
            font=FONT_BOLD, command=self._do_close,
        ).pack(side="right", padx=(6, 12), pady=5)

        # Content area — stacked panel frames
        content = tk.Frame(self, bg=BG_DEEP)
        content.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        def _get_api():
            return self._api

        def _get_game_domain():
            return self._game_domain

        def _get_installed_mod_ids():
            """Return set of Nexus mod IDs already installed for the current game."""
            game = self._game
            if game is None or not game.is_configured():
                return set()
            try:
                staging = game.get_effective_mod_staging_path()
                if not staging or not Path(staging).is_dir():
                    return set()
                installed = scan_installed_mods(Path(staging))
                domain = (self._game_domain or "").lower()
                return {
                    m.mod_id for m in installed
                    if m.mod_id > 0 and (not domain or (m.game_domain or "").lower() == domain)
                }
            except Exception:
                return set()

        self._panel_frames = {}
        for name in ("Browse", "Tracked", "Endorsed", "Trending"):
            frame = tk.Frame(content, bg=BG_DEEP)
            frame.grid(row=0, column=0, sticky="nsew")
            frame.grid_rowconfigure(1, weight=1)
            frame.grid_columnconfigure(0, weight=1)
            self._panel_frames[name] = frame

        self._tracked_panel = TrackedModsPanel(
            self._panel_frames["Tracked"],
            log_fn=self._log,
            get_api=_get_api,
            get_game_domain=_get_game_domain,
            install_fn=self._install_from_tracked,
            get_installed_mod_ids=_get_installed_mod_ids,
        )
        self._endorsed_panel = EndorsedModsPanel(
            self._panel_frames["Endorsed"],
            log_fn=self._log,
            get_api=_get_api,
            get_game_domain=_get_game_domain,
            install_fn=self._install_from_endorsed,
            get_installed_mod_ids=_get_installed_mod_ids,
        )
        self._browse_panel = BrowseModsPanel(
            self._panel_frames["Browse"],
            log_fn=self._log,
            get_api=_get_api,
            get_game_domain=_get_game_domain,
            install_fn=self._install_from_browse,
            get_installed_mod_ids=_get_installed_mod_ids,
        )
        self._trending_panel = TrendingModsPanel(
            self._panel_frames["Trending"],
            log_fn=self._log,
            get_api=_get_api,
            get_game_domain=_get_game_domain,
            install_fn=self._install_from_trending,
            get_installed_mod_ids=_get_installed_mod_ids,
        )

        # If not logged in, show a centered login prompt over the content area
        if self._api is None and self._on_open_settings:
            login_frame = tk.Frame(content, bg=BG_DEEP)
            login_frame.grid(row=0, column=0, sticky="nsew")
            login_frame.grid_rowconfigure(0, weight=1)
            login_frame.grid_columnconfigure(0, weight=1)

            inner = ctk.CTkFrame(login_frame, fg_color=BG_PANEL, corner_radius=10)
            inner.grid(row=0, column=0)

            ctk.CTkLabel(
                inner, text="You are not logged in to Nexus Mods.",
                font=FONT_BOLD, text_color=TEXT_MAIN,
            ).pack(padx=32, pady=(24, 6))
            ctk.CTkLabel(
                inner, text="Log in to browse, track, and download mods.",
                font=FONT_NORMAL, text_color=TEXT_DIM,
            ).pack(padx=32, pady=(0, 16))
            ctk.CTkButton(
                inner, text="Log in via Nexus Mods", width=200, height=36,
                fg_color="#d98f40", hover_color="#e5a04d", text_color="white",
                font=FONT_BOLD, command=self._on_open_settings,
            ).pack(padx=32, pady=(0, 24))
            return

        self._current_panel = "Browse"
        self._show_panel("Browse")

    def _show_panel(self, name: str):
        """Switch to the Browse, Tracked, Endorsed, or Trending panel."""
        for n, frame in self._panel_frames.items():
            if n == name:
                frame.grid()
            else:
                frame.grid_remove()
        self._current_panel = name
        # Highlight active button
        for btn, n in [
            (self._browse_btn, "Browse"),
            (self._tracked_btn, "Tracked"),
            (self._endorsed_btn, "Endorsed"),
            (self._trending_btn, "Trending"),
        ]:
            btn.configure(fg_color=ACCENT_HOV if n == name else ACCENT)
        # Auto-refresh the panel when switching to it
        panel_map = {
            "Browse": self._browse_panel,
            "Tracked": self._tracked_panel,
            "Endorsed": self._endorsed_panel,
            "Trending": self._trending_panel,
        }
        panel = panel_map.get(name)
        if panel is not None and hasattr(panel, "refresh"):
            panel.refresh()

    def _do_close(self):
        if self._on_close:
            self._on_close()
        else:
            self.place_forget()

    def _install_from_tracked(self, entry):
        self._install_nexus_mod(
            entry, "Tracked Mods",
            lambda e: (e.domain_name, e.mod_id, e.name),
        )

    def _install_from_endorsed(self, entry):
        self._install_nexus_mod(
            entry, "Endorsed Mods",
            lambda e: (e.domain_name, e.mod_id, e.name),
        )

    def _install_from_browse(self, entry):
        self._install_nexus_mod(
            entry, "Browse",
            lambda e: (e.domain_name, e.mod_id, e.name),
        )

    def _install_from_trending(self, entry):
        self._install_nexus_mod(
            entry, "Trending",
            lambda e: (e.domain_name, e.mod_id, e.name),
        )

    def _install_nexus_mod(self, entry, label: str, extract_fn: Callable):
        """Download and install a mod from Nexus (Tracked/Endorsed/Browse)."""
        domain, mod_id, mod_name = extract_fn(entry)
        from types import SimpleNamespace
        e = SimpleNamespace(domain_name=domain, mod_id=mod_id, name=mod_name)
        app = self._app_root
        mod_panel = getattr(app, "_mod_panel", None)
        install_nexus_mod_from_entry(app, self._api, self._game, mod_panel, self._log, e, label)
