"""
Browse Mods panel — displays Trending / Latest Added / Latest Updated mods
from Nexus Mods for the currently selected game.

Fetches mod lists via the Nexus v1 REST API.  The API already returns full
mod info objects so no per-mod enrichment is needed (unlike Tracked/Endorsed).

Each mod is shown as a CTkCard with: mod image, name/author/stats, summary,
and View + Install buttons.  Right-click also offers Track Mod and Endorse Mod.
"""

from __future__ import annotations

import threading
import tkinter as tk
from Utils.xdg import open_url
from dataclasses import dataclass
from typing import Callable, Optional

import customtkinter as ctk

from gui.nexus_mod_list_panel_base import _NexusModListPanel
from Utils.ui_config import load_nexus_show_adult, save_nexus_show_adult
from gui.theme import (
    BG_HEADER,
    BG_ROW,
    ACCENT,
    ACCENT_HOV,
    TEXT_MAIN,
    TEXT_DIM,
    FONT_HEADER,
    FONT_SMALL,
)

# Mods per page when browsing
PAGE_SIZE = 30


@dataclass
class BrowseModEntry:
    """A mod entry from the browse endpoints."""
    mod_id: int = 0
    domain_name: str = ""
    name: str = ""
    author: str = ""
    version: str = ""
    summary: str = ""
    description: str = ""
    endorsement_count: int = 0
    downloads_total: int = 0
    picture_url: str = ""
    contains_adult_content: bool = False


CATEGORIES = [
    ("Trending",        "get_trending"),
    ("Latest Added",    "get_latest_added"),
    ("Latest Updated",  "get_latest_updated"),
    ("Top Downloaded",  "get_top_mods"),
]

# Only the categories shown in the UI — the others remain in CATEGORIES
# above so they can be re-enabled by moving them here.
VISIBLE_CATEGORIES = [
    ("Top Downloaded",  "get_top_mods"),
]


class BrowseModsPanel(_NexusModListPanel):
    """
    Card-grid panel listing browseable mods from Nexus (Trending / Latest
    Added / Latest Updated / Top Downloaded).

    Built into an existing parent widget (a tab frame from CTkTabview).
    """

    _toolbar_column = 1
    _has_cat_sidebar = True

    def __init__(
        self,
        parent_tab: tk.Widget,
        log_fn: Optional[Callable] = None,
        get_api: Optional[Callable] = None,
        get_game_domain: Optional[Callable] = None,
        install_fn: Optional[Callable] = None,
        get_installed_mod_ids: Optional[Callable[[], set]] = None,
        visible_categories: Optional[list[tuple[str, str]]] = None,
    ):
        self._visible_categories = visible_categories if visible_categories is not None else VISIBLE_CATEGORIES
        self._cat_idx: int = 0
        self._page: int = 0
        self._search_active: bool = False
        self._search_query: str = ""
        self._show_adult_var: tk.BooleanVar | None = None
        super().__init__(
            parent_tab,
            log_fn=log_fn,
            get_api=get_api,
            get_game_domain=get_game_domain,
            install_fn=install_fn,
            get_installed_mod_ids=get_installed_mod_ids,
        )

    def _initial_status_text(self) -> str:
        return "Click Refresh to browse mods"

    def _build(self, tab):
        """Override _build to add the search bar row and 3-row cat sidebar rowspan."""
        col = self._toolbar_column
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_rowconfigure(2, weight=0)
        tab.grid_columnconfigure(col, weight=1)

        if self._has_cat_sidebar:
            tab.grid_columnconfigure(0, weight=0, minsize=0)
            # Browse has 3 rows (toolbar, canvas, search bar), so rowspan=3
            self._build_cat_side_panel(tab, rowspan=3)

        # Toolbar
        toolbar = tk.Frame(tab, bg=BG_HEADER, height=28)
        toolbar.grid(row=0, column=col, sticky="ew")
        toolbar.grid_propagate(False)

        self._refresh_btn = ctk.CTkButton(
            toolbar, text="↺ Refresh", width=72, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self.refresh,
        )
        self._refresh_btn.pack(side="left", padx=4, pady=2)

        self._build_toolbar(toolbar)

        self._status_label = ctk.CTkLabel(
            toolbar, text=self._initial_status_text(), anchor="w",
            font=FONT_SMALL, text_color=TEXT_DIM, fg_color=BG_HEADER,
        )
        self._status_label.pack(side="left", padx=4, fill="x", expand=True)

        # Scrollable card area
        from gui.theme import BG_DEEP
        self._canvas_frame = canvas_frame = tk.Frame(tab, bg=BG_DEEP, bd=0, highlightthickness=0)
        canvas_frame.grid(row=1, column=col, sticky="nsew")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(
            canvas_frame, bg=BG_DEEP, bd=0,
            highlightthickness=0, yscrollincrement=1, takefocus=0,
        )
        self._vsb = tk.Scrollbar(
            canvas_frame, orient="vertical", command=self._canvas.yview,
            bg="#383838", troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
        )
        self._canvas.configure(yscrollcommand=self._vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._vsb.grid(row=0, column=1, sticky="ns")

        self._inner = ctk.CTkFrame(self._canvas, fg_color=BG_DEEP)
        self._inner_id = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")

        self._inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind("<Map>", self._on_canvas_map)
        self._canvas.bind("<Button-4>",   lambda e: self._scroll(-100))
        self._canvas.bind("<Button-5>",   lambda e: self._scroll(100))
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._inner.bind("<Button-4>",    lambda e: self._scroll(-100))
        self._inner.bind("<Button-5>",    lambda e: self._scroll(100))
        self._inner.bind("<MouseWheel>",  self._on_mousewheel)

        # Search bar (row 2)
        search_bar = tk.Frame(tab, bg=BG_HEADER, height=30)
        search_bar.grid(row=2, column=col, sticky="ew")
        search_bar.grid_propagate(False)

        ctk.CTkLabel(
            search_bar, text="Search:",
            font=FONT_SMALL, text_color=TEXT_DIM, fg_color=BG_HEADER,
        ).pack(side="left", padx=(8, 4), pady=4)

        self._search_var = tk.StringVar()
        self._search_entry = ctk.CTkEntry(
            search_bar,
            textvariable=self._search_var,
            fg_color=BG_ROW, text_color=TEXT_MAIN,
            font=FONT_SMALL, height=26,
            border_width=0,
        )
        self._search_entry.pack(side="left", fill="x", expand=True, pady=4, padx=(0, 4))
        self._search_entry.bind("<Return>",    lambda _e: self._do_search())
        self._search_entry.bind("<Control-a>", lambda _e: (
            self._search_entry.select_range(0, "end"),
            self._search_entry.icursor("end"),
            "break",
        )[-1])

        self._search_btn = ctk.CTkButton(
            search_bar, text="Search", width=64, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self._do_search,
        )
        self._search_btn.pack(side="left", padx=2, pady=4)

        self._clear_btn = ctk.CTkButton(
            search_bar, text="✕", width=32, height=26,
            fg_color="#b33a3a", hover_color="#c94848", text_color="white",
            font=FONT_HEADER, command=self._clear_search,
        )
        self._clear_btn.pack(side="left", padx=(0, 8), pady=4)

    def _build_toolbar(self, toolbar: tk.Frame) -> None:
        # Category cycle button (only shown when there's more than one visible category)
        self._cat_btn = ctk.CTkButton(
            toolbar, text=f"▸ {self._visible_categories[0][0]}", width=100, height=26,
            fg_color=BG_HEADER, hover_color=BG_ROW, text_color=TEXT_MAIN,
            font=FONT_HEADER, command=self._cycle_category,
        )
        if len(self._visible_categories) > 1:
            self._cat_btn.pack(side="left", padx=4, pady=2)

        self._cat_filter_btn = ctk.CTkButton(
            toolbar, text="Categories", width=80, height=26,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            font=FONT_HEADER, command=lambda: self._toggle_cat_sidebar("Browse: "),
        )
        self._cat_filter_btn.pack(side="left", padx=4, pady=2)

        self._prev_btn = ctk.CTkButton(
            toolbar, text="Previous", width=70, height=26,
            fg_color="#c37800", hover_color="#e28b00", text_color="white",
            font=FONT_HEADER, command=self._go_prev_page,
            state="disabled",
        )
        self._prev_btn.pack(side="left", padx=4, pady=2)

        self._next_btn = ctk.CTkButton(
            toolbar, text="Next", width=52, height=26,
            fg_color="#c37800", hover_color="#e28b00", text_color="white",
            font=FONT_HEADER, command=self._go_next_page,
            state="disabled",
        )
        self._next_btn.pack(side="left", padx=4, pady=2)

        self._show_adult_var = tk.BooleanVar(value=load_nexus_show_adult())
        ctk.CTkCheckBox(
            toolbar, text="Show Adult", variable=self._show_adult_var,
            height=26, font=FONT_HEADER, text_color=TEXT_MAIN,
            command=self._on_show_adult_toggle,
        ).pack(side="left", padx=(10, 4), pady=2)

    # ------------------------------------------------------------------
    # Category cycling
    # ------------------------------------------------------------------

    def _cycle_category(self):
        self._cat_idx = (self._cat_idx + 1) % len(self._visible_categories)
        label, _ = self._visible_categories[self._cat_idx]
        self._cat_btn.configure(text=f"▸ {label}")
        self.refresh()

    # ------------------------------------------------------------------
    # Refresh / Pagination
    # ------------------------------------------------------------------

    def refresh(self):
        """Fetch page 0 of the current category and rebuild cards."""
        self._page = 0
        self._load_page()

    def _go_prev_page(self):
        if self._page > 0 and not self._loading:
            self._page -= 1
            if self._search_active:
                self._do_search_page()
            else:
                self._load_page()

    def _go_next_page(self):
        if not self._loading:
            if len(self._entries) >= PAGE_SIZE:
                self._page += 1
                if self._search_active:
                    self._do_search_page()
                else:
                    self._load_page()

    def _on_show_adult_toggle(self):
        save_nexus_show_adult(self._show_adult_var.get())
        self._build_cards()

    def _build_cards(self):
        show_adult = self._show_adult_var is not None and self._show_adult_var.get()
        if not show_adult:
            orig = self._entries
            self._entries = [e for e in orig if not e.contains_adult_content]
            super()._build_cards()
            self._entries = orig
        else:
            super()._build_cards()

    def _load_page(self):
        """Fetch the current page and replace cards."""
        api = self._get_api()
        if api is None:
            self._log("Browse: Login to Nexus first")
            return
        domain = self._get_game_domain()
        if not domain:
            self._log("Browse: No game selected.")
            return
        self._sync_game_domain(domain)

        if self._loading:
            return
        self._loading = True
        self._refresh_btn.configure(state="disabled")
        self._prev_btn.configure(state="disabled")
        self._next_btn.configure(state="disabled")
        cat_label, cat_method = self._visible_categories[self._cat_idx]
        page = self._page
        self._status_label.configure(text=f"Loading {cat_label} (page {page + 1})…")
        self._show_loader()

        def _worker():
            try:
                entries = self._fetch_page(api, cat_method, domain, page=page,
                                           cat_names=self._selected_cat_names or None)

                def _done():
                    self._entries = entries
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._prev_btn.configure(state="normal" if page > 0 else "disabled")
                    self._next_btn.configure(
                        state="normal" if len(entries) >= PAGE_SIZE else "disabled"
                    )
                    self._build_cards()
                    self._canvas.yview_moveto(0)
                    self._status_label.configure(text=f"{cat_label}: page {page + 1}")
                    self._log(f"Browse: Loaded page {page + 1} — {len(entries)} {cat_label.lower()} mod(s).")

                self._parent.after(0, _done)

            except Exception as exc:
                def _err(exc=exc):
                    self._hide_loader()
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._prev_btn.configure(state="normal")
                    self._next_btn.configure(state="normal")
                    self._status_label.configure(text="Error")
                    self._log(f"Browse: Failed — {exc}")
                self._parent.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def _fetch_page(api, cat_method: str, domain: str, page: int,
                    cat_names: list | None = None) -> list[BrowseModEntry]:
        """Call the API and return a list of BrowseModEntry for one page."""
        if cat_method == "get_top_mods":
            mod_infos = api.get_top_mods(
                domain, count=PAGE_SIZE, offset=page * PAGE_SIZE, category_names=cat_names
            )
        else:
            mod_infos = getattr(api, cat_method)(domain)

        entries: list[BrowseModEntry] = []
        for info in mod_infos:
            get = info.get if isinstance(info, dict) else lambda k, d=None: getattr(info, k, d)
            entries.append(BrowseModEntry(
                mod_id=get("mod_id", 0),
                domain_name=get("domain_name", domain),
                name=get("name", "") or f"Mod {get('mod_id', 0)}",
                author=get("author", ""),
                version=get("version", ""),
                summary=get("summary", ""),
                description=get("description", ""),
                endorsement_count=get("endorsement_count", 0),
                downloads_total=get("downloads_total", 0),
                picture_url=get("picture_url", ""),
                contains_adult_content=bool(get("contains_adult_content", False)),
            ))
        return entries

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _do_search(self):
        query_text = self._search_var.get().strip()
        if not query_text:
            return
        self._search_query = query_text
        self._search_active = True
        self._page = 0
        self._do_search_page()

    def _do_search_page(self):
        api = self._get_api()
        if api is None:
            self._log("Browse: Login to Nexus first")
            return
        domain = self._get_game_domain()
        if not domain:
            self._log("Browse: No game selected.")
            return
        self._sync_game_domain(domain)
        if self._loading:
            return

        query_text = self._search_query
        page = self._page
        self._loading = True
        self._prev_btn.configure(state="disabled")
        self._next_btn.configure(state="disabled")
        self._search_btn.configure(state="disabled")
        self._status_label.configure(text=f"Searching '{query_text}' (page {page + 1})…")
        self._show_loader()

        def _worker():
            try:
                cat_names = self._selected_cat_names or None
                mod_infos = api.search_mods(
                    domain, query_text,
                    count=PAGE_SIZE, offset=page * PAGE_SIZE,
                    category_names=cat_names,
                )
                entries: list[BrowseModEntry] = []
                for info in mod_infos:
                    get = info.get if isinstance(info, dict) else lambda k, d=None: getattr(info, k, d)
                    entries.append(BrowseModEntry(
                        mod_id=get("mod_id", 0),
                        domain_name=get("domain_name", domain),
                        name=get("name", "") or f"Mod {get('mod_id', 0)}",
                        author=get("author", ""),
                        version=get("version", ""),
                        summary=get("summary", ""),
                        description=get("description", ""),
                        endorsement_count=get("endorsement_count", 0),
                        downloads_total=get("downloads_total", 0),
                        picture_url=get("picture_url", ""),
                        contains_adult_content=bool(get("contains_adult_content", False)),
                    ))

                def _done():
                    self._entries = entries
                    self._loading = False
                    self._search_btn.configure(state="normal")
                    self._prev_btn.configure(state="normal" if page > 0 else "disabled")
                    self._next_btn.configure(
                        state="normal" if len(entries) >= PAGE_SIZE else "disabled"
                    )
                    self._status_label.configure(
                        text=f"'{query_text}': page {page + 1} ({len(entries)} result(s))"
                    )
                    self._build_cards()
                    self._canvas.yview_moveto(0)
                    self._log(f"Browse: {len(entries)} search result(s) for '{query_text}' page {page + 1} in {domain}.")

                self._parent.after(0, _done)

            except Exception as exc:
                def _err(exc=exc):
                    self._hide_loader()
                    self._loading = False
                    self._search_btn.configure(state="normal")
                    self._prev_btn.configure(state="normal" if page > 0 else "disabled")
                    self._next_btn.configure(state="normal")
                    self._status_label.configure(text="Search error")
                    self._log(f"Browse: Search failed — {exc}")
                self._parent.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    def _clear_search(self):
        self._search_var.set("")
        self._search_active = False
        self._search_query = ""
        self._entries = []
        self._clear_cards()
        self._prev_btn.configure(state="disabled")
        self._next_btn.configure(state="disabled")
        cat_label, _ = self._visible_categories[self._cat_idx]
        self._status_label.configure(text=f"▸ {cat_label} — click Refresh")

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _show_context_menu(self, event, entry: BrowseModEntry, url: str):
        menu = self._get_or_create_context_menu()
        menu.clear()
        menu.add_command("Open on Nexus", lambda: open_url(url))
        menu.add_command("Install Mod", lambda: self._install_fn(entry))
        menu.add_separator()
        menu.add_command("Track Mod", lambda: self._track_mod(entry))
        menu.add_command("Endorse Mod", lambda: self._endorse_mod(entry))
        menu.popup(event.x_root, event.y_root)

    def _track_mod(self, entry: BrowseModEntry):
        api = self._get_api()
        if api is None:
            self._log("Browse: No API key set.")
            return

        def _worker():
            try:
                api.track_mod(entry.domain_name, entry.mod_id)
                self._parent.after(0,
                    lambda: self._log(f"Browse: Now tracking '{entry.name}' ({entry.mod_id})."))
            except Exception as exc:
                self._parent.after(0,
                    lambda e=exc: self._log(f"Browse: Track failed — {e}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _endorse_mod(self, entry: BrowseModEntry):
        api = self._get_api()
        if api is None:
            self._log("Browse: No API key set.")
            return

        def _worker():
            try:
                api.endorse_mod(entry.domain_name, entry.mod_id, entry.version)
                self._parent.after(0,
                    lambda: self._log(f"Browse: Endorsed '{entry.name}' ({entry.mod_id})."))
            except Exception as exc:
                self._parent.after(0,
                    lambda e=exc: self._log(f"Browse: Endorse failed — {e}"))

        threading.Thread(target=_worker, daemon=True).start()
